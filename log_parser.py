"""Local-first log parsing: Drain3 templates + SQLite occurrences.

A *record* is the one type that flows through everything::

    {"message": str,      # text handed to Drain3 as-is
     "ts": int,           # UTC Unix epoch
     "source_key": str,   # opaque origin id, pre-built by the caller/fetcher
     "extra": dict}       # arbitrary kept fields, free-form

Three collaborators, driven entirely by the coordinator; none of them holds a
back-reference to another:

* ``TemplateModel``  -- Drain3 + FilePersistence. Owns the template tree only.
* ``SqliteStore``    -- events and fetched_ranges. Knows nothing of Drain3.
* ``LogParser``      -- coverage/gap/fetch logic. The public surface.

Note on the Drain3 snapshot: ``TemplateMiner.save_state`` pickles the whole
``Drain`` object, and ``LogCluster`` carries a ``size`` counter, so the state
file holds a per-template occurrence *count*. That is an aggregate integer, not
occurrence records -- no timestamps, no source keys, no rows -- and it never
affects matching. SQLite remains the sole source of truth for occurrences.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, Iterable, List, Tuple

from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig

Record = Dict[str, Any]
Range = Tuple[int, int]
FetchFn = Callable[[int, int], List[Record]]

DEFAULT_MARGIN_SEC = 60


# --------------------------------------------------------------------------
# Coverage algebra. Ranges are inclusive integer seconds: [start, end].
# --------------------------------------------------------------------------

def merge_ranges(ranges: Iterable[Range]) -> List[Range]:
    """Merge overlapping and *adjacent* inclusive ranges.

    Adjacency matters: on inclusive integer seconds (100, 140) and (141, 200)
    are contiguous, so they must collapse to (100, 200). Treating them as
    separate would leave a phantom zero-width gap and trigger a pointless fetch.
    """
    ordered = sorted(ranges)
    if not ordered:
        return []

    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def missing_ranges(t1: int, t2: int, covered: Iterable[Range]) -> List[Range]:
    """Sub-intervals of [t1, t2] not covered by ``covered``."""
    if t2 < t1:
        return []

    gaps: List[Range] = []
    cursor = t1
    for start, end in merge_ranges(covered):
        if end < cursor:
            continue
        if start > t2:
            break
        if start > cursor:
            gaps.append((cursor, min(start - 1, t2)))
        cursor = max(cursor, end + 1)
        if cursor > t2:
            break

    if cursor <= t2:
        gaps.append((cursor, t2))
    return gaps


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

class TemplateModel:
    """Drain3 template tree with its own file persistence."""

    def __init__(self, state_path: str, config: TemplateMinerConfig | None = None):
        # Pass an explicit config: TemplateMiner(config=None) silently loads
        # drain3.ini from the current working directory, which would make
        # parsing behaviour depend on where the process was launched.
        self._miner = TemplateMiner(
            FilePersistence(state_path),
            config=config or TemplateMinerConfig(),
        )

    def template_id(self, message: str) -> int:
        """Match or learn ``message``; return its cluster id."""
        # message arrives already separated from its timestamp -- feed it as-is.
        return self._miner.add_log_message(message)["cluster_id"]

    def save(self) -> None:
        """Flush the template tree so learning since the last snapshot survives."""
        self._miner.save_state("close")


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    template_id INTEGER NOT NULL,
    source_key  TEXT    NOT NULL,
    extra       TEXT
);
CREATE TABLE IF NOT EXISTS fetched_ranges(
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL
);
-- Dedup is enforced here, in the database, not in application code: identity is
-- (template_id, ts, source_key) and inserts use INSERT OR IGNORE. Overlapping
-- padded fetches are therefore physically unable to create duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_identity
    ON events(template_id, ts, source_key);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_template_ts ON events(template_id, ts);
"""


class SqliteStore:
    """SQLite occurrence store plus the record of which windows were fetched."""

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def add_event(self, template_id: int, ts: int, source_key: str, extra: dict) -> bool:
        """Insert one occurrence. Returns False when dedup rejected it."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO events(ts, template_id, source_key, extra) "
            "VALUES(?, ?, ?, ?)",
            (ts, template_id, source_key, json.dumps(extra or {})),
        )
        self._db.commit()
        return cursor.rowcount == 1

    def read_events(self, t1: int, t2: int) -> List[Record]:
        rows = self._db.execute(
            "SELECT ts, template_id, source_key, extra FROM events "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts, id",
            (t1, t2),
        ).fetchall()
        return [
            {
                "ts": ts,
                "template_id": template_id,
                "source_key": source_key,
                "extra": json.loads(extra) if extra else {},
            }
            for ts, template_id, source_key, extra in rows
        ]

    def count_events(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def fetched_ranges(self) -> List[Range]:
        return [
            (start, end)
            for start, end in self._db.execute(
                "SELECT start_ts, end_ts FROM fetched_ranges"
            ).fetchall()
        ]

    def record_range(self, start_ts: int, end_ts: int) -> None:
        self._db.execute(
            "INSERT INTO fetched_ranges(start_ts, end_ts) VALUES(?, ?)",
            (start_ts, end_ts),
        )
        self._db.commit()

    def covered(self, t1: int, t2: int) -> bool:
        return not missing_ranges(t1, t2, self.fetched_ranges())

    def missing(self, t1: int, t2: int) -> List[Range]:
        return missing_ranges(t1, t2, self.fetched_ranges())

    def close(self) -> None:
        self._db.close()


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------

class LogParser:
    """Parses, stores and serves log records local-first.

    ``fetch_fn(t1, t2) -> list[record]`` is the only thing that touches the
    remote log source. It stays dumb: it answers "give me records between A and
    B" and knows nothing about coverage, padding or dedup.
    """

    def __init__(
        self,
        fetch_fn: FetchFn,
        db_path: str,
        state_path: str,
        margin_sec: int = DEFAULT_MARGIN_SEC,
    ):
        self._fetch_fn = fetch_fn
        self._margin_sec = margin_sec
        self.model = TemplateModel(state_path)
        self.store = SqliteStore(db_path)

    def ingest(self, record: Record) -> bool:
        """Match/learn the message, then write one row. Returns False if deduped."""
        template_id = self.model.template_id(record["message"])
        return self.store.add_event(
            template_id,
            record["ts"],
            record["source_key"],
            record.get("extra") or {},
        )

    def query(self, t1: int, t2: int) -> List[Record]:
        """Return records in [t1, t2], fetching only windows not already fetched."""
        for gap_start, gap_end in self.store.missing(t1, t2):
            # Fetch wide: pad the gap so records near its edges are not missed.
            fetched = self._fetch_fn(
                gap_start - self._margin_sec,
                gap_end + self._margin_sec,
            )
            for record in fetched or ():
                self.ingest(record)
            # Record exact: only the un-padded gap is marked as fetched, so the
            # padding never inflates our claim about what we actually have.
            self.store.record_range(gap_start, gap_end)

        return self.store.read_events(t1, t2)

    def close(self) -> None:
        self.model.save()
        self.store.close()

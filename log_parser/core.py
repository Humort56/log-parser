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

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig

logger = logging.getLogger(__name__)

Record = dict[str, Any]
Range = tuple[int, int]
FetchFn = Callable[[int, int], list[Record]]

DEFAULT_MARGIN_SEC = 60


# --------------------------------------------------------------------------
# Coverage algebra. Ranges are inclusive integer seconds: [start, end].
# --------------------------------------------------------------------------


def merge_ranges(ranges: Iterable[Range]) -> list[Range]:
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


def missing_ranges(t1: int, t2: int, covered: Iterable[Range]) -> list[Range]:
    """Sub-intervals of [t1, t2] not covered by ``covered``."""
    if t2 < t1:
        return []

    gaps: list[Range] = []
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


#: Config attributes that decide which messages collapse into which template.
#: Only these are fingerprinted: the rest (profiling, snapshot cadence) change
#: how drain3 runs, not what it produces, and must not trigger a warning.
_MINING_CONFIG_KEYS = (
    "drain_depth",
    "drain_extra_delimiters",
    "drain_max_children",
    "drain_max_clusters",
    "drain_sim_th",
    "mask_prefix",
    "mask_suffix",
    "parametrize_numeric_tokens",
)


def config_fingerprint(config: TemplateMinerConfig) -> str:
    """A stable digest of the settings that affect template identity.

    Masking instructions are folded in by their patterns: they rewrite messages
    before mining, so changing them changes the resulting templates just as
    surely as ``sim_th`` does.
    """
    values: dict[str, Any] = {key: getattr(config, key, None) for key in _MINING_CONFIG_KEYS}
    values["masking_instructions"] = sorted(
        # Instruction objects are drain3-internal and not reliably comparable;
        # their regex pattern is the part that changes behaviour.
        str(getattr(rule, "pattern", rule))
        for rule in config.masking_instructions or ()
    )
    blob = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class TemplateModel:
    """Drain3 template tree with its own file persistence."""

    def __init__(self, state_path: str, config: TemplateMinerConfig | None = None):
        # Pass an explicit config: TemplateMiner(config=None) silently loads
        # drain3.ini from the current working directory, which would make
        # parsing behaviour depend on where the process was launched.
        config = config or TemplateMinerConfig()
        self._fingerprint_path = Path(f"{state_path}.config")
        self._fingerprint = config_fingerprint(config)
        self._warn_on_config_change()
        self._miner = TemplateMiner(
            FilePersistence(state_path),
            config=config,
        )

    def _warn_on_config_change(self) -> None:
        """Warn when this config would mine differently than the snapshot did.

        Mining settings decide which messages share a ``template_id``, and ids
        are what SQLite stores. Reopening an existing snapshot under changed
        settings therefore leaves the store holding ids from the old vocabulary
        while new rows get ids from the new one -- and because ``fetched_ranges``
        still marks those windows covered, a re-query will not re-derive them.
        Nothing raises, because the caller may have changed the config on
        purpose; but the mismatch must not be silent.

        The fingerprint is a sidecar rather than something embedded in the
        snapshot: that file is an opaque pickle drain3 owns, and writing our own
        fields into it would couple us to its format.
        """
        try:
            previous = self._fingerprint_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return  # First run with this snapshot, or one written before configs were tracked.
        except OSError:
            logger.warning(
                "Could not read %s; skipping the config check",
                self._fingerprint_path,
                exc_info=True,
            )
            return

        if previous != self._fingerprint:
            logger.warning(
                "Drain3 config changed since %s was written (%s -> %s). Template ids already "
                "in the database were mined under the old settings and will not match ids "
                "mined now. Use a fresh state_path and db_path, or reparse from scratch.",
                self._fingerprint_path.name,
                previous,
                self._fingerprint,
            )

    def template_id(self, message: str) -> int:
        """Match or learn ``message``; return its cluster id."""
        # message arrives already separated from its timestamp -- feed it as-is.
        # drain3 is untyped, so the id comes back as Any; pin it to int here
        # rather than letting that leak into every caller.
        return int(self._miner.add_log_message(message)["cluster_id"])

    def clusters(self) -> dict[int, tuple[str, int]]:
        """``{cluster_id: (template_text, all_time_occurrences)}`` from the tree.

        Exposed here so callers do not have to reach through ``_miner`` into
        drain3's internals to render template text.
        """
        return {
            cluster.cluster_id: (cluster.get_template(), cluster.size)
            for cluster in self._miner.drain.clusters
        }

    def save(self) -> None:
        """Flush the template tree so learning since the last snapshot survives."""
        self._miner.save_state("close")
        # Written after the snapshot, so a failed save never leaves a fingerprint
        # claiming to describe a tree that was not persisted.
        try:
            self._fingerprint_path.write_text(self._fingerprint, encoding="utf-8")
        except OSError:
            # The templates are saved either way; losing the sidecar only costs
            # the config-change warning, which must not be worth failing a save.
            logger.warning("Could not write %s", self._fingerprint_path, exc_info=True)


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
        # timeout: the UI reads this store from a second connection while a
        # query may be writing, so a busy database should wait rather than
        # raise immediately.
        self._db = sqlite3.connect(db_path, timeout=30.0)
        # WAL lets that reader proceed concurrently with the writer instead of
        # blocking. It is recorded in the file header and so persists across
        # connections -- note it is unsuitable on network filesystems.
        self._db.execute("PRAGMA journal_mode=WAL")
        # NORMAL trades an fsync per commit for one at checkpoint; with WAL the
        # worst case is losing the last transactions on power loss, not a
        # corrupt database.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._in_bulk = False

    @contextmanager
    def bulk(self) -> Iterator[None]:
        """Group inserts into one transaction, committing once on exit.

        ``add_event`` commits per row so a direct ``ingest`` is durable
        immediately. That costs a commit per record when ingesting a whole
        fetched window, which is what :meth:`LogParser.query` does -- so it
        wraps its ingest loop in this instead.
        """
        self._in_bulk = True
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        finally:
            self._in_bulk = False

    def add_event(self, template_id: int, ts: int, source_key: str, extra: dict[str, Any]) -> bool:
        """Insert one occurrence. Returns False when dedup rejected it."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO events(ts, template_id, source_key, extra) VALUES(?, ?, ?, ?)",
            (ts, template_id, source_key, json.dumps(extra or {})),
        )
        if not self._in_bulk:
            self._db.commit()
        # rowcount is set by the INSERT itself, so this stays correct inside a
        # bulk block where the commit has not happened yet.
        return cursor.rowcount == 1

    def read_events(self, t1: int, t2: int) -> list[Record]:
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
        return int(self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def template_counts(self, t1: int, t2: int) -> list[tuple[int, int]]:
        """(template_id, occurrences) in [t1, t2], most frequent first.

        Aggregated in SQL rather than over read rows so the counts stay exact
        even when a caller reads only a capped slice of a large window. Served
        by ix_events_template_ts.
        """
        return [
            (template_id, count)
            for template_id, count in self._db.execute(
                "SELECT template_id, COUNT(*) AS n FROM events "
                "WHERE ts BETWEEN ? AND ? GROUP BY template_id ORDER BY n DESC, template_id",
                (t1, t2),
            ).fetchall()
        ]

    def ts_bounds(self) -> tuple[int, int] | None:
        """(min_ts, max_ts) across all events, or None when the store is empty."""
        low, high = self._db.execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
        return None if low is None else (low, high)

    def fetched_ranges(self) -> list[Range]:
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

    def missing(self, t1: int, t2: int) -> list[Range]:
        return missing_ranges(t1, t2, self.fetched_ranges())

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


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
        config: TemplateMinerConfig | None = None,
    ):
        self._fetch_fn = fetch_fn
        self._margin_sec = margin_sec
        # config tunes how drain3 mines templates (sim_th, depth, masking...).
        # None means drain3's own defaults -- never drain3.ini from the cwd; see
        # TemplateModel. Changing it against an existing store is a reparse, not
        # a setting: TemplateModel logs a warning if the snapshot disagrees.
        self.model = TemplateModel(state_path, config)
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

    def query(self, t1: int, t2: int) -> list[Record]:
        """Return records in [t1, t2], fetching only windows not already fetched."""
        gaps = self.store.missing(t1, t2)
        logger.debug("query [%d, %d]: %d gap(s) to fetch", t1, t2, len(gaps))
        for gap_start, gap_end in gaps:
            # Fetch wide: pad the gap so records near its edges are not missed.
            fetched = self._fetch_fn(
                gap_start - self._margin_sec,
                gap_end + self._margin_sec,
            )
            # One transaction for the whole gap rather than one commit per row.
            with self.store.bulk():
                stored = sum(self.ingest(record) for record in fetched or ())
            logger.debug(
                "gap [%d, %d]: fetched %d record(s), stored %d",
                gap_start,
                gap_end,
                len(fetched or ()),
                stored,
            )
            # Record exact: only the un-padded gap is marked as fetched, so the
            # padding never inflates our claim about what we actually have.
            self.store.record_range(gap_start, gap_end)

        return self.store.read_events(t1, t2)

    def close(self) -> None:
        self.model.save()
        self.store.close()

    def __enter__(self) -> LogParser:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

"""The two V1 tests: ingestion records correctly, and padding does not duplicate."""

import json
import sqlite3

from log_parser import LogParser


def make_parser(tmp_path, fetch_fn, margin_sec=60):
    return LogParser(
        fetch_fn=fetch_fn,
        db_path=str(tmp_path / "events.db"),
        state_path=str(tmp_path / "drain3.bin"),
        margin_sec=margin_sec,
    )


def test_ingestion_records_correctly(tmp_path):
    """Known records land in SQLite with the right columns; extra round-trips."""
    parser = make_parser(tmp_path, fetch_fn=lambda t1, t2: [])

    records = [
        {
            "message": "User 12 logged in from 10.0.0.1",
            "ts": 1_700_000_000,
            "source_key": "clientA|server1",
            "extra": {"level": "INFO", "attempt": 1},
        },
        {
            "message": "User 34 logged in from 10.0.0.2",
            "ts": 1_700_000_060,
            "source_key": "clientB|server1",
            "extra": {"level": "INFO", "attempt": 2},
        },
        {
            "message": "Disk sda full at 91%",
            "ts": 1_700_000_120,
            "source_key": "clientA|server2",
            "extra": {"level": "ERROR", "nested": {"disk": "sda"}},
        },
    ]
    for record in records:
        assert parser.ingest(record) is True

    rows = sqlite3.connect(str(tmp_path / "events.db")).execute(
        "SELECT ts, template_id, source_key, extra FROM events ORDER BY ts"
    ).fetchall()

    assert len(rows) == len(records)
    for row, expected in zip(rows, records):
        ts, template_id, source_key, extra = row
        assert ts == expected["ts"]
        assert isinstance(template_id, int) and template_id > 0
        assert source_key == expected["source_key"]
        # extra survives the JSON round-trip intact, nesting included.
        assert json.loads(extra) == expected["extra"]

    # The two login lines share a template; the disk line does not.
    template_ids = [row[1] for row in rows]
    assert template_ids[0] == template_ids[1]
    assert template_ids[2] != template_ids[0]

    parser.close()


def test_padding_does_not_duplicate(tmp_path):
    """A padded fetch that re-pulls already-stored events must not duplicate them."""
    events = [
        {
            "message": f"Request {i} completed",
            "ts": ts,
            "source_key": "clientA|server1",
            "extra": {"seq": i},
        }
        for i, ts in enumerate(range(1_000, 1_301, 20))
    ]

    calls = []

    def fetch_fn(t1, t2):
        # Deliberately dumb: return everything in the requested window, with no
        # awareness of what the caller already has.
        calls.append((t1, t2))
        return [e for e in events if t1 <= e["ts"] <= t2]

    # margin large enough that the second query's padded fetch reaches back into
    # the window covered by the first.
    parser = make_parser(tmp_path, fetch_fn, margin_sec=100)

    parser.query(1_000, 1_150)
    count_after_first = parser.store.count_events()
    assert count_after_first > 0

    # Adjacent window: its gap starts at 1151, so the padded fetch begins at
    # 1051 and re-delivers events already stored by the first query.
    parser.query(1_151, 1_300)
    count_after_second = parser.store.count_events()

    # The refetch really happened and really overlapped -- otherwise an unchanged
    # count would only prove that nothing was fetched twice.
    assert len(calls) == 2
    assert calls[1][0] < calls[0][1], "second fetch should overlap the first window"
    overlapping = [e for e in events if calls[1][0] <= e["ts"] <= calls[0][1]]
    assert overlapping, "expected events to be pulled twice"

    # Every event now stored exactly once: total equals the distinct events in
    # both queried windows, and re-ingesting the overlap added nothing.
    expected_total = len([e for e in events if 1_000 - 100 <= e["ts"] <= 1_300 + 100])
    assert count_after_second == expected_total

    distinct = parser.store._db.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT template_id, ts, source_key FROM events)"
    ).fetchone()[0]
    assert distinct == count_after_second

    # Re-running a now-covered query fetches nothing more and changes nothing.
    parser.query(1_000, 1_300)
    assert len(calls) == 2
    assert parser.store.count_events() == count_after_second

    parser.close()

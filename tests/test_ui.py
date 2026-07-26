"""Tests for the UI's pure helpers.

These run without a Streamlit runtime: everything exercised here is plain
functions over plain data. The two that matter most are the UTC conversion
(a local-time leak silently shifts every window) and the JSON rendering of
`extra` values (a mismatch makes the two filters disagree about the same row).
"""

import datetime as dt
import json

import pytest

pytest.importorskip("streamlit")

from log_parser.fetchers import validated
from log_parser.ui import (
    ANY_KEY,
    UNKNOWN_TEMPLATE,
    discover_extra_keys,
    extra_values_for_key,
    format_ts,
    render_value,
    row_haystack,
    row_matches_filters,
    rows_to_dataframe,
    template_table,
    to_epoch,
)

UTC = dt.timezone.utc


def make_row(ts=1_700_000_000, template_id=1, source_key="clientA|server1", extra=None):
    return {
        "ts": ts,
        "template_id": template_id,
        "source_key": source_key,
        "extra": {} if extra is None else extra,
    }


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def test_to_epoch_is_utc():
    # 2023-11-14 22:13:20 UTC == 1700000000
    assert to_epoch(dt.date(2023, 11, 14), dt.time(22, 13, 20)) == 1_700_000_000


def test_format_ts_is_utc():
    assert format_ts(1_700_000_000) == "2023-11-14 22:13:20"


def test_to_epoch_round_trips_format_ts():
    ts = 1_700_086_400
    moment = dt.datetime.fromtimestamp(ts, tz=UTC)
    assert to_epoch(moment.date(), moment.time()) == ts


@pytest.mark.parametrize("tz", ["Asia/Tokyo", "America/New_York", "UTC"])
def test_time_helpers_ignore_local_timezone(monkeypatch, tz):
    """The whole point of the tzinfo/tz= arguments.

    Without them these functions would follow the machine's zone and every
    query window would silently shift by its offset.
    """
    monkeypatch.setenv("TZ", tz)
    import time as _time

    if hasattr(_time, "tzset"):  # POSIX only; on Windows the env var is inert
        _time.tzset()

    assert to_epoch(dt.date(2023, 11, 14), dt.time(22, 13, 20)) == 1_700_000_000
    assert format_ts(1_700_000_000) == "2023-11-14 22:13:20"


# --------------------------------------------------------------------------
# extra rendering and discovery
# --------------------------------------------------------------------------


def test_render_value_uses_json_not_python_repr():
    """A str() rendering would produce ['a', 'b'] and never match the stored JSON."""
    assert render_value(["a", "b"]) == '["a", "b"]'
    assert render_value(["a", "b"]) != str(["a", "b"])
    assert render_value({"x": 1}) == '{"x": 1}'
    assert render_value(None) == "null"
    assert render_value(True) == "true"
    assert render_value("INFO") == '"INFO"'


def test_render_value_matches_what_is_stored():
    """The rendered value must be findable in the JSON the store round-trips."""
    extra = {"tags": ["a", "b"]}
    stored = json.dumps(json.loads(json.dumps(extra)), sort_keys=True)
    assert render_value(extra["tags"]) in stored


def test_discover_extra_keys_orders_by_frequency():
    rows = [
        make_row(extra={"level": "INFO", "host": "h1"}),
        make_row(extra={"level": "ERROR"}),
        make_row(extra={}),
        make_row(extra={"level": "WARN", "zebra": 1}),
    ]
    assert discover_extra_keys(rows) == ["level", "host", "zebra"]


def test_discover_extra_keys_handles_empty_and_missing():
    assert discover_extra_keys([]) == []
    assert discover_extra_keys([{"ts": 1, "template_id": 1, "source_key": "s"}]) == []


def test_extra_values_for_key_renders_nested_as_json():
    rows = [
        make_row(extra={"tags": ["a", "b"]}),
        make_row(extra={"tags": ["a", "b"]}),
        make_row(extra={"tags": {"x": 1}}),
        make_row(extra={"level": "INFO"}),
    ]
    values = extra_values_for_key(rows, "tags")
    assert values == ['["a", "b"]', '{"x": 1}']
    assert "['a', 'b']" not in values


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def test_no_filters_matches_everything():
    assert row_matches_filters(make_row(), "User <*> logged in") is True


def test_extra_key_only_requires_presence():
    row = make_row(extra={"level": "INFO"})
    assert row_matches_filters(row, "", extra_key="level") is True
    assert row_matches_filters(row, "", extra_key="host") is False


def test_any_key_sentinel_does_not_filter():
    row = make_row(extra={})
    assert row_matches_filters(row, "", extra_key=ANY_KEY) is True


def test_extra_key_and_values():
    row = make_row(extra={"level": "INFO"})
    assert row_matches_filters(row, "", extra_key="level", extra_values=['"INFO"']) is True
    assert row_matches_filters(row, "", extra_key="level", extra_values=['"ERROR"']) is False


def test_nested_extra_value_filter_uses_json_rendering():
    row = make_row(extra={"tags": ["a", "b"]})
    assert row_matches_filters(row, "", extra_key="tags", extra_values=['["a", "b"]']) is True
    assert row_matches_filters(row, "", extra_key="tags", extra_values=["['a', 'b']"]) is False


def test_search_matches_template_text():
    """The message is not stored, so template text must be searchable."""
    row = make_row(extra={})
    assert row_matches_filters(row, "Disk <*> full at <*>", query="disk") is True
    assert row_matches_filters(row, "Disk <*> full at <*>", query="network") is False


def test_search_matches_extra_and_source_key():
    row = make_row(source_key="clientB|server9", extra={"level": "ERROR"})
    assert row_matches_filters(row, "", query="server9") is True
    assert row_matches_filters(row, "", query="error") is True


def test_search_is_case_insensitive():
    row = make_row(extra={"level": "ERROR"})
    assert row_matches_filters(row, "Disk Full", query="DISK") is True
    assert row_matches_filters(row, "Disk Full", query="error") is True


def test_source_key_filter():
    row = make_row(source_key="clientA|server1")
    assert row_matches_filters(row, "", source_keys=["clientA|server1"]) is True
    assert row_matches_filters(row, "", source_keys=["clientB|server2"]) is False


def test_filters_are_anded():
    row = make_row(source_key="clientA|server1", extra={"level": "INFO"})
    assert (
        row_matches_filters(
            row,
            "Disk full",
            extra_key="level",
            extra_values=['"INFO"'],
            query="disk",
            source_keys=["clientA|server1"],
        )
        is True
    )
    # One failing predicate is enough to reject.
    assert (
        row_matches_filters(
            row,
            "Disk full",
            extra_key="level",
            extra_values=['"INFO"'],
            query="disk",
            source_keys=["clientB|server2"],
        )
        is False
    )


def test_haystack_is_lowercased_and_includes_all_parts():
    haystack = row_haystack(
        make_row(source_key="ClientA|Server1", extra={"level": "ERROR"}), "Disk Full"
    )
    assert haystack == haystack.lower()
    for part in ("disk full", "clienta|server1", "error"):
        assert part in haystack


# --------------------------------------------------------------------------
# Table building
# --------------------------------------------------------------------------


def test_rows_to_dataframe_columns_and_values():
    rows = [make_row(ts=1_700_000_000, template_id=7, extra={"level": "INFO"})]
    frame = rows_to_dataframe(rows, {7: ("User <*> logged in", 3)})
    assert list(frame.columns) == [
        "time_utc",
        "ts",
        "template_id",
        "template",
        "source_key",
        "extra",
    ]
    row = frame.iloc[0]
    assert row["time_utc"] == "2023-11-14 22:13:20"
    assert row["ts"] == 1_700_000_000
    assert row["template"] == "User <*> logged in"
    assert row["extra"] == '{"level": "INFO"}'


def test_rows_to_dataframe_degrades_on_unknown_template():
    """A stale or missing snapshot must not raise."""
    frame = rows_to_dataframe([make_row(template_id=42)], {})
    assert frame.iloc[0]["template"] == UNKNOWN_TEMPLATE.format(tid=42)


def test_rows_to_dataframe_empty_keeps_columns():
    assert list(rows_to_dataframe([], {}).columns) == [
        "time_utc",
        "ts",
        "template_id",
        "template",
        "source_key",
        "extra",
    ]


def test_template_table_counts_and_spans():
    counts = [(1, 3), (2, 1)]
    templates = {1: ("User <*> logged in", 99), 2: ("Disk <*> full", 5)}
    filtered = [make_row(ts=100, template_id=1), make_row(ts=300, template_id=1)]

    frame = template_table(counts, templates, filtered)
    first = frame.iloc[0]
    assert first["template_id"] == 1
    assert first["count_in_window"] == 3  # whole window
    assert first["count_filtered"] == 2  # after filters
    assert first["first_seen_utc"] == format_ts(100)
    assert first["last_seen_utc"] == format_ts(300)
    # Drain3's own counter is reported separately from the windowed count.
    assert first["drain3_all_time_size"] == 99

    second = frame.iloc[1]
    assert second["count_filtered"] == 0
    assert second["first_seen_utc"] == ""


# --------------------------------------------------------------------------
# Fetcher validation
# --------------------------------------------------------------------------


def test_validated_drops_malformed_records_and_counts_them():
    good = {"message": "ok", "ts": 100, "source_key": "s", "extra": {"a": 1}}
    bad = [
        {"message": "no ts", "source_key": "s"},
        {"ts": 100, "source_key": "s"},  # no message
        {"message": "m", "ts": "100", "source_key": "s"},  # ts not an int
        {"message": "m", "ts": True, "source_key": "s"},  # bool is not a time
        {"message": "m", "ts": 100},  # no source_key
        "not a dict",
    ]
    skipped = []
    fetch = validated(lambda t1, t2: [good, *bad], skipped)

    assert fetch(0, 1000) == [good]
    assert len(skipped) == len(bad)


def test_validated_coerces_missing_extra_to_dict():
    records = [
        {"message": "m", "ts": 1, "source_key": "s"},
        {"message": "m", "ts": 2, "source_key": "s", "extra": None},
        {"message": "m", "ts": 3, "source_key": "s", "extra": "nope"},
    ]
    out = validated(lambda t1, t2: records, [])(0, 10)
    assert [r["extra"] for r in out] == [{}, {}, {}]


def test_validated_tolerates_none_from_fetcher():
    assert validated(lambda t1, t2: None, [])(0, 10) == []


# --------------------------------------------------------------------------
# run_app argument handling
# --------------------------------------------------------------------------


def test_run_app_requires_a_fetcher():
    """Without a source every window returns empty, which reads as a bug.

    Better to fail at the call site, where the fix is, than to render an app
    that silently cannot fetch anything.
    """
    from log_parser.ui import run_app

    with pytest.raises(TypeError):
        run_app()


def test_run_app_rejects_a_bare_fetch_fn():
    """The likely mistake is passing the function instead of the Fetcher.

    Streamlit swallows the resulting AttributeError into a page-wide traceback,
    so name the expected type before any widget is drawn.
    """
    from log_parser.ui import run_app

    with pytest.raises(TypeError, match="single Fetcher"):
        run_app(lambda t1, t2: [])


def test_demo_fetcher_is_a_fresh_instance_each_call():
    """A function, not a shared constant: callers cannot mutate each other."""
    from log_parser.fetchers import demo_fetcher

    first, second = demo_fetcher(), demo_fetcher()
    assert first is not second
    assert first == second  # frozen dataclass: equal by value
    assert first.config_fields is not second.config_fields


def test_demo_fetcher_builds_a_working_fetch_fn():
    from log_parser.fetchers import demo_fetcher

    fetch = demo_fetcher().build(demo_fetcher().defaults())
    records = fetch(1_700_000_000, 1_700_000_600)
    assert records, "demo fetcher produced no records"
    assert all(r["ts"] >= 1_700_000_000 and r["ts"] <= 1_700_000_600 for r in records)
    # More than one message shape, or the templates tab has nothing to show.
    assert len({r["message"].split()[0] for r in records}) > 1

"""Streamlit UI for the log parser.

The app is meant to be launched from *your own* script, so your fetcher lives in
your code rather than in an installed file that an upgrade overwrites::

    # app.py
    from log_parser import Fetcher, run_app

    run_app(Fetcher(name="prod", description="...", build=build_prod),
            title="prod logs")

then ``streamlit run app.py``. ``python -m log_parser`` runs the same app with a
synthetic demo source, for a look around before wiring anything real.

The app is a plain frontend to :meth:`LogParser.query`: every window the user
loads goes through it, and V1 decides on its own whether that means reading
SQLite or calling ``fetch_fn`` for the ranges it is missing. There is no
read-only mode and no fetch toggle, because ``query`` already makes that
distinction correctly.

This module imports streamlit at the top level; ``log_parser/__init__.py``
deliberately does not import this module, so the parser stays usable when
streamlit is absent.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import traceback
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import streamlit as st

from log_parser.core import (
    DEFAULT_MARGIN_SEC,
    LogParser,
    Record,
    SqliteStore,
    TemplateModel,
)
from log_parser.fetchers import Fetcher, validated

UTC = dt.timezone.utc
ANY_KEY = "— any —"
DEFAULT_ROW_CAP = 50_000
UNKNOWN_TEMPLATE = "⟨unknown template {tid}⟩"


# --------------------------------------------------------------------------
# Pure helpers -- no streamlit, no I/O. Everything here is unit-tested.
# --------------------------------------------------------------------------

def to_epoch(date: dt.date, time: dt.time) -> int:
    """Combine a UTC date and time into a Unix epoch second.

    The ``tzinfo`` argument is load-bearing: ``datetime.combine(d, t).timestamp()``
    interprets the result in the machine's local zone, which would silently shift
    every query window by the UTC offset.
    """
    return int(dt.datetime.combine(date, time, tzinfo=UTC).timestamp())


def format_ts(ts: int) -> str:
    """Render an epoch second as a UTC string.

    ``tz=UTC`` is mandatory here for the same reason as in :func:`to_epoch`;
    without it the display drifts by the local offset.
    """
    return dt.datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def render_value(value: Any) -> str:
    """Render an ``extra`` value as JSON.

    Not ``str()``: that yields Python reprs (``['a', 'b']``) which would never
    match the JSON actually stored (``["a", "b"]``), so the key/value picker and
    the free-text search would disagree about the very same row.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def discover_extra_keys(rows: Sequence[Record]) -> List[str]:
    """Top-level ``extra`` keys present in ``rows``, most common first."""
    counts: Dict[str, int] = {}
    for row in rows:
        for key in row.get("extra") or {}:
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts, key=lambda k: (-counts[k], k))


def extra_values_for_key(rows: Sequence[Record], key: str) -> List[str]:
    """Distinct JSON-rendered values of ``extra[key]``, most common first."""
    counts: Dict[str, int] = {}
    for row in rows:
        extra = row.get("extra") or {}
        if key in extra:
            rendered = render_value(extra[key])
            counts[rendered] = counts.get(rendered, 0) + 1
    return sorted(counts, key=lambda v: (-counts[v], v))


def row_haystack(row: Record, template_text: str) -> str:
    """The text a free-text search scans for one row.

    Includes the mined template because the raw message is not stored -- SQLite
    holds only ``template_id`` -- so a search for words the user remembers
    seeing would otherwise never match anything.
    """
    return " ".join((
        template_text,
        row.get("source_key", ""),
        render_value(row.get("extra") or {}),
    )).lower()


def row_matches_filters(
    row: Record,
    template_text: str,
    *,
    extra_key: Optional[str] = None,
    extra_values: Sequence[str] = (),
    query: str = "",
    source_keys: Sequence[str] = (),
) -> bool:
    """Whether ``row`` survives every active filter (all ANDed)."""
    if source_keys and row.get("source_key") not in source_keys:
        return False

    if extra_key and extra_key != ANY_KEY:
        extra = row.get("extra") or {}
        if extra_key not in extra:
            return False
        # No values selected means "has this key at all", which is a useful
        # filter in its own right on sparsely-populated extras.
        if extra_values and render_value(extra[extra_key]) not in extra_values:
            return False

    if query and query.lower() not in row_haystack(row, template_text):
        return False

    return True


def rows_to_dataframe(
    rows: Sequence[Record], templates: Dict[int, Tuple[str, int]]
) -> pd.DataFrame:
    """Build the events table. Unknown template ids degrade, never raise."""
    return pd.DataFrame([
        {
            "time_utc": format_ts(row["ts"]),
            "ts": row["ts"],
            "template_id": row["template_id"],
            "template": templates.get(
                row["template_id"], (UNKNOWN_TEMPLATE.format(tid=row["template_id"]), 0)
            )[0],
            "source_key": row["source_key"],
            "extra": render_value(row.get("extra") or {}),
        }
        for row in rows
    ], columns=["time_utc", "ts", "template_id", "template", "source_key", "extra"])


def template_table(
    counts: Sequence[Tuple[int, int]],
    templates: Dict[int, Tuple[str, int]],
    filtered_rows: Sequence[Record],
) -> pd.DataFrame:
    """Per-template summary for the window, most frequent first."""
    filtered_counts: Dict[int, int] = {}
    spans: Dict[int, Tuple[int, int]] = {}
    for row in filtered_rows:
        tid, ts = row["template_id"], row["ts"]
        filtered_counts[tid] = filtered_counts.get(tid, 0) + 1
        low, high = spans.get(tid, (ts, ts))
        spans[tid] = (min(low, ts), max(high, ts))

    records = []
    for tid, count in counts:
        text, all_time = templates.get(tid, (UNKNOWN_TEMPLATE.format(tid=tid), 0))
        span = spans.get(tid)
        records.append({
            "template_id": tid,
            "template": text,
            "count_in_window": count,
            "count_filtered": filtered_counts.get(tid, 0),
            "first_seen_utc": format_ts(span[0]) if span else "",
            "last_seen_utc": format_ts(span[1]) if span else "",
            "drain3_all_time_size": all_time,
        })
    return pd.DataFrame(records, columns=[
        "template_id", "template", "count_in_window", "count_filtered",
        "first_seen_utc", "last_seen_utc", "drain3_all_time_size",
    ])


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

def load_templates(state_path: str) -> Dict[int, Tuple[str, int]]:
    """``{cluster_id: (template_text, all_time_size)}`` from the Drain3 snapshot.

    Constructing a TemplateModel neither creates nor rewrites the snapshot; only
    saving does. A missing file yields no clusters, and an unreadable one is
    swallowed: the events table is still perfectly usable with ids alone, so a
    foreign or truncated snapshot should cost the template *text*, not the page.
    """
    try:
        model = TemplateModel(state_path)
        return {
            cluster.cluster_id: (cluster.get_template(), cluster.size)
            for cluster in model._miner.drain.clusters
        }
    except Exception:  # noqa: BLE001 -- unpickling can fail in many ways
        return {}


def run_query(
    fetch_fn, db_path: str, state_path: str, margin_sec: int, t1: int, t2: int
) -> Dict[str, Any]:
    """Query ``[t1, t2]`` through V1, then read back what the UI needs.

    Opens a parser, queries, and closes -- ``close()`` saves the Drain3 tree, and
    Streamlit sessions end without a reliable teardown hook, so deferring it
    would strand every template learned during the session and leave the stored
    rows pointing at cluster ids the snapshot has never heard of.
    """
    skipped: List[Record] = []
    parser = LogParser(
        fetch_fn=validated(fetch_fn, skipped),
        db_path=db_path,
        state_path=state_path,
        margin_sec=margin_sec,
    )
    try:
        gaps = parser.store.missing(t1, t2)
        rows = parser.query(t1, t2)
        counts = parser.store.template_counts(t1, t2)
        bounds = parser.store.ts_bounds()
        total = parser.store.count_events()
    finally:
        # Runs even when fetch_fn raised: V1 has already ingested whatever
        # arrived before the failure, and those rows need their templates saved.
        parser.close()

    return {
        "rows": rows,
        "counts": counts,
        "bounds": bounds,
        "total": total,
        "gaps": gaps,
        "skipped": len(skipped),
        "templates": load_templates(state_path),
    }


def peek_store(db_path: str) -> Optional[Dict[str, Any]]:
    """Cheap status read for the sidebar; ``None`` when there is no store yet."""
    import os

    if not os.path.exists(db_path):
        return None
    store = SqliteStore(db_path)
    try:
        return {
            "total": store.count_events(),
            "bounds": store.ts_bounds(),
            "ranges": store.fetched_ranges(),
        }
    finally:
        store.close()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def _sidebar_fetcher(fetcher: Fetcher) -> Tuple[Any, str]:
    st.sidebar.subheader("Source")
    st.sidebar.caption(f"**{fetcher.name}** — {fetcher.description}")

    config: Dict[str, Any] = {}
    for field in fetcher.config_fields:
        widget_key = f"cfg_{fetcher.name}_{field.key}"
        if field.kind == "int":
            config[field.key] = st.sidebar.number_input(
                field.label, value=int(field.default), step=1,
                help=field.help or None, key=widget_key,
            )
        elif field.kind == "float":
            config[field.key] = st.sidebar.number_input(
                field.label, value=float(field.default),
                help=field.help or None, key=widget_key,
            )
        else:
            config[field.key] = st.sidebar.text_input(
                field.label, value=str(field.default),
                help=field.help or None, key=widget_key,
            )
    return fetcher.build(config), fetcher.name


def _sidebar_window(status: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    st.sidebar.subheader("Time window (UTC)")

    if status and status.get("bounds"):
        low, high = status["bounds"]
        start_default = dt.datetime.fromtimestamp(low, tz=UTC)
        end_default = dt.datetime.fromtimestamp(high, tz=UTC)
    else:
        end_default = dt.datetime.now(tz=UTC)
        start_default = end_default - dt.timedelta(days=1)

    col1, col2 = st.sidebar.columns(2)
    start_date = col1.date_input("Start date (UTC)", start_default.date())
    start_time = col2.time_input("Start time (UTC)", start_default.time().replace(microsecond=0))
    col3, col4 = st.sidebar.columns(2)
    end_date = col3.date_input("End date (UTC)", end_default.date())
    end_time = col4.time_input("End time (UTC)", end_default.time().replace(microsecond=0))

    t1, t2 = to_epoch(start_date, start_time), to_epoch(end_date, end_time)
    # Shown so the UTC conversion is auditable by eye rather than taken on trust.
    st.sidebar.caption(f"Epoch `{t1}` → `{t2}` · inclusive [start, end]")
    return t1, t2


def _sidebar_filters(rows: Sequence[Record]) -> Dict[str, Any]:
    st.sidebar.subheader("Filters")
    if not rows:
        st.sidebar.caption("Load a window to filter it.")
        return {}

    keys = discover_extra_keys(rows)
    extra_key = st.sidebar.selectbox("extra key", [ANY_KEY] + keys)
    extra_values: List[str] = []
    if extra_key != ANY_KEY:
        options = extra_values_for_key(rows, extra_key)
        extra_values = st.sidebar.multiselect(
            f"{extra_key} is", options,
            help="Values are shown as JSON, matching how they are stored.",
        )

    sources = sorted({row["source_key"] for row in rows})
    source_keys = st.sidebar.multiselect("source_key", sources)
    query = st.sidebar.text_input(
        "Search", help="Matches template text, source_key and extra (case-insensitive).",
    )
    return {
        "extra_key": extra_key,
        "extra_values": extra_values,
        "query": query,
        "source_keys": source_keys,
    }


def run_app(
    fetcher: Fetcher,
    *,
    title: str = "log-parser",
    icon: str = "🪵",
    db_path: str = "events.db",
    state_path: str = "drain3.bin",
) -> None:
    """Render the whole app. Call this from your own Streamlit script.

    Pass the source it should read::

        from log_parser import Fetcher, run_app
        run_app(Fetcher(name="prod", description="...", build=build_prod),
                title="prod logs")

    One app, one source: the store is a single ``events.db`` whose coverage
    ranges record *that* a window was fetched, not which source answered. Two
    sources sharing a store would let one's ranges mask the other's gaps. Point
    a second script at a second ``db_path`` instead.

    The path arguments only seed the sidebar inputs; the user can still change
    them at runtime.
    """
    if not isinstance(fetcher, Fetcher):
        raise TypeError(
            f"run_app() takes a single Fetcher, got {type(fetcher).__name__}. "
            "See log_parser.fetchers for its shape."
        )
    st.set_page_config(page_title=title, page_icon=icon, layout="wide")
    st.title(f"{icon} {title}")

    st.sidebar.subheader("Store")
    db_path = st.sidebar.text_input("Database", db_path)
    state_path = st.sidebar.text_input("Drain3 snapshot", state_path)
    margin_sec = st.sidebar.number_input(
        "Fetch margin (s)", value=DEFAULT_MARGIN_SEC, min_value=0, step=10,
        help="Gaps are fetched padded by this much; the un-padded gap is recorded.",
    )

    status = peek_store(db_path)
    if status is None:
        st.sidebar.caption(f"`{db_path}` — not found (created on first load)")
    else:
        span = ""
        if status["bounds"]:
            span = f" · {format_ts(status['bounds'][0])} → {format_ts(status['bounds'][1])}"
        st.sidebar.caption(f"`{db_path}` — {status['total']:,} events{span}")

    fetch_fn, fetcher_name = _sidebar_fetcher(fetcher)
    t1, t2 = _sidebar_window(status)

    if t2 < t1:
        # missing_ranges() returns [] for an inverted window, so query() would
        # quietly return nothing while looking fully covered.
        st.error("End must be at or after start.")
        st.stop()

    load = st.sidebar.button("Load window", type="primary", width="stretch")
    st.sidebar.caption(
        "Loading queries the store and fetches any ranges it is missing."
    )

    cache_key = (db_path, state_path, int(margin_sec), t1, t2, fetcher_name)
    if st.session_state.get("cache_key") not in (None, cache_key):
        st.sidebar.caption("⚠️ Settings changed — press Load window to refresh.")

    if load:
        with st.spinner(f"Querying {format_ts(t1)} → {format_ts(t2)} (UTC)…"):
            try:
                st.session_state["result"] = run_query(
                    fetch_fn, db_path, state_path, int(margin_sec), t1, t2
                )
                st.session_state["cache_key"] = cache_key
                st.session_state.pop("error", None)
            except sqlite3.OperationalError as exc:
                st.session_state["error"] = (
                    f"SQLite is busy or unavailable: {exc}. "
                    "Another process may be writing to this database.", "",
                )
            except Exception as exc:  # noqa: BLE001 -- surfaced, not swallowed
                st.session_state["error"] = (
                    f"The fetcher failed: {exc}", traceback.format_exc(),
                )

    if "error" in st.session_state:
        message, detail = st.session_state["error"]
        st.error(message)
        if detail:
            with st.expander("Details"):
                st.code(detail)
        st.caption(
            "Records fetched before the failure were stored, and their ranges "
            "recorded — loading again resumes from the gap that remains."
        )

    result = st.session_state.get("result")
    if result is None:
        st.info(
            "Pick a time window in the sidebar and press **Load window**. "
            "Windows already stored are served locally; anything missing is fetched."
        )
        return

    rows, templates = result["rows"], result["templates"]
    filters = _sidebar_filters(rows)
    # Filtering runs over the rows already loaded: no I/O, so typing in the
    # search box cannot trigger a remote fetch on every keystroke.
    filtered = [
        row for row in rows
        if row_matches_filters(
            row,
            templates.get(row["template_id"], ("", 0))[0],
            extra_key=filters.get("extra_key"),
            extra_values=filters.get("extra_values", ()),
            query=filters.get("query", ""),
            source_keys=filters.get("source_keys", ()),
        )
    ]

    if result["skipped"]:
        st.warning(
            f"{result['skipped']:,} malformed record(s) from the fetcher were "
            "skipped: each must have a string `message`, an int `ts` and a "
            "string `source_key`."
        )

    unknown = {r["template_id"] for r in rows} - set(templates)
    if unknown:
        st.warning(
            f"{len(unknown)} template id(s) in this window are absent from "
            f"`{state_path}` — is it the snapshot that goes with this database?"
        )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("In window", f"{len(rows):,}")
    col2.metric("After filters", f"{len(filtered):,}")
    col3.metric("Templates", f"{len(result['counts']):,}")
    col4.metric("Gaps fetched", f"{len(result['gaps']):,}")
    if result["gaps"]:
        st.caption("Fetched: " + ", ".join(
            f"{format_ts(a)} → {format_ts(b)}" for a, b in result["gaps"]
        ))

    events_tab, templates_tab = st.tabs(["Events", "Templates"])

    with events_tab:
        if not rows:
            bounds = result["bounds"]
            if result["total"] and bounds:
                st.info(
                    f"No events in this window. The store spans "
                    f"{format_ts(bounds[0])} → {format_ts(bounds[1])} (UTC)."
                )
            else:
                st.info("No events stored yet for this window.")
        elif not filtered:
            st.info(f"{len(rows):,} events in the window, none match the filters.")
        else:
            capped = filtered[:DEFAULT_ROW_CAP]
            if len(filtered) > DEFAULT_ROW_CAP:
                st.warning(
                    f"Showing the first {DEFAULT_ROW_CAP:,} of {len(filtered):,} "
                    "matching events — narrow the window. The CSV download "
                    "still contains every match."
                )
            frame = rows_to_dataframe(capped, templates)
            st.dataframe(
                frame, width="stretch", hide_index=True,
                column_config={
                    "template": st.column_config.TextColumn("template", width="large"),
                    "extra": st.column_config.TextColumn("extra", width="large"),
                },
            )
            st.download_button(
                "Download filtered as CSV",
                rows_to_dataframe(filtered, templates).to_csv(index=False).encode(),
                file_name=f"events_{t1}_{t2}.csv",
                mime="text/csv",
            )

    with templates_tab:
        if not result["counts"]:
            st.info("No templates in this window.")
        else:
            st.dataframe(
                template_table(result["counts"], templates, filtered),
                width="stretch", hide_index=True,
                column_config={
                    "template": st.column_config.TextColumn("template", width="large"),
                    "drain3_all_time_size": st.column_config.NumberColumn(
                        "drain3 all-time",
                        help=(
                            "Drain3's own counter from the snapshot: all-time "
                            "across every window, not scoped to this one. "
                            "SQLite is the source of truth for occurrences."
                        ),
                    ),
                },
            )


if __name__ == "__main__":
    # Reached by `streamlit run .../log_parser/ui.py`, i.e. `python -m
    # log_parser`. No caller can pass a source down this path, so offer the
    # demo; a real source belongs in the user's own app.py.
    from log_parser.fetchers import demo_fetcher

    run_app(demo_fetcher())

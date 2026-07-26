# log-parser

Local-first log parsing: [Drain3](https://github.com/IBM/Drain3) templates + SQLite
occurrences.

Parses log messages into templates, stores every occurrence in SQLite, and serves
time-range queries locally — reaching the remote source only for windows never
fetched before.

## Install

```bash
pip install git+https://github.com/Humort56/log-parser.git
```

Pin a tag or commit for reproducible installs (no tags are published yet, so
pin a commit until one is):

```bash
pip install git+https://github.com/Humort56/log-parser.git@<commit-sha>
```

The web UI is an optional extra, so installing the library alone stays light:

```bash
pip install "log-parser[ui] @ git+https://github.com/Humort56/log-parser.git"
```

## Usage

Construct with a `fetch_fn(t1, t2) -> list[record]`. That function is the only thing
that touches the remote log source; it knows nothing about coverage, padding, or dedup.

```python
from log_parser import LogParser


def fetch_fn(t1, t2):
    # Return every record whose ts falls in [t1, t2].
    return [
        {
            "message": "User 12 logged in from 10.0.0.1",
            "ts": 1_700_000_000,
            "source_key": "clientA|server1",
            "extra": {"level": "INFO"},
        },
    ]


# Use it as a context manager: leaving the block flushes the Drain3 template
# snapshot, even if the body raises. `parser.close()` does the same by hand.
with LogParser(
    fetch_fn=fetch_fn,
    db_path="events.db",
    state_path="drain3.bin",
    margin_sec=60,
) as parser:
    parser.ingest(
        {
            "message": "Disk sda full at 91%",
            "ts": 1_700_000_120,
            "source_key": "clientA|server2",
            "extra": {"level": "ERROR"},
        }
    )

    rows = parser.query(1_700_000_000, 1_700_000_200)
```

### The record

One dict flows through everything:

| Field        | Type   | Meaning                                            |
| ------------ | ------ | -------------------------------------------------- |
| `message`    | `str`  | Text handed to Drain3 as-is (timestamp already separated) |
| `ts`         | `int`  | UTC Unix epoch                                      |
| `source_key` | `str`  | Opaque origin id, pre-built by the caller/fetcher    |
| `extra`      | `dict` | Arbitrary kept fields, stored as JSON               |

`query` returns rows with `ts`, `template_id`, `source_key`, and `extra`
(JSON-decoded).

`parser.store` also exposes `count_events()`, `template_counts(t1, t2)` (per-template
occurrence counts for a window, most frequent first) and `ts_bounds()` (the full span
of stored events).

## How queries stay local-first

1. If `[t1, t2]` is fully covered by previously fetched ranges, read from SQLite and
   return — no fetch.
2. Otherwise compute the missing sub-intervals. For each, call `fetch_fn` once with
   padding: `[gap_start - margin_sec, gap_end + margin_sec]`.
3. Ingest everything returned through the normal ingest path.
4. Record the **un-padded** gap. Fetch wide, record exact — the padding never inflates
   the claim about what is actually held.

Padded fetches deliberately overlap, so the same events get pulled more than once.
Dedup is enforced **in the database**, not in application code: identity is
`(template_id, ts, source_key)` under a `UNIQUE` index, and inserts use
`INSERT OR IGNORE`. Duplicates are physically unable to be created. `extra` is not part
of identity — a re-delivered event with different `extra` is still the same event.

## Web UI

A Streamlit app for browsing what has been parsed. Pick a UTC window, press
**Load window**, and filter the results by time, `source_key`, or `extra`
content — either with the key/value picker or the free-text search, which also
matches the mined template text.

For a look around with synthetic data and no configuration:

```bash
pip install "log-parser[ui] @ git+https://github.com/Humort56/log-parser.git"
log-parser                 # or: python -m log_parser
```

### Pointing it at your logs

Write your own `app.py`. `log_parser` is an ordinary import, and your fetcher is
passed *into* the app — nothing is registered globally and there is no file
inside the package to edit:

```python
# app.py
from log_parser import ConfigField, Fetcher, run_app


def build_fetcher(config):
    base_url = config["base_url"]  # from the sidebar widgets below

    def fetch_fn(t1, t2):
        # Return every record whose ts falls in [t1, t2] (inclusive, UTC epoch).
        return [
            {
                "message": hit["msg"],
                "ts": int(hit["timestamp"]),
                "source_key": f"{hit['cluster']}|{hit['host']}",
                "extra": {"level": hit["level"]},
            }
            for hit in my_client.search(base_url, t1, t2)
        ]

    return fetch_fn


run_app(
    Fetcher(
        name="my source",
        description="Reads from the prod log API.",
        build=build_fetcher,
        config_fields=[ConfigField("base_url", "Base URL", default="https://...")],
    ),
    title="prod logs",
)
```

```bash
streamlit run app.py
```

There is a ready-to-edit copy in [examples/app.py](examples/app.py). Streamlit
options work as usual (`streamlit run app.py --server.port 8600`); `log-parser`
forwards anything it does not recognise, so `log-parser --server.port 8600`
works too. `--help` and `--version` are handled by `log-parser` itself.

`build(config)` is split from `fetch_fn` so an expensive client is constructed
once per settings change rather than once per fetch. `config_fields` render as
sidebar widgets automatically.

The app is a plain frontend to `LogParser.query`: loading a window reads locally
when it is already covered and fetches only the ranges that are missing. There
is no separate "offline" mode, because `query` already makes that decision.

### Tuning how messages are mined

Drain3's own settings — the similarity threshold, tree depth, masking rules —
are passed as a `TemplateMinerConfig`, re-exported so you need no second import:

```python
from log_parser import LogParser, TemplateMinerConfig

config = TemplateMinerConfig()
config.drain_sim_th = 0.6   # stricter: more templates, each narrower
config.drain_depth = 5

parser = LogParser(fetch_fn, db_path="events.db", state_path="drain3.bin", config=config)
```

`run_app(..., config=config)` takes the same object. It is a constructor
argument rather than a sidebar widget on purpose — see below.

**A config change is a reparse, not a setting.** These values decide which
messages collapse into the same template, and the resulting `template_id` is
what SQLite stores. Pointing a changed config at an existing store leaves old
rows carrying ids mined under the old settings while new rows get ids from the
new ones — and since `fetched_ranges` still marks those windows covered,
re-querying will not re-derive them. Use a fresh `db_path` **and** `state_path`,
or reparse from scratch.

To make that failure visible rather than silent, the mining settings are
fingerprinted into a sidecar file next to the snapshot (`drain3.bin.config`),
written whenever the snapshot is saved. Reopening a snapshot whose fingerprint
disagrees logs a warning naming both digests. Nothing raises — you may have
changed the config deliberately — but it will not pass unnoticed. Settings that
do not affect mining (profiling, snapshot cadence) are excluded, so they never
trigger it.

### Custom views and layout

What the page draws is extensible from that same `app.py`. `views=` adds tabs;
`layout=` replaces individual regions. Nothing inside the package needs editing,
so an upgrade keeps your rendering:

```python
from log_parser import Fetcher, Layout, View, ViewContext, run_app
import streamlit as st


def render_levels(ctx: ViewContext) -> None:
    counts = {}
    for row in ctx.filtered:
        level = row.get("extra", {}).get("level", "unknown")
        counts[level] = counts.get(level, 0) + 1
    st.bar_chart(counts)


run_app(
    Fetcher(name="prod", description="...", build=build_fetcher),
    views=[View("Levels", render_levels)],
    layout=Layout(metrics=my_metrics_row),
)
```

Every view and most `Layout` callbacks receive a `ViewContext` describing the
loaded window:

| Field | Meaning |
| --- | --- |
| `result` | The whole `QueryResult`: `rows`, `counts`, `bounds`, `total`, `gaps`, `skipped`, `templates` |
| `filtered` | `result.rows` after the sidebar filters — what a view normally renders |
| `t1`, `t2` | The window bounds, UTC epoch seconds |
| `db_path`, `state_path` | The paths currently in the sidebar |

`Layout` fields, all optional — anything left unset keeps its built-in:

| Field | Signature |
| --- | --- |
| `metrics`, `warnings`, `events`, `templates` | `(ViewContext) -> None` |
| `error` | `(message: str, detail: str) -> None` |
| `filters` | `(rows) -> dict` |

The two exceptions are forced by when they run. `error` draws after a *failed*
query, when there is no `QueryResult` to build a context from. `filters` runs
before a context exists — it produces the filtering the context reports — and
its returned dict must carry the keys the filter step reads: `extra_key`,
`extra_values`, `query` and `source_keys`. Returning `{}` filters nothing.

`replace_default_views=True` drops Events and Templates and shows only your
`views` (it requires at least one, or the page would render no tabs at all).
`sidebar_extra=` takes a zero-argument callable drawn at the end of the sidebar;
it runs before a window is loaded, so read its widgets back through
`st.session_state` in whichever view needs them.

Bad arguments raise `TypeError`/`ValueError` at the call site, before any widget
is drawn, rather than surfacing as a page-wide traceback.

**One app reads one source.** The store records *that* a window was fetched, not
which source answered, so two sources sharing an `events.db` would let one's
coverage mask the other's gaps. For a second source, use a second script with its
own `db_path`.

Records that lack a string `message`, an int `ts`, or a string `source_key` are
dropped before ingestion and reported in the UI, so one malformed record cannot
abort an otherwise good fetch.

Note that `log_parser.ui` is the only module that imports Streamlit — `import
log_parser` never does, so the parser keeps working wherever the `ui` extra is
not installed. `run_app` is re-exported from `log_parser` but resolved lazily, so
importing it is what pulls Streamlit in, not importing the parser.

## Development

Requires Python 3.10 or newer.

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,ui]"    # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest
```

Omit `,ui` to work on the parser alone; the UI tests skip themselves when
Streamlit is absent.

The same three gates run in CI on every push and pull request, across Python
3.10–3.13:

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy
```

Tests live in [tests/](tests/): two cover the module contract (ingestion, and
that padding cannot duplicate); the rest cover the coverage algebra
(`merge_ranges`, `missing_ranges`), the UI's pure helpers, and the package
facade.

## Notes

- Times are UTC Unix epoch integers throughout. Ranges are inclusive: `[start, end]`.
- `TemplateMiner(config=None)` would silently read `drain3.ini` from the working
  directory, so the module always passes an explicit config — parsing behavior does not
  depend on where the process was launched. Omitting `config=` gets Drain3's built-in
  defaults, never a file picked up from the cwd.
- The Drain3 state file holds the template tree plus an aggregate per-template `size`
  counter that Drain3 maintains internally. SQLite remains the sole source of truth for
  occurrences.

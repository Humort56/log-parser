"""Describing a ``fetch_fn`` source to the UI.

A fetch function answers exactly one question -- "give me records between A and
B" -- and knows nothing about coverage, padding or dedup. :class:`Fetcher` wraps
one with enough metadata for the UI to render its configuration, so a new source
needs no changes to ``log_parser.ui``.

Sources are passed *in*, from your own script::

    # app.py
    from log_parser import ConfigField, Fetcher, run_app

    def build(config):
        url = config["url"]
        def fetch_fn(t1, t2):
            ...                       # return a list of records
        return fetch_fn

    run_app(Fetcher(
        name="my source",
        description="Reads from ...",
        config_fields=[ConfigField("url", "Base URL", default="https://...")],
        build=build,
    ))

There is no registry and no import-time registration: the app reads exactly the
source handed to it, so what it talks to is decided by the argument at the call
site rather than by which modules happen to have been imported. One app serves
one source -- see :func:`log_parser.ui.run_app` for why.

This module deliberately does not import streamlit: the UI reads
``config_fields`` and renders the widgets itself.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from log_parser.core import FetchFn, Record

logger = logging.getLogger(__name__)

__all__ = [
    "DEMO_FETCHER_NAME",
    "ConfigField",
    "Fetcher",
    "FieldKind",
    "demo_fetcher",
    "validated",
]

#: Widget type for a ConfigField. A typo here used to fall through silently to
#: the text widget; as a Literal it is a type error instead.
FieldKind = Literal["text", "int", "float"]


@dataclass(frozen=True)
class ConfigField:
    """One configuration input for a fetcher, rendered as a sidebar widget."""

    key: str
    label: str
    default: Any
    kind: FieldKind = "text"
    help: str = ""


@dataclass(frozen=True)
class Fetcher:
    """A named ``fetch_fn`` factory plus the configuration it needs.

    ``build(config)`` receives the current values of ``config_fields`` and
    returns the ``fetch_fn`` itself. Splitting it in two means an expensive
    client is constructed once per settings change rather than once per fetch.
    """

    name: str
    description: str
    build: Callable[[dict[str, Any]], FetchFn]
    config_fields: list[ConfigField] = field(default_factory=list)

    def defaults(self) -> dict[str, Any]:
        return {f.key: f.default for f in self.config_fields}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validated(fetch_fn: FetchFn, skipped: list[Record]) -> FetchFn:
    """Wrap ``fetch_fn`` so malformed records are dropped rather than ingested.

    ``LogParser.ingest`` indexes ``message``/``ts``/``source_key`` directly, so a
    third-party fetcher returning a record missing any of them would raise
    mid-window, after earlier records were already stored. Dropping them here
    keeps one bad record from aborting an otherwise good fetch; the caller shows
    ``skipped`` so the loss is visible rather than silent.
    """

    def wrapped(t1: int, t2: int) -> list[Record]:
        clean: list[Record] = []
        for record in fetch_fn(t1, t2) or ():
            # FetchFn says this is always a dict, so mypy calls the branch
            # unreachable -- but the whole point here is that the fetcher is
            # third-party code that may not honour its annotation.
            if not isinstance(record, dict):
                skipped.append(record)  # type: ignore[unreachable]
                continue
            message, ts, source_key = (
                record.get("message"),
                record.get("ts"),
                record.get("source_key"),
            )
            # bool is an int subclass; a True timestamp is a bug, not a time.
            if (
                not isinstance(message, str)
                or not isinstance(ts, int)
                or isinstance(ts, bool)
                or not isinstance(source_key, str)
            ):
                skipped.append(record)
                continue
            extra = record.get("extra")
            clean.append(
                {
                    "message": message,
                    "ts": ts,
                    "source_key": source_key,
                    "extra": extra if isinstance(extra, dict) else {},
                }
            )
        if skipped:
            logger.warning(
                "Dropped %d malformed record(s) from the fetch of [%d, %d]",
                len(skipped),
                t1,
                t2,
            )
        return clean

    return wrapped


# --------------------------------------------------------------------------
# Built-in: synthetic demo data
# --------------------------------------------------------------------------

DEMO_FETCHER_NAME = "demo (synthetic)"

#: Ceiling on records one demo fetch will build. A year-wide window at the
#: densest setting would otherwise materialise ~31M dicts in memory.
MAX_DEMO_RECORDS = 200_000

_DEMO_MESSAGES: tuple[tuple[str, str], ...] = (
    ("User {n} logged in from 10.0.0.{octet}", "INFO"),
    ("Request {n} completed in {ms}ms", "INFO"),
    ("Disk sd{disk} full at {pct}%", "ERROR"),
    ("Connection to db-{n} timed out after {ms}ms", "WARN"),
    ("Cache miss for key user:{n}", "DEBUG"),
)


def _build_demo(config: dict[str, Any]) -> FetchFn:
    per_minute = max(1, int(config.get("per_minute", 6)))
    sources = max(1, int(config.get("sources", 3)))

    def fetch_fn(t1: int, t2: int) -> list[Record]:
        # Events sit on a fixed absolute grid (multiples of `step` since the
        # epoch), never one relative to t1. Two overlapping fetches must agree
        # exactly on the events in the seconds they share, otherwise identity
        # (template_id, ts, source_key) differs and V1's dedup cannot collapse
        # them -- the padded refetch would silently double the rows.
        step = max(1, 60 // per_minute)
        first = t1 + (-t1 % step)
        records: list[Record] = []
        # Truncate from the end of the window rather than thinning the grid:
        # the absolute positions of the records that *are* returned must not
        # change, or dedup across overlapping fetches breaks.
        last = min(t2, first + (MAX_DEMO_RECORDS - 1) * step)
        if last < t2:
            logger.warning(
                "Demo window [%d, %d] capped at %d records; showing up to %d",
                t1,
                t2,
                MAX_DEMO_RECORDS,
                last,
            )
        for ts in range(first, last + 1, step):
            rng = random.Random(ts)
            # Index by grid position, not by ts: ts is always a multiple of
            # step, so `ts % len(...)` would collapse to one shape whenever
            # step and the shape count share a factor.
            template, level = _DEMO_MESSAGES[(ts // step) % len(_DEMO_MESSAGES)]
            message = template.format(
                n=rng.randint(1, 999),
                octet=rng.randint(1, 254),
                ms=rng.randint(5, 5000),
                disk="abcd"[rng.randrange(4)],
                pct=rng.randint(80, 99),
            )
            host = f"server{ts % sources + 1}"
            records.append(
                {
                    "message": message,
                    "ts": ts,
                    "source_key": f"clientA|{host}",
                    "extra": {
                        "level": level,
                        "host": host,
                        # A nested value on purpose: it exercises the JSON rendering
                        # path in the UI's filters from the very first window.
                        "trace": {"span": rng.randint(1000, 9999), "retries": ts % 3},
                        "tags": ["demo", level.lower()],
                    },
                }
            )
        return records

    return fetch_fn


def demo_fetcher() -> Fetcher:
    """The synthetic source used by ``python -m log_parser``.

    A function rather than a module-level constant so that nothing is built at
    import time, and so a caller cannot mutate a shared instance.
    """
    return Fetcher(
        name=DEMO_FETCHER_NAME,
        description=(
            "Deterministic synthetic logs, generated locally. No network. "
            "Lets the UI be explored without configuring a real source."
        ),
        build=_build_demo,
        config_fields=[
            ConfigField(
                "per_minute",
                "Events per minute",
                6,
                "int",
                help="Higher values produce denser windows.",
            ),
            ConfigField(
                "sources",
                "Distinct source hosts",
                3,
                "int",
                help="How many source_key values to spread events across.",
            ),
        ],
    )

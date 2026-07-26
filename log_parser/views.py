"""Describing custom rendering to the UI.

:mod:`log_parser.fetchers` lets a caller decide *where the logs come from*
without editing library code. This module is the same seam for *how they are
displayed*: a caller adds tabs and replaces individual regions of the page from
its own ``app.py``, and upgrading the package does not overwrite any of it.

Rendering is passed *in*, from your own script::

    # app.py
    from log_parser import Fetcher, Layout, View, run_app

    def render_latency(ctx):
        import streamlit as st
        st.bar_chart({"ms": [r["extra"].get("ms", 0) for r in ctx.filtered]})

    run_app(
        Fetcher(name="prod", description="...", build=build_prod),
        views=[View("Latency", render_latency)],
        layout=Layout(metrics=my_metrics),
    )

As with fetchers there is no registry and no import-time registration: the page
renders exactly the callables handed to it, so what is drawn is decided by the
argument at the call site rather than by which modules happen to have been
imported.

This module deliberately does not import streamlit. It defines the contract;
:mod:`log_parser.ui` does the calling, and a callback draws with whatever
streamlit API it likes. Keeping the import out means these types stay part of
the eagerly-imported facade -- ``from log_parser import View`` costs nothing --
while streamlit is still only pulled in by ``run_app`` itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from log_parser.core import Record

__all__ = [
    "Layout",
    "QueryResult",
    "View",
    "ViewContext",
    "resolve_views",
]


@dataclass(frozen=True)
class QueryResult:
    """Everything one loaded window gives the page.

    A dataclass rather than a dict so a mistyped field is an error at the call
    site instead of a ``KeyError`` halfway down the render. It lives here rather
    than in the UI because a third-party view reads it, which makes it part of
    the public contract.
    """

    rows: list[Record]
    counts: list[tuple[int, int]]
    bounds: tuple[int, int] | None
    total: int
    gaps: list[tuple[int, int]]
    skipped: int
    templates: dict[int, tuple[str, int]]


@dataclass(frozen=True)
class ViewContext:
    """The loaded window, handed to every view and region callback.

    ``rows`` is everything the window holds; ``filtered`` is what survived the
    sidebar filters. A view renders ``filtered`` unless it deliberately wants
    the unfiltered set, which stays reachable as ``result.rows``.

    Callbacks are handed data that is already loaded and are not expected to
    query: filtering happens in memory precisely so that typing in the sidebar
    cannot trigger a fetch. A view that genuinely needs its own I/O can still do
    it, but it opts into the cost rather than inheriting it.
    """

    result: QueryResult
    filtered: list[Record]
    t1: int
    t2: int
    db_path: str
    state_path: str


@dataclass(frozen=True)
class View:
    """One tab: a label and the callable that draws its body.

    ``render`` is called with a :class:`ViewContext` while the tab is the active
    streamlit container, so it can draw with plain ``st.*`` calls and does not
    need to know anything about tabs.
    """

    name: str
    render: Callable[[ViewContext], None]


@dataclass(frozen=True)
class Layout:
    """Replacements for the page's built-in regions. ``None`` means "built-in".

    One dataclass rather than a keyword argument per region: ``run_app`` keeps a
    signature a reader can hold in their head, and a region added in a later
    version is a new optional field here instead of a change to that signature.

    Most callbacks take a :class:`ViewContext`, so a caller writes one shape
    everywhere. The two exceptions are forced by *when* they run:

    * ``error`` renders after a failed query, when there is no ``QueryResult``
      to build a context from, so it takes the message and traceback directly.
    * ``filters`` runs *before* a context exists -- it produces the very
      filtering the context reports. It receives the window's rows and must
      return a dict carrying the keys the filter step reads: ``extra_key``,
      ``extra_values``, ``query`` and ``source_keys``. Returning ``{}`` filters
      nothing, which is what the built-in does for an empty window.
    """

    metrics: Callable[[ViewContext], None] | None = None
    warnings: Callable[[ViewContext], None] | None = None
    events: Callable[[ViewContext], None] | None = None
    templates: Callable[[ViewContext], None] | None = None
    error: Callable[[str, str], None] | None = None
    filters: Callable[[Sequence[Record]], dict[str, Any]] | None = None

    #: Field names whose value, when not None, must be callable. Derived from
    #: the annotations rather than repeated by hand so that adding a region
    #: above cannot silently escape validation.
    def callbacks(self) -> dict[str, Any]:
        """``{field_name: value}`` for every override that was actually set."""
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if getattr(self, name) is not None
        }


def resolve_views(
    defaults: Sequence[View],
    extra: Sequence[View],
    *,
    replace_defaults: bool,
) -> list[View]:
    """The final tab list: either ``extra`` alone, or the defaults plus it.

    Pure, so the ordering rules are decided (and readable) without a streamlit
    runtime. Validation of the arguments themselves happens in ``run_app``,
    before any widget is drawn.
    """
    return list(extra) if replace_defaults else [*defaults, *extra]

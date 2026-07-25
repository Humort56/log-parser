"""Local-first log parsing: Drain3 templates + SQLite occurrences.

Public facade. ``from log_parser import LogParser`` keeps working exactly as it
did when this package was a single module.

This file must NOT import :mod:`log_parser.ui` or streamlit. The UI is an
optional extra (``pip install log-parser[ui]``), and streamlit is a heavy
dependency; importing it here would make the *parser* unimportable for anyone
who installed the library alone or whose streamlit install is broken. Only
``log_parser.ui`` imports it, and only when that module is actually used.
"""

from log_parser.core import (
    DEFAULT_MARGIN_SEC,
    FetchFn,
    LogParser,
    Range,
    Record,
    SqliteStore,
    TemplateModel,
    merge_ranges,
    missing_ranges,
)

__all__ = [
    "DEFAULT_MARGIN_SEC",
    "FetchFn",
    "LogParser",
    "Range",
    "Record",
    "SqliteStore",
    "TemplateModel",
    "merge_ranges",
    "missing_ranges",
]

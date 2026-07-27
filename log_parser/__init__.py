"""Local-first log parsing: Drain3 templates + SQLite occurrences.

Public facade. ``from log_parser import LogParser`` keeps working exactly as it
did when this package was a single module.

This file must NOT import :mod:`log_parser.ui` or streamlit. The UI is an
optional extra (``pip install log-parser[ui]``), and streamlit is a heavy
dependency; importing it here would make the *parser* unimportable for anyone
who installed the library alone or whose streamlit install is broken. Only
``log_parser.ui`` imports it, and only when that module is actually used.

:mod:`log_parser.fetchers` and :mod:`log_parser.views` are safe to re-export
here -- both import nothing but the standard library and :mod:`log_parser.core`
-- so a user's ``app.py`` can declare its fetcher *and* its custom rendering
with a single ``from log_parser import ...``. ``views`` describes what the page
draws but never imports streamlit itself, which is what keeps it on this side of
the line.

``run_app`` is the one exception: it lives in :mod:`log_parser.ui` and needs
streamlit, so it is resolved lazily by ``__getattr__`` below. That keeps
``from log_parser import Fetcher, run_app`` working in an app script while
``import log_parser`` still costs nothing for a pipeline that never renders.
"""

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

# Re-exported so tuning drain3 does not force a second, version-coupled import
# into a user's app.py -- `from log_parser import TemplateMinerConfig`.
from drain3.template_miner_config import TemplateMinerConfig

from log_parser.checks import CheckReport, CheckStep, check
from log_parser.core import (
    DEFAULT_MARGIN_SEC,
    FetchFn,
    LogParser,
    Range,
    Record,
    SqliteStore,
    TemplateModel,
    config_fingerprint,
    merge_ranges,
    missing_ranges,
)
from log_parser.fetchers import ConfigField, Fetcher
from log_parser.views import Layout, QueryResult, View, ViewContext

if TYPE_CHECKING:  # For type checkers only -- never executed at runtime.
    from log_parser.ui import run_app

try:
    __version__ = version("log-parser")
except PackageNotFoundError:  # Running from a source tree that was never installed.
    __version__ = "0.0.0+unknown"

# A library configures no handlers of its own: without this, a consuming app
# that never set up logging would print "No handlers could be found" the first
# time this package logged anything.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "DEFAULT_MARGIN_SEC",
    "CheckReport",
    "CheckStep",
    "ConfigField",
    "FetchFn",
    "Fetcher",
    "Layout",
    "LogParser",
    "QueryResult",
    "Range",
    "Record",
    "SqliteStore",
    "TemplateMinerConfig",
    "TemplateModel",
    "View",
    "ViewContext",
    "__version__",
    "check",
    "config_fingerprint",
    "merge_ranges",
    "missing_ranges",
    "run_app",
]


def __getattr__(name: str) -> Any:
    """Resolve ``run_app`` on first access (PEP 562), importing streamlit then.

    Raising ``AttributeError`` for everything else is required: without it,
    ``hasattr``/``from … import`` probes for absent names would import the UI.
    """
    if name == "run_app":
        from log_parser.ui import run_app

        return run_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

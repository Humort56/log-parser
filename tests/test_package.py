"""The package facade keeps V1's public surface intact.

`log_parser` was a single module before the UI arrived. Callers importing it
must not notice the change, and — more easily broken — importing it must not
drag in streamlit, so the parser stays usable wherever the optional UI extra
is not installed.
"""

import ast
import pathlib
import subprocess
import sys

import log_parser

V1_NAMES = [
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


def test_public_names_are_exported():
    for name in V1_NAMES:
        assert hasattr(log_parser, name), f"log_parser.{name} disappeared"
    # Subset, not equality: the facade may gain names (the UI added Fetcher,
    # ConfigField and run_app). Only *losing* a V1 name breaks a caller.
    assert set(V1_NAMES) <= set(log_parser.__all__)


def test_everything_in_all_is_reachable():
    """`__all__` and the module must agree — including the lazy `run_app`.

    `from log_parser import *` reads `__all__`, so a name listed there but not
    resolvable is an ImportError for the user and nothing at all for us.
    """
    for name in log_parser.__all__:
        assert getattr(log_parser, name) is not None


def test_unknown_attribute_still_raises_attribute_error():
    """The `__getattr__` hook must not swallow typos into an import attempt."""
    import pytest

    with pytest.raises(AttributeError):
        # The bare access *is* the assertion: it must reach __getattr__ and
        # raise rather than being swallowed into an import attempt.
        log_parser.definitely_not_a_real_name  # noqa: B018


def test_core_classes_are_usable_from_the_facade(tmp_path):
    parser = log_parser.LogParser(
        fetch_fn=lambda t1, t2: [],
        db_path=str(tmp_path / "events.db"),
        state_path=str(tmp_path / "drain3.bin"),
    )
    assert (
        parser.ingest(
            {
                "message": "User 1 logged in",
                "ts": 100,
                "source_key": "s",
                "extra": {},
            }
        )
        is True
    )
    parser.close()

    assert log_parser.merge_ranges([(100, 140), (141, 200)]) == [(100, 200)]
    assert log_parser.missing_ranges(100, 200, [(100, 140)]) == [(141, 200)]


def test_facade_does_not_import_streamlit_at_module_level():
    """Static check: no import of streamlit/pandas anywhere in __init__.py.

    Complements the runtime check below — this one fails loudly at the exact
    place a future edit would introduce the problem.
    """
    source = pathlib.Path(log_parser.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "streamlit" not in imported
    assert "pandas" not in imported


def test_importing_the_parser_does_not_pull_in_streamlit():
    """Runtime check: `import log_parser` must leave streamlit unimported.

    Uses a subprocess because this test session has already imported streamlit
    via test_ui.py, so sys.modules here proves nothing.
    """
    code = (
        "import sys; import log_parser; "
        "assert 'streamlit' not in sys.modules, 'log_parser pulled in streamlit'; "
        "assert 'pandas' not in sys.modules, 'log_parser pulled in pandas'; "
        # Using the parser must stay clean too: `run_app` is resolved lazily,
        # and anything that touches it eagerly (a stray hasattr, a dir() scan)
        # would defeat the whole arrangement.
        "log_parser.LogParser, log_parser.Fetcher, log_parser.merge_ranges; "
        "assert 'streamlit' not in sys.modules, 'attribute access pulled in streamlit'; "
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_run_app_is_importable_from_the_facade():
    """`from log_parser import run_app` must work — that is the documented API.

    Guarded, because it is the one facade name that needs the optional extra.
    """
    import pytest

    pytest.importorskip("streamlit")

    from log_parser import run_app
    from log_parser.ui import run_app as direct

    assert run_app is direct

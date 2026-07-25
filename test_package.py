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
    "DEFAULT_MARGIN_SEC", "FetchFn", "LogParser", "Range", "Record",
    "SqliteStore", "TemplateModel", "merge_ranges", "missing_ranges",
]


def test_public_names_are_exported():
    for name in V1_NAMES:
        assert hasattr(log_parser, name), f"log_parser.{name} disappeared"
    assert sorted(log_parser.__all__) == sorted(V1_NAMES)


def test_core_classes_are_usable_from_the_facade(tmp_path):
    parser = log_parser.LogParser(
        fetch_fn=lambda t1, t2: [],
        db_path=str(tmp_path / "events.db"),
        state_path=str(tmp_path / "drain3.bin"),
    )
    assert parser.ingest({
        "message": "User 1 logged in", "ts": 100,
        "source_key": "s", "extra": {},
    }) is True
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
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout

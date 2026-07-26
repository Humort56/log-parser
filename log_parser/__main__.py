"""``python -m log_parser`` -- launch the Streamlit UI with the demo source.

This is the zero-configuration tour, not the way to run a real deployment: it
can only offer the built-in synthetic fetcher, because a source registered in
*this* process would not survive into the one Streamlit spawns for the script.
To use your own fetcher, register it in your own ``app.py`` and run
``streamlit run app.py`` -- see :mod:`log_parser.ui`.

Streamlit runs a *script*, not a module: ``streamlit run -m pkg`` is not
supported. So resolve the installed path of :mod:`log_parser.ui` and hand that
file to Streamlit's CLI; its ``__main__`` block registers the demo source.
"""

from __future__ import annotations

import sys


def main() -> int:
    # Import the UI itself, not just streamlit: it also needs pandas, and a
    # missing one of those should produce the same guidance rather than a
    # traceback from halfway down the import chain.
    try:
        from streamlit.web import cli as stcli

        from log_parser import ui
    except ImportError as exc:
        print(
            f"The Streamlit UI needs the optional 'ui' extra ({exc}):\n"
            "    pip install 'log-parser[ui]'\n"
            "The parser itself works without it.",
            file=sys.stderr,
        )
        return 1

    # Anything after `python -m log_parser` is forwarded to streamlit, so
    # `--server.port 8600` and friends keep working.
    sys.argv = ["streamlit", "run", ui.__file__, *sys.argv[1:]]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())

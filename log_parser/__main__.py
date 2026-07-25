"""``python -m log_parser`` -- launch the Streamlit UI.

Streamlit runs a *script*, not a module: ``streamlit run -m pkg`` is not
supported. So resolve the installed path of :mod:`log_parser.ui` and hand that
file to Streamlit's CLI. Doing it this way means the UI ships and launches from
an installed package, with no repo checkout and nothing for the user to locate.
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

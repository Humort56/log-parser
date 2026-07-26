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

import argparse
import logging
import sys
from collections.abc import Sequence

from log_parser import __version__

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="log-parser",
        description="Launch the Streamlit UI with the built-in demo source.",
        epilog=(
            "Unrecognized arguments are forwarded to Streamlit, e.g. "
            "`log-parser --server.port 8600`. Note that --help and --version "
            "are handled here and no longer reach `streamlit run`."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    parser.add_argument("--version", action="version", version=f"log-parser {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log at DEBUG instead of INFO."
    )
    # parse_known_args, not parse_args: everything this parser does not claim is
    # a streamlit flag and must survive to be forwarded.
    args, streamlit_args = parser.parse_known_args(argv)

    # This is the one place the package owns the process, so it is the only
    # place that may configure the root logger. Done before the import guard so
    # the failure below is reported through the same channel as everything else.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Import the UI itself, not just streamlit: it also needs pandas, and a
    # missing one of those should produce the same guidance rather than a
    # traceback from halfway down the import chain.
    try:
        from streamlit.web import cli as stcli

        from log_parser import ui
    except ImportError as exc:
        logger.error(
            "The Streamlit UI needs the optional 'ui' extra (%s):\n"
            "    pip install 'log-parser[ui]'\n"
            "The parser itself works without it.",
            exc,
        )
        return 1

    sys.argv = ["streamlit", "run", ui.__file__, *streamlit_args]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())

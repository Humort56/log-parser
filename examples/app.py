"""Example Streamlit app -- copy this and point it at your own log source.

Run it with::

    streamlit run app.py

Everything specific to *your* logs lives here, in your own file. `log_parser`
stays an ordinary dependency you can upgrade without losing this code.
"""

from typing import Any

from log_parser import ConfigField, Fetcher, FetchFn, Record, View, ViewContext, run_app


def build_fetcher(config: dict[str, Any]) -> FetchFn:
    """Return the ``fetch_fn`` the parser calls for ranges it does not have.

    ``config`` holds the values of the ``config_fields`` below, as edited in the
    sidebar. Build your client here -- this runs once per settings change, not
    once per fetch.
    """
    base_url = config["base_url"]

    def fetch_fn(t1: int, t2: int) -> list[Record]:
        """Return every record whose ``ts`` falls in ``[t1, t2]`` (inclusive).

        Both bounds are UTC Unix epoch seconds. Fetch the whole range in one
        call if you can; the parser already avoids asking for windows it holds.

        Replace the body below with a real query, e.g.::

            response = requests.get(
                f"{base_url}/search", params={"from": t1, "to": t2}, timeout=30
            )
            response.raise_for_status()
            return [
                {
                    "message": hit["msg"],
                    "ts": int(hit["timestamp"]),
                    "source_key": f'{hit["cluster"]}|{hit["host"]}',
                    "extra": {"level": hit["level"], "pod": hit["pod"]},
                }
                for hit in response.json()["hits"]
            ]
        """
        raise NotImplementedError(f"Implement fetch_fn to read {base_url} between {t1} and {t2}.")

    return fetch_fn


def render_levels(ctx: ViewContext) -> None:
    """An extra tab, drawn after the built-in Events and Templates.

    ``ctx.filtered`` is the loaded window after the sidebar filters; the
    unfiltered rows stay available as ``ctx.result.rows``. The data is already
    in memory, so a view costs nothing to draw and never triggers a fetch.
    """
    import streamlit as st

    counts: dict[str, int] = {}
    for row in ctx.filtered:
        level = str(row.get("extra", {}).get("level", "unknown"))
        counts[level] = counts.get(level, 0) + 1

    if not counts:
        st.info("No events match the current filters.")
        return
    st.bar_chart(counts)


run_app(
    Fetcher(
        name="my source",
        description="Describe where these logs come from; shown in the sidebar.",
        build=build_fetcher,
        # Rendered as sidebar widgets automatically. kind is "text"/"int"/"float".
        config_fields=[
            ConfigField("base_url", "Base URL", default="https://logs.example.com"),
        ],
    ),
    title="my logs",
    # One app reads one source into one store. For a second source, copy this
    # file and give it its own db_path so the coverage ranges stay separate.
    db_path="events.db",
    # Extra tabs. Add `replace_default_views=True` to drop Events and Templates
    # and show only these.
    views=[View("Levels", render_levels)],
    # Individual regions can be replaced too, without touching the rest of the
    # page -- anything left unset keeps its built-in:
    #
    #     from log_parser import Layout
    #     layout=Layout(metrics=my_metrics, events=my_events_table),
)

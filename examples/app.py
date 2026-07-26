"""Example Streamlit app -- copy this and point it at your own log source.

Run it with::

    streamlit run app.py

Everything specific to *your* logs lives here, in your own file. `log_parser`
stays an ordinary dependency you can upgrade without losing this code.
"""

from typing import Any

from log_parser import ConfigField, Fetcher, FetchFn, Record, run_app


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
)

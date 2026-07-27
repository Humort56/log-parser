"""Test-running a fetcher and a drain3 config before trusting them.

The UI's only way to exercise a fetcher is to load a window, and that writes to
the real store: by the time a bad ``ts`` or a config that collapses everything
into one template shows up, the rows are already committed -- and because
``fetched_ranges`` marks those windows covered, re-querying will not re-derive
them. :func:`check` does the same work against a temporary directory instead,
then writes a Markdown report of the templates it mined and the rows they
produced::

    from log_parser import check
    from myapp import FETCHER, CONFIG

    report = check(FETCHER, last="10m", config=CONFIG)
    print(report)          # short summary
    assert report.ok       # usable straight from your own pytest

The report is the point, not a pass/fail line. Whether ``drain_sim_th`` merged
two things you needed kept apart is a question only the template list can
answer, and two reports from two configs diff cleanly against each other.

This module imports no streamlit, so it can be re-exported from the package
facade alongside :mod:`log_parser.fetchers`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from drain3.template_miner_config import TemplateMinerConfig

from log_parser.core import LogParser, Range, Record, config_fingerprint
from log_parser.fetchers import Fetcher, validated

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_REPORT_NAME",
    "CheckReport",
    "CheckStep",
    "check",
    "parse_duration",
]

#: Written to the working directory unless ``out=`` says otherwise.
DEFAULT_REPORT_NAME = "check-report.md"

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str | int) -> int:
    """Seconds from ``"30s"``/``"10m"``/``"2h"``/``"7d"``, or a plain int.

    A bare int is already seconds. ``bool`` is rejected rather than counted as
    0/1: it is an ``int`` subclass, so ``last=True`` would otherwise mean a
    one-second window -- the same trap :func:`log_parser.fetchers.validated`
    guards against for timestamps.
    """
    if isinstance(value, bool):
        raise TypeError(f"last= takes a duration like '10m' or a number of seconds, got {value!r}.")

    if isinstance(value, int):
        seconds = value
    else:
        text = value.strip().lower()
        unit = _UNITS.get(text[-1:])
        try:
            # No unit suffix means seconds, so "600" and "600s" agree.
            seconds = int(text) if unit is None else int(text[:-1]) * unit
        except ValueError:
            raise ValueError(
                f"Could not read last={value!r}. Use a number of seconds, or a "
                f"duration like '30s', '10m', '2h', '7d'."
            ) from None

    if seconds <= 0:
        raise ValueError(f"last={value!r} is not a positive duration.")
    return seconds


def _resolve_window(last: str | int | None, window: Range | None) -> tuple[Range, str]:
    """Turn the two window arguments into one range plus how it was chosen.

    Neither argument gets a default on purpose. An implicit "last 10 minutes"
    against a source whose data is a day old returns nothing, and an empty
    report reads exactly like a broken fetcher -- being asked which window you
    meant is cheaper than debugging that.
    """
    if last is not None and window is not None:
        raise ValueError("check() takes either last= or window=, not both.")

    if last is not None:
        seconds = parse_duration(last)
        now = int(time.time())
        # Inclusive at both ends, like every other range in this package, so a
        # 600s window spans 601 seconds of grid.
        return (now - seconds, now), f"last={last!r}"

    if window is not None:
        t1, t2 = window
        if t2 < t1:
            # missing_ranges() returns [] for an inverted window, so this would
            # otherwise run to completion and report zero records as if the
            # fetcher were empty.
            raise ValueError(f"check(window=({t1}, {t2})) ends before it starts.")
        return (t1, t2), "fixed window"

    raise ValueError(
        "check() needs a window: pass last='10m' for a window ending now, or "
        "window=(t1, t2) for fixed UTC epoch seconds."
    )


@dataclass(frozen=True)
class CheckStep:
    """One stage of a check, and whether it survived."""

    name: str
    ok: bool
    detail: str
    error: str = ""


@dataclass(frozen=True)
class CheckReport:
    """What a test run found. ``report.ok`` is the one-line answer."""

    fetcher_name: str
    window: Range
    window_source: str
    steps: list[CheckStep] = field(default_factory=list)
    rows: list[Record] = field(default_factory=list)
    skipped: list[Record] = field(default_factory=list)
    templates: dict[int, tuple[str, int]] = field(default_factory=dict)
    fingerprint: str = ""
    previous_fingerprint: str | None = None
    deduped: int = 0
    report_path: Path | None = None

    @property
    def ok(self) -> bool:
        """True when every step passed.

        Derived rather than stored so it cannot drift out of agreement with
        ``steps``.
        """
        return all(step.ok for step in self.steps)

    def to_markdown(self) -> str:
        """The full report. Separate from writing it so it is testable as text."""
        t1, t2 = self.window
        lines = [
            f'# check report — "{self.fetcher_name}"',
            "",
            f"- window: `{_utc(t1)}` → `{_utc(t2)}` ({t2 - t1}s, {self.window_source})",
            f"- result: **{'PASSED' if self.ok else 'FAILED'}**",
            f"- drain3 fingerprint: `{self.fingerprint}`",
            "",
            "| step | ok | detail |",
            "| --- | --- | --- |",
        ]
        lines += [
            f"| {step.name} | {'✅' if step.ok else '❌'} | "
            f"{_cell(step.detail if step.ok else f'{step.detail} — {step.error}')} |"
            for step in self.steps
        ]

        lines += ["", f"## Templates ({len(self.templates)})", ""]
        if self.templates:
            lines += ["| id | count | template |", "| --- | --- | --- |"]
            # Frequent templates first: those are the ones worth judging a
            # config by. Ties break on id so two runs order identically.
            for template_id, (text, count) in sorted(
                self.templates.items(), key=lambda item: (-item[1][1], item[0])
            ):
                lines.append(f"| {template_id} | {count} | `{_cell(text)}` |")
        else:
            lines.append("No templates were mined.")

        lines += ["", f"## Events ({len(self.rows)})", ""]
        if self.rows:
            lines += [
                "| ts | utc | template_id | source_key | extra |",
                "| --- | --- | --- | --- | --- |",
            ]
            for row in self.rows:
                extra = json.dumps(row.get("extra") or {}, sort_keys=True, default=str)
                lines.append(
                    f"| {row['ts']} | {_utc(row['ts'])} | {row['template_id']} "
                    f"| `{_cell(row['source_key'])}` | `{_cell(extra)}` |"
                )
        else:
            lines.append("No events were stored.")

        lines += ["", f"## Skipped records ({len(self.skipped)})", ""]
        if self.skipped:
            lines.append("Dropped before ingest — missing or mistyped `message`/`ts`/`source_key`:")
            lines += ["", "```"]
            lines += [repr(record) for record in self.skipped]
            lines.append("```")
        else:
            lines.append("None.")

        return "\n".join(lines) + "\n"

    def __str__(self) -> str:
        head = f"{'PASSED' if self.ok else 'FAILED'} — {self.fetcher_name}"
        body = [
            f"  {step.name:<9} {'ok ' if step.ok else 'FAIL'}  {step.detail}" for step in self.steps
        ]
        tail = f"\n  report    {self.report_path}" if self.report_path else ""
        return "\n".join([head, *body]) + tail


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(text: str) -> str:
    """Make a value safe inside a Markdown table cell.

    Pipes are the real hazard, not a theoretical one: ``source_key`` is
    conventionally ``"cluster|host"``, so an unescaped one would end the cell
    early and shift every remaining column.
    """
    return text.replace("|", r"\|").replace("\n", " ")


def check(
    fetcher: Fetcher,
    *,
    last: str | int | None = None,
    window: Range | None = None,
    config: TemplateMinerConfig | None = None,
    fetcher_config: dict[str, Any] | None = None,
    state_path: str | None = None,
    out: str | Path | None = DEFAULT_REPORT_NAME,
) -> CheckReport:
    """Run ``fetcher`` over one window and report what it and ``config`` produce.

    Nothing durable is parsed: the templates and rows are built in a temporary
    directory that is deleted before this returns, so it is safe to run against
    a live deployment. The only file left behind is the Markdown report.

    ``last``
        A window ending now -- ``"10m"``, ``"2h"``, ``"7d"``, or seconds.
    ``window``
        A fixed ``(t1, t2)`` of inclusive UTC epoch seconds.

        Exactly one of the two is required; see :func:`_resolve_window`.
    ``config``
        The :class:`~drain3.template_miner_config.TemplateMinerConfig` to mine
        with. Omitted means drain3's defaults, matching :class:`LogParser`.
    ``fetcher_config``
        Values for the fetcher's ``config_fields``. Defaults to
        :meth:`Fetcher.defaults`, i.e. what the sidebar would start with.
    ``state_path``
        An *existing* snapshot to compare fingerprints against. Read only, never
        written -- this is how a config change that would invalidate stored
        template ids becomes visible before you reparse.
    ``out``
        Where to write the report. ``None`` writes no file.

    Failures are recorded as steps rather than raised, so a broken fetcher still
    produces a report naming what broke. Bad *arguments* still raise: those are
    call-site bugs, not results.
    """
    # Mirrors ui._check_arguments, deliberately duplicated rather than imported:
    # that module pulls in streamlit, which this one must stay free of.
    if not isinstance(fetcher, Fetcher):
        raise TypeError(
            f"check() takes a single Fetcher, got {type(fetcher).__name__}. "
            "See log_parser.fetchers for its shape."
        )

    resolved, window_source = _resolve_window(last, window)
    t1, t2 = resolved
    steps: list[CheckStep] = []
    skipped: list[Record] = []
    rows: list[Record] = []
    templates: dict[int, tuple[str, int]] = {}
    deduped = 0

    config = config or TemplateMinerConfig()
    fingerprint = config_fingerprint(config)
    previous = _read_fingerprint(state_path) if state_path else None

    def finish() -> CheckReport:
        report = CheckReport(
            fetcher_name=fetcher.name,
            window=resolved,
            window_source=window_source,
            steps=steps,
            rows=rows,
            skipped=skipped,
            templates=templates,
            fingerprint=fingerprint,
            previous_fingerprint=previous,
            deduped=deduped,
            report_path=None,
        )
        if out is None:
            return report
        path = Path(out)
        # encoding is explicit: Windows would otherwise write cp1252, and
        # template text can carry anything the source logged.
        path.write_text(report.to_markdown(), encoding="utf-8")
        # Frozen dataclass, so the path is threaded through a copy rather than
        # assigned -- writing has to happen first to know it succeeded.
        return CheckReport(**{**vars(report), "report_path": path})

    settings = fetcher.defaults() if fetcher_config is None else fetcher_config
    try:
        fetch_fn = fetcher.build(dict(settings))
    except Exception as exc:  # noqa: BLE001 - third-party build, any failure is a result
        steps.append(CheckStep("build", False, "fetcher.build() raised", repr(exc)))
        return finish()
    steps.append(CheckStep("build", True, f"built with {settings or '{}'}"))

    started = time.monotonic()
    try:
        fetched = validated(fetch_fn, skipped)(t1, t2)
    except Exception as exc:  # noqa: BLE001 - ditto for the fetch itself
        steps.append(CheckStep("fetch", False, "fetch_fn() raised", repr(exc)))
        return finish()
    elapsed = time.monotonic() - started
    total = len(fetched) + len(skipped)
    steps.append(CheckStep("fetch", True, f"{total} record(s) in {elapsed:.2f}s"))

    # Both of these leave nothing to mine, and both mean the fetcher is not
    # usable as configured -- so they are failures, not an empty success.
    if not fetched:
        steps.append(
            CheckStep(
                "records",
                False,
                f"no usable records ({len(skipped)} skipped)",
                "The fetcher returned nothing valid for this window.",
            )
        )
        return finish()
    steps.append(CheckStep("records", True, f"{len(fetched)} valid, {len(skipped)} skipped"))

    try:
        rows, templates, deduped = _mine(fetched, resolved, config)
    except Exception as exc:  # noqa: BLE001 - drain3/sqlite failures belong in the report
        steps.append(CheckStep("mining", False, "parsing raised", repr(exc)))
        return finish()
    steps.append(
        CheckStep(
            "mining",
            True,
            f"{len(templates)} template(s) from {len(fetched)} message(s), {deduped} deduped",
        )
    )

    if state_path:
        steps.append(_snapshot_step(state_path, fingerprint, previous))

    return finish()


def _mine(
    records: list[Record], window: Range, config: TemplateMinerConfig
) -> tuple[list[Record], dict[int, tuple[str, int]], int]:
    """Parse ``records`` in a temp directory and read back what was stored.

    ``LogParser.ingest`` rather than ``query``: ingest is exactly
    match-then-store, while query would write ``fetched_ranges`` and call the
    fetcher a second time. The rows come back out of SQLite rather than being
    the input list, so the report shows the *stored* shape -- template ids
    assigned, ``extra`` round-tripped through JSON, dedup applied.
    """
    t1, t2 = window
    with TemporaryDirectory(prefix="log-parser-check-") as tmp:
        root = Path(tmp)
        with LogParser(
            lambda _t1, _t2: [],  # never called: nothing here goes through query()
            db_path=str(root / "events.db"),
            state_path=str(root / "drain3.bin"),
            config=config,
        ) as parser:
            with parser.store.bulk():
                stored = sum(parser.ingest(record) for record in records)
            # Widen the read past the requested window: a fetcher may legitimately
            # return records slightly outside it, and dropping them from the report
            # would hide exactly the kind of off-by-a-timezone bug worth catching.
            low = min((record["ts"] for record in records), default=t1)
            high = max((record["ts"] for record in records), default=t2)
            return (
                parser.store.read_events(min(t1, low), max(t2, high)),
                parser.model.clusters(),
                len(records) - stored,
            )


def _read_fingerprint(state_path: str) -> str | None:
    """The fingerprint recorded beside an existing snapshot, if there is one."""
    try:
        return Path(f"{state_path}.config").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Could not read the fingerprint beside %s", state_path, exc_info=True)
        return None


def _snapshot_step(state_path: str, fingerprint: str, previous: str | None) -> CheckStep:
    """Compare this config against the one an existing snapshot was built with.

    Currently this mismatch is only ever logged, so in the UI it is invisible
    unless you happen to be watching the terminal. A changed config leaves old
    rows carrying ids from the old vocabulary while new rows get ids from the
    new one, which is a reparse rather than a setting -- worth failing a check.
    """
    name = Path(f"{state_path}.config").name
    if previous is None:
        return CheckStep("snapshot", True, f"no existing {name}; nothing to compare")
    if previous == fingerprint:
        return CheckStep("snapshot", True, f"fingerprint matches {name}")
    return CheckStep(
        "snapshot",
        False,
        f"config changed since {name} was written ({previous} → {fingerprint})",
        "Template ids already stored were mined under the old settings. Use a "
        "fresh db_path and state_path, or reparse from scratch.",
    )

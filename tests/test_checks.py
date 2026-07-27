"""The headless test run: ``check()``.

Its whole reason to exist is that the only other way to exercise a fetcher --
loading a window in the UI -- writes to the real store, so a bad fetcher or an
unsuitable drain3 config is discovered only after rows are committed under ids
that a re-query will not re-derive.

Two properties therefore matter more than the rest, and are tested hardest:
``check()`` must leave nothing behind but its report, and the report must
actually reflect what was mined rather than echoing the input.
"""

import json
import time

import pytest
from drain3.template_miner_config import TemplateMinerConfig

from log_parser import CheckReport, TemplateModel, check, config_fingerprint
from log_parser.checks import parse_duration
from log_parser.fetchers import ConfigField, Fetcher, demo_fetcher

# The demo fetcher places events on a fixed absolute grid, so an aligned window
# yields a count that does not depend on when the test ran: at the default 6/min
# the step is 10s, and [t1, t2] is inclusive, so 600s spans 61 points.
ALIGNED = (1769504400, 1769505000)
ALIGNED_RECORDS = 61


def fetcher(build, *, name="test source", config_fields=()):
    """A Fetcher wrapping ``build``, with the boilerplate filled in."""
    return Fetcher(
        name=name,
        description="Used by the checks tests.",
        build=build,
        config_fields=list(config_fields),
    )


def returning(*fetched):
    """A ``build`` whose ``fetch_fn`` always returns ``fetched``."""

    def build(_config):
        return lambda _t1, _t2: list(fetched)

    return build


# --------------------------------------------------------------------------
# Durations and window resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30s", 30), ("10m", 600), ("2h", 7200), ("7d", 604800), ("600", 600), (600, 600)],
)
def test_parse_duration_units(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["10x", "", "m", "abc", "1.5h", 0, -60])
def test_parse_duration_rejects_nonsense(value):
    with pytest.raises(ValueError):
        parse_duration(value)


def test_parse_duration_rejects_bool():
    """``True`` is an int, so without an explicit guard last=True means one second.

    Silently checking a 1s window would report an empty fetcher rather than the
    mistake, which is the same trap ``validated`` guards against for timestamps.
    """
    with pytest.raises(TypeError):
        parse_duration(True)


def test_window_is_required():
    """No default window on purpose: an implicit "last 10 minutes" against a
    source whose data is older returns nothing, and an empty report reads
    exactly like a broken fetcher."""
    with pytest.raises(ValueError, match="needs a window"):
        check(demo_fetcher(), out=None)


def test_last_and_window_together_are_rejected():
    with pytest.raises(ValueError, match="not both"):
        check(demo_fetcher(), last="10m", window=ALIGNED, out=None)


def test_inverted_window_is_rejected():
    """missing_ranges() returns [] for an inverted window, so this would
    otherwise run to completion and report zero records as a fetcher problem."""
    with pytest.raises(ValueError, match="ends before it starts"):
        check(demo_fetcher(), window=(ALIGNED[1], ALIGNED[0]), out=None)


def test_last_and_equivalent_fixed_window_agree(monkeypatch):
    """The two forms are one feature: the same span must give the same report.

    time.time is pinned so the relative window lands on the same absolute grid
    as the fixed one -- otherwise the demo fetcher's alignment makes the counts
    differ by one for reasons that have nothing to do with window resolution.
    """
    monkeypatch.setattr(time, "time", lambda: float(ALIGNED[1]))

    relative = check(demo_fetcher(), last="10m", out=None)
    fixed = check(demo_fetcher(), window=ALIGNED, out=None)

    assert relative.window == fixed.window == ALIGNED
    assert len(relative.rows) == len(fixed.rows) == ALIGNED_RECORDS
    assert relative.templates == fixed.templates


def test_report_names_how_the_window_was_chosen():
    """A report read later has to be self-describing about its own window."""
    assert "last='10m'" in check(demo_fetcher(), last="10m", out=None).to_markdown()
    assert "fixed window" in check(demo_fetcher(), window=ALIGNED, out=None).to_markdown()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_demo_fetcher_passes_with_the_expected_templates():
    """_DEMO_MESSAGES has five shapes, so mining must find exactly five.

    Any other number means the config never reached drain3, or that messages
    were fed in already-templated -- both of which would otherwise show up as a
    plausible-looking report.
    """
    report = check(demo_fetcher(), window=ALIGNED, out=None)

    assert report.ok
    assert len(report.rows) == ALIGNED_RECORDS
    assert len(report.templates) == 5
    assert [step.name for step in report.steps] == ["build", "fetch", "records", "mining"]


def test_rows_come_back_from_the_store_not_from_the_fetch():
    """The report must show the *stored* shape, not the input echoed back.

    Reading through SQLite is what proves a template_id was actually assigned
    and that ``extra`` survived the JSON round trip -- the two things a caller
    is judging the config on.
    """

    def build(_config):
        return lambda _t1, _t2: [
            {"message": "hello world", "ts": 1000, "source_key": "a|b", "extra": {"n": 1}}
        ]

    report = check(fetcher(build), window=(900, 1100), out=None)

    (row,) = report.rows
    assert "message" not in row  # replaced by the mined template
    assert row["template_id"] in report.templates
    assert row["extra"] == {"n": 1}


def test_fetcher_config_overrides_the_defaults():
    """Without an override the sidebar defaults are used, which is what a real
    run would start from; with one, the check must honour it."""
    seen = {}

    def build(config):
        seen.update(config)
        return lambda _t1, _t2: [{"message": "m", "ts": 1000, "source_key": "a", "extra": {}}]

    spec = fetcher(build, config_fields=[ConfigField("url", "URL", default="https://default")])

    check(spec, window=(900, 1100), out=None)
    assert seen["url"] == "https://default"

    check(spec, window=(900, 1100), fetcher_config={"url": "https://override"}, out=None)
    assert seen["url"] == "https://override"


def test_config_changes_the_templates_it_mines():
    """The report is only useful if it responds to the config being checked.

    A strict threshold must split messages that a loose one merges; if both
    produce the same template count the config never reached drain3.
    """
    loose = check(demo_fetcher(), window=ALIGNED, config=_config(drain_sim_th=0.2), out=None)
    strict = check(demo_fetcher(), window=ALIGNED, config=_config(drain_sim_th=0.95), out=None)

    assert len(strict.templates) > len(loose.templates)


def _config(**overrides):
    cfg = TemplateMinerConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# --------------------------------------------------------------------------
# Failures are results, not tracebacks
# --------------------------------------------------------------------------


def test_a_raising_build_is_reported_not_raised():
    """A broken fetcher must still produce a report naming what broke."""

    def build(_config):
        raise RuntimeError("no credentials")

    report = check(fetcher(build), window=ALIGNED, out=None)

    assert not report.ok
    (step,) = report.steps
    assert step.name == "build"
    assert "no credentials" in step.error


def test_a_raising_fetch_is_reported_not_raised():
    def build(_config):
        def fetch_fn(_t1, _t2):
            raise TimeoutError("upstream timed out")

        return fetch_fn

    report = check(fetcher(build), window=ALIGNED, out=None)

    assert not report.ok
    assert [step.name for step in report.steps] == ["build", "fetch"]
    assert "upstream timed out" in report.steps[-1].error


def test_an_empty_fetch_fails_rather_than_passing_vacuously():
    """Zero records means the fetcher is unusable for this window. Reporting
    that as a pass would make the check worthless exactly when it matters."""
    report = check(fetcher(returning()), window=ALIGNED, out=None)

    assert not report.ok
    assert report.steps[-1].name == "records"


def test_malformed_records_are_skipped_and_counted():
    """check() reuses fetchers.validated, so the dry run and the real ingest
    agree on what counts as valid -- a record dropped here is one that would
    have been dropped at ingest too."""

    def build(_config):
        return lambda _t1, _t2: [
            {"message": "good", "ts": 1000, "source_key": "a", "extra": {}},
            {"message": "no ts", "source_key": "a"},
            {"message": "bad ts", "ts": "1000", "source_key": "a"},
            "not even a dict",
        ]

    report = check(fetcher(build), window=(900, 1100), out=None)

    assert report.ok  # one good record is enough to mine
    assert len(report.rows) == 1
    assert len(report.skipped) == 3


def test_all_records_malformed_fails():
    report = check(fetcher(returning({"message": "no ts"})), window=(900, 1100), out=None)

    assert not report.ok
    assert report.steps[-1].name == "records"


def test_ok_is_derived_from_the_steps():
    """Stored separately it could disagree with them."""
    assert CheckReport("f", (0, 1), "fixed window").ok  # no steps, nothing failed


# --------------------------------------------------------------------------
# The drain3 snapshot comparison
# --------------------------------------------------------------------------


def test_matching_snapshot_fingerprint_passes(tmp_path):
    cfg = _config(drain_sim_th=0.6)
    state_path = str(tmp_path / "drain3.bin")
    model = TemplateModel(state_path, cfg)
    model.template_id("User 12 logged in")
    model.save()

    report = check(demo_fetcher(), window=ALIGNED, config=cfg, state_path=state_path, out=None)

    assert report.ok
    assert report.previous_fingerprint == config_fingerprint(cfg)
    assert report.steps[-1].name == "snapshot"


def test_changed_config_against_an_existing_snapshot_fails(tmp_path):
    """This mismatch is currently only ever logged, so in the UI it is invisible
    unless you happen to be watching the terminal. Surfacing it is one of the
    two things a test run is for."""
    state_path = str(tmp_path / "drain3.bin")
    model = TemplateModel(state_path, _config(drain_sim_th=0.6))
    model.template_id("User 12 logged in")
    model.save()

    report = check(
        demo_fetcher(),
        window=ALIGNED,
        config=_config(drain_sim_th=0.95),
        state_path=state_path,
        out=None,
    )

    assert not report.ok
    assert report.steps[-1].name == "snapshot"
    assert report.previous_fingerprint != report.fingerprint


def test_absent_snapshot_is_not_a_failure(tmp_path):
    """A first run has nothing to compare against, which is normal."""
    report = check(
        demo_fetcher(),
        window=ALIGNED,
        state_path=str(tmp_path / "nothing-here.bin"),
        out=None,
    )

    assert report.ok
    assert report.previous_fingerprint is None


def test_the_real_state_path_is_never_written(tmp_path):
    """The snapshot is read to compare fingerprints and nothing more; parsing
    into it would defeat the point of checking before you commit."""
    state_path = tmp_path / "drain3.bin"
    check(demo_fetcher(), window=ALIGNED, state_path=str(state_path), out=None)

    assert not state_path.exists()


# --------------------------------------------------------------------------
# The report file
# --------------------------------------------------------------------------


def test_check_leaves_nothing_behind_but_the_report(tmp_path, monkeypatch):
    """The invariant that makes this safe to run against a live deployment.

    Everything is parsed in a temp directory, so no events.db and no drain3.bin
    may appear in the working directory.
    """
    monkeypatch.chdir(tmp_path)
    check(demo_fetcher(), window=ALIGNED)

    assert [path.name for path in tmp_path.iterdir()] == ["check-report.md"]


def test_out_none_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = check(demo_fetcher(), window=ALIGNED, out=None)

    assert report.report_path is None
    assert list(tmp_path.iterdir()) == []


def test_report_path_is_recorded(tmp_path):
    out = tmp_path / "custom.md"
    report = check(demo_fetcher(), window=ALIGNED, out=out)

    assert report.report_path == out
    assert out.read_text(encoding="utf-8").startswith("# check report")


def test_report_contains_every_event(tmp_path):
    """Not a sample: the events table is the evidence for judging a config, so
    truncating it would quietly hide the rows that motivated the check."""
    out = tmp_path / "r.md"
    report = check(demo_fetcher(), window=ALIGNED, out=out)
    text = out.read_text(encoding="utf-8")

    assert f"## Events ({ALIGNED_RECORDS})" in text
    for row in report.rows:
        assert str(row["ts"]) in text


def test_pipes_in_source_key_stay_inside_their_cell():
    """source_key is conventionally "cluster|host", so an unescaped pipe would
    end the cell early and shift every remaining column of the table."""

    def build(_config):
        return lambda _t1, _t2: [
            {"message": "m", "ts": 1000, "source_key": "clientA|server1", "extra": {}}
        ]

    text = check(fetcher(build), window=(900, 1100), out=None).to_markdown()

    row = next(line for line in text.splitlines() if "clientA" in line and line.startswith("|"))
    assert r"clientA\|server1" in row
    assert row.count("|") - row.count(r"\|") == 6  # five columns


def test_report_records_failures(tmp_path):
    """A partial report is exactly what is wanted when debugging, so the file
    is written even when a step failed."""
    out = tmp_path / "r.md"

    def build(_config):
        raise RuntimeError("boom")

    check(fetcher(build), window=ALIGNED, out=out)
    text = out.read_text(encoding="utf-8")

    assert "**FAILED**" in text
    assert "boom" in text


def test_markdown_extra_is_valid_json():
    """The extra column is meant to be readable and machine-parseable both."""

    def build(_config):
        return lambda _t1, _t2: [
            {"message": "m", "ts": 1000, "source_key": "a", "extra": {"level": "INFO", "n": 3}}
        ]

    text = check(fetcher(build), window=(900, 1100), out=None).to_markdown()

    cell = next(line for line in text.splitlines() if '"level"' in line).split("`")[-2]
    assert json.loads(cell) == {"level": "INFO", "n": 3}

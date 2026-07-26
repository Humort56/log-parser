"""Drain3 mining config: the fingerprint, and the warning when it changes.

These settings decide which messages collapse into which template, and the
resulting ``template_id`` is what SQLite stores. Reopening a snapshot under
changed settings therefore silently splits the store's id vocabulary in two --
old rows keep old ids, new rows get new ones, and ``fetched_ranges`` still
marks those windows covered so a re-query will not re-derive them.

Nothing raises (a caller may have changed the config deliberately), so a
warning is the entire safety net. That makes these tests the only thing standing
between a drain3 upgrade and the guard quietly ceasing to work.
"""

import logging

from drain3.template_miner_config import TemplateMinerConfig

from log_parser import LogParser, TemplateModel, config_fingerprint


def config(**overrides):
    """A TemplateMinerConfig with ``overrides`` applied."""
    cfg = TemplateMinerConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def save_snapshot(tmp_path, cfg, message="User 12 logged in from 10.0.0.1"):
    """Mine one message under ``cfg`` and persist, writing the sidecar."""
    model = TemplateModel(str(tmp_path / "drain3.bin"), cfg)
    model.template_id(message)
    model.save()
    return model


# --------------------------------------------------------------------------
# The fingerprint itself
# --------------------------------------------------------------------------


def test_identical_configs_fingerprint_the_same():
    assert config_fingerprint(config()) == config_fingerprint(config())


def test_every_mining_setting_changes_the_fingerprint():
    """The guard is only as good as this list.

    A drain3 upgrade that renames one of these would make ``getattr(...,
    None)`` return the same default for every config, collapsing distinct
    settings onto one digest and disabling the warning with no other symptom.
    Each case here is a mining setting whose change *must* be detectable.
    """
    baseline = config_fingerprint(config())
    changed = {
        "drain_sim_th": 0.9,
        "drain_depth": 6,
        "drain_max_children": 50,
        "drain_max_clusters": 500,
        "drain_extra_delimiters": ["_"],
        "mask_prefix": "[",
        "mask_suffix": "]",
        "parametrize_numeric_tokens": False,
    }
    for key, value in changed.items():
        assert config_fingerprint(config(**{key: value})) != baseline, (
            f"changing {key} left the fingerprint unchanged -- the config guard "
            "would not fire for it"
        )


def test_non_mining_settings_do_not_change_the_fingerprint():
    """Profiling and snapshot cadence change how drain3 runs, not what it mines.

    Fingerprinting them would cry wolf: a user toggling profiling would be told
    their template ids are suspect when nothing about mining moved.
    """
    baseline = config_fingerprint(config())
    for key, value in (
        ("profiling_enabled", True),
        ("profiling_report_sec", 5),
        ("snapshot_interval_minutes", 99),
        ("snapshot_compress_state", False),
        ("parameter_extraction_cache_capacity", 10),
    ):
        assert config_fingerprint(config(**{key: value})) == baseline, (
            f"{key} does not affect mining but changed the fingerprint"
        )


def test_fingerprint_is_a_short_stable_string():
    """Stored verbatim in a sidecar file, so it must stay filesystem-friendly."""
    digest = config_fingerprint(config())
    assert isinstance(digest, str)
    assert digest.isalnum() and len(digest) == 16


# --------------------------------------------------------------------------
# The sidecar
# --------------------------------------------------------------------------


def test_sidecar_is_written_on_save_not_on_open(tmp_path):
    """Written after the snapshot, so it never describes a tree that was not saved."""
    sidecar = tmp_path / "drain3.bin.config"
    model = TemplateModel(str(tmp_path / "drain3.bin"), config())
    model.template_id("User 12 logged in from 10.0.0.1")
    assert not sidecar.exists(), "sidecar appeared before save()"

    model.save()
    assert sidecar.read_text(encoding="utf-8") == config_fingerprint(config())


def test_missing_sidecar_is_silent(tmp_path, caplog):
    """A snapshot written before configs were tracked must not warn.

    The sidecar is deleted rather than skipped so the snapshot itself stays a
    real one -- the case being covered is a *valid* tree with no fingerprint
    beside it, which is exactly what upgrading to this version looks like.
    """
    save_snapshot(tmp_path, config())
    (tmp_path / "drain3.bin.config").unlink()

    with caplog.at_level(logging.WARNING, logger="log_parser.core"):
        TemplateModel(str(tmp_path / "drain3.bin"), config(drain_sim_th=0.9))
    assert caplog.text == ""


def test_unreadable_sidecar_does_not_crash(tmp_path, caplog):
    """A directory where the sidecar should be: log it, keep going."""
    (tmp_path / "drain3.bin.config").mkdir()
    with caplog.at_level(logging.WARNING, logger="log_parser.core"):
        TemplateModel(str(tmp_path / "drain3.bin"), config())
    assert "skipping the config check" in caplog.text


# --------------------------------------------------------------------------
# The warning
# --------------------------------------------------------------------------


def test_reopening_with_the_same_config_is_silent(tmp_path, caplog):
    cfg = config(drain_sim_th=0.5)
    save_snapshot(tmp_path, cfg)

    with caplog.at_level(logging.WARNING, logger="log_parser.core"):
        TemplateModel(str(tmp_path / "drain3.bin"), cfg)
    assert caplog.text == ""


def test_reopening_with_a_changed_config_warns(tmp_path, caplog):
    save_snapshot(tmp_path, config(drain_sim_th=0.4))

    with caplog.at_level(logging.WARNING, logger="log_parser.core"):
        TemplateModel(str(tmp_path / "drain3.bin"), config(drain_sim_th=0.9))

    assert "config changed" in caplog.text
    # Both digests are named, so the change is identifiable after the fact.
    assert config_fingerprint(config(drain_sim_th=0.4)) in caplog.text
    assert config_fingerprint(config(drain_sim_th=0.9)) in caplog.text


def test_a_changed_config_warns_but_does_not_raise(tmp_path, caplog):
    """Changing the config may be deliberate -- reparsing is the caller's call."""
    save_snapshot(tmp_path, config(drain_sim_th=0.4))

    with caplog.at_level(logging.WARNING, logger="log_parser.core"):
        model = TemplateModel(str(tmp_path / "drain3.bin"), config(drain_sim_th=0.9))
    assert model.template_id("Disk sda full at 91%") > 0


def test_toggling_a_non_mining_setting_does_not_warn(tmp_path, caplog):
    """The exclusion list, end to end: no false alarm from profiling."""
    save_snapshot(tmp_path, config(drain_sim_th=0.5))

    with caplog.at_level(logging.WARNING, logger="log_parser.core"):
        TemplateModel(
            str(tmp_path / "drain3.bin"), config(drain_sim_th=0.5, profiling_enabled=True)
        )
    assert caplog.text == ""


# --------------------------------------------------------------------------
# Plumbing: the config reaches drain3 through LogParser
# --------------------------------------------------------------------------


MESSAGES = (
    "User 1 logged in from 10.0.0.5",
    "User 2 logged in from 10.0.0.9",
    "Disk sda full at 91%",
)


def parse_with(tmp_path, cfg, name):
    """Ingest MESSAGES under ``cfg`` into their own store; return the templates."""

    def fetch_fn(t1, _t2):
        # The whole fixture fits in one gap, so the end bound goes unused.
        return [
            {"message": m, "ts": t1 + i, "source_key": "h1", "extra": {}}
            for i, m in enumerate(MESSAGES)
        ]

    with LogParser(
        fetch_fn=fetch_fn,
        db_path=str(tmp_path / f"{name}.db"),
        state_path=str(tmp_path / f"{name}.bin"),
        config=cfg,
    ) as parser:
        parser.query(1000, 1100)
        return parser.model.clusters()


def test_sim_th_reaches_drain3_through_log_parser(tmp_path):
    """A loose threshold generalises the two login lines; a strict one does not.

    This is what proves the config is actually plumbed rather than accepted and
    dropped: the same three messages mine into a different number of templates.
    """
    loose = parse_with(tmp_path, config(drain_sim_th=0.2), "loose")
    strict = parse_with(tmp_path, config(drain_sim_th=0.9), "strict")

    assert len(loose) < len(strict)
    assert any("<*>" in text for text, _ in loose.values())
    # Strict keeps them apart, so both logins survive as their own templates.
    assert len(strict) == len(MESSAGES)


def test_log_parser_without_a_config_still_works(tmp_path):
    """Omitting config= gets drain3's defaults, never drain3.ini from the cwd."""
    templates = parse_with(tmp_path, None, "default")
    assert templates


def test_changing_the_config_warns_through_log_parser(tmp_path, caplog):
    """The warning must reach someone using the public entry point.

    The TemplateModel tests above cover the check itself; this covers the path a
    caller actually takes. A LogParser that accepted ``config`` but stopped
    handing it to TemplateModel would still mine correctly on a fresh store and
    would only be caught here.
    """
    parse_with(tmp_path, config(drain_sim_th=0.4), "shared")

    with caplog.at_level(logging.WARNING, logger="log_parser.core"):
        parse_with(tmp_path, config(drain_sim_th=0.9), "shared")

    assert "config changed" in caplog.text


def test_reusing_the_same_config_through_log_parser_is_silent(tmp_path, caplog):
    """The negative half: reopening a store as-is must not cry wolf."""
    cfg = config(drain_sim_th=0.4)
    parse_with(tmp_path, cfg, "steady")

    with caplog.at_level(logging.WARNING, logger="log_parser.core"):
        parse_with(tmp_path, cfg, "steady")

    assert "config changed" not in caplog.text

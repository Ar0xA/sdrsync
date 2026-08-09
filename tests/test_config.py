"""AppSettings.load()/save() robustness against a hand-edited or
partially-written config.json -- see project_brief.md's v6 hardening
round for why this needed hardening (unguarded dict access downstream in
gui/app.py, non-atomic save())."""
import json

import sdrsync.config as config_module
from sdrsync.config import AppSettings


def _use_tmp_config(monkeypatch, tmp_path):
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)
    return config_file


def test_load_defaults_when_no_file(monkeypatch, tmp_path):
    _use_tmp_config(monkeypatch, tmp_path)
    settings = AppSettings.load()
    assert settings.rigctld_port == 4532
    assert settings.user_sites == []


def test_load_skips_malformed_user_sites_entries(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({
        "user_sites": [
            {"name": "Good", "url": "http://good.example/", "driver_type": "kiwisdr"},
            {"name": "Missing URL", "driver_type": "kiwisdr"},
            "not even a dict",
            {"name": "", "url": "http://empty-name.example/", "driver_type": "kiwisdr"},
            None,
        ]
    }), encoding="utf-8")

    settings = AppSettings.load()

    assert settings.user_sites == [{"name": "Good", "url": "http://good.example/", "driver_type": "kiwisdr"}]


def test_load_ignores_invalid_user_sites_type(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"user_sites": "not a list"}), encoding="utf-8")

    settings = AppSettings.load()

    assert settings.user_sites == []


def test_load_ignores_wrong_type_scalar_and_falls_back_to_default(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"rigctld_port": "4532", "headless": "yes"}), encoding="utf-8")

    settings = AppSettings.load()

    assert settings.rigctld_port == 4532  # int default, not the string
    assert settings.headless is False  # bool default, not the string


def test_load_accepts_poll_interval_as_int_or_float(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"poll_interval_s": 1}), encoding="utf-8")

    assert AppSettings.load().poll_interval_s == 1

    config_file.write_text(json.dumps({"poll_interval_s": "0.1"}), encoding="utf-8")

    assert AppSettings.load().poll_interval_s == 0.2  # falls back to default, not the string


def test_load_clamps_out_of_range_poll_interval(monkeypatch, tmp_path):
    """A hand-edited 0 or negative poll_interval_s must not reach
    asyncio.wait_for(timeout=...) in sync/engine.py's poll loop -- that
    would busy-loop hammering rigctld."""
    config_file = _use_tmp_config(monkeypatch, tmp_path)

    config_file.write_text(json.dumps({"poll_interval_s": -5}), encoding="utf-8")
    assert AppSettings.load().poll_interval_s == config_module.MIN_POLL_INTERVAL_S

    config_file.write_text(json.dumps({"poll_interval_s": 0}), encoding="utf-8")
    assert AppSettings.load().poll_interval_s == config_module.MIN_POLL_INTERVAL_S

    config_file.write_text(json.dumps({"poll_interval_s": 9999}), encoding="utf-8")
    assert AppSettings.load().poll_interval_s == config_module.MAX_POLL_INTERVAL_S


def test_load_accepts_valid_reverse_sync_range(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(
        json.dumps({"reverse_sync_min_hz": 1_800_000, "reverse_sync_max_hz": 30_000_000}), encoding="utf-8",
    )
    settings = AppSettings.load()
    assert settings.reverse_sync_min_hz == 1_800_000
    assert settings.reverse_sync_max_hz == 30_000_000


def test_load_defaults_reverse_sync_range_to_unrestricted(monkeypatch, tmp_path):
    _use_tmp_config(monkeypatch, tmp_path)
    settings = AppSettings.load()
    assert settings.reverse_sync_min_hz is None
    assert settings.reverse_sync_max_hz is None


def test_load_accepts_reverse_sync_range_with_only_one_bound_set(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"reverse_sync_min_hz": 100_000}), encoding="utf-8")
    settings = AppSettings.load()
    assert settings.reverse_sync_min_hz == 100_000
    assert settings.reverse_sync_max_hz is None


def test_load_rejects_wrong_type_reverse_sync_range(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(
        json.dumps({"reverse_sync_min_hz": "not a number", "reverse_sync_max_hz": True}), encoding="utf-8",
    )
    settings = AppSettings.load()
    assert settings.reverse_sync_min_hz is None
    assert settings.reverse_sync_max_hz is None


def test_load_clamps_negative_reverse_sync_bounds_to_unrestricted(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"reverse_sync_min_hz": -100}), encoding="utf-8")
    assert AppSettings.load().reverse_sync_min_hz is None

    config_file.write_text(json.dumps({"reverse_sync_max_hz": -1}), encoding="utf-8")
    assert AppSettings.load().reverse_sync_max_hz is None


def test_load_swaps_inverted_reverse_sync_range():
    """An inverted range (min > max) is almost always a transposition of
    the intended bounds. It must be SWAPPED, not reset to unrestricted:
    unlike poll_interval_s (where the safe correction is the permissive
    one), LOOSENING is the unsafe direction for this guard -- it bounds
    what a public WebSDR page may retune a real transmitter to, so
    dropping both bounds would silently leave no guard at all while the
    user still believes reverse sync is confined to, say, HF."""
    filtered = {"reverse_sync_min_hz": 30_000_000, "reverse_sync_max_hz": 1_800_000}
    config_module._clamp_reverse_sync_range(filtered)
    assert filtered["reverse_sync_min_hz"] == 1_800_000
    assert filtered["reverse_sync_max_hz"] == 30_000_000


def test_load_swaps_inverted_reverse_sync_range_end_to_end(monkeypatch, tmp_path):
    """The swap must survive the full load() path, not just the helper --
    a hand-edited config.json with min/max transposed still ends up with
    an enforced (corrected) range on the AppSettings instance."""
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(
        json.dumps({"reverse_sync_min_hz": 30_000_000, "reverse_sync_max_hz": 1_800_000}), encoding="utf-8",
    )
    settings = AppSettings.load()
    assert settings.reverse_sync_min_hz == 1_800_000
    assert settings.reverse_sync_max_hz == 30_000_000


def test_clamp_reverse_sync_bounds_passes_through_a_valid_range():
    """The shared helper (used by both config.load() and gui/app.py's
    range fields) must leave a sane range completely alone, including the
    one-bound-only and both-unset cases."""
    assert config_module.clamp_reverse_sync_bounds(1_800_000, 30_000_000) == (1_800_000, 30_000_000)
    assert config_module.clamp_reverse_sync_bounds(1_800_000, None) == (1_800_000, None)
    assert config_module.clamp_reverse_sync_bounds(None, 30_000_000) == (None, 30_000_000)
    assert config_module.clamp_reverse_sync_bounds(None, None) == (None, None)


def test_clamp_reverse_sync_bounds_allows_an_equal_min_and_max():
    """min == max is a degenerate but legitimate single-frequency lock,
    not an inverted range -- must pass through untouched (engine.py's own
    check is a strict inequality, so that exact value is still allowed)."""
    assert config_module.clamp_reverse_sync_bounds(14_074_000, 14_074_000) == (14_074_000, 14_074_000)


def test_clamp_reverse_sync_bounds_drops_only_the_negative_bound():
    """A negative bound can't be satisfied by any real frequency, so it
    goes to None (unrestricted on that side only) -- the OTHER, valid
    bound must survive rather than being collateral damage."""
    assert config_module.clamp_reverse_sync_bounds(-100, 30_000_000) == (None, 30_000_000)
    assert config_module.clamp_reverse_sync_bounds(1_800_000, -1) == (1_800_000, None)
    assert config_module.clamp_reverse_sync_bounds(-5, -9) == (None, None)


def test_clamp_reverse_sync_bounds_does_not_swap_when_a_bound_was_dropped():
    """A negative min is dropped BEFORE the inversion check, so the pair
    can't then be 'swapped' into resurrecting the discarded value."""
    assert config_module.clamp_reverse_sync_bounds(-30_000_000, 1_800_000) == (None, 1_800_000)


def test_load_does_not_crash_on_non_object_top_level_json(monkeypatch, tmp_path):
    """config.json containing valid JSON that isn't an object (a list,
    string, number, or null) must fall back to defaults, not raise --
    data.items() on a non-dict previously escaped the except clause."""
    config_file = _use_tmp_config(monkeypatch, tmp_path)

    for bad_top_level in ["[]", '"just a string"', "5", "null"]:
        config_file.write_text(bad_top_level, encoding="utf-8")
        settings = AppSettings.load()  # must not raise
        assert settings.rigctld_port == 4532


def test_load_does_not_crash_on_garbage_json(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text("{not valid json", encoding="utf-8")

    settings = AppSettings.load()  # must not raise

    assert settings.rigctld_port == 4532


def test_save_then_load_round_trips_valid_data(monkeypatch, tmp_path):
    _use_tmp_config(monkeypatch, tmp_path)
    original = AppSettings(rigctld_port=4534, user_sites=[
        {"name": "A", "url": "http://a.example/", "driver_type": "websdr_org"},
    ])

    original.save()
    reloaded = AppSettings.load()

    assert reloaded.rigctld_port == 4534
    assert reloaded.user_sites == [{"name": "A", "url": "http://a.example/", "driver_type": "websdr_org"}]


def test_save_does_not_leave_a_tmp_file_behind(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    AppSettings().save()

    assert config_file.exists()
    assert not (tmp_path / "config.json.tmp").exists()


def test_load_accepts_valid_rig_backend(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"rig_backend": "flrig"}), encoding="utf-8")

    assert AppSettings.load().rig_backend == "flrig"


def test_load_rejects_invalid_rig_backend(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"rig_backend": "omnirig"}), encoding="utf-8")

    assert AppSettings.load().rig_backend == "rigctld"  # falls back to default


def test_load_ignores_wrong_type_flrig_fields(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"flrig_host": 12345, "flrig_port": "12345"}), encoding="utf-8")

    settings = AppSettings.load()

    assert settings.flrig_host == "127.0.0.1"
    assert settings.flrig_port == 12345


def test_save_then_load_round_trips_flrig_settings(monkeypatch, tmp_path):
    _use_tmp_config(monkeypatch, tmp_path)
    original = AppSettings(rig_backend="flrig", flrig_host="192.168.1.50", flrig_port=12346)

    original.save()
    reloaded = AppSettings.load()

    assert reloaded.rig_backend == "flrig"
    assert reloaded.flrig_host == "192.168.1.50"
    assert reloaded.flrig_port == 12346


def test_load_defaults_imported_and_curated_sites_to_empty_list(monkeypatch, tmp_path):
    _use_tmp_config(monkeypatch, tmp_path)
    settings = AppSettings.load()
    assert settings.imported_sites == []
    assert settings.curated_sites == []


def test_load_skips_malformed_imported_sites_entries(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({
        "imported_sites": [
            {"name": "Good", "url": "http://good.example/", "driver_type": "kiwisdr"},
            {"name": "Missing URL", "driver_type": "kiwisdr"},
            "not even a dict",
            {"name": "", "url": "http://empty-name.example/", "driver_type": "kiwisdr"},
            None,
        ]
    }), encoding="utf-8")

    settings = AppSettings.load()

    assert settings.imported_sites == [{"name": "Good", "url": "http://good.example/", "driver_type": "kiwisdr"}]


def test_load_skips_malformed_curated_sites_entries(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({
        "curated_sites": [
            {"name": "Good", "url": "http://good.example/", "driver_type": "websdr_org"},
            {"name": "Bad", "url": "", "driver_type": "websdr_org"},
        ]
    }), encoding="utf-8")

    settings = AppSettings.load()

    assert settings.curated_sites == [{"name": "Good", "url": "http://good.example/", "driver_type": "websdr_org"}]


def test_load_ignores_invalid_imported_and_curated_sites_type(monkeypatch, tmp_path):
    config_file = _use_tmp_config(monkeypatch, tmp_path)
    config_file.write_text(json.dumps({"imported_sites": "not a list", "curated_sites": 5}), encoding="utf-8")

    settings = AppSettings.load()

    assert settings.imported_sites == []
    assert settings.curated_sites == []


def test_save_then_load_round_trips_imported_and_curated_sites(monkeypatch, tmp_path):
    _use_tmp_config(monkeypatch, tmp_path)
    original = AppSettings(
        imported_sites=[{"name": "I", "url": "http://i.example/", "driver_type": "kiwisdr"}],
        curated_sites=[{"name": "C", "url": "http://c.example/", "driver_type": "openwebrx"}],
    )

    original.save()
    reloaded = AppSettings.load()

    assert reloaded.imported_sites == [{"name": "I", "url": "http://i.example/", "driver_type": "kiwisdr"}]
    assert reloaded.curated_sites == [{"name": "C", "url": "http://c.example/", "driver_type": "openwebrx"}]

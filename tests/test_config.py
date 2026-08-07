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

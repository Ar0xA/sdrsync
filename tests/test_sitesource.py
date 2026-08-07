"""Pure JSON-parsing/validation tests for sitesource.py -- the strict
external-data validator (driver_type-registered + name/URL-collision
rejection) lives here, not test_config.py, per the v8 plan's validation-
location fix (see project_brief.md)."""
import asyncio
import json

import pytest

from sdrsync.config import WebSDRSite
from sdrsync.sitesource import load_site_list_from_file, validate_site_list

EXISTING = [WebSDRSite(name="Known", url="http://known.example/", driver_type="kiwisdr")]


def test_validate_site_list_accepts_valid_entries():
    raw = [{"name": "A", "url": "http://a.example/", "driver_type": "websdr_org"}]
    assert validate_site_list(raw, EXISTING) == raw


def test_validate_site_list_rejects_unregistered_driver_type():
    raw = [{"name": "A", "url": "http://a.example/", "driver_type": "not_a_real_driver"}]
    assert validate_site_list(raw, EXISTING) == []


def test_validate_site_list_rejects_malformed_shape():
    raw = [
        {"name": "Missing URL", "driver_type": "kiwisdr"},
        "not even a dict",
        {"name": "", "url": "http://empty-name.example/", "driver_type": "kiwisdr"},
        None,
    ]
    assert validate_site_list(raw, EXISTING) == []


def test_validate_site_list_rejects_name_collision_with_existing():
    raw = [{"name": "Known", "url": "http://different.example/", "driver_type": "kiwisdr"}]
    assert validate_site_list(raw, EXISTING) == []


def test_validate_site_list_rejects_url_collision_with_existing():
    raw = [{"name": "Different name", "url": "http://known.example/", "driver_type": "kiwisdr"}]
    assert validate_site_list(raw, EXISTING) == []


def test_validate_site_list_rejects_collision_within_same_batch():
    raw = [
        {"name": "A", "url": "http://a.example/", "driver_type": "kiwisdr"},
        {"name": "A", "url": "http://a-different.example/", "driver_type": "kiwisdr"},
        {"name": "B", "url": "http://a.example/", "driver_type": "kiwisdr"},
    ]
    assert validate_site_list(raw, EXISTING) == [{"name": "A", "url": "http://a.example/", "driver_type": "kiwisdr"}]


def test_validate_site_list_handles_non_list_top_level():
    assert validate_site_list({"not": "a list"}, EXISTING) == []
    assert validate_site_list("also not a list", EXISTING) == []
    assert validate_site_list(None, EXISTING) == []


def test_load_site_list_from_file_success(tmp_path):
    f = tmp_path / "sites.json"
    f.write_text(json.dumps([{"name": "A", "url": "http://a.example/", "driver_type": "openwebrx"}]), encoding="utf-8")

    sites, message = load_site_list_from_file(str(f), EXISTING)

    assert sites == [{"name": "A", "url": "http://a.example/", "driver_type": "openwebrx"}]
    assert "1" in message


def test_load_site_list_from_file_missing_file(tmp_path):
    sites, message = load_site_list_from_file(str(tmp_path / "does_not_exist.json"), EXISTING)
    assert sites is None
    assert message


def test_load_site_list_from_file_malformed_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not valid json", encoding="utf-8")

    sites, message = load_site_list_from_file(str(f), EXISTING)

    assert sites is None
    assert message


def test_load_site_list_from_file_empty_list_is_a_valid_empty_result(tmp_path):
    """An empty [] is a deliberately-emptied list, not a failure -- must
    return ([], message), not (None, message), so a caller can tell "the
    maintainer cleared this list" apart from "the fetch/parse failed" and
    actually clear the bucket rather than leaving stale entries behind."""
    f = tmp_path / "empty.json"
    f.write_text("[]", encoding="utf-8")

    sites, message = load_site_list_from_file(str(f), EXISTING)

    assert sites == []
    assert "empty" in message


def test_load_site_list_from_file_all_entries_rejected(tmp_path):
    f = tmp_path / "all_bad.json"
    f.write_text(json.dumps([{"name": "Bad", "url": "", "driver_type": "kiwisdr"}]), encoding="utf-8")

    sites, message = load_site_list_from_file(str(f), EXISTING)

    assert sites is None
    assert "No valid sites" in message


def test_load_site_list_from_file_non_list_top_level(tmp_path):
    f = tmp_path / "not_a_list.json"
    f.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    sites, message = load_site_list_from_file(str(f), EXISTING)

    assert sites is None
    assert "did not contain a JSON list" in message


def test_fetch_site_list_rejects_non_json_body(monkeypatch):
    import sdrsync.sitesource as sitesource_module

    async def fake_run_in_executor(_none, fn, *args):
        return "not json at all", ""

    class FakeLoop:
        run_in_executor = staticmethod(fake_run_in_executor)

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop())

    async def run():
        return await sitesource_module.fetch_site_list("http://example.invalid/sites.json", EXISTING)

    sites, message = asyncio.run(run())
    assert sites is None
    assert "valid JSON" in message


def test_fetch_site_list_propagates_fetch_failure(monkeypatch):
    import sdrsync.sitesource as sitesource_module

    async def fake_run_in_executor(_none, fn, *args):
        return None, "Could not reach http://example.invalid/ (offline)"

    class FakeLoop:
        run_in_executor = staticmethod(fake_run_in_executor)

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop())

    async def run():
        return await sitesource_module.fetch_site_list("http://example.invalid/sites.json", EXISTING)

    sites, message = asyncio.run(run())
    assert sites is None
    assert message == "Could not reach http://example.invalid/ (offline)"

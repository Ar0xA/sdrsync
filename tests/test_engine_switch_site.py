"""The rig and WebSDR subsystems have independent lifecycles: picking a
different WebSDR must never touch the rigctld connection. The reverse is
NOT symmetric (deliberately, since v9): stopping the rig also stops an
active WebSDR session, since WebSDR has nothing driving its sync without
a rig (it can't even be started without one -- see gui/app.py's Connect
gating). Uses stub objects instead of a real browser/socket, same pattern
as test_engine_mode_independence.py.

StubWebViewHost stands in for gui/webview_host.py's WebViewHost --
satisfies SyncEngine's WebViewHost Protocol (create_page/destroy_page)
without touching wx at all, so these tests stay fast and headless.
"""
import asyncio
import queue

import sdrsync.sync.engine as engine_module
from sdrsync.config import AppSettings, WebSDRSite
from sdrsync.sync.engine import SyncEngine
from sdrsync.websdr.base import WebSDRStatus


class StubDriver:
    def __init__(self, url: str, cw_offset_hz: int = 0) -> None:
        self.url = url
        self.attached = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def tune_hz(self, freq_hz: int) -> bool:
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        return True

    async def set_muted(self, muted: bool) -> None:
        pass

    async def get_status(self) -> WebSDRStatus:
        return WebSDRStatus(connected=True)


class StubRig:
    """Just enough of RigctldClient's interface to prove close() was (or
    wasn't) called."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class StubPage:
    """Sentinel page -- nothing in these tests touches its content, only
    identity (via StubWebViewHost's created/destroyed lists)."""


class StubWebViewHost:
    """Satisfies SyncEngine's WebViewHost Protocol. Tracks created/
    destroyed pages so tests can assert on WebView lifecycle without a
    real wx.App/WebView existing."""

    def __init__(self) -> None:
        self.created: list[StubPage] = []
        self.destroyed: list[StubPage] = []

    async def create_page(self, loop, on_dead=None):
        page = StubPage()
        self.created.append(page)
        return page

    async def destroy_page(self, page) -> None:
        self.destroyed.append(page)


async def _noop_attach_supervisor(page) -> None:
    """Stands in for the real attach-retry loop -- returns immediately so
    the test doesn't need a real attach cycle."""
    return


def make_engine() -> SyncEngine:
    settings = AppSettings()
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=StubWebViewHost())
    engine._attach_supervisor = _noop_attach_supervisor
    return engine


def test_start_websdr_does_not_touch_inactive_rig(monkeypatch):
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    asyncio.run(engine._start_websdr(site))

    assert engine._websdr_active is True
    assert engine._rig_active is False
    assert engine._rig is None


def test_switching_site_swaps_driver_and_resets_latches_without_touching_rig(monkeypatch):
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    monkeypatch.setitem(engine_module.DRIVERS, "kiwisdr", StubDriver)

    engine = make_engine()
    site_a = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    site_b = WebSDRSite(name="B", url="http://b.invalid/", driver_type="kiwisdr")

    async def run():
        await engine._start_websdr(site_a)
        original_driver = engine._driver
        assert engine.site is site_a
        original_rig = engine._rig  # None -- rig was never started

        engine._last_sent_freq = 14074000
        engine._last_sent_mode_key = ("USB", 2700)
        engine._last_ptt = False

        await engine._start_websdr(site_b)  # the "switch" -- same call as connect

        assert original_driver.closed is True
        assert engine.site is site_b
        assert engine._driver is not original_driver
        assert engine._driver.url == "http://b.invalid/"
        assert engine._last_sent_freq is None
        assert engine._last_sent_mode_key is None
        assert engine._last_ptt is None
        assert engine._rig is original_rig  # still untouched
        # Switching sites must tear down the first WebView, not leak it.
        assert len(engine._webview_host.created) == 2
        assert len(engine._webview_host.destroyed) == 1

    asyncio.run(run())


def test_stop_websdr_does_not_touch_rig(monkeypatch):
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    engine._rig_active = True  # pretend the rig subsystem is up
    rig_stub = StubRig()
    engine._rig = rig_stub

    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        await engine._start_websdr(site)
        assert engine._websdr_active is True
        await engine._stop_websdr()
        assert engine._websdr_active is False
        assert engine.site is None
        assert len(engine._webview_host.destroyed) == 1
        # The whole point: stopping WebSDR never touches the rig.
        assert engine._rig is rig_stub
        assert rig_stub.closed is False
        assert engine._rig_active is True

    asyncio.run(run())


def test_stop_rig_also_stops_an_active_websdr_session(monkeypatch):
    """v9: deliberate one-directional exception to the independent-
    lifecycles principle -- see the module docstring above."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        await engine._start_websdr(site)
        driver = engine._driver
        engine._rig_active = True
        rig_stub = StubRig()
        engine._rig = rig_stub

        await engine._stop_rig()

        assert engine._rig_active is False
        assert engine._rig is None
        assert rig_stub.closed is True
        assert engine._websdr_active is False
        assert engine._driver is None
        assert driver.closed is True

    asyncio.run(run())


def test_stop_rig_leaves_an_inactive_websdr_session_alone(monkeypatch):
    """The cascade only fires when WebSDR is actually active -- confirms
    _stop_rig() doesn't unconditionally call _stop_websdr() in a way that
    would raise/misbehave when there's nothing to stop."""
    engine = make_engine()

    async def run():
        engine._rig_active = True
        rig_stub = StubRig()
        engine._rig = rig_stub

        await engine._stop_rig()  # must not raise

        assert engine._rig_active is False
        assert engine._websdr_active is False

    asyncio.run(run())


def test_thread_safe_entry_points_are_noop_before_run():
    """_loop is only set once run() starts; calling any thread-safe entry
    point before that (or after a full stop) must not raise, and must not
    schedule work that's never awaited."""
    settings = AppSettings()
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=StubWebViewHost())
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    engine.start_websdr_from_other_thread(site)  # must not raise
    engine.stop_websdr_from_other_thread()
    engine.start_rig_from_other_thread("rigctld", "127.0.0.1", 4532, True)
    engine.stop_rig_from_other_thread()


def test_page_death_recreates_the_websdr_session(monkeypatch):
    """A WebView dying (script timeout with no way to cancel it -- see
    WxPageAdapter's on_dead) must trigger a fresh _start_websdr(), the
    same recovery path an explicit reconnect takes -- this was a real gap
    the Block B implementation review found (nothing previously called
    the callback at all)."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        assert len(engine._webview_host.created) == 1
        generation = engine._websdr_generation

        engine._on_page_dead(generation, "test-induced death")
        # _on_page_dead schedules _handle_page_dead via call_soon_threadsafe
        # -- let the loop process it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(engine._webview_host.created) == 2
        assert engine._websdr_active is True

    asyncio.run(run())


def test_stale_page_death_notification_is_ignored(monkeypatch):
    """A dead-notification tagged with an old generation (from a page
    that's already been replaced/torn down) must not trigger a spurious
    recreation of whatever's active now."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        stale_generation = engine._websdr_generation
        await engine._stop_websdr()

        engine._on_page_dead(stale_generation, "stale")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert engine._websdr_active is False
        assert len(engine._webview_host.created) == 1  # no spurious recreation

    asyncio.run(run())

"""The rig and WebSDR subsystems have independent lifecycles: picking a
different WebSDR must never touch the rigctld connection, and vice versa.
Uses stub objects instead of a real browser/socket, same pattern as
test_engine_mode_independence.py.
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


class StubBrowser:
    def __init__(self) -> None:
        self.closed = False
        self._connected = True

    async def new_page(self):
        return object()  # sentinel page; nothing in these tests touches it

    def is_connected(self) -> bool:
        return self._connected

    async def close(self) -> None:
        self.closed = True
        self._connected = False


class StubChromium:
    async def launch(self, **kwargs):
        return StubBrowser()


class StubPlaywright:
    """Stands in for the object async_playwright() yields -- only
    .chromium.launch() is exercised by _start_websdr()."""

    def __init__(self) -> None:
        self.chromium = StubChromium()


async def _noop_attach_supervisor(page) -> None:
    """Stands in for the real attach-retry loop -- returns immediately so
    the test doesn't need a real attach cycle."""
    return


def make_engine() -> SyncEngine:
    """An engine with a stubbed-in Playwright session, as if run() had
    already entered async_playwright() -- _start_websdr() needs this to do
    anything at all."""
    settings = AppSettings()
    engine = SyncEngine(settings, status_queue=queue.Queue())
    engine._pw = StubPlaywright()
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
        # The whole point: stopping WebSDR never touches the rig.
        assert engine._rig is rig_stub
        assert rig_stub.closed is False
        assert engine._rig_active is True

    asyncio.run(run())


def test_stop_rig_does_not_touch_websdr(monkeypatch):
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
        # The whole point: stopping the rig never touches WebSDR.
        assert engine._websdr_active is True
        assert engine._driver is driver
        assert driver.closed is False

    asyncio.run(run())


def test_thread_safe_entry_points_are_noop_before_run():
    """_loop is only set once run() starts; calling any thread-safe entry
    point before that (or after a full stop) must not raise, and must not
    schedule work that's never awaited."""
    settings = AppSettings()
    engine = SyncEngine(settings, status_queue=queue.Queue())
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    engine.start_websdr_from_other_thread(site)  # must not raise
    engine.stop_websdr_from_other_thread()
    engine.start_rig_from_other_thread("127.0.0.1", 4532, True)
    engine.stop_rig_from_other_thread()

"""Engine-level: an unsupported hamlib mode must not block frequency sync.

The driver (WebsdrOrgDriver.set_mode) is the one responsible for gracefully
skipping modes it can't map (see test_websdr_org_mode_mapping.py); this test
pins down the engine's half of the contract -- it must keep calling
set_mode/tune_hz independently every tick regardless of whether the driver
could actually apply the mode, using stub rig/driver objects instead of a
real socket or browser.
"""
import asyncio
import queue

from sdrsync.config import AppSettings
from sdrsync.rig.rigctld import RigState
from sdrsync.sync.engine import SyncEngine
from sdrsync.websdr.base import WebSDRStatus


class StubRig:
    def __init__(self, state: RigState) -> None:
        self.state = state

    async def ensure_connected(self) -> bool:
        return True

    async def get_state(self) -> RigState:
        return self.state

    def reconnect_delay(self) -> float:
        return 1.0

    async def close(self) -> None:
        pass


class StubDriver:
    """attached=True and no-op close() so it stands in for WebsdrOrgDriver in
    engine-level tests without a browser."""

    def __init__(self) -> None:
        self.attached = True
        self.tuned: list[int] = []
        self.modes: list[tuple] = []

    async def tune_hz(self, freq_hz: int) -> bool:
        self.tuned.append(freq_hz)
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        self.modes.append((hamlib_mode, passband_hz))
        return True

    async def set_muted(self, muted: bool) -> None:
        pass

    async def get_status(self) -> WebSDRStatus:
        return WebSDRStatus(connected=True)

    async def close(self) -> None:
        pass


class _UnusedWebViewHost:
    """These tests drive engine._tick() directly against stub rig/driver
    objects and never touch WebView creation/teardown -- satisfies
    SyncEngine's WebViewHost Protocol without needing a real one."""

    async def create_page(self, loop, on_dead=None):
        raise AssertionError("not expected to be called in these tests")

    async def destroy_page(self, page) -> None:
        raise AssertionError("not expected to be called in these tests")


def make_engine() -> SyncEngine:
    settings = AppSettings()
    return SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())


def test_unsupported_mode_does_not_block_frequency_sync():
    engine = make_engine()
    stub_rig = StubRig(RigState(freq_hz=14074000, mode="DSTAR", passband_hz=2700, ptt=False))
    stub_driver = StubDriver()
    engine._rig = stub_rig
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    async def run_two_ticks():
        await engine._tick()
        # Backdate so the second tick clears the frequency debounce window
        # without an actual sleep.
        engine._pending_freq_since -= 1.0
        await engine._tick()

    asyncio.run(run_two_ticks())

    # The engine calls set_mode every tick the (mode, passband) changes,
    # regardless of whether the driver could actually map "DSTAR" -- that's
    # the driver's job to skip gracefully, not the engine's to pre-filter.
    assert stub_driver.modes == [("DSTAR", 2700)]
    # Frequency sync is completely independent and must still happen.
    assert stub_driver.tuned == [14074000]


class FailingDriver(StubDriver):
    """Returns False (no-op / failed) from tune_hz and set_mode -- e.g. what
    a real driver does while not yet attached, or after a failed page call."""

    async def tune_hz(self, freq_hz: int) -> bool:
        self.tuned.append(freq_hz)
        return False

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        self.modes.append((hamlib_mode, passband_hz))
        return False


def test_failed_push_is_retried_not_latched_as_sent():
    """A driver call that returns False must NOT be recorded in
    _last_sent_freq/_last_sent_mode_key -- otherwise a no-op during an
    outage would be mistaken for "already delivered" and never retried
    once the WebSDR recovers."""
    engine = make_engine()
    stub_rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    failing_driver = FailingDriver()
    engine._rig = stub_rig
    engine._rig_active = True
    engine._driver = failing_driver
    engine._websdr_active = True

    async def run_three_ticks():
        await engine._tick()
        engine._pending_freq_since -= 1.0
        await engine._tick()
        engine._pending_freq_since -= 1.0
        await engine._tick()

    asyncio.run(run_three_ticks())

    assert engine._last_sent_freq is None
    assert engine._last_sent_mode_key is None
    # Every tick after the debounce window elapses retries the failed push.
    assert failing_driver.tuned.count(14074000) >= 2
    assert failing_driver.modes.count(("USB", 2700)) >= 2

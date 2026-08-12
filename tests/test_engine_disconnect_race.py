"""A concurrent Disconnect (_stop_websdr(), reachable via a user click, the
rig-stop cascade, or _handle_page_dead()) can run on the same event loop
while _tick() is suspended inside an earlier await in the SAME tick (e.g.
the PTT-edge mute call), nulling engine._driver out from under it. Every
subsequent bare self._driver.<method>() access in that tick must not
raise AttributeError -- it must be treated as "nothing left to do this
tick", the same way the code already treats "not attached"."""
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

    async def close(self) -> None:
        pass

    async def set_freq(self, freq_hz: int) -> bool:
        return True

    async def set_mode(self, mode_name: str) -> bool:
        return True


class DisconnectingDriver:
    """Its set_muted() call simulates a concurrent _stop_websdr() completing
    while THIS driver call was in flight -- by the time it returns,
    engine._driver is already None, exactly as a real teardown running
    during a suspended await on the same loop would leave it. get_status()
    would raise AttributeError if called on self.engine._driver afterward
    without a None-check."""

    def __init__(self, engine: SyncEngine) -> None:
        self.attached = True
        self.engine = engine
        self.mute_calls: list[bool] = []
        self.get_status_calls = 0

    async def set_muted(self, muted: bool) -> None:
        self.mute_calls.append(muted)
        self.engine._driver = None
        self.engine._websdr_active = False

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        raise AssertionError("must not be called: transmitting suppresses the forward push branch")

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        raise AssertionError("must not be called: transmitting suppresses the forward push branch")

    async def get_status(self) -> WebSDRStatus:
        self.get_status_calls += 1
        return WebSDRStatus(connected=True)

    def hamlib_mode_from_status(self, status: WebSDRStatus):
        return None

    def rig_freq_from_status(self, status: WebSDRStatus):
        return None

    async def close(self) -> None:
        pass


class _UnusedWebViewHost:
    async def create_page(self, loop, on_dead=None):
        raise AssertionError("not expected to be called in these tests")

    async def destroy_page(self, page, loop=None) -> None:
        raise AssertionError("not expected to be called in these tests")


def test_driver_going_none_mid_tick_during_ptt_mute_does_not_crash():
    settings = AppSettings(mute_on_tx=True)
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True))
    engine._rig_active = True
    driver = DisconnectingDriver(engine)
    engine._driver = driver
    engine._websdr_active = True

    # PTT rising edge -> set_muted(True) fires, which (simulating a
    # concurrent Disconnect) nulls engine._driver mid-tick. The rest of
    # this tick (transmitting -> skip forward push -> get_status()) must
    # complete cleanly rather than raising.
    asyncio.run(engine._tick())

    assert driver.mute_calls == [True]
    # get_status() is legitimately called once at the very top of _tick()
    # (before the mute call/race), while the driver was still real -- the
    # SECOND, later call this tick (the one this fix guards) must have been
    # skipped rather than crashing or firing again against the gone driver.
    assert driver.get_status_calls == 1
    assert engine._driver is None
    assert engine._websdr_active is False


def test_driver_going_none_mid_tick_during_mode_push_does_not_crash():
    """Same race, but the driver goes None inside the mode-push branch
    (not-transmitting path) instead of the mute path -- covers the other
    call site fixed alongside get_status()."""
    settings = AppSettings(mute_on_tx=True)
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True

    class DisconnectingModeDriver(DisconnectingDriver):
        async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
            self.engine._driver = None
            self.engine._websdr_active = False
            return False

    driver = DisconnectingModeDriver(engine)
    engine._driver = driver
    engine._websdr_active = True

    asyncio.run(engine._tick())

    # Same reasoning as the mute-path test: called once at the top of
    # _tick() before the mode-push race, not a second time afterward.
    assert driver.get_status_calls == 1
    assert engine._driver is None

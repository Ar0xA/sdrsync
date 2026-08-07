"""Engine-level: toggling mute_on_tx off while still muted from an earlier
PTT transition must actually unmute, not leave the WebSDR muted forever.

Found during the v8 implementation review: the original _tick() gated
BOTH the mute and unmute calls behind the *current* mute_on_tx value, so
unchecking mute_on_tx mid-transmission skipped the falling edge's
set_muted(False) call entirely -- no future PTT edge could ever unmute it
again, since by then the gate was already off. Fixed by always unmuting
on the falling edge regardless of the current mute_on_tx value (muting on
the rising edge stays gated, as intended)."""
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
    def __init__(self) -> None:
        self.attached = True
        self.mute_calls: list[bool] = []

    async def tune_hz(self, freq_hz: int) -> bool:
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        return True

    async def set_muted(self, muted: bool) -> None:
        self.mute_calls.append(muted)

    async def get_status(self) -> WebSDRStatus:
        return WebSDRStatus(connected=True)

    async def close(self) -> None:
        pass


class _UnusedWebViewHost:
    async def create_page(self, loop, on_dead=None):
        raise AssertionError("not expected to be called in these tests")

    async def destroy_page(self, page) -> None:
        raise AssertionError("not expected to be called in these tests")


def test_unmute_still_fires_after_mute_on_tx_disabled_mid_transmission():
    settings = AppSettings(mute_on_tx=True)
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())
    stub_driver = StubDriver()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True))
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    async def run():
        await engine._tick()  # PTT rising edge, mute_on_tx True -> mutes
        settings.mute_on_tx = False  # user unchecks the box mid-TX
        engine._rig.state = RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()  # PTT falling edge -- must still unmute

    asyncio.run(run())

    assert stub_driver.mute_calls == [True, False]


def test_mute_stays_gated_off_when_mute_on_tx_disabled_before_transmission():
    settings = AppSettings(mute_on_tx=False)
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())
    stub_driver = StubDriver()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True))
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    asyncio.run(engine._tick())

    # mute_on_tx was already off before the rising edge -- must not mute.
    assert stub_driver.mute_calls == []

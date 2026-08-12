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
from sdrsync.sync.engine import WEBSDR_MIN_WRITE_GAP_S, SyncEngine
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

    async def set_freq(self, freq_hz: int) -> bool:
        return True

    async def set_mode(self, mode_name: str) -> bool:
        return True


class StubDriver:
    CW_VARIANT_IS_AMBIGUOUS = True

    def __init__(self) -> None:
        self.attached = True
        self.mute_calls: list[bool] = []

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        return True

    async def set_muted(self, muted: bool) -> None:
        self.mute_calls.append(muted)

    async def get_status(self) -> WebSDRStatus:
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
        # v14: mute/unmute are writes to the WebSDR page and share the
        # global WEBSDR_MIN_WRITE_GAP_S floor, so back-to-back ticks with
        # no wall-clock delay between them would otherwise be throttled.
        # (That the deferred unmute is not LOST is covered separately, by
        # test_ptt_edge_deferred_by_write_gap_still_fires_on_a_later_tick.)
        engine._last_websdr_write_at -= WEBSDR_MIN_WRITE_GAP_S + 0.1
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



def test_ptt_edge_deferred_by_write_gap_still_fires_on_a_later_tick():
    """v14: mute/unmute are real writes into the WebSDR page, so they sit
    behind the same WEBSDR_MIN_WRITE_GAP_S floor as the forward pushes.
    A PTT edge that arrives inside that window must be DEFERRED, never
    dropped -- _last_ptt is deliberately not latched unless the call
    actually goes out. Dropping it would resurrect exactly the bug this
    module exists for: a WebSDR left muted forever with no future edge
    able to clear it."""
    settings = AppSettings(mute_on_tx=True)
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())
    stub_driver = StubDriver()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True))
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # rising edge -> mutes, and stamps the write gap
    assert stub_driver.mute_calls == [True]

    # Falling edge arrives while the gap is still closed.
    engine._rig.state = RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False)
    asyncio.run(engine._tick())
    assert stub_driver.mute_calls == [True]  # deferred...
    assert engine._last_ptt is True  # ...and NOT latched away

    # Next tick past the gap: the unmute still happens.
    engine._last_websdr_write_at -= WEBSDR_MIN_WRITE_GAP_S + 0.1
    asyncio.run(engine._tick())
    assert stub_driver.mute_calls == [True, False]
    assert engine._last_ptt is False

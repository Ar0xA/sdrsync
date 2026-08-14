"""Engine-level: toggling mute_on_tx off while still muted from an earlier
PTT transition must actually unmute, not leave the WebSDR muted forever.

Found during the v8 implementation review: the original _tick() gated
BOTH the mute and unmute calls behind the *current* mute_on_tx value, so
unchecking mute_on_tx mid-transmission skipped the falling edge's
set_muted(False) call entirely -- no future PTT edge could ever unmute it
again, since by then the gate was already off. Fixed by always unmuting
on the falling edge regardless of the current mute_on_tx value (muting on
the rising edge stays gated, as intended).

v15: mute-on-TX moved from a per-site JS call on the driver
(self._driver.set_muted()) to a native, page-adapter-level call
(self._page.set_muted()) -- see browser_shim.py's module comment. It is
no longer a "write to the WebSDR page" and is therefore NOT subject to
WEBSDR_MIN_WRITE_GAP_S any more (see
test_mute_fires_immediately_even_inside_the_websdr_write_gap below,
which replaced the old deferred-by-the-write-gap test)."""
import asyncio
import queue
import time

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


class StubPage:
    def __init__(self) -> None:
        self.mute_calls: list[bool] = []

    async def set_muted(self, muted: bool) -> None:
        self.mute_calls.append(muted)


class StubDriver:
    CW_VARIANT_IS_AMBIGUOUS = True

    def __init__(self) -> None:
        self.attached = True

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        return True

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


def _make_engine(settings: AppSettings, rig_state: RigState) -> tuple[SyncEngine, StubPage]:
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())
    stub_page = StubPage()
    engine._rig = StubRig(rig_state)
    engine._rig_active = True
    engine._driver = StubDriver()
    engine._page = stub_page
    engine._websdr_active = True
    return engine, stub_page


def test_unmute_still_fires_after_mute_on_tx_disabled_mid_transmission():
    settings = AppSettings(mute_on_tx=True)
    engine, stub_page = _make_engine(settings, RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True))

    async def run():
        await engine._tick()  # PTT rising edge, mute_on_tx True -> mutes
        settings.mute_on_tx = False  # user unchecks the box mid-TX
        engine._rig.state = RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()  # PTT falling edge -- must still unmute

    asyncio.run(run())

    assert stub_page.mute_calls == [True, False]


def test_mute_stays_gated_off_when_mute_on_tx_disabled_before_transmission():
    settings = AppSettings(mute_on_tx=False)
    engine, stub_page = _make_engine(settings, RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True))

    asyncio.run(engine._tick())

    # mute_on_tx was already off before the rising edge -- must not mute.
    assert stub_page.mute_calls == []


def test_mute_fires_immediately_even_inside_the_websdr_write_gap():
    """v15: mute/unmute are a native WebView-level call, not a write into
    the WebSDR page, so they must NOT sit behind WEBSDR_MIN_WRITE_GAP_S
    the way forward freq/mode pushes do -- the whole point of the native
    path is that it doesn't share the page's own write cadence at all.
    Regression guard for the old (pre-v15) deferred-by-the-write-gap
    behavior accidentally coming back."""
    settings = AppSettings(mute_on_tx=True)
    engine, stub_page = _make_engine(settings, RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True))
    # Simulate a forward push having just written to the page THIS instant
    # -- if mute were still gated by the shared write-gap floor, this
    # would defer it to a later tick.
    engine._last_websdr_write_at = time.monotonic()
    assert (time.monotonic() - engine._last_websdr_write_at) < WEBSDR_MIN_WRITE_GAP_S

    asyncio.run(engine._tick())

    assert stub_page.mute_calls == [True]
    assert engine._last_ptt is True

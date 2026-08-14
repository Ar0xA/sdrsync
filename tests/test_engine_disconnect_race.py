"""A concurrent Disconnect (_stop_websdr(), reachable via a user click, the
rig-stop cascade, or _handle_page_dead()) can run on the same event loop
while _tick() is suspended inside an earlier await in the SAME tick,
nulling engine._driver (and, for the ptt_mute test below, engine._page)
out from under it. Every subsequent bare self._driver.<method>() access
in that tick must not raise AttributeError -- it must be treated as
"nothing left to do this tick", the same way the code already treats
"not attached".

The ptt_mute test's stub simulates this by mutating engine state
synchronously from inside its own awaited set_muted() -- it does not rely
on _tick() genuinely suspending at that specific call: since v15,
WxPageAdapter.set_muted() is fire-and-forget (no await inside it at all),
so the real code can no longer actually yield there. The test is still a
real regression guard for the later guards it exercises (a stub whose
set_muted() does NOT touch engine state would make it pass whether or not
those guards existed at all -- confirmed by a bug-hunter review pass, not
just asserted here).

get_status() call counts below assume ONE get_status() call per tick in
the steady state (rig connected, no early return), not two -- a LATER
bug-hunter review pass found _tick() used to also fetch it unconditionally
at the very top, whether or not any of the three early-return branches
that actually use that value fire, doubling the real per-tick WebView
page-poll cost in the common case for nothing. Fetched lazily now, only
by the branches that need it -- none of which run in these tests (the rig
is always active/connected here), so the only get_status() call left is
the one inside the not-transmitting forward-push/reverse-sync block."""
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


class StubPage:
    """Its set_muted() call simulates a concurrent _stop_websdr() completing
    while THIS page call was in flight -- by the time it returns,
    engine._driver/_page are already None and _websdr_active False,
    exactly as the real _stop_websdr() (which nulls all three together)
    running during a suspended await on the same loop would leave it."""

    def __init__(self, engine: SyncEngine) -> None:
        self.engine = engine
        self.mute_calls: list[bool] = []

    async def set_muted(self, muted: bool) -> None:
        self.mute_calls.append(muted)
        self.engine._driver = None
        self.engine._page = None
        self.engine._websdr_active = False


class DisconnectingDriver:
    """get_status() would raise AttributeError if called on
    self.engine._driver afterward without a None-check."""

    CW_VARIANT_IS_AMBIGUOUS = True

    def __init__(self, engine: SyncEngine) -> None:
        self.attached = True
        self.engine = engine
        self.get_status_calls = 0

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
    page = StubPage(engine)
    engine._page = page
    engine._websdr_active = True

    # PTT rising edge -> set_muted(True) fires, which (simulating a
    # concurrent Disconnect) nulls engine._driver/_page mid-tick. The rest
    # of this tick (transmitting -> skip forward push -> get_status()) must
    # complete cleanly rather than raising.
    asyncio.run(engine._tick())

    assert page.mute_calls == [True]
    # get_status() is only ever called inside the not-transmitting
    # forward-push/reverse-sync block -- PTT is True here, so
    # "transmitting" skips that whole block, and get_status() must never
    # be called at all (not even once): the driver is already gone by the
    # time anything downstream would try.
    assert driver.get_status_calls == 0
    assert engine._driver is None
    assert engine._page is None
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

    # The mode push (which nulls the driver) runs BEFORE the block's own
    # get_status() call, so that call must never happen at all -- the
    # None-check ahead of it must see the driver already gone.
    assert driver.get_status_calls == 0
    assert engine._driver is None


def test_driver_going_none_during_the_get_status_call_skips_reverse_sync():
    """The get_status() call right before the reverse-sync gate can
    itself complete successfully -- bound to the OLD driver object at
    call time -- even though engine._driver has already gone None by the
    time it returns (a concurrent _stop_websdr()/_stop_rig() completing
    while this exact await was suspended). _reverse_sync_tick()
    dereferences self._driver immediately with no guard of its own, so
    the reverse-sync gate at the call site must re-check self._driver is
    not None, not just websdr_status is not None."""
    settings = AppSettings(mute_on_tx=True)
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True

    class DisconnectingStatusDriver(DisconnectingDriver):
        async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
            # False, not True: a successful forward push this same tick
            # would stamp _forward_push_completed_at to "now" and keep the
            # reverse-sync holdoff gate closed, masking the exact bug this
            # test exists to catch.
            return False

        async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
            return False

        async def get_status(self) -> WebSDRStatus:
            self.get_status_calls += 1
            # The ONE call this tick (the not-transmitting block's own
            # get_status(), right before the reverse-sync gate -- see the
            # module docstring, this is no longer preceded by a separate
            # eager top-of-tick call). Simulate a concurrent stop
            # completing while this exact await was in flight: it still
            # returns a real, connected status (bound to this driver
            # instance), but engine._driver is gone by the time control
            # returns.
            self.engine._driver = None
            self.engine._websdr_active = False
            return WebSDRStatus(connected=True)

    driver = DisconnectingStatusDriver(engine)
    engine._driver = driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # must not raise

    assert driver.get_status_calls == 1
    assert engine._driver is None

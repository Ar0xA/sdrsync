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
from sdrsync.sync.engine import FULL_RESYNC_INTERVAL_S, SyncEngine
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

    async def set_mode(self, mode_name: str, passband_hz) -> bool:
        return True


class StubDriver:
    """attached=True and no-op close() so it stands in for WebsdrOrgDriver in
    engine-level tests without a browser."""

    def __init__(self) -> None:
        self.attached = True
        self.tuned: list[int] = []
        self.tune_verify_flags: list[bool] = []
        self.modes: list[tuple] = []

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        self.tuned.append(freq_hz)
        self.tune_verify_flags.append(verify)
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        self.modes.append((hamlib_mode, passband_hz))
        return True

    async def set_muted(self, muted: bool) -> None:
        pass

    async def get_status(self) -> WebSDRStatus:
        return WebSDRStatus(connected=True)

    def hamlib_mode_from_status(self, status: WebSDRStatus):
        return None

    def rig_freq_from_status(self, status: WebSDRStatus):
        return None

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

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
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


def test_periodic_full_resync_repushes_even_when_dedupe_latches_already_match():
    """Safety net: even if _last_sent_mode_key/_last_sent_freq already
    look like a match (so the normal dedupe path would skip pushing),
    a forward re-push still fires once FULL_RESYNC_INTERVAL_S has
    elapsed -- guards against the WebSDR page and the rig silently
    drifting apart with nothing left to notice, regardless of the exact
    mechanism that caused the drift."""
    engine = make_engine()
    stub_rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    stub_driver = StubDriver()
    engine._rig = stub_rig
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    async def run_two_ticks():
        await engine._tick()
        engine._pending_freq_since -= 1.0
        await engine._tick()

    asyncio.run(run_two_ticks())
    assert stub_driver.modes == [("USB", 2700)]
    assert stub_driver.tuned == [14074000]

    # Nothing changed on the rig -- a normal tick must NOT re-push.
    asyncio.run(engine._tick())
    assert stub_driver.modes == [("USB", 2700)]
    assert stub_driver.tuned == [14074000]

    # Once the periodic resync interval has elapsed, the next tick
    # re-pushes both, even though the dedupe latches still match.
    engine._last_full_resync_at -= FULL_RESYNC_INTERVAL_S + 0.1
    asyncio.run(engine._tick())
    assert stub_driver.modes == [("USB", 2700), ("USB", 2700)]
    assert stub_driver.tuned == [14074000, 14074000]


def test_periodic_resync_of_unchanged_freq_still_verifies_with_no_reverse_push_in_flight():
    """v12: tune_hz(verify=...) must be True for a periodic resync of an
    UNCHANGED frequency as long as no reverse-sync ladder is actively
    retrying -- verify=False unconditionally for every unchanged-value
    resync would silently give up the resync's only mechanism for
    detecting/repairing the WebSDR page's band selection having drifted
    from this driver's own tracking (see websdr_org.py's
    _verify_freq_applied), not just the narrow window where an armed
    corrective re-tune could actually fight a concurrent reverse push."""
    engine = make_engine()
    stub_rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    stub_driver = StubDriver()
    engine._rig = stub_rig
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    async def run_two_ticks():
        await engine._tick()
        engine._pending_freq_since -= 1.0
        await engine._tick()

    asyncio.run(run_two_ticks())
    assert stub_driver.tuned == [14074000]
    assert stub_driver.tune_verify_flags == [True]  # a genuine change always verifies

    # The forward MODE re-push on this same resync tick would set
    # _last_sent_freq = None (see engine.py: "a mode change can change the
    # effective frequency"), which makes freq_changed True and would let this
    # test pass even WITHOUT the `or (...)` refinement. Drop the rig's mode
    # for this tick so the mode branch is skipped and _last_sent_freq
    # survives -- now freq_changed is genuinely False and only the
    # refinement can make verify True.
    stub_rig.state = RigState(freq_hz=14074000, mode=None, passband_hz=None, ptt=False)
    engine._last_full_resync_at -= FULL_RESYNC_INTERVAL_S + 0.1
    asyncio.run(engine._tick())
    assert stub_driver.tuned == [14074000, 14074000]
    # Unchanged value AND freq_changed is False -- still verifies, because
    # nothing reverse-sync is in flight.
    assert stub_driver.tune_verify_flags == [True, True]


def test_periodic_resync_of_unchanged_freq_skips_verify_while_reverse_push_in_flight():
    """v12: the one case tune_hz(verify=False) is actually for -- a
    reverse-sync mode push is actively retrying when the periodic resync
    of an UNCHANGED frequency fires. Uses _mode_push (not _freq_push):
    the forward freq push's own outer gate already requires
    self._freq_push is None to fire at all, so _freq_push can never be
    the thing making verify False at this call site -- only a
    concurrently in-flight _mode_push can."""
    from sdrsync.sync.engine import _ReversePush

    engine = make_engine()
    stub_rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    stub_driver = StubDriver()
    engine._rig = stub_rig
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    async def run_two_ticks():
        await engine._tick()
        engine._pending_freq_since -= 1.0
        await engine._tick()

    asyncio.run(run_two_ticks())
    assert stub_driver.tune_verify_flags == [True]

    engine._last_full_resync_at -= FULL_RESYNC_INTERVAL_S + 0.1
    engine._mode_push = _ReversePush(target="LSB")  # a reverse-sync mode ladder is actively retrying
    asyncio.run(engine._tick())
    assert stub_driver.tuned == [14074000, 14074000]
    assert stub_driver.tune_verify_flags == [True, False]

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
import time

import pytest

from sdrsync.config import AppSettings
from sdrsync.rig.rigctld import RigState
from sdrsync.sync.engine import (
    FORWARD_PUSH_BACKOFF_MAX_S,
    FORWARD_PUSH_IMMEDIATE_RETRY_MAX_FAILURES,
    FREQ_DEBOUNCE_S,
    FULL_RESYNC_INTERVAL_S,
    WEBSDR_MIN_WRITE_GAP_S,
    SyncEngine,
    _ForwardPushBackoff,
)
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

    async def destroy_page(self, page, loop=None) -> None:
        raise AssertionError("not expected to be called in these tests")


def make_engine() -> SyncEngine:
    settings = AppSettings()
    return SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())


def _clear_websdr_write_gap(engine: SyncEngine) -> None:
    """Backdates the global WebSDR-write rate limiter
    (WEBSDR_MIN_WRITE_GAP_S) so a test driving consecutive ticks with no
    real wall-clock delay between them isn't throttled by the same floor
    that bounds a real session's write rate. Exact counterpart of
    test_engine_reverse_sync.py's _clear_rig_write_gap()."""
    engine._last_websdr_write_at -= WEBSDR_MIN_WRITE_GAP_S + 0.1


def _clear_forward_backoff(engine: SyncEngine) -> None:
    """Backdates both per-axis forward-push failure ladders, so a test
    exercising repeated FAILING pushes isn't waiting out the real
    FORWARD_PUSH_BACKOFF_BASE_S. Counterpart of
    test_engine_reverse_sync.py's _clear_push_backoff()."""
    engine._forward_freq_backoff.next_attempt_at = 0.0
    engine._forward_mode_backoff.next_attempt_at = 0.0


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
        _clear_websdr_write_gap(engine)
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
    once the WebSDR recovers.

    Since v14 the retry is additionally SPACED by the per-axis failure
    ladder (see _ForwardPushBackoff) rather than firing every tick, so
    this drives the ladder forward explicitly. The property under test is
    unchanged: the failed value stays un-latched and does get retried."""
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
        _clear_websdr_write_gap(engine)
        _clear_forward_backoff(engine)
        await engine._tick()
        engine._pending_freq_since -= 1.0
        _clear_websdr_write_gap(engine)
        _clear_forward_backoff(engine)
        await engine._tick()
        engine._pending_freq_since -= 1.0
        _clear_websdr_write_gap(engine)
        _clear_forward_backoff(engine)
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
        _clear_websdr_write_gap(engine)
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
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert stub_driver.modes == [("USB", 2700), ("USB", 2700)]
    assert stub_driver.tuned == [14074000, 14074000]


def test_a_fine_9_to_10hz_retune_still_reaches_the_websdr():
    """Regression test for a live bug: a genuine, intentional 9-10 Hz
    fine-tune step (common CW/RTTY spotting granularity, e.g. 14074800 ->
    14074810) was being silently dropped forever, not just filtered once.
    With FREQ_CHANGE_THRESHOLD_HZ previously at 10 and a strict '>'
    comparison, a new rig reading exactly 10 Hz away from the current
    pending candidate never exceeded the threshold, so it was never even
    adopted as a new candidate -- every later poll kept comparing against
    the same stale candidate and kept failing the same way, so the target
    frequency displayed/tuned on the WebSDR never moved at all."""
    engine = make_engine()
    state = RigState(freq_hz=14074800, mode="USB", passband_hz=2700, ptt=False)
    stub_rig = StubRig(state)
    stub_driver = StubDriver()
    engine._rig = stub_rig
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    async def settle():
        await engine._tick()
        engine._pending_freq_since -= FREQ_DEBOUNCE_S + 0.1
        _clear_websdr_write_gap(engine)
        await engine._tick()

    asyncio.run(settle())
    assert stub_driver.tuned == [14074800]

    # A real, deliberate 10 Hz fine-tune step -- must not be swallowed.
    state.freq_hz = 14074810
    asyncio.run(settle())
    assert 14074810 in stub_driver.tuned


def test_an_exact_1hz_step_still_reaches_the_websdr():
    """Regression test for a live bug found on a real rig: the first fix
    for the 9-10 Hz issue above lowered FREQ_CHANGE_THRESHOLD_HZ from 10
    to 1, which still silently dropped a genuine step of EXACTLY 1 Hz --
    many rigs offer a dedicated 1 Hz fine-tune step button, and a strict
    '>' comparison against a threshold of 1 never lets an exactly-1
    difference through. FREQ_CHANGE_THRESHOLD_HZ is now 0, which only
    ever ignores a truly UNCHANGED reading (diff of exactly 0)."""
    engine = make_engine()
    state = RigState(freq_hz=14074500, mode="USB", passband_hz=2700, ptt=False)
    stub_rig = StubRig(state)
    stub_driver = StubDriver()
    engine._rig = stub_rig
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True

    async def settle():
        await engine._tick()
        engine._pending_freq_since -= FREQ_DEBOUNCE_S + 0.1
        _clear_websdr_write_gap(engine)
        await engine._tick()

    asyncio.run(settle())
    assert stub_driver.tuned == [14074500]

    # The rig's own finest step button: exactly 1 Hz.
    state.freq_hz = 14074499
    asyncio.run(settle())
    assert 14074499 in stub_driver.tuned


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
        _clear_websdr_write_gap(engine)
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
    _clear_websdr_write_gap(engine)
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
        _clear_websdr_write_gap(engine)
        await engine._tick()

    asyncio.run(run_two_ticks())
    assert stub_driver.tune_verify_flags == [True]

    engine._last_full_resync_at -= FULL_RESYNC_INTERVAL_S + 0.1
    engine._mode_push = _ReversePush(target="LSB")  # a reverse-sync mode ladder is actively retrying
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert stub_driver.tuned == [14074000, 14074000]
    assert stub_driver.tune_verify_flags == [True, False]


# ----------------------------------------------------------------------
# v14 "good network citizen" round: the forward direction (rig -> WebSDR)
# writes into a stranger's volunteer-run receiver, and had no rate limit
# of any kind. These cover the global write gap, the per-axis failure
# ladder, the periodic-resync stamp fix, and the forward-side generation
# guard.
# ----------------------------------------------------------------------


class TogglingDriver(StubDriver):
    """Driver whose push results can be flipped mid-test, for exercising
    the failure ladder's recovery path."""

    def __init__(self) -> None:
        super().__init__()
        self.mode_result = True
        self.freq_result = True

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        self.tuned.append(freq_hz)
        self.tune_verify_flags.append(verify)
        return self.freq_result

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        self.modes.append((hamlib_mode, passband_hz))
        return self.mode_result


def test_write_gap_lets_only_one_of_two_back_to_back_writes_reach_the_driver():
    """Item 1: two pushes closer together than WEBSDR_MIN_WRITE_GAP_S --
    only the first actually reaches the driver. Uses FailingDriver so
    neither push latches as sent, proving the SECOND call is suppressed
    by the write gap itself and not merely deduped away."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    failing_driver = FailingDriver()
    engine._driver = failing_driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # mode pushes (freq only arms its debounce)
    assert failing_driver.modes == [("USB", 2700)]
    assert failing_driver.tuned == []

    # Second tick, no wall-clock delay: the freq push is otherwise fully
    # eligible (debounce satisfied, nothing latched) but must be held off.
    engine._pending_freq_since -= 1.0
    _clear_forward_backoff(engine)
    asyncio.run(engine._tick())
    assert failing_driver.tuned == []

    # Once the gap has elapsed it goes through.
    _clear_websdr_write_gap(engine)
    _clear_forward_backoff(engine)
    asyncio.run(engine._tick())
    assert failing_driver.tuned == [14074000]


def test_write_gap_is_one_global_floor_shared_by_both_axes():
    """Both axes read a single gate computed once per tick, so a mode and
    a frequency push in the SAME tick pass together (one logical retune)
    rather than each keeping its own independent floor."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    stub_driver = StubDriver()
    engine._driver = stub_driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # mode pushes, freq arms its debounce
    engine._pending_freq_since -= 1.0
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())  # freq pushes
    assert stub_driver.modes == [("USB", 2700)]
    assert stub_driver.tuned == [14074000]

    # A resync forces BOTH axes in one tick: both go through on the one
    # shared gate (the mode push's own timestamp must not lock out the
    # freq push that follows it in the same tick).
    engine._last_full_resync_at -= FULL_RESYNC_INTERVAL_S + 0.1
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert stub_driver.modes == [("USB", 2700), ("USB", 2700)]
    assert stub_driver.tuned == [14074000, 14074000]


def test_forward_push_backoff_ladder_doubles_and_caps():
    """Item 2: 1s, 2s, 4s, 8s, 16s, then capped at 30s."""
    backoff = _ForwardPushBackoff()
    for expected in (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0):
        before = time.monotonic()
        backoff.record_failure()
        assert backoff.next_attempt_at - before == pytest.approx(expected, abs=0.05)
    assert backoff.failures == 7


def test_forward_push_backoff_success_resets_the_ladder():
    backoff = _ForwardPushBackoff()
    backoff.record_failure()
    backoff.record_failure()
    assert backoff.failures == 2
    backoff.record_success()
    assert backoff.failures == 0
    assert backoff.next_attempt_at == 0.0


def test_forward_push_backoff_new_target_grants_one_immediate_retry_while_shallow():
    backoff = _ForwardPushBackoff()
    backoff.note_target(14_074_000)
    backoff.record_failure()
    assert backoff.next_attempt_at > time.monotonic()  # backing off

    backoff.note_target(14_200_000)  # the rig genuinely moved
    assert backoff.next_attempt_at == 0.0  # one prompt try for the new value
    # ...but the failure count itself is NOT forgiven -- only success does that.
    assert backoff.failures == 1


def test_forward_push_backoff_new_target_does_not_evade_a_deep_ladder():
    """The hole this closes: a rig being spun continuously presents a
    'new' target every tick, which would otherwise clear next_attempt_at
    forever and defeat the ladder entirely."""
    backoff = _ForwardPushBackoff()
    for _ in range(FORWARD_PUSH_IMMEDIATE_RETRY_MAX_FAILURES):
        backoff.record_failure()
    assert backoff.failures == FORWARD_PUSH_IMMEDIATE_RETRY_MAX_FAILURES
    armed_at = backoff.next_attempt_at

    backoff.note_target("a completely new target")
    assert backoff.next_attempt_at == armed_at  # waits out the backoff like anything else


def test_failing_forward_push_backs_off_instead_of_retrying_every_tick():
    """Integration: the ladder actually gates a real tick, so a
    permanently-failing push stops being re-sent at the poll rate."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    failing_driver = FailingDriver()
    engine._driver = failing_driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # mode push fails -> ladder arms
    assert failing_driver.modes == [("USB", 2700)]
    assert engine._forward_mode_backoff.failures == 1

    # Write gap cleared, so only the ladder can be holding it back.
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert failing_driver.modes == [("USB", 2700)]  # not retried yet

    _clear_forward_backoff(engine)
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert failing_driver.modes == [("USB", 2700), ("USB", 2700)]
    assert engine._forward_mode_backoff.failures == 2


def test_successful_push_clears_a_previously_armed_forward_ladder():
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    driver = TogglingDriver()
    driver.mode_result = False
    engine._driver = driver
    engine._websdr_active = True

    asyncio.run(engine._tick())
    assert engine._forward_mode_backoff.failures == 1

    driver.mode_result = True  # the WebSDR recovers
    _clear_forward_backoff(engine)
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert engine._forward_mode_backoff.failures == 0
    assert engine._forward_mode_backoff.next_attempt_at == 0.0


def test_periodic_resync_held_back_by_the_write_gap_is_not_stamped_as_done():
    """Item 3, the one thing that must not regress: stamping
    _last_full_resync_at for a resync that never actually happened would
    silently push the safety net out by another FULL_RESYNC_INTERVAL_S
    every time a push is skipped -- disabling the very mechanism that
    repairs a persistent desync."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    stub_driver = StubDriver()
    engine._driver = stub_driver
    engine._websdr_active = True

    asyncio.run(engine._tick())
    engine._pending_freq_since -= 1.0
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert stub_driver.modes == [("USB", 2700)]
    assert stub_driver.tuned == [14074000]

    # A resync comes due while the write gap is still closed (the freq
    # push above only just happened).
    engine._last_full_resync_at -= FULL_RESYNC_INTERVAL_S + 0.1
    stamp_before = engine._last_full_resync_at
    asyncio.run(engine._tick())

    assert stub_driver.modes == [("USB", 2700)]  # nothing was sent...
    assert stub_driver.tuned == [14074000]
    assert engine._last_full_resync_at == stamp_before  # ...so nothing recorded as done

    # The very next opportunity still attempts it.
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert stub_driver.modes == [("USB", 2700), ("USB", 2700)]
    assert stub_driver.tuned == [14074000, 14074000]
    assert engine._last_full_resync_at > stamp_before


def test_a_ladder_blocked_axis_still_blocks_its_own_repush():
    """The per-axis ladder's own job, unchanged: while an axis is backing
    off, it does not re-push -- not even for a periodic resync."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    failing_driver = FailingDriver()
    engine._driver = failing_driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # mode push fails, arming the mode ladder
    assert engine._forward_mode_backoff.failures == 1

    engine._last_full_resync_at -= FULL_RESYNC_INTERVAL_S + 0.1
    _clear_websdr_write_gap(engine)  # only the ladder is in the way now
    asyncio.run(engine._tick())

    assert failing_driver.modes == [("USB", 2700)]  # ladder blocked the re-push


class ModeFailingDriver(StubDriver):
    """One sick axis, one healthy one: set_mode() always fails (so the
    mode ladder arms and stays armed), tune_hz() always succeeds."""

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        self.modes.append((hamlib_mode, passband_hz))
        return False


def test_one_axis_in_its_failure_ladder_does_not_hold_the_resync_stamp_hostage():
    """Only the global WRITE GAP counts as "held back" for the periodic
    resync stamp. An axis sitting in its own backoff ladder has an
    independent retry schedule, and withholding the stamp for it left
    due_for_periodic_resync permanently True -- which forced the OTHER,
    healthy axis past its dedupe latch on every single tick, bounded only
    by the 0.5s write gap. That is traffic amplification against a
    volunteer's receiver, currently masked only by
    FORWARD_PUSH_BACKOFF_MAX_S and FULL_RESYNC_INTERVAL_S both happening
    to be 30.0."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    driver = ModeFailingDriver()
    engine._driver = driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # mode push fails -> mode ladder armed; freq debounce arms
    assert engine._forward_mode_backoff.failures == 1
    assert driver.tuned == []

    # A resync falls due. The write gap is clear and the frequency axis is
    # perfectly healthy; only the mode ladder is still backing off.
    engine._last_full_resync_at -= FULL_RESYNC_INTERVAL_S + 0.1
    stamp_before = engine._last_full_resync_at
    engine._pending_freq_since -= 1.0
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())

    assert driver.modes == [("USB", 2700)]  # the sick axis correctly stayed quiet...
    assert driver.tuned == [14074000]  # ...the healthy one did its resync push...
    assert engine._last_full_resync_at > stamp_before  # ...and that counted as done

    # The point of the stamp advancing: the next tick is NOT another
    # forced resync, so the healthy axis goes back to its dedupe latch
    # instead of re-pushing an unchanged frequency every 0.5s forever.
    engine._pending_freq_since -= 1.0
    _clear_websdr_write_gap(engine)
    asyncio.run(engine._tick())
    assert driver.tuned == [14074000]


class SlowModeDriver(StubDriver):
    """set_mode() suspends for real (like a page.evaluate round-trip
    would), giving another task on the same loop a chance to run
    mid-await -- the same technique as the v13 Hold-toggle regression
    test in test_engine_reverse_sync.py."""

    def __init__(self) -> None:
        super().__init__()
        self.entered_set_mode = asyncio.Event()

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        self.modes.append((hamlib_mode, passband_hz))
        self.entered_set_mode.set()
        await asyncio.sleep(0.02)
        return True


class SlowTuneDriver(StubDriver):
    def __init__(self) -> None:
        super().__init__()
        self.entered_tune = asyncio.Event()

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        self.tuned.append(freq_hz)
        self.tune_verify_flags.append(verify)
        self.entered_tune.set()
        await asyncio.sleep(0.02)
        return True


def test_reset_during_forward_mode_push_discards_the_stale_write():
    """Item 4: _handle_page_dead -> _start_websdr -> _reset_sync_latches
    can land while a forward push is awaiting the driver. That write went
    to the page that just died; letting its bookkeeping land would mark
    the freshly-reattached page as already up to date and suppress its own
    re-push. Mirrors the reverse side's long-standing generation guard."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    driver = SlowModeDriver()
    engine._driver = driver
    engine._websdr_active = True

    async def reset_during_the_push():
        tick = asyncio.create_task(engine._tick())
        await driver.entered_set_mode.wait()
        engine._reset_sync_latches()  # the attach supervisor, on this same loop
        await tick

    asyncio.run(reset_during_the_push())

    assert driver.modes == [("USB", 2700)]  # the write did physically happen
    # ...but none of its bookkeeping survived the reset, so the
    # reattached page gets pushed to again rather than being assumed current.
    assert engine._last_sent_mode_key is None
    assert engine._last_pushed_to_websdr_mode is None
    assert engine._reverse_reseed_due is False


def test_reset_during_forward_freq_push_discards_the_stale_write():
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode=None, passband_hz=None, ptt=False))
    engine._rig_active = True
    driver = SlowTuneDriver()
    engine._driver = driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # arms the freq debounce (mode is None: nothing to push)
    engine._pending_freq_since -= 1.0
    _clear_websdr_write_gap(engine)

    async def reset_during_the_push():
        tick = asyncio.create_task(engine._tick())
        await driver.entered_tune.wait()
        engine._reset_sync_latches()
        await tick

    asyncio.run(reset_during_the_push())

    assert driver.tuned == [14074000]
    assert engine._last_sent_freq is None
    assert engine._last_pushed_to_websdr_freq is None
    assert engine._reverse_reseed_due is False


def test_hold_toggle_during_a_forward_push_does_not_discard_its_bookkeeping():
    """The Hold toggle pauses the WebSDR -> rig direction only --
    set_reverse_sync_held()'s own contract says forward sync is left
    completely untouched. _apply_reverse_sync_held() bumps
    _sync_latch_generation (correct, for reverse sync's own in-flight
    state), so once the forward push grew a generation guard of its own
    it had to be a SEPARATE counter: sharing one meant a Hold click
    landing mid-await inside set_mode()/tune_hz() silently discarded that
    forward push's bookkeeping and forced a redundant re-push."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    driver = SlowModeDriver()
    engine._driver = driver
    engine._websdr_active = True

    async def hold_during_the_push():
        tick = asyncio.create_task(engine._tick())
        await driver.entered_set_mode.wait()
        engine._apply_reverse_sync_held(True)  # the GUI thread, marshalled onto this loop
        await tick

    asyncio.run(hold_during_the_push())

    assert driver.modes == [("USB", 2700)]
    # Hold is a reverse-side concept: the forward push that was already
    # in flight still latches normally.
    assert engine._last_sent_mode_key == ("USB", 2700)
    assert engine._last_pushed_to_websdr_mode == "USB"
    assert engine._reverse_sync_held is True


def test_hold_toggle_still_supersedes_reverse_sync_state():
    """Guards the other half of the split: giving the forward push its
    own counter must not stop Hold from cancelling reverse-side
    bookkeeping, which is what _sync_latch_generation is for."""
    engine = make_engine()
    before = engine._sync_latch_generation
    engine._apply_reverse_sync_held(True)
    assert engine._sync_latch_generation > before


def test_reset_bumps_both_generation_counters():
    """_reset_sync_latches() is the one event that genuinely invalidates
    BOTH directions' bookkeeping -- the page they referred to is gone."""
    engine = make_engine()
    forward_before = engine._forward_latch_generation
    reverse_before = engine._sync_latch_generation
    engine._reset_sync_latches()
    assert engine._forward_latch_generation > forward_before
    assert engine._sync_latch_generation > reverse_before


def test_superseded_mode_push_skips_the_rest_of_the_forward_block():
    """A forward generation mismatch must skip the REST of the forward
    block outright, the way _reverse_sync_tick() returns on mismatch.

    Falling through instead was safe only by accident:
    _reset_sync_latches() also clears _pending_freq, which incidentally
    routed the frequency branch into its harmless re-arm-debounce path.
    This test deliberately restores _pending_freq right after the reset,
    standing in for any future reset path that bumps the generation
    without also clearing pending forward state -- the contract under
    test is "mismatch means skip", not "some other field happened to be
    None"."""
    engine = make_engine()
    engine._rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    driver = SlowModeDriver()
    engine._driver = driver
    engine._websdr_active = True

    asyncio.run(engine._tick())  # arms the freq debounce and latches the mode
    engine._rig.state = RigState(freq_hz=14074000, mode="CW", passband_hz=500, ptt=False)
    engine._pending_freq_since -= 1.0
    _clear_websdr_write_gap(engine)

    async def reset_during_the_mode_push():
        # The event is still set from the first tick's own set_mode --
        # without clearing it the wait() below returns before the second
        # tick has even started, and the reset would land BEFORE the
        # generation is captured rather than mid-await.
        driver.entered_set_mode.clear()
        tick = asyncio.create_task(engine._tick())
        await driver.entered_set_mode.wait()
        pending = engine._pending_freq
        engine._reset_sync_latches()
        engine._pending_freq = pending  # see the docstring
        engine._pending_freq_since = time.monotonic() - 1.0
        await tick

    asyncio.run(reset_during_the_mode_push())

    assert driver.modes == [("USB", 2700), ("CW", 500)]
    # The frequency axis belonged to the same superseded cycle, so it must
    # not have gone out to the (now replaced) page at all.
    assert driver.tuned == []


def test_forward_push_backoff_exponent_is_clamped():
    """Not merely wasteful: at ~1024 consecutive failures
    `FORWARD_PUSH_BACKOFF_BASE_S * (2 ** (failures - 1))` raises
    OverflowError converting the int to float. That is raised inside
    _tick(), so _poll_loop()'s catch-all would log it every single tick
    and forward sync would be permanently broken. At the 30s ceiling it
    takes only ~8.5 hours of a persistently failing push (a rig parked
    outside every band the receiver covers, say) to get there."""
    backoff = _ForwardPushBackoff()
    for failures in (1024, 5000):
        backoff.failures = failures - 1
        backoff.record_failure()  # must not raise
        assert backoff.next_attempt_at - time.monotonic() <= FORWARD_PUSH_BACKOFF_MAX_S + 0.5


def test_forward_sync_paused_blocks_both_axes_and_unpausing_fires_the_pending_push():
    """GUI rewrite's Pause sync toggle (rig -> WebSDR direction only).
    Counterpart of test_engine_reverse_sync.py's
    test_reverse_sync_held_suppresses_the_whole_reverse_tick(), for the
    forward direction. Pending freq/mode bookkeeping must keep
    accumulating while paused (no latch-clearing, unlike Hold) so
    unpausing fires an immediate corrective push through the ordinary
    debounce/threshold check rather than needing a resume step of its
    own."""
    engine = make_engine()
    stub_rig = StubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    stub_driver = StubDriver()
    engine._rig = stub_rig
    engine._rig_active = True
    engine._driver = stub_driver
    engine._websdr_active = True
    engine._forward_sync_paused = True

    async def run_while_paused_then_unpause():
        await engine._tick()  # baseline tick, nothing pending yet
        stub_rig.state = RigState(freq_hz=14075000, mode="CW", passband_hz=500, ptt=False)
        engine._pending_freq_since -= 1.0  # would clear the debounce window
        _clear_websdr_write_gap(engine)
        await engine._tick()  # still paused -- must be a complete no-op push-wise
        assert stub_driver.tuned == []
        assert stub_driver.modes == []

        engine._forward_sync_paused = False
        engine._pending_freq_since -= 1.0
        _clear_websdr_write_gap(engine)
        await engine._tick()  # unpaused -- the pending target must go out now

    asyncio.run(run_while_paused_then_unpause())

    assert stub_driver.tuned == [14075000]
    assert stub_driver.modes == [("CW", 500)]


def test_set_forward_sync_paused_from_other_thread_sets_directly_before_loop_starts():
    """Mirrors set_reverse_sync_held's own before-loop-starts contract:
    with no running loop yet, the setter must write the field directly
    rather than silently drop the call (there is nothing to
    call_soon_threadsafe onto)."""
    engine = make_engine()
    assert engine._loop is None
    engine.set_forward_sync_paused_from_other_thread(True)
    assert engine._forward_sync_paused is True
    engine.set_forward_sync_paused_from_other_thread(False)
    assert engine._forward_sync_paused is False

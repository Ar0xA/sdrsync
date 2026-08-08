"""Engine-level: v11 reverse sync (WebSDR -> rig) dedupe/loop-prevention,
using scripted stub rig/driver objects, same style as
test_engine_mode_independence.py/test_engine_mute_on_tx.py.

Covers: an echo of the engine's own just-pushed value must NOT trigger a
reverse push; a genuinely new value must trigger exactly one
set_freq/set_mode call, correctly debounced; a value observed while
transmitting must be fully suppressed; the first observation after
attach is a baseline (not pushed).

Note on sequencing: the engine's own first tick after attach always
forward-pushes the rig's initial mode (freq arms but doesn't push until
its own debounce elapses) -- and a successful forward push resets the
REVERSE_HOLDOFF_S window from inside that same tick, so reverse sync
never runs on that first tick. _settle_and_capture_baseline() drives
enough ticks (clearing each direction's own debounce as needed) to reach
a tick where nothing new needs forward-pushing, which is when the
reverse baseline actually gets captured -- this mirrors real engine
behavior, not a test shortcut."""
import asyncio
import queue

from sdrsync.config import AppSettings
from sdrsync.rig.rigctld import RigState
from sdrsync.sync.engine import (
    REVERSE_FREQ_CHANGE_THRESHOLD_HZ,
    REVERSE_FREQ_DEBOUNCE_S,
    REVERSE_HOLDOFF_S,
    SyncEngine,
)
from sdrsync.websdr.base import WebSDRStatus


class StubReverseRig:
    def __init__(self, state: RigState) -> None:
        self.state = state
        self.set_freqs: list[int] = []
        self.set_modes: list[tuple] = []
        self.set_freq_result = True
        self.set_mode_result = True

    async def ensure_connected(self) -> bool:
        return True

    async def get_state(self) -> RigState:
        return self.state

    async def close(self) -> None:
        pass

    async def set_freq(self, freq_hz: int) -> bool:
        self.set_freqs.append(freq_hz)
        return self.set_freq_result

    async def set_mode(self, mode_name: str, passband_hz) -> bool:
        self.set_modes.append((mode_name, passband_hz))
        return self.set_mode_result


class StubReverseDriver:
    """attached=True, and hamlib_mode_from_status()/rig_freq_from_status()
    that just pass the status's own fields through -- these tests are
    about engine-level dedupe/debounce logic, not per-driver
    mode-mapping/CW-offset (already covered by their own pure-function
    tests)."""

    def __init__(self, status: WebSDRStatus) -> None:
        self.attached = True
        self.status = status
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
        return self.status

    def hamlib_mode_from_status(self, status: WebSDRStatus):
        return status.mode

    def rig_freq_from_status(self, status: WebSDRStatus):
        return status.freq_hz

    async def close(self) -> None:
        pass


class _UnusedWebViewHost:
    async def create_page(self, loop, on_dead=None):
        raise AssertionError("not expected to be called in these tests")

    async def destroy_page(self, page) -> None:
        raise AssertionError("not expected to be called in these tests")


def make_engine() -> SyncEngine:
    settings = AppSettings()
    return SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())


def _clear_holdoff(engine: SyncEngine) -> None:
    engine._forward_push_completed_at -= REVERSE_HOLDOFF_S + 0.1


def _clear_reverse_debounce(engine: SyncEngine) -> None:
    engine._pending_reverse_freq_since -= REVERSE_FREQ_DEBOUNCE_S + 0.1


def _settle_and_capture_baseline(engine: SyncEngine) -> None:
    """Drives ticks until the initial forward push(es) are done and the
    reverse baseline has been captured -- see module docstring."""
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # mode forward-pushes; freq only arms
    engine._pending_freq_since -= 1.0
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # freq forward-pushes now
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # nothing left to forward-push -> baseline captured
    assert engine._reverse_baseline_captured is True


def test_first_observation_is_a_baseline_not_a_push():
    """The very first reverse-eligible tick after attach must seed the
    baseline, never push -- otherwise a WebSDR site's own default
    frequency could get pushed onto the real rig."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True

    _settle_and_capture_baseline(engine)

    assert stub_rig.set_freqs == []
    assert stub_rig.set_modes == []


def test_genuinely_new_freq_triggers_exactly_one_debounced_push():
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True
    _settle_and_capture_baseline(engine)

    # User clicks the waterfall to a new frequency.
    status.freq_hz = 14200000
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # arms the reverse debounce, no push yet
    assert stub_rig.set_freqs == []
    _clear_reverse_debounce(engine)
    _clear_holdoff(engine)

    asyncio.run(engine._tick())  # debounce elapsed -> pushes once
    assert stub_rig.set_freqs == [14200000]

    # Same (unchanged) value on a later tick must not push again.
    _clear_holdoff(engine)
    asyncio.run(engine._tick())
    assert stub_rig.set_freqs == [14200000]


def test_echo_of_own_pushed_value_does_not_trigger_a_reverse_push():
    """Right after _settle_and_capture_baseline()'s own forward pushes,
    reverse sync must still be inside the hold-off window and not act on
    the (unchanged, self-caused) status -- proves the hold-off mechanism
    actually gates on real elapsed time, not just tick count."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True

    # Deliberately do NOT clear the hold-off here -- immediately after the
    # forward pushes below, reverse sync must stay suppressed.
    asyncio.run(engine._tick())  # mode forward-pushes
    engine._pending_freq_since -= 1.0
    asyncio.run(engine._tick())  # freq forward-pushes
    assert stub_driver.modes == [("USB", 2700)]
    assert stub_driver.tuned == [14074000]

    asyncio.run(engine._tick())  # still within REVERSE_HOLDOFF_S
    assert stub_rig.set_freqs == []
    assert stub_rig.set_modes == []


def test_transmitting_fully_suppresses_reverse_push():
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True
    _settle_and_capture_baseline(engine)

    stub_rig.state.ptt = True
    status.freq_hz = 14200000
    _clear_holdoff(engine)
    asyncio.run(engine._tick())
    _clear_reverse_debounce(engine)
    _clear_holdoff(engine)
    asyncio.run(engine._tick())

    assert stub_rig.set_freqs == []


def test_genuinely_new_mode_triggers_exactly_one_push():
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True
    _settle_and_capture_baseline(engine)

    status.mode = "LSB"
    _clear_holdoff(engine)
    asyncio.run(engine._tick())
    # Base mode changed (USB -> LSB, StubReverseRig's own state.mode
    # never mutates) -> rig default passband (None), not the rig's
    # existing 2700.
    assert stub_rig.set_modes == [("LSB", None)]

    # Unchanged on the next tick must not re-push.
    _clear_holdoff(engine)
    asyncio.run(engine._tick())
    assert stub_rig.set_modes == [("LSB", None)]


def test_reverse_mode_push_reuses_rig_passband_when_base_mode_unchanged():
    """engine.py's passband decision: reuse the rig's own current
    passband_hz when the reverse-mapped mode's base is unchanged from
    what the rig already reports (only narrow/wide varies) -- 0/None
    only when the base mode itself changes (covered by
    test_genuinely_new_mode_triggers_exactly_one_push above). Calls
    _reverse_sync_tick() directly to isolate this one decision from the
    discrete-event gating already covered elsewhere."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=1800, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._driver = stub_rig, stub_driver
    engine._reverse_baseline_captured = True
    engine._last_observed_mode_key = "LSB"
    engine._last_pushed_to_websdr_mode = "LSB"

    asyncio.run(engine._reverse_sync_tick(status, stub_rig.state))

    assert stub_rig.set_modes == [("USB", 1800)]


class OutOfBandDriver(StubReverseDriver):
    """A driver whose forward tune_hz()/set_mode() always fail, e.g. the
    real websdr_org.py behavior when the rig's frequency is outside every
    band the WebSDR covers."""

    async def tune_hz(self, freq_hz: int) -> bool:
        self.tuned.append(freq_hz)
        return False

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        self.modes.append((hamlib_mode, passband_hz))
        return False


def test_baseline_freq_prevents_yank_to_websdr_default_after_forward_failure():
    """Regression for a real bug found by independent review: without
    also gating on _last_observed_freq (not just _last_pushed_to_websdr_freq,
    which stays None when the forward push never succeeds), the WebSDR
    site's own default frequency -- sitting there because the forward
    push legitimately can't reach it -- got pushed onto the real rig,
    retuning it away from a completely unrelated band."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=145500000, mode="FM", passband_hz=15000, ptt=False))
    # WebSDR's own default -- the rig (2m FM) can never reach this via a
    # forward push, so it just sits there, unrelated to anything the
    # user did.
    status = WebSDRStatus(connected=True, freq_hz=7100000, mode="USB")
    stub_driver = OutOfBandDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True

    for _ in range(5):
        _clear_holdoff(engine)
        asyncio.run(engine._tick())

    assert stub_rig.set_freqs == []
    assert stub_rig.set_modes == []


def test_rejected_freq_reverse_push_is_not_retried_every_tick():
    """Regression: a persistently-rejected value must be latched as
    'already considered' so it doesn't re-fire set_freq() (and, for
    flrig, its own poll-until-match budget) every single tick forever."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    stub_rig.set_freq_result = False
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True
    _settle_and_capture_baseline(engine)

    status.freq_hz = 30000000  # rejected by the rig every time it's tried
    for _ in range(5):
        _clear_holdoff(engine)
        _clear_reverse_debounce(engine)
        asyncio.run(engine._tick())

    assert stub_rig.set_freqs.count(30000000) == 1


def test_forward_push_echo_with_collapsed_mode_does_not_bounce_back():
    """Regression for a real bug found by independent review:
    _last_pushed_to_websdr_mode stored the rig-native mode name (e.g.
    'PKTUSB'), but the reverse-mapped observed mode is always the
    canonical, WebSDR-normalized form (e.g. 'USB') -- for every
    many-to-one forward mapping those strings could never compare equal,
    so a forward push's own echo was misread as a fresh user edit and
    bounced a collapsed-but-different mode back to the rig, silently
    overwriting e.g. a chosen data/CW-variant mode with plain USB/CW."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True
    _settle_and_capture_baseline(engine)

    # Rig switches to a mode that forward-collapses to the SAME WebSDR
    # string ("USB") -- the real driver would apply it and the page
    # would still read "USB", but the rig's own reported mode is now
    # "PKTUSB", not "USB".
    stub_rig.state.mode = "PKTUSB"
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # forward push applies PKTUSB; page stays "USB"
    assert stub_driver.modes[-1] == ("PKTUSB", 2700)

    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # reseed tick: page still "USB" -> nothing to push
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # fully settled -> still nothing to push

    assert stub_rig.set_modes == []


def test_ptt_none_inherits_last_known_state_for_reverse_gate():
    """Regression: a single transient PTT-read failure (state.ptt is
    None) mid-transmission must not be treated as 'now receiving' by the
    reverse gate -- it must inherit the last known PTT value, same as
    the forward direction's own transmitting flag does."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # establishes _last_ptt = True (transmitting)

    stub_rig.state.ptt = None  # transient read failure, still actually mid-TX
    status.freq_hz = 14200000
    _clear_holdoff(engine)
    asyncio.run(engine._tick())
    _clear_reverse_debounce(engine)
    _clear_holdoff(engine)
    asyncio.run(engine._tick())

    assert stub_rig.set_freqs == []


class UnmappedModeDriver(StubReverseDriver):
    def hamlib_mode_from_status(self, status: WebSDRStatus):
        return None if status.mode == "IQ" else status.mode


def test_unmapped_reverse_mode_logs_and_still_reverse_syncs_frequency():
    """An unmapped WebSDR mode must not crash or block frequency reverse
    sync -- mirrors the forward direction's established mode/frequency
    independence (test_engine_mode_independence.py)."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = UnmappedModeDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True
    _settle_and_capture_baseline(engine)

    status.mode = "IQ"
    status.freq_hz = 14200000
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # mode unmapped -> no mode push; freq arms
    assert stub_rig.set_modes == []
    _clear_reverse_debounce(engine)
    _clear_holdoff(engine)
    asyncio.run(engine._tick())

    assert stub_rig.set_freqs == [14200000]


def test_concurrent_reset_during_await_wins_over_stale_reverse_push():
    """Regression: _attach_supervisor can call _reset_sync_latches() from
    a different task on the same loop while _reverse_sync_tick() is
    awaiting rig.set_freq()/set_mode() -- the reset must win, not get
    silently overwritten once the stale await finally returns."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True
    _settle_and_capture_baseline(engine)

    status.freq_hz = 14200000
    _clear_holdoff(engine)
    asyncio.run(engine._tick())  # arms the reverse debounce
    _clear_reverse_debounce(engine)
    _clear_holdoff(engine)

    async def set_freq_that_resets(freq_hz):
        # Simulates the attach supervisor's _reset_sync_latches() landing
        # while this call is in flight.
        engine._reset_sync_latches()
        stub_rig.set_freqs.append(freq_hz)
        return True

    stub_rig.set_freq = set_freq_that_resets
    asyncio.run(engine._tick())

    assert engine._reverse_baseline_captured is False


def test_stale_mode_rejection_warning_self_heals_once_rig_catches_up():
    """Regression: a reverse mode push that set_mode() reported as
    rejected (its own verify budget exhausted) can still, on real
    hardware, land slightly later -- the next regular poll must clear
    the stale warning once the rig's own state.mode confirms it,
    instead of leaving a false "rejected" alarm displayed forever."""
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True

    # Simulate an earlier tick that flagged a mode rejection.
    engine._last_reverse_mode_reject_key = "LSB"
    engine._reverse_sync_error = "Rig rejected mode 'LSB' pushed from WebSDR"

    # The rig now reports the previously-rejected mode -- it caught up.
    stub_rig.state.mode = "LSB"
    _clear_holdoff(engine)
    asyncio.run(engine._tick())

    assert engine._last_reverse_mode_reject_key is None
    assert engine._reverse_sync_error is None


def test_stale_freq_rejection_warning_self_heals_once_rig_catches_up():
    engine = make_engine()
    stub_rig = StubReverseRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    status = WebSDRStatus(connected=True, freq_hz=14074000, mode="USB")
    stub_driver = StubReverseDriver(status)
    engine._rig, engine._rig_active, engine._driver, engine._websdr_active = stub_rig, True, stub_driver, True

    engine._last_reverse_freq_reject_key = 14200000
    engine._reverse_sync_error = "Rig rejected frequency 14200000 Hz from WebSDR"

    # Within tolerance of the previously-rejected value -- it caught up.
    stub_rig.state.freq_hz = 14200000 + REVERSE_FREQ_CHANGE_THRESHOLD_HZ
    _clear_holdoff(engine)
    asyncio.run(engine._tick())

    assert engine._last_reverse_freq_reject_key is None
    assert engine._reverse_sync_error is None

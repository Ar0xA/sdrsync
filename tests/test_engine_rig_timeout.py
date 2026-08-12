"""Engine-level: an unreachable rig must eventually give up instead of
leaving the GUI stuck at "connecting..." forever (v9 item 6) -- and the
connect budget only applies to the time-to-FIRST-connect, not every
reconnect attempt after a real drop (v9 item 6's design note)."""
import asyncio
import queue
import time

from sdrsync.config import AppSettings
from sdrsync.rig.rigctld import RigState
from sdrsync.sync.engine import RIG_CONNECT_TIMEOUT_S, SyncEngine


class NeverConnectsRig:
    async def ensure_connected(self) -> bool:
        return False

    async def get_state(self) -> RigState:
        raise AssertionError("get_state() should never be called if ensure_connected() failed")

    async def close(self) -> None:
        pass


class AlwaysConnectsRig:
    def __init__(self) -> None:
        self.ensure_connected_calls = 0

    async def ensure_connected(self) -> bool:
        self.ensure_connected_calls += 1
        return True

    async def get_state(self) -> RigState:
        return RigState(freq_hz=None, mode=None, passband_hz=None, ptt=None)

    async def close(self) -> None:
        pass


class _UnusedWebViewHost:
    async def create_page(self, loop, on_dead=None):
        raise AssertionError("not expected to be called in these tests")

    async def destroy_page(self, page, loop=None) -> None:
        raise AssertionError("not expected to be called in these tests")


def make_engine() -> SyncEngine:
    settings = AppSettings()
    return SyncEngine(settings, status_queue=queue.Queue(), webview_host=_UnusedWebViewHost())


def test_gives_up_once_the_connect_deadline_has_passed():
    engine = make_engine()
    engine._rig = NeverConnectsRig()
    engine._rig_active = True
    engine._rig_backend, engine._rig_host, engine._rig_port = "rigctld", "10.0.0.1", 4532
    # Deadline already in the past -- avoids an actual 30s sleep in the test.
    engine._rig_connect_deadline = time.monotonic() - 1.0

    asyncio.run(engine._tick())

    assert engine._rig_active is False
    assert engine._rig is None
    assert engine._rig_error is not None
    assert "10.0.0.1:4532" in engine._rig_error
    assert f"{RIG_CONNECT_TIMEOUT_S:.0f}s" in engine._rig_error


def test_does_not_give_up_before_the_deadline():
    engine = make_engine()
    engine._rig = NeverConnectsRig()
    engine._rig_active = True
    engine._rig_backend, engine._rig_host, engine._rig_port = "rigctld", "10.0.0.1", 4532
    engine._rig_connect_deadline = time.monotonic() + 100.0  # far in the future

    asyncio.run(engine._tick())

    assert engine._rig_active is True
    assert engine._rig is not None
    assert engine._rig_error is None


def test_successful_connect_clears_the_deadline():
    engine = make_engine()
    rig = AlwaysConnectsRig()
    engine._rig = rig
    engine._rig_active = True
    engine._rig_connect_deadline = time.monotonic() + 100.0

    asyncio.run(engine._tick())

    assert engine._rig_connect_deadline is None
    assert rig.ensure_connected_calls == 1


def test_rig_generation_lets_gui_distinguish_backlog_from_a_new_connect_attempt():
    """Regression test for a live bug: while the rig is idle after a
    failed connect, _tick() keeps publishing a fresh snapshot every poll
    carrying the same self._rig_error (see that field's own docstring in
    engine.py), so the GUI's status_queue can be holding a backlog of
    stale snapshots from the FAILED attempt at the exact moment the user
    starts flrig/rigctld and clicks Connect again. The GUI resets its
    one-shot error-popup guard synchronously on click, but
    start_rig_from_other_thread() only runs the real _start_rig() call
    later, asynchronously, on the engine's own thread -- without a
    generation tag on every snapshot, one of those backlog snapshots
    gets drained right after the click and re-pops "connection failed"
    for an attempt that hasn't even started yet, even though the new
    attempt goes on to succeed moments later (confirmed live).

    This pins the engine-side half of the fix: every snapshot from one
    _start_rig() attempt -- including its eventual give-up -- shares ONE
    generation, and the next _start_rig() call strictly bumps it before
    publishing anything else, so the GUI can filter out any snapshot
    whose generation isn't newer than what it had already seen before
    the click."""
    engine = make_engine()

    async def run():
        await engine._start_rig("rigctld", "10.0.0.1", 4532, use_mock=False)
        first_generation = engine._rig_generation
        assert first_generation == 1

        # Swap in a stub post-construction -- avoids relying on real
        # networking/timing to reach the give-up branch; _start_rig()'s
        # own bookkeeping (generation bump, deadline, publish) already ran
        # for real above, which is the part under test.
        engine._rig = NeverConnectsRig()
        engine._rig_connect_deadline = time.monotonic() - 1.0  # already past
        await engine._tick()
        assert engine._rig_active is False
        assert engine._rig_error is not None
        # The give-up itself must NOT bump the generation -- it's still
        # reporting the outcome of the SAME attempt the click started.
        assert engine._rig_generation == first_generation

        # Drain every snapshot _start_rig()/_tick() published above --
        # this is exactly the backlog that could still be sitting in
        # status_queue when the user clicks Connect again.
        backlog = []
        while True:
            try:
                backlog.append(engine.status_queue.get_nowait())
            except queue.Empty:
                break
        assert backlog
        assert all(snap.rig_generation == first_generation for snap in backlog)

        # A fresh Connect click starts a new attempt -- its very first
        # published snapshot must carry a STRICTLY greater generation
        # than every backlog snapshot above, which is what lets the GUI
        # tell them apart regardless of queue timing.
        await engine._start_rig("rigctld", "10.0.0.1", 4532, use_mock=False)
        assert engine._rig_generation == first_generation + 1
        new_snap = engine.status_queue.get_nowait()
        assert new_snap.rig_generation == first_generation + 1
        assert new_snap.rig_generation > max(s.rig_generation for s in backlog)

    asyncio.run(run())

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

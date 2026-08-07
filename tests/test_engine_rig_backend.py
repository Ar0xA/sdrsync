"""_start_rig() must construct the right client class for the selected
backend, and switching backends must tear down the previous rig client
cleanly -- mirrors test_engine_switch_site.py's pattern (stub webview
host, no real sockets/browser). Neither RigctldClient nor FlrigClient
connects anything at construction time, so _start_rig(use_mock=False)
can be exercised directly without hitting the network."""
import asyncio
import queue

from sdrsync.config import AppSettings
from sdrsync.rig.flrig import FlrigClient
from sdrsync.rig.rigctld import RigctldClient
from sdrsync.sync.engine import SyncEngine


class StubWebViewHost:
    async def create_page(self, loop, on_dead=None):
        raise AssertionError("not expected to be called in these tests")

    async def destroy_page(self, page) -> None:
        raise AssertionError("not expected to be called in these tests")


def make_engine() -> SyncEngine:
    settings = AppSettings()
    return SyncEngine(settings, status_queue=queue.Queue(), webview_host=StubWebViewHost())


def test_start_rig_rigctld_backend_constructs_rigctld_client():
    engine = make_engine()

    asyncio.run(engine._start_rig("rigctld", "127.0.0.1", 4532, False))

    assert isinstance(engine._rig, RigctldClient)
    assert engine._rig_active is True


def test_start_rig_flrig_backend_constructs_flrig_client():
    engine = make_engine()

    asyncio.run(engine._start_rig("flrig", "127.0.0.1", 12345, False))

    assert isinstance(engine._rig, FlrigClient)
    assert engine._rig_active is True


def test_switching_backend_replaces_the_client():
    engine = make_engine()

    async def run():
        await engine._start_rig("rigctld", "127.0.0.1", 4532, False)
        first_rig = engine._rig
        assert isinstance(first_rig, RigctldClient)

        await engine._start_rig("flrig", "127.0.0.1", 12345, False)

        assert isinstance(engine._rig, FlrigClient)
        assert engine._rig is not first_rig

    asyncio.run(run())

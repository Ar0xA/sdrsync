"""SyncEngine._poll_loop() must honor AppSettings.poll_interval_s (and pick
up changes live, since the GUI updates it without requiring a reconnect --
see gui/app.py's _on_poll_interval_changed). Uses the same stub pattern as
test_engine_switch_site.py."""
import asyncio
import queue

from sdrsync.config import AppSettings
from sdrsync.sync.engine import SyncEngine


class StubWebViewHost:
    async def create_page(self, loop, on_dead=None):
        raise AssertionError("not expected to be called in these tests")

    async def destroy_page(self, page) -> None:
        raise AssertionError("not expected to be called in these tests")


def make_engine(poll_interval_s: float) -> SyncEngine:
    settings = AppSettings(poll_interval_s=poll_interval_s)
    return SyncEngine(settings, status_queue=queue.Queue(), webview_host=StubWebViewHost())


def test_poll_loop_ticks_faster_with_a_shorter_interval(monkeypatch):
    tick_count = 0

    async def counting_tick(self):
        nonlocal tick_count
        tick_count += 1

    monkeypatch.setattr(SyncEngine, "_tick", counting_tick)
    engine = make_engine(poll_interval_s=0.02)

    async def run():
        task = asyncio.ensure_future(engine._poll_loop())
        await asyncio.sleep(0.15)
        engine.stop_event.set()
        await task

    asyncio.run(run())

    # ~0.15s / 0.02s tick interval -- comfortably more than a 0.2s default
    # interval would ever produce in the same window, without being so
    # tight a test that normal scheduling jitter flakes it.
    assert tick_count >= 4


def test_poll_loop_picks_up_a_live_settings_change(monkeypatch):
    """The GUI updates settings.poll_interval_s directly (no engine
    restart) -- _poll_loop must read it fresh each iteration, not cache it
    at construction/loop-start time. Note: a change only takes effect once
    the *current* wait completes (asyncio.wait_for's timeout is fixed at
    call time), not mid-wait -- so this waits out a full old-interval
    cycle before checking the new, faster rate actually kicked in."""
    tick_count = 0

    async def counting_tick(self):
        nonlocal tick_count
        tick_count += 1

    monkeypatch.setattr(SyncEngine, "_tick", counting_tick)
    engine = make_engine(poll_interval_s=0.1)  # starts slower

    async def run():
        task = asyncio.ensure_future(engine._poll_loop())
        await asyncio.sleep(0.03)
        ticks_while_slow = tick_count
        engine.settings.poll_interval_s = 0.02  # GUI-style live update
        # Long enough to outlast one full old-interval (0.1s) cycle plus
        # several new-interval (0.02s) cycles afterward.
        await asyncio.sleep(0.35)
        engine.stop_event.set()
        await task
        return ticks_while_slow

    ticks_while_slow = asyncio.run(run())

    assert ticks_while_slow <= 1  # only the immediate first tick so far
    assert tick_count >= ticks_while_slow + 5  # sped up once the setting change was picked up


def test_poll_interval_field_defaults_match_previous_hardcoded_value():
    assert AppSettings().poll_interval_s == 0.2

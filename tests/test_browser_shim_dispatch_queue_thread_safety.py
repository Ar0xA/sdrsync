"""WxPageAdapter._dispatch_queue is mutated from two different threads:
_on_script_result() runs on the GUI thread (every real wx event does).
_fail_pending() (called from _mark_dead()) is also reachable from the
BACKGROUND asyncio thread -- via _await_ready()'s and _run_script()'s own
timeout paths, and via close() (all called from sync/engine.py's
coroutines, never the GUI thread). Before the fix, _on_script_result()'s
"if not self._dispatch_queue: return" check followed by a separate
popleft() were two unsynchronized steps -- a concurrent _fail_pending()
drain landing between them raised IndexError out of a wx event handler.

Deliberately NOT using test_browser_shim_run_script.py's single-threaded
wx.CallAfter-pump harness -- this is a genuine multi-thread data race that
an asyncio-ordering simulation on one thread cannot reproduce.
"""
import asyncio
import sys
import threading
import time
from collections import deque

import wx

from sdrsync.websdr.browser_shim import BrowserError, WxPageAdapter


class _FakeWebview:
    def Bind(self, *args, **kwargs) -> None:
        pass

    def AddScriptMessageHandler(self, name: str) -> bool:
        return True

    def AddUserScript(self, js: str) -> bool:
        return True

    def RunScriptAsync(self, script: str) -> None:
        pass


class _FakeScriptResultEvent:
    def IsError(self) -> bool:
        return False

    def GetString(self) -> str:
        return "ok"


class _RaceyDeque(deque):
    """A deque whose FIRST popleft() call fires a concurrent
    _fail_pending() drain (on a real second thread) before actually
    popping, then waits briefly -- deterministically reproducing "another
    thread emptied the queue between the truthiness check and the pop"
    instead of hoping real thread scheduling happens to land in a
    several-bytecode-wide window. Only fires once, so _fail_pending()'s
    OWN popleft() calls (when unsynchronized) don't recurse into this."""

    def __init__(self, *args, on_first_popleft=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_first_popleft = on_first_popleft

    def popleft(self):
        cb, self._on_first_popleft = self._on_first_popleft, None
        if cb is not None:
            cb()
        return super().popleft()


def _make_adapter(monkeypatch) -> tuple[WxPageAdapter, asyncio.AbstractEventLoop]:
    monkeypatch.setattr(wx, "IsMainThread", lambda: True)
    loop = asyncio.new_event_loop()
    adapter = WxPageAdapter(_FakeWebview(), loop, already_created=True)
    return adapter, loop


def test_concurrent_fail_pending_during_on_script_result_check_does_not_corrupt_queue(monkeypatch):
    """Deterministic reproduction of the original bug's exact shape: a
    concurrent drain lands between _on_script_result()'s "is there
    anything queued" check and its popleft(). Must not raise IndexError
    -- the lock added by the fix serializes the two, so the background
    thread's drain simply waits its turn instead of interleaving."""
    adapter, loop = _make_adapter(monkeypatch)
    try:
        fut = loop.create_future()
        fut.cancel()  # already done() -- _fail_pending won't need a running loop

        drain_thread_started = threading.Event()

        def fire_concurrent_drain() -> None:
            def run_drain() -> None:
                drain_thread_started.set()
                adapter._fail_pending(BrowserError("concurrent drain"))

            threading.Thread(target=run_drain, daemon=True).start()
            # Give the background thread a real chance to run: with the
            # fix's lock in place, it will be blocked trying to acquire
            # the SAME lock this thread (inside _on_script_result) is
            # currently holding -- so it can't actually drain anything
            # yet, no matter how long we wait here.
            drain_thread_started.wait(timeout=2.0)
            time.sleep(0.2)

        adapter._dispatch_queue = _RaceyDeque([fut], on_first_popleft=fire_concurrent_drain)

        # Must not raise, and must return the entry that was really there
        # -- proving the concurrent drain was serialized after this call,
        # not interleaved into the middle of it.
        adapter._on_script_result(_FakeScriptResultEvent())
    finally:
        loop.close()


def test_dispatch_queue_survives_concurrent_gui_and_background_thread_access(monkeypatch):
    """Coarser best-effort smoke test alongside the deterministic one
    above: real sustained concurrent access from multiple threads must
    never raise, at a high GIL switch rate to maximize the chance of
    catching anything the deterministic test's specific shape doesn't."""
    adapter, loop = _make_adapter(monkeypatch)
    original_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        stop = threading.Event()
        errors: list[BaseException] = []
        ITERATIONS = 20000

        def gui_thread_worker() -> None:
            evt = _FakeScriptResultEvent()
            try:
                while not stop.is_set():
                    adapter._on_script_result(evt)
            except BaseException as e:  # noqa: BLE001 -- must catch everything to report it
                errors.append(e)
                stop.set()

        def background_thread_worker() -> None:
            try:
                while not stop.is_set():
                    adapter._fail_pending(BrowserError("stress"))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
                stop.set()

        def refill_worker() -> None:
            # Keeps the queue non-empty so both racer threads actually
            # contend over real entries instead of idling on an empty
            # deque. Every future is pre-cancelled (already done()) so
            # neither racer ever needs a RUNNING event loop to resolve it
            # via call_soon_threadsafe. Appends directly (not through the
            # adapter's own locked do_run() path, and deliberately not
            # taking the lock itself) -- a single deque.append() is its
            # own atomic operation either way; this thread's job is purely
            # to keep supplying contention for the other two, not to be
            # part of what's under test.
            try:
                for _ in range(ITERATIONS):
                    if stop.is_set():
                        break
                    fut = loop.create_future()
                    fut.cancel()
                    adapter._dispatch_queue.append(fut)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
            finally:
                stop.set()

        threads = [
            threading.Thread(target=gui_thread_worker),
            threading.Thread(target=background_thread_worker),
            threading.Thread(target=refill_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "a worker thread hung"

        assert errors == [], f"cross-thread access to _dispatch_queue raised: {errors!r}"
    finally:
        sys.setswitchinterval(original_interval)
        loop.close()

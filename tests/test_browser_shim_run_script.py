"""WxPageAdapter._run_script()/_on_script_result() against a fake webview
widget -- covers the FIFO dispatch-queue bookkeeping, which is pure Python
logic and doesn't need a real wx.html2.WebView to exercise.

Regression guard for a real bug found by a code review pass: RunScriptAsync
can't be cancelled once dispatched, and _on_script_result() had no way to
tell an abandoned script's late result apart from the currently-pending
call's result (it just trusted whatever self._pending_script_future
pointed at). _run_script() cleared that attribute on a TIMEOUT (via
_mark_dead(), which kills the whole adapter -- appropriate there, since a
timeout means something is actually broken) but NOT on a plain
asyncio.CancelledError (routine -- e.g. SwitchWebsdr cancelling an
in-progress attach, or a superseded frequency-verify task), which is a
completely normal, frequent occurrence that must not kill the adapter.
Left unfixed: a later, unrelated call's _on_script_result event would
silently receive the abandoned call's result instead of its own, and that
later call's real (correct) result would then arrive with nothing left
pending to claim it and be silently dropped.

This went through TWO design iterations, both caught by review passes
against this exact test file, worth recording so a future change doesn't
reintroduce either:

  1. A single "pending future" slot plus a separate integer "orphan
     credit" counter, incremented from _run_script()'s except
     CancelledError block (the asyncio-loop thread) and decremented from
     _on_script_result() (the wx GUI thread) -- a genuine, traced (if
     narrow) cross-thread race on the counter.
  2. Moving the credit bookkeeping into do_run() (GUI thread, same as
     _on_script_result(), so no cross-thread race) fixed that, but only
     recorded a credit if the caller was ALREADY cancelled at the moment
     do_run() ran RunScriptAsync -- cancellation landing AFTER dispatch
     (the much wider window: the whole script round-trip, not just
     wx.CallAfter's queue latency) recorded nothing, so that script's
     late result was still delivered to whatever call was next in line.
     The regression test for that case at the time only ever simulated
     ONE result event for two dispatched scripts, so it passed whether or
     not the bug was present -- a tautological guard.

The actual fix (self._dispatch_queue, a deque appended to by do_run()
after every RunScriptAsync call that succeeds, popped from the left by
_on_script_result()) doesn't need to know or care WHEN a caller was
cancelled relative to dispatch -- every successful dispatch is enqueued
unconditionally, in order, and a popped future that's already done()
(cancelled or otherwise) is just silently discarded in its correct FIFO
position. That symmetry is exactly what the tests below are checking:
both cancellation orderings, and multiple real script deliveries in a
row, go through the identical enqueue-then-pop path with no special
casing to get wrong.
"""
import asyncio

import wx

from sdrsync.websdr.browser_shim import WxPageAdapter


class _FakeWebview:
    """Just enough of wx.html2.WebView's surface for WxPageAdapter's
    __init__ and _run_script()/_on_script_result() -- no real widget, no
    real WebView2/WebKitGTK backend. RunScriptAsync is fire-and-forget,
    same as the real one: it only records the call, the test itself
    decides when (and whether) to simulate the matching result event."""

    def __init__(self) -> None:
        self.run_script_calls: list[str] = []

    def Bind(self, event, handler) -> None:
        pass

    def AddScriptMessageHandler(self, name: str) -> bool:
        return True

    def AddUserScript(self, js: str) -> bool:
        return True

    def RunScriptAsync(self, script: str) -> None:
        self.run_script_calls.append(script)


class _FakeScriptResultEvent:
    def __init__(self, result: str) -> None:
        self._result = result

    def IsError(self) -> bool:
        return False

    def GetString(self) -> str:
        return self._result


class _GuiThreadQueue:
    """Stand-in for wx's real GUI-thread event/callback queue. wx.CallAfter
    is monkeypatched to push onto this instead of running immediately --
    callbacks only actually run when the test calls pump(), which is how
    these tests control exactly when a dispatch "happens on the GUI
    thread" relative to an asyncio-side cancellation."""

    def __init__(self) -> None:
        self._pending: list = []

    def push(self, fn, args, kwargs) -> None:
        self._pending.append((fn, args, kwargs))

    def pump(self) -> None:
        pending, self._pending = self._pending, []
        for fn, args, kwargs in pending:
            fn(*args, **kwargs)


def _make_adapter(monkeypatch, loop) -> tuple[WxPageAdapter, _FakeWebview, _GuiThreadQueue]:
    gui_queue = _GuiThreadQueue()
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: gui_queue.push(fn, a, kw))
    webview = _FakeWebview()
    adapter = WxPageAdapter(webview, loop, already_created=True)
    # WxPageAdapter.__init__ itself dispatches nothing through CallAfter
    # (its ready-setup runs inline for already_created=True), but pump any
    # incidental queuing anyway so each test starts from a clean queue.
    gui_queue.pump()
    return adapter, webview, gui_queue


async def _cancel(task: "asyncio.Task") -> None:
    task.cancel()
    try:
        await task
        assert False, "expected CancelledError"
    except asyncio.CancelledError:
        pass


def test_cancelling_before_dispatch_still_delivers_the_next_calls_result_correctly(monkeypatch):
    """Cancellation lands before do_run() ever reaches the GUI thread --
    the narrower of the two orderings, but still must not let A's
    eventual (late) result reach B."""
    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop)

        task_a = asyncio.ensure_future(adapter._run_script("SCRIPT_A"))
        await asyncio.sleep(0)  # _run_script acquires the lock and queues do_run
        assert webview.run_script_calls == []  # not dispatched yet -- still queued

        await _cancel(task_a)
        assert len(adapter._dispatch_queue) == 0  # do_run hasn't run yet -- nothing enqueued

        gui_queue.pump()  # wx's GUI thread finally gets to it, after the cancellation
        assert webview.run_script_calls == ["SCRIPT_A"]
        assert len(adapter._dispatch_queue) == 1

        task_b = asyncio.ensure_future(adapter._run_script("SCRIPT_B"))
        await asyncio.sleep(0)
        gui_queue.pump()
        assert webview.run_script_calls == ["SCRIPT_A", "SCRIPT_B"]
        assert len(adapter._dispatch_queue) == 2

        # Both real WebView2 events arrive, strictly FIFO -- A's (abandoned) first.
        adapter._on_script_result(_FakeScriptResultEvent("RESULT_OF_SCRIPT_A"))
        assert len(adapter._dispatch_queue) == 1
        assert not task_b.done()  # must NOT have been resolved by A's result

        adapter._on_script_result(_FakeScriptResultEvent("RESULT_OF_SCRIPT_B"))
        result_b = await asyncio.wait_for(task_b, timeout=1.0)
        assert result_b == "RESULT_OF_SCRIPT_B"
        assert len(adapter._dispatch_queue) == 0

    asyncio.run(run())


def test_cancelling_after_dispatch_still_delivers_the_next_calls_result_correctly(monkeypatch):
    """The ordering the earlier (counter-based) version of this fix got
    wrong: do_run() already ran -- SCRIPT_A is already enqueued and
    dispatched -- and ONLY THEN is the caller cancelled. Both of A's and
    B's real result events must still land on the correct call."""
    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop)

        task_a = asyncio.ensure_future(adapter._run_script("SCRIPT_A"))
        await asyncio.sleep(0)
        gui_queue.pump()  # do_run runs NOW, while task_a is still un-cancelled
        assert webview.run_script_calls == ["SCRIPT_A"]
        assert len(adapter._dispatch_queue) == 1

        await _cancel(task_a)
        # Cancelling task_a does not, and must not, remove SCRIPT_A's
        # already-enqueued future -- a real result for it is still coming.
        assert len(adapter._dispatch_queue) == 1

        task_b = asyncio.ensure_future(adapter._run_script("SCRIPT_B"))
        await asyncio.sleep(0)
        gui_queue.pump()
        assert webview.run_script_calls == ["SCRIPT_A", "SCRIPT_B"]
        assert len(adapter._dispatch_queue) == 2

        # THE regression: deliver BOTH events, in dispatch order. The
        # earlier buggy version only ever simulated one event here, which
        # is exactly why it didn't catch this.
        adapter._on_script_result(_FakeScriptResultEvent("RESULT_OF_SCRIPT_A"))
        assert not task_b.done()  # A's result must not have resolved B

        adapter._on_script_result(_FakeScriptResultEvent("RESULT_OF_SCRIPT_B"))
        result_b = await asyncio.wait_for(task_b, timeout=1.0)
        assert result_b == "RESULT_OF_SCRIPT_B"

    asyncio.run(run())


def test_multiple_cancellations_then_a_real_call_all_resolve_in_fifo_order(monkeypatch):
    """Several cancelled calls (mixing both orderings above) followed by
    a real one -- every abandoned result must be discarded in its correct
    position before the real one is accepted."""
    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop)

        # A: cancelled before dispatch.
        task_a = asyncio.ensure_future(adapter._run_script("SCRIPT_A"))
        await asyncio.sleep(0)
        await _cancel(task_a)
        gui_queue.pump()

        # B: cancelled after dispatch.
        task_b = asyncio.ensure_future(adapter._run_script("SCRIPT_B"))
        await asyncio.sleep(0)
        gui_queue.pump()
        await _cancel(task_b)

        assert webview.run_script_calls == ["SCRIPT_A", "SCRIPT_B"]
        assert len(adapter._dispatch_queue) == 2

        task_c = asyncio.ensure_future(adapter._run_script("SCRIPT_C"))
        await asyncio.sleep(0)
        gui_queue.pump()
        assert webview.run_script_calls == ["SCRIPT_A", "SCRIPT_B", "SCRIPT_C"]

        adapter._on_script_result(_FakeScriptResultEvent("RESULT_OF_SCRIPT_A"))
        adapter._on_script_result(_FakeScriptResultEvent("RESULT_OF_SCRIPT_B"))
        assert not task_c.done()

        adapter._on_script_result(_FakeScriptResultEvent("RESULT_OF_SCRIPT_C"))
        result_c = await asyncio.wait_for(task_c, timeout=1.0)
        assert result_c == "RESULT_OF_SCRIPT_C"
        assert len(adapter._dispatch_queue) == 0

    asyncio.run(run())


def test_normal_uncancelled_call_still_gets_its_own_result(monkeypatch):
    """Baseline/contrast: no cancellation involved at all -- must keep
    working exactly as before."""
    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop)

        task = asyncio.ensure_future(adapter._run_script("SCRIPT_A"))
        await asyncio.sleep(0)
        gui_queue.pump()
        adapter._on_script_result(_FakeScriptResultEvent("RESULT_OF_SCRIPT_A"))
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == "RESULT_OF_SCRIPT_A"
        assert len(adapter._dispatch_queue) == 0

    asyncio.run(run())


def test_consecutive_real_calls_never_cross_wires(monkeypatch):
    """No cancellation anywhere -- three back-to-back real calls, each
    must get exactly its own result. Guards against an off-by-one in the
    FIFO pop/append pairing under ordinary (non-orphaned) traffic."""
    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop)

        results = []
        for script in ("SCRIPT_A", "SCRIPT_B", "SCRIPT_C"):
            task = asyncio.ensure_future(adapter._run_script(script))
            await asyncio.sleep(0)
            gui_queue.pump()
            adapter._on_script_result(_FakeScriptResultEvent(f"RESULT_OF_{script}"))
            results.append(await asyncio.wait_for(task, timeout=1.0))

        assert results == ["RESULT_OF_SCRIPT_A", "RESULT_OF_SCRIPT_B", "RESULT_OF_SCRIPT_C"]
        assert len(adapter._dispatch_queue) == 0

    asyncio.run(run())


def test_runscriptasync_raising_does_not_enqueue_anything(monkeypatch):
    """If RunScriptAsync itself throws (e.g. the widget went away), no
    wxEVT_WEBVIEW_SCRIPT_RESULT is ever coming for that dispatch --
    nothing must be added to the queue for it, whether or not the caller
    was already cancelled, or a later call's real result would
    permanently pop the wrong (never-coming) entry first."""
    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop)

        def raise_run_script_async(script):
            raise RuntimeError("widget is gone")

        webview.RunScriptAsync = raise_run_script_async

        task_a = asyncio.ensure_future(adapter._run_script("SCRIPT_A"))
        await asyncio.sleep(0)
        await _cancel(task_a)

        gui_queue.pump()  # do_run runs, RunScriptAsync raises
        assert len(adapter._dispatch_queue) == 0

    asyncio.run(run())


def test_mark_dead_fails_every_still_queued_dispatch(monkeypatch):
    """_fail_pending() (via _mark_dead(), e.g. a script timeout) must
    drain the WHOLE queue, not just one slot -- an abandoned call from
    earlier must not be left dangling forever once the adapter itself
    is torn down."""
    async def run():
        loop = asyncio.get_running_loop()
        adapter, webview, gui_queue = _make_adapter(monkeypatch, loop)

        task_a = asyncio.ensure_future(adapter._run_script("SCRIPT_A"))
        await asyncio.sleep(0)
        gui_queue.pump()
        await _cancel(task_a)  # abandoned, but still enqueued (real result never simulated)

        assert len(adapter._dispatch_queue) == 1

        adapter._mark_dead("simulated failure")
        assert len(adapter._dispatch_queue) == 0
        assert adapter._alive is False

    asyncio.run(run())

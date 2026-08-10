"""wx.html2.WebView-backed shim implementing the narrow subset of
Playwright's Page API the three WebSDR drivers actually use, so those
driver modules keep working with only an import-line change after the
Playwright -> wxPython migration.

See the "Block B design" (and its two review-driven revisions -- a plan
review and an implementation review, each of which found and fixed real
bugs by testing live against the real wx API rather than trusting the
API docs) in the v5 migration plan for the reasoning behind every
non-obvious choice here:
C:\\Users\\ABEL75\\.claude\\plans\\rippling-roaming-forest.md

Load-bearing facts this module relies on (verified live, not assumed):
    - wx.html2's EVT_WEBVIEW_SCRIPT_RESULT carries no usable request-id
      (GetInt() is just the success/fail flag, clientData never round-
      trips) -- calls are serialized per adapter with a lock instead of
      id-correlated, relying on WebView2's confirmed strict FIFO delivery
      per WebView.
    - RunScriptAsync() on an already-Destroy()'d WebView segfaults the
      whole process, no Python exception -- every dispatch checks
      liveness on the GUI thread, right before touching the widget, not
      just at the calling coroutine's entry.
    - RunScriptAsync evaluates a bare expression, so a Playwright-style
      arrow-function source ("() => ...") comes back as its own
      stringified source, not invoked -- every driver call is wrapped as
      an IIFE and its result JSON.stringify()'d before being read back.
    - WebViewEvent.IsTargetMainFrame() returns False even for genuine
      top-level navigations in this wx/WebView2 binding (confirmed via a
      standalone diagnostic) -- goto() instead arms a per-navigation
      generation marker on the GUI thread (immediately before LoadURL)
      and cross-checks the LOADED event's URL against
      WebView.GetCurrentURL(), neither of which depend on that flag.
    - On Windows, WebView.New() returns before the underlying CoreWebView2
      actually exists -- every public method awaits an explicit "ready"
      future resolved from wxEVT_WEBVIEW_CREATED (no EVT_ binder for this
      one, needs wx.PyEventBinder) before touching the widget. This is
      Windows-specific, not a universal wx.html2 fact: on GTK (Linux/
      macOS), wxEVT_WEBVIEW_CREATED doesn't exist at all, and WebView.New()
      IS synchronously ready (confirmed live via the 2026-08-07 WSL2
      spike) -- __init__ does the ready-setup inline there instead of
      waiting for any event. GTK also has no pre-bound
      EVT_WEBVIEW_SCRIPT_RESULT shortcut constant (only the raw
      wxEVT_WEBVIEW_SCRIPT_RESULT value), needing the same
      wx.PyEventBinder treatment.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
from typing import Any, Callable, Optional, Protocol

import wx
import wx.html2

logger = logging.getLogger("sdrsync.websdr.browser_shim")

# Internal per-script-call timeout (not caller-configurable) -- bounds how
# long a single RunScriptAsync can stay pending before this adapter marks
# itself dead. Separate from goto()/wait_for_function()'s caller-supplied
# timeouts (which drivers pass in Playwright's millisecond convention).
SCRIPT_TIMEOUT_S = 15.0
DEFAULT_TIMEOUT_MS = 15000
READY_TIMEOUT_S = 20.0
POLL_INTERVAL_S = 0.1
CONSOLE_MESSAGE_HANDLER_NAME = "sdrsyncConsole"

# wxEVT_WEBVIEW_CREATED and the EVT_WEBVIEW_SCRIPT_RESULT shortcut
# constant both do not exist on GTK wx builds (confirmed via a live
# hasattr() check during the 2026-08-07 WSL2 Linux spike) -- referencing
# either unconditionally would raise AttributeError at import time on
# Linux/macOS, before any object is even constructed. Guarded here so the
# module is importable on every platform; see WxPageAdapter.__init__ for
# how the missing readiness event is worked around on non-Windows.
if sys.platform == "win32":
    _EVT_WEBVIEW_CREATED = wx.PyEventBinder(wx.html2.wxEVT_WEBVIEW_CREATED)
    _EVT_WEBVIEW_SCRIPT_RESULT = wx.html2.EVT_WEBVIEW_SCRIPT_RESULT
else:
    _EVT_WEBVIEW_CREATED = None
    # expectedIDs=1 matches wx's own EVT_WEBVIEW_SCRIPT_RESULT definition
    # exactly (wx/html2.py: wx.PyEventBinder(wxEVT_WEBVIEW_SCRIPT_RESULT, 1)) --
    # harmless either way since Bind() below passes no source/id, but no
    # reason to diverge from wx's own binder when it's free to match.
    _EVT_WEBVIEW_SCRIPT_RESULT = wx.PyEventBinder(wx.html2.wxEVT_WEBVIEW_SCRIPT_RESULT, 1)

# Matches a Playwright-style function-source string ("() => ...",
# "(x) => ...", "async (x) => ...", "function(x) {...}"). Used only by
# wait_for_function(), which (unlike evaluate(), where every real driver
# call site always passes a function) accepts either a function or a raw
# boolean expression, matching Playwright's own auto-detection there.
_FUNC_RE = re.compile(r"^\s*(async\s+)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|^\s*(async\s+)?function\b")


class BrowserError(Exception):
    """Raised for any browser/page-level failure -- the shim's equivalent
    of Playwright's Error, caught the same way (`except BrowserError`)
    every driver already catches `except PlaywrightError`."""


class ConsoleMessage:
    """Shape-compatible with what driver _on_console(msg) handlers expect
    from a Playwright ConsoleMessage: .type and .text attributes."""
    __slots__ = ("type", "text")

    def __init__(self, type_: str, text: str) -> None:
        self.type = type_
        self.text = text


class PageLike(Protocol):
    """The exact Playwright Page API surface the three WebSDR drivers
    use -- kept separate from the concrete WxPageAdapter so driver-level
    tests can keep using a lightweight stub without a real wx.App/WebView
    existing (no existing driver test actually exercises a page object,
    but new engine-level tests will want this). timeout is in
    milliseconds, matching Playwright's convention -- every real driver
    call site already passes e.g. timeout=15000."""

    async def goto(self, url: str, timeout: Optional[float] = None) -> None: ...
    async def evaluate(self, js: str, *args: Any) -> Any: ...
    async def wait_for_function(self, js: str, timeout: Optional[float] = None) -> None: ...
    def on(self, event: str, handler: Callable) -> None: ...
    mouse: Any  # .click(x: int, y: int) -> Awaitable[None]


def _parse_js_result(raw: str) -> Any:
    if raw == "undefined":
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise BrowserError(f"Could not parse JS result as JSON: {raw!r} ({e})")


def _safe_set_result(fut: "asyncio.Future", value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


def _safe_set_exception(fut: "asyncio.Future", exc: Exception) -> None:
    if not fut.done():
        fut.set_exception(exc)


def _require_background_thread(method_name: str) -> None:
    # A plain assert is stripped under `python -O`, defeating the one
    # enforcement mechanism for this module's central invariant ("the GUI
    # thread never blocks waiting for a result") -- this raises
    # unconditionally instead.
    if wx.IsMainThread():
        raise RuntimeError(f"WxPageAdapter.{method_name}() called from the GUI thread")


def _console_shim_js(handler_name: str) -> str:
    """Injected once per page load (AddUserScript survives every
    navigation automatically, including in-page ones -- unlike posting on
    goto() alone). Forwards console.error/warn and uncaught errors through
    wx's script-message channel, since wx.html2 has no direct equivalent
    to Playwright's page.on("console"/"pageerror")."""
    return f"""
(function() {{
    function post(payload) {{
        try {{ window.{handler_name}.postMessage(JSON.stringify(payload)); }} catch (e) {{}}
    }}
    var origError = console.error;
    console.error = function() {{
        post({{kind: 'console', level: 'error', text: Array.prototype.slice.call(arguments).join(' ')}});
        return origError.apply(console, arguments);
    }};
    var origWarn = console.warn;
    console.warn = function() {{
        post({{kind: 'console', level: 'warning', text: Array.prototype.slice.call(arguments).join(' ')}});
        return origWarn.apply(console, arguments);
    }};
    window.addEventListener('error', function(e) {{
        post({{kind: 'error', text: String((e && (e.error || e.message)) || e)}});
    }});
    window.addEventListener('unhandledrejection', function(e) {{
        post({{kind: 'error', text: 'Unhandled promise rejection: ' + String(e.reason)}});
    }});
}})();
"""


class _Mouse:
    def __init__(self, adapter: "WxPageAdapter") -> None:
        self._adapter = adapter

    async def click(self, x: int, y: int) -> None:
        await self._adapter._simulate_click(x, y)


class WxPageAdapter:
    """Wraps one wx.html2.WebView child widget. Does not own the
    WebView's or its host frame's lifecycle -- the caller (SyncEngine,
    Block C) creates/destroys both; this adapter only navigates/
    controls/reads the widget it's given, mirroring how the drivers
    already treat `page` as something handed to them, not owned.

    Threading contract: every public method here must be called from a
    thread OTHER than the wx GUI thread (enforced, not just documented).
    Each call is dispatched onto the GUI thread via wx.CallAfter and its
    result is awaited via an asyncio Future resolved from the WebView's
    own event handlers -- the GUI thread itself never blocks waiting on
    anything.

    Death/recovery: this adapter can mark itself permanently dead (a
    script timing out with no way to cancel it, or explicit close()) --
    when that happens, on_dead (if supplied) is invoked exactly once so
    the caller can tear down and recreate the WebView, mirroring how an
    unrecoverable Playwright page failure already triggers a fresh
    attach() in sync/engine.py's attach-supervisor. Without on_dead
    wired up, a dead adapter fails every subsequent call cleanly but
    nothing ever replaces it -- Block C must supply this.
    """

    def __init__(
        self,
        webview: "wx.html2.WebView",
        loop: "asyncio.AbstractEventLoop",
        on_screen_presenter: Optional[Callable[[bool], None]] = None,
        on_dead: Optional[Callable[[str], None]] = None,
        already_created: bool = False,
    ) -> None:
        # `loop` must be the event loop that will actually await this
        # adapter's coroutines (SyncEngine's background asyncio loop) --
        # NOT auto-detected via asyncio.get_event_loop(). Widget setup
        # below (Bind/AddScriptMessageHandler/AddUserScript) requires
        # __init__ itself to run on the GUI thread (via wx.CallAfter from
        # the caller), where there is no running asyncio loop at all;
        # auto-detecting there silently grabs a throwaway loop and every
        # Future this adapter creates ends up attached to the wrong loop
        # (confirmed: "Future attached to a different loop" on every
        # call). The caller must capture its own loop reference (e.g.
        # asyncio.get_running_loop()) before dispatching construction to
        # the GUI thread, and pass it in here.
        self.webview = webview
        # Called with True to bring the WebView's host frame to a real
        # on-screen position (needed for a genuine OS-level click to
        # satisfy a WebSDR's audio autoplay gate -- see mouse.click()
        # below) and False to restore it off-screen. Block C's engine
        # owns what "on/off screen" actually means (position, taskbar
        # flags); this adapter only needs the hook.
        self._on_screen_presenter = on_screen_presenter
        self._on_dead = on_dead
        self._dead_reported = False

        self._alive = True
        self._lock = asyncio.Lock()
        self._loop = loop

        self._pending_script_future: Optional["asyncio.Future"] = None
        # Armed on the GUI thread, immediately before LoadURL() actually
        # runs (not when goto() is called) -- see _on_loaded for why.
        self._nav_generation = 0
        self._armed_nav_generation = 0
        self._pending_goto: Optional[tuple] = None  # (generation, future)

        self._console_handlers: list[Callable[[ConsoleMessage], None]] = []
        self._pageerror_handlers: list[Callable[[Exception], None]] = []
        self._warned_no_presenter = False

        self.mouse = _Mouse(self)

        self._ready: "asyncio.Future" = loop.create_future()

        self.webview.Bind(_EVT_WEBVIEW_SCRIPT_RESULT, self._on_script_result)
        self.webview.Bind(wx.html2.EVT_WEBVIEW_LOADED, self._on_loaded)
        self.webview.Bind(wx.html2.EVT_WEBVIEW_ERROR, self._on_webview_error)
        self.webview.Bind(wx.html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, self._on_script_message)
        self.webview.Bind(wx.EVT_WINDOW_DESTROY, self._on_window_destroy)
        if sys.platform == "win32":
            self.webview.Bind(_EVT_WEBVIEW_CREATED, self._on_webview_created)

        if already_created or sys.platform != "win32":
            # Windows: CoreWebView2 already exists (e.g. this adapter is
            # replacing a prior one on a WebView that's already
            # initialized) -- __init__ itself runs on the GUI thread (per
            # the threading contract above), so it's safe to do the
            # ready-setup inline rather than waiting for an event that
            # will never fire again.
            # Non-Windows (Linux/macOS): there is no wxEVT_WEBVIEW_CREATED
            # to wait for at all -- WebKitGTK's underlying GtkWidget is
            # created synchronously inside WebView.New() (confirmed via
            # the 2026-08-07 WSL2 spike: LoadURL() called immediately
            # after New() with no wait worked every time), so the page is
            # always effectively "already created" there.
            self._do_ready_setup()

    # ------------------------------------------------------------------ public API
    def on(self, event: str, handler: Callable) -> None:
        if event == "console":
            self._console_handlers.append(handler)
        elif event == "pageerror":
            self._pageerror_handlers.append(handler)
        else:
            raise ValueError(f"Unsupported event type: {event!r}")

    async def goto(self, url: str, timeout: Optional[float] = None) -> None:
        """timeout is in milliseconds (Playwright convention -- every
        real driver call site passes e.g. timeout=15000)."""
        _require_background_thread("goto")
        await self._await_ready()
        if not self._alive:
            raise BrowserError("page adapter is not attached (destroyed)")

        fut: "asyncio.Future" = self._loop.create_future()
        self._nav_generation += 1
        my_gen = self._nav_generation
        self._pending_goto = (my_gen, fut)

        def do_load():
            if not self._alive:
                self._loop.call_soon_threadsafe(
                    _safe_set_exception, fut, BrowserError("page adapter destroyed before navigation ran")
                )
                return
            # Armed here, right before LoadURL actually runs -- arming it
            # in goto() itself (on the calling thread) was a no-op: it
            # was always equal to self._nav_generation by construction,
            # so the "is this stale?" check in _on_loaded could never
            # fire, and a LOADED event left over from a still-loading
            # previous page could resolve THIS goto() before the new
            # navigation even started (confirmed reachable: drivers call
            # goto() on every attach retry against the same page).
            self._armed_nav_generation = my_gen
            try:
                self.webview.LoadURL(url)
            except Exception as e:
                self._loop.call_soon_threadsafe(_safe_set_exception, fut, BrowserError(f"LoadURL failed: {e}"))

        wx.CallAfter(do_load)
        timeout_s = (timeout / 1000.0) if timeout else (DEFAULT_TIMEOUT_MS / 1000.0)
        try:
            await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            raise BrowserError(f"goto({url!r}) timed out after {timeout_s}s")

    async def evaluate(self, js: str, *args: Any) -> Any:
        """js must be a Playwright-style function source ("() => ..." /
        "(x) => ..."), matching what every real driver call site already
        passes -- RunScriptAsync evaluates a bare expression, so this
        wraps it as an IIFE and JSON.stringify()'s the result."""
        _require_background_thread("evaluate")
        await self._await_ready()
        args_js = ", ".join(json.dumps(a) for a in args)
        script = f"JSON.stringify(({js})({args_js}))"
        raw = await self._run_script(script)
        return _parse_js_result(raw)

    async def wait_for_function(self, js: str, timeout: Optional[float] = None) -> None:
        """timeout is in milliseconds (Playwright convention)."""
        _require_background_thread("wait_for_function")
        await self._await_ready()
        timeout_s = (timeout / 1000.0) if timeout else (DEFAULT_TIMEOUT_MS / 1000.0)
        deadline = time.monotonic() + timeout_s
        expr = f"({js})()" if _FUNC_RE.match(js) else f"({js})"
        script = f"JSON.stringify(!!({expr}))"
        while True:
            if not self._alive:
                raise BrowserError("page adapter is not attached (destroyed)")
            try:
                raw = await self._run_script(script)
                if _parse_js_result(raw) is True:
                    return
            except BrowserError:
                # A page mid-navigation (globals not yet defined) makes
                # the predicate throw -- keep polling until the deadline
                # instead of failing the whole wait immediately, mirroring
                # how Playwright's wait_for_function rides out a live page.
                pass
            if time.monotonic() >= deadline:
                raise BrowserError(f"wait_for_function timed out after {timeout_s}s: {js!r}")
            await asyncio.sleep(POLL_INTERVAL_S)

    def set_on_dead(self, on_dead: Optional[Callable[[str], None]]) -> None:
        """Rebinds the on_dead callback in place -- for SyncEngine's
        switch-in-place path (_switch_websdr()), which reuses an existing
        page/adapter across a site change instead of going through
        create_page()/destroy_page() again. Each switch bumps the
        engine's generation counter, and the callback closure captures
        that generation by value, so the OLD callback would report a
        post-switch crash under a generation _handle_page_dead() would
        then (correctly, but wrongly here) treat as stale and ignore --
        this keeps crash recovery working across a switch, same as it
        already does across create_page()."""
        self._on_dead = on_dead

    async def close(self) -> None:
        """Marks this adapter dead. Does NOT destroy the underlying
        WebView/frame -- Block C's engine owns and destroys those, same
        as the other drivers only ever drop their own page reference
        rather than calling browser.close() themselves. Safe to call
        more than once. Does NOT fire on_dead -- that signal means
        "died unexpectedly, please recreate me," which an explicit,
        caller-initiated close() is not (confirmed reachable: a normal
        Disconnect calls this, and on_dead firing anyway raced the
        engine's event loop closing on app shutdown -- RuntimeError
        propagating out of a wx.CallAfter callback)."""
        self._mark_dead("closed", notify=False)
        self._cleanup_widget_bindings()

    # ------------------------------------------------------------------ internal: readiness
    def _do_ready_setup(self) -> None:
        """GUI thread only. Registers the console/pageerror shim and
        resolves self._ready. AddScriptMessageHandler/AddUserScript both
        return bool and never raise on failure -- their return values are
        checked and logged, since a bare try/except around them (as an
        earlier draft had) catches nothing and silently leaves the
        console/pageerror channel dead."""
        if not self._ready.done():
            handler_ok = self.webview.AddScriptMessageHandler(CONSOLE_MESSAGE_HANDLER_NAME)
            if not handler_ok:
                logger.warning("AddScriptMessageHandler(%r) returned False -- console/pageerror "
                                "shim will not receive messages", CONSOLE_MESSAGE_HANDLER_NAME)
            script_ok = self.webview.AddUserScript(_console_shim_js(CONSOLE_MESSAGE_HANDLER_NAME))
            if not script_ok:
                logger.warning("AddUserScript() for the console/pageerror shim returned False")
            self._loop.call_soon_threadsafe(_safe_set_result, self._ready, None)

    def _on_webview_created(self, evt) -> None:
        assert wx.IsMainThread()
        self._do_ready_setup()

    async def _await_ready(self) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), timeout=READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._mark_dead("CoreWebView2 never became ready")
            raise BrowserError(f"WebView2 was not ready within {READY_TIMEOUT_S}s")

    # ------------------------------------------------------------------ internal: script execution
    async def _run_script(self, script: str) -> str:
        if not self._alive:
            raise BrowserError("page adapter is not attached (destroyed)")
        async with self._lock:
            if not self._alive:
                raise BrowserError("page adapter is not attached (destroyed)")
            fut: "asyncio.Future" = self._loop.create_future()
            self._pending_script_future = fut

            def do_run():
                if not self._alive:
                    # Checked again here, on the GUI thread, right before
                    # touching the widget -- RunScriptAsync on an
                    # already-Destroy()'d WebView segfaults the whole
                    # process with no Python exception (confirmed via
                    # spike), so this check cannot live only at the
                    # calling coroutine's entry.
                    self._loop.call_soon_threadsafe(
                        _safe_set_exception, fut, BrowserError("page adapter destroyed before script ran")
                    )
                    return
                try:
                    self.webview.RunScriptAsync(script)
                except Exception as e:
                    self._loop.call_soon_threadsafe(
                        _safe_set_exception, fut, BrowserError(f"RunScriptAsync failed: {e}")
                    )

            wx.CallAfter(do_run)
            try:
                return await asyncio.wait_for(fut, timeout=SCRIPT_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Can't cancel an in-flight script, and results are
                # strictly FIFO -- failing just this one future and
                # moving on would let the eventual real result land on a
                # LATER call instead. Kill the whole adapter instead, and
                # notify on_dead so the caller can recreate it -- without
                # that notification, a dead adapter fails every call
                # cleanly forever with nothing ever replacing it.
                self._mark_dead(f"script timed out after {SCRIPT_TIMEOUT_S}s")
                raise BrowserError(f"script timed out after {SCRIPT_TIMEOUT_S}s (adapter marked dead)")

    def _mark_dead(self, reason: str, notify: bool = True) -> None:
        was_alive = self._alive
        self._alive = False
        self._fail_pending(BrowserError(f"page adapter dead: {reason}"))
        if notify and was_alive and not self._dead_reported and self._on_dead is not None:
            self._dead_reported = True
            cb = self._on_dead
            wx.CallAfter(cb, reason)

    def _fail_pending(self, exc: BrowserError) -> None:
        fut = self._pending_script_future
        self._pending_script_future = None
        if fut is not None and not fut.done():
            self._loop.call_soon_threadsafe(_safe_set_exception, fut, exc)
        goto = self._pending_goto
        self._pending_goto = None
        if goto is not None:
            _, gfut = goto
            if not gfut.done():
                self._loop.call_soon_threadsafe(_safe_set_exception, gfut, exc)

    # ------------------------------------------------------------------ internal: click/audio-unlock
    async def _simulate_click(self, x: int, y: int) -> None:
        """Real OS-level click (wx.UIActionSimulator), not a JS
        .click()/dispatchEvent() -- Chromium/WebView2 explicitly treats
        JS-dispatched events as untrusted and excludes them from
        satisfying the autoplay-gesture requirement (confirmed during
        Block A's spike). WebKitGTK/Cocoa WebKit enforce the identical
        trusted-gesture requirement (confirmed working via the 2026-08-07
        WSL2 Linux spike using this same wx.UIActionSimulator call, no
        platform-specific code needed here). Requires the host frame to
        be at a genuine on-screen position for the OS to deliver the
        input at all."""
        _require_background_thread("mouse.click")
        if not self._alive:
            return
        if self._on_screen_presenter is None:
            if not self._warned_no_presenter:
                self._warned_no_presenter = True
                logger.warning(
                    "mouse.click() called with no on_screen_presenter configured -- audio "
                    "autoplay-gate clicks will silently no-op, WebSDR audio may never unlock"
                )
            return
        done = threading.Event()

        def do_click():
            if not self._alive:
                done.set()
                return
            try:
                self._on_screen_presenter(True)
                wx.SafeYield()
                if not self._alive:  # SafeYield can re-enter and process a Destroy queued elsewhere
                    return
                screen_pos = self.webview.ClientToScreen(wx.Point(x, y))
                sim = wx.UIActionSimulator()
                sim.MouseMove(screen_pos)
                wx.MilliSleep(30)
                sim.MouseClick(wx.MOUSE_BTN_LEFT)
                # MouseClick() injects via SendInput and returns before
                # the OS actually delivers the WM_LBUTTONDOWN/UP to the
                # window -- restoring off-screen on the very next line
                # (an earlier draft did this) can move the window out
                # from under the pointer before delivery, so the click
                # lands nowhere and the autoplay gesture never registers.
                wx.MilliSleep(150)
            except Exception as e:
                logger.debug("Simulated click failed (non-fatal): %s", e)
            finally:
                if self._alive:
                    self._on_screen_presenter(False)
                done.set()

        wx.CallAfter(do_click)
        await self._loop.run_in_executor(None, done.wait, 5.0)

    # ------------------------------------------------------------------ internal: teardown
    def _cleanup_widget_bindings(self) -> None:
        """GUI-thread-safe to call more than once. Best-effort -- the
        widget may already be gone by the time this runs."""
        def do_cleanup():
            try:
                self.webview.RemoveAllUserScripts()
            except Exception:
                pass
            try:
                self.webview.RemoveScriptMessageHandler(CONSOLE_MESSAGE_HANDLER_NAME)
            except Exception:
                pass
        wx.CallAfter(do_cleanup)
        self._console_handlers.clear()
        self._pageerror_handlers.clear()

    def _on_window_destroy(self, evt) -> None:
        # Backstop: if something destroys the widget without going
        # through close() first, this still stops _alive from staying
        # True (which would otherwise risk the RunScriptAsync-on-a-
        # destroyed-widget segfault the moment another call is dispatched).
        assert wx.IsMainThread()
        self._mark_dead("widget destroyed")
        evt.Skip()

    # ------------------------------------------------------------------ wx event handlers (GUI thread)
    def _on_script_result(self, evt: "wx.html2.WebViewEvent") -> None:
        assert wx.IsMainThread()
        if not self._alive:
            return
        fut = self._pending_script_future
        self._pending_script_future = None
        if fut is None or fut.done():
            return
        if evt.IsError():
            self._loop.call_soon_threadsafe(_safe_set_exception, fut, BrowserError(evt.GetString()))
        else:
            self._loop.call_soon_threadsafe(_safe_set_result, fut, evt.GetString())

    def _on_loaded(self, evt: "wx.html2.WebViewEvent") -> None:
        assert wx.IsMainThread()
        if sys.platform != "win32" and evt.GetURL() == "about:blank":
            # GTK's own internal initial navigation fires a spurious
            # LOADED for about:blank before the caller's real goto()
            # target loads (confirmed live, 2026-08-07 WSL2 spike --
            # Windows' equivalent quirk instead manifests as a spurious
            # ERROR/CONNECTION_ABORTED, handled separately below in
            # _on_webview_error, which needs no change). Explicit skip
            # here rather than trusting the generation/URL check below
            # alone, since dispatch ordering between WebView.New()'s
            # internal navigation and our own first LoadURL() isn't
            # guaranteed.
            logger.debug("Ignoring spurious about:blank LOADED event (GTK first-navigation quirk)")
            return
        goto = self._pending_goto
        if goto is None:
            return
        gen, fut = goto
        if gen != self._armed_nav_generation or fut.done():
            return
        # IsTargetMainFrame() was expected to filter out subframe LOADED
        # events but empirically returns False even for the genuine
        # top-level navigation in this wx/WebView2 binding (confirmed via
        # a standalone diagnostic). Filter by URL instead: a subframe's
        # LOADED carries the subframe's own URL and won't match
        # GetCurrentURL(), while a redirected main-frame LOADED still
        # matches because GetCurrentURL() tracks the redirect target.
        try:
            current = self.webview.GetCurrentURL()
        except Exception:
            current = None
        if current is not None and evt.GetURL() != current:
            return
        self._pending_goto = None
        self._nudge_repaint()
        self._loop.call_soon_threadsafe(_safe_set_result, fut, None)

    def _nudge_repaint(self) -> None:
        """WebView2/WebKitGTK's compositor doesn't reliably present pixels
        for a freshly-navigated page in this window -- confirmed via live
        testing (Show()/LoadURL() alone left the on-screen window flat
        gray despite JS reads and audio both working correctly the whole
        time; an external OS-level resize was the only thing that made it
        actually paint). A genuine resize event delivered through the
        message loop is what's needed, not just a redraw request -- a
        1px-and-back SetSize is the cheapest reliable way to generate one.
        Harmless to call even while off-screen/hidden."""
        if not self._alive:
            return
        try:
            size = self.webview.GetSize()
            self.webview.SetSize(wx.Size(size.width, max(size.height - 1, 1)))
            self.webview.SetSize(size)
        except Exception as e:
            logger.debug("Repaint nudge failed (non-fatal): %s", e)

    def _on_webview_error(self, evt: "wx.html2.WebViewEvent") -> None:
        assert wx.IsMainThread()
        error_text = evt.GetString()
        if "CONNECTION_ABORTED" in error_text:
            # Not a real failure -- this fires when a navigation gets
            # superseded by a newer one, most commonly a freshly-created
            # WebView's own automatic initial navigation to about:blank
            # getting cancelled by our very first LoadURL() (confirmed
            # reproducible: only ever seen on a WebView's first-ever
            # navigation, never on a second/third goto() against an
            # already-settled one). The real navigation's own LOADED
            # event still arrives shortly after -- rejecting the pending
            # goto() future here would fail a goto() that actually
            # succeeds a moment later. goto()'s own timeout remains the
            # backstop for a navigation that's genuinely stuck.
            logger.debug("Ignoring CONNECTION_ABORTED WebView error (superseded navigation): %s", error_text)
            return
        goto = self._pending_goto
        if goto is not None:
            gen, fut = goto
            if gen == self._armed_nav_generation and not fut.done():
                self._pending_goto = None
                self._loop.call_soon_threadsafe(
                    _safe_set_exception, fut, BrowserError(f"navigation error: {error_text}")
                )
        else:
            # Not tied to a pending goto() -- e.g. a subresource/iframe
            # failing to load on an otherwise-healthy page. Playwright's
            # pageerror only fires for uncaught JS exceptions, never
            # navigation failures, and drivers latch _last_page_error
            # from pageerror until the next successful attach() -- fanning
            # this out to pageerror handlers would leave a permanently
            # wrong error displayed in the GUI for a fully healthy
            # connection. Log only.
            logger.debug("WebView navigation error (no pending goto): %s", evt.GetString())

    def _on_script_message(self, evt) -> None:
        assert wx.IsMainThread()
        try:
            payload = json.loads(evt.GetString())
        except (json.JSONDecodeError, TypeError):
            return
        kind = payload.get("kind")
        if kind == "console":
            msg = ConsoleMessage(payload.get("level", "log"), payload.get("text", ""))
            for handler in list(self._console_handlers):
                # Dispatched via call_soon_threadsafe, not called
                # directly -- these are driver callbacks that ran on the
                # asyncio loop under Playwright; calling them straight
                # from the GUI thread here would quietly make arbitrary
                # driver code (and a page that spams console.error) run
                # on the GUI thread, which the rest of this module treats
                # as a hard invariant to avoid.
                self._loop.call_soon_threadsafe(_dispatch_handler, handler, msg)
        elif kind == "error":
            exc = BrowserError(payload.get("text", "unknown page error"))
            for handler in list(self._pageerror_handlers):
                self._loop.call_soon_threadsafe(_dispatch_handler, handler, exc)


def _dispatch_handler(handler: Callable, arg: Any) -> None:
    try:
        handler(arg)
    except Exception:
        logger.exception("driver console/pageerror handler raised")

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
from collections import deque
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


# ---------------------------------------------------------------------------
# Native audio mute -- mutes the WHOLE WebView's audio output at the browser
# engine level, with zero page interaction. Replaces the old approach of
# calling each WebSDR site's own JS mute function (e.g. KiwiSDR's
# toggle_or_set_mute()): that depended on whatever that specific site chose
# to implement "mute" as, confirmed live to add up to ~1s of perceptible
# lag on every one of the four site families -- inherent to relying on 4
# independently-written, unverified third-party JS implementations rather
# than something sdrsync controls directly.
#
# Both wx.html2 backends have a real native "mute this webview" API, just
# not exposed through wx's own cross-platform WebView class -- reached here
# via GetNativeBackend()'s raw pointer (confirmed: GTK returns a real
# WebKitWebView*, MSW an ICoreWebView2* -- both per wxWidgets' own C++
# source, src/gtk/webview_webkit2.cpp and src/msw/webview_edge.cpp).
#
#   - GTK/WebKitGTK: webkit_web_view_set_is_muted() -- a plain native C
#     function, called via ctypes against libwebkit2gtk directly (no new
#     dependency). VERIFIED LIVE on this machine: a standalone round-trip
#     test (set muted -> get_is_muted() reflects it -> unset -> reflects
#     that too) passed exactly as expected.
#   - MSW/Edge WebView2: ICoreWebView2_8.IsMuted, a COM property. NOT
#     live-verified -- no Windows access in this environment. Reached via
#     raw vtable-slot indexing (manual QueryInterface + a direct call at a
#     fixed slot offset, deliberately NOT using comtypes' higher-level
#     interface-class machinery, which still requires knowing this same
#     slot count and additionally risks a reference-counting mismatch
#     against wx's own owned WebView2 reference). The slot offset was
#     computed from Microsoft's own shipped WebView2.h (extracted from the
#     real Microsoft.Web.WebView2 NuGet package, not a third-party binding
#     -- an initial attempt using a third-party Go binding's vtable struct
#     was caught to be wrong: it embedded only IUnknown ahead of
#     ICoreWebView2_8's own methods, omitting the entire real
#     ICoreWebView2 -> _7 inheritance chain that precedes them in memory,
#     which would have called through a garbage function pointer).
#     IUnknown ABI (QueryInterface/AddRef/Release at slots 0/1/2) is
#     universal across every COM interface and not itself at risk.
#     Slot arithmetic (own method counts, in real header declaration
#     order, each cross-checked against Microsoft Learn's own docs page
#     for that interface):
#       IUnknown............................  3
#       ICoreWebView2 (base)................ 58
#       ICoreWebView2_2......................  7
#       ICoreWebView2_3......................  5
#       ICoreWebView2_4......................  4
#       ICoreWebView2_5......................  2
#       ICoreWebView2_6......................  1
#       ICoreWebView2_7......................  1
#                                     subtotal = 81
#       ICoreWebView2_8's own, in order: add_IsMutedChanged(+0),
#       remove_IsMutedChanged(+1), get_IsMuted(+2), put_IsMuted(+3)
#                                  PutIsMuted slot = 84
#     Needs real Windows testing before being trusted in place of the old
#     per-site JS mute -- a wrong slot here is a genuine crash risk (an
#     arbitrary function-pointer call), not a caught exception, which is
#     exactly why every call below is wrapped and why this whole block is
#     documented this thoroughly.

# Candidate WebKitGTK sonames, in no particular preference order -- see
# _load_webkit2gtk()'s use of RTLD_NOLOAD below for why order doesn't
# matter here. A fixed try-order was a real bug caught by a bug-hunter
# review pass: wx.html2's actual WebKitGTK backend version is a
# distro/package matter, not something this module controls, and a
# distro that links wx against 4.0 (confirmed real: Ubuntu 22.04's
# libwxgtk-webview3.0-gtk3-0v5 depends on libwebkit2gtk-4.0-37, per its
# own package metadata -- the exact distro CLAUDE.md's own install
# instructions target) while 4.1 also happens to be installed would have
# silently loaded the WRONG library and handed it a pointer created by
# the other one -- WebKitGTK's own GObject type-check guard on
# set_is_muted() then just logs a GLib CRITICAL and does nothing (no
# Python exception, so nothing here would have noticed), and cross-
# library GObject/libsoup version conflicts were reproduced to also hang
# or abort the whole process in the worst case.
_WEBKIT2GTK_SONAMES = ("libwebkit2gtk-4.1.so.0", "libwebkit2gtk-4.0.so.37")
_webkit2gtk_lib = None
_webkit2gtk_load_attempted = False


def _load_webkit2gtk():
    """Only ever returns a library already loaded into THIS process (via
    RTLD_NOLOAD -- fails instead of loading a fresh, possibly different
    instance) -- i.e. whichever WebKitGTK wx.html2's own backend already
    pulled in, never a second, independently-loaded one. Returns None
    (native mute unavailable, not guessed at) if none of the known
    sonames are already loaded, rather than risk loading a mismatched
    library instance."""
    import ctypes
    import os

    for soname in _WEBKIT2GTK_SONAMES:
        try:
            lib = ctypes.CDLL(soname, mode=os.RTLD_NOLOAD)
        except OSError:
            continue
        lib.webkit_web_view_set_is_muted.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.webkit_web_view_set_is_muted.restype = None
        lib.webkit_web_view_get_is_muted.argtypes = [ctypes.c_void_p]
        lib.webkit_web_view_get_is_muted.restype = ctypes.c_int
        return lib
    return None


def _gtk_native_set_muted(native_ptr: Any, muted: bool) -> bool:
    """native_ptr is whatever GetNativeBackend() returned (a sip.voidptr
    wrapping the real WebKitWebView*, confirmed live) -- int() extracts
    the raw address from it the same way on a plain int, if some other
    wx build ever returns one directly.

    Verifies via a get_is_muted() readback rather than trusting
    set_is_muted() blind -- a bug-hunter review pass found that a
    cross-library pointer mismatch (see _load_webkit2gtk()'s own
    docstring) makes WebKitGTK's own type-check guard silently no-op the
    call with no Python-visible signal at all; the readback is what
    actually distinguishes "muted" from "asked to mute, nothing
    happened"."""
    global _webkit2gtk_lib, _webkit2gtk_load_attempted
    import ctypes

    if not _webkit2gtk_load_attempted:
        _webkit2gtk_load_attempted = True
        _webkit2gtk_lib = _load_webkit2gtk()
        if _webkit2gtk_lib is None:
            logger.warning(
                "native audio mute unavailable: none of %s are already loaded in this process",
                _WEBKIT2GTK_SONAMES,
            )
    if _webkit2gtk_lib is None:
        return False
    ptr = ctypes.cast(int(native_ptr), ctypes.c_void_p)
    _webkit2gtk_lib.webkit_web_view_set_is_muted(ptr, 1 if muted else 0)
    now_muted = bool(_webkit2gtk_lib.webkit_web_view_get_is_muted(ptr))
    if now_muted != muted:
        return False
    return True


# See the big comment block above for how this offset was derived and its
# residual, un-live-tested risk.
_ICOREWEBVIEW2_8_IID = "E9632730-6E1E-43AB-B7B8-7B2C9E62E094"
_PUT_IS_MUTED_SLOT = 84


def _win32_guid_struct(guid_str: str):
    import ctypes
    import uuid

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_uint8 * 8),
        ]

    return _GUID.from_buffer_copy(uuid.UUID(guid_str).bytes_le)


def _win32_vtable_call(obj_ptr: int, slot: int, functype, *args):
    import ctypes

    vtable_ptr = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    func_ptr = ctypes.cast(vtable_ptr, ctypes.POINTER(ctypes.c_void_p))[slot]
    func = ctypes.cast(func_ptr, functype)
    return func(obj_ptr, *args)


def _win32_native_set_muted(native_ptr: Any, muted: bool) -> bool:
    """See the module-level comment above -- NOT live-verified, no Windows
    access in this environment. Manually calls QueryInterface for
    ICoreWebView2_8 (universal IUnknown slot 0) then PutIsMuted at its
    computed slot, releasing the queried interface afterward (universal
    IUnknown slot 2) -- entirely manual reference counting, matching real
    C++ COM usage, rather than trusting an automatic wrapper's lifecycle
    against a pointer this adapter does not itself own."""
    import ctypes

    try:
        obj = int(native_ptr)
        qi_func = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )
        release_func = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        put_is_muted_func = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int)

        iid = _win32_guid_struct(_ICOREWEBVIEW2_8_IID)
        out = ctypes.c_void_p()
        hr = _win32_vtable_call(obj, 0, qi_func, ctypes.byref(iid), ctypes.byref(out))
        if hr < 0 or not out.value:
            logger.warning(
                "native WebView2 mute unavailable: QueryInterface(ICoreWebView2_8) failed "
                "(hr=0x%08x) -- WebView2 Runtime may be older than 1.0.1072.54",
                hr & 0xFFFFFFFF,
            )
            return False
        try:
            hr = _win32_vtable_call(out.value, _PUT_IS_MUTED_SLOT, put_is_muted_func, 1 if muted else 0)
            if hr < 0:
                logger.warning("native WebView2 mute failed: put_IsMuted returned hr=0x%08x", hr & 0xFFFFFFFF)
                return False
            return True
        finally:
            _win32_vtable_call(out.value, 2, release_func)
    except Exception as e:
        logger.warning("native WebView2 mute failed: %s", e, exc_info=True)
        return False


def _native_set_muted(native_ptr: Any, muted: bool) -> bool:
    if sys.platform == "win32":
        return _win32_native_set_muted(native_ptr, muted)
    return _gtk_native_set_muted(native_ptr, muted)


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
    async def set_muted(self, muted: bool) -> None: ...
    def on(self, event: str, handler: Callable) -> None: ...
    mouse: Any  # .click(x: int, y: int) -> Awaitable[None]


# Default polling budget for click_element_if_present() below, used for a
# caller that awaits it synchronously (a longer default would delay
# attach() itself). KiwiSDR/OpenWebRX's audio-unlock overlays are each
# gated behind THEIR OWN page-internal trigger (confirmed live: KiwiSDR's
# is a WebSocket "camp" message handler / early UI-init call, not simply
# tied to page load or ws_snd/demodulator readiness) -- confirmed live
# that this can fire well after both of those are ready, so a caller that
# actually needs to catch a late-appearing overlay should pass an
# explicit, longer timeout_s rather than relying on this default.
CLICK_ELEMENT_POLL_TIMEOUT_S = 2.0
CLICK_ELEMENT_POLL_INTERVAL_S = 0.2


_VISIBLE_HITTESTABLE_CENTER_JS = (
    "(sel) => { "
    "const candidates = document.querySelectorAll(sel); "
    "for (const el of candidates) { "
    "const r = el.getBoundingClientRect(); "
    "if (r.width === 0 || r.height === 0) continue; "
    "const s = getComputedStyle(el); "
    "if (s.display === 'none' || s.visibility === 'hidden') continue; "
    "const cx = r.x + r.width / 2, cy = r.y + r.height / 2; "
    # A matching element can exist, have a non-zero rect, and still not
    # be what a real click at its own center would actually hit -- e.g.
    # a hidden mobile/compact layout duplicate positioned off-screen-
    # but-technically-laid-out, or something else (higher z-index, an
    # overlay) actually covering it. Confirmed live as a real cause of
    # clicks landing on the wrong control entirely. elementFromPoint()
    # is the authoritative "what would a click here actually hit" check
    # -- skip any candidate that fails it rather than trusting the rect
    # alone.
    "if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) continue; "
    "const hit = document.elementFromPoint(cx, cy); "
    "if (!hit || !el.contains(hit) && hit !== el) continue; "
    "return {x: cx, y: cy, "
    "rectX: r.x, rectY: r.y, rectW: r.width, rectH: r.height, "
    "innerW: window.innerWidth, innerH: window.innerHeight, "
    "dpr: window.devicePixelRatio}; "
    "} "
    "return null; }"
)


async def element_is_present(page: "PageLike", selector: str) -> bool:
    """One-shot, no polling, no click: True if a visible, hit-testable
    element matching `selector` exists right now. Exists to let a caller
    verify a click actually had an effect (the element is now gone)
    rather than trusting that dispatching a click is proof it worked --
    confirmed live as a real gap: KiwiSDR's own audio-unlock overlay
    renders with a no-op onclick='' when the receiver requires an
    operator-typed ID first (cfg.require_id), so click_element_if_present
    happily "succeeds" at clicking something that does nothing."""
    try:
        rect = await page.evaluate(_VISIBLE_HITTESTABLE_CENTER_JS, selector)
    except BrowserError:
        return False
    return rect is not None


async def click_element_if_present(
    page: "PageLike", selector: str, *, timeout_s: float = CLICK_ELEMENT_POLL_TIMEOUT_S,
) -> bool:
    """Best-effort: if a visible element matching `selector` exists within
    timeout_s, sends a REAL OS-level click (via page.mouse.click -- see
    WxPageAdapter._simulate_click's own docstring for why a real click is
    required: WebKitGTK/Chromium both reject a JS-dispatched click as an
    untrusted gesture) at its center and returns True. Returns False if no
    such element ever appears in time (e.g. the page's own audio was never
    gated in the first place -- confirmed live on Windows/WebView2, where
    sdrsync's own --autoplay-policy flag means KiwiSDR/OpenWebRX's audio-
    unlock overlays never even render).

    True here means "a click was dispatched at the element", NOT "the
    click had the intended effect" -- see element_is_present()'s own
    docstring for a real, confirmed case where those differ.

    Exists because some WebSDR pages tie their OWN audio-unlock
    (AudioContext.resume()) specifically to a click on ONE particular
    on-page element, not just any trusted click anywhere (unlike
    websdr_org.py's driver-side corner-click) -- e.g. KiwiSDR's
    #id-play-button-container, OpenWebRX's #openwebrx-autoplay-overlay."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            rect = await page.evaluate(_VISIBLE_HITTESTABLE_CENTER_JS, selector)
        except BrowserError as e:
            logger.debug("click_element_if_present(%r) probe failed: %s", selector, e)
            rect = None
        if rect is not None:
            # Scale CSS-pixel coordinates by devicePixelRatio before
            # handing them to page.mouse.click() -- confirmed live (on a
            # display where devicePixelRatio != 1.0) that wx's own
            # ClientToScreen()/UIActionSimulator coordinate space is NOT
            # the same as the browser's CSS-pixel space, and the
            # resulting error grows with distance from the viewport
            # origin: invisible on a large, centrally-placed target
            # (KiwiSDR's/OpenWebRX's full-viewport overlays, or
            # UberSDR's wide .start__go button) but enough to land on a
            # completely different, neighboring control for a small
            # target further from the origin (a topbar icon button).
            dpr = rect.get("dpr") or 1.0
            click_x, click_y = round(rect["x"] * dpr), round(rect["y"] * dpr)
            logger.debug(
                "click_element_if_present(%r) found element -- %r -- clicking at (%d, %d) (dpr=%s)",
                selector, rect, click_x, click_y, dpr,
            )
            # Best-effort per this function's whole contract -- a click
            # failure here (e.g. the page navigated away between the probe
            # and the click) must not raise out and be mistaken by a
            # caller's own try/except BrowserError for a real attach
            # failure.
            try:
                await page.mouse.click(click_x, click_y)
            except BrowserError as e:
                logger.debug("click_element_if_present(%r) click itself failed: %s", selector, e)
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(CLICK_ELEMENT_POLL_INTERVAL_S)


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

        # One entry per RunScriptAsync dispatch that has actually
        # succeeded, appended (on the GUI thread, by do_run() inside
        # _run_script()) in dispatch order and popped from the left (also
        # GUI thread, by _on_script_result()) in that same order --
        # RunScriptAsync can't be cancelled once dispatched, and wx.html2
        # delivers wxEVT_WEBVIEW_SCRIPT_RESULT strictly FIFO per adapter
        # with no other way to correlate a result to the call that
        # triggered it (see the module docstring's "load-bearing facts").
        # A cancelled caller (self._lock still held until its
        # CancelledError propagates, so at most a handful of these can
        # ever be outstanding, one per call abandoned while its script
        # was still in flight) simply leaves its future in this queue,
        # already done() -- _on_script_result() pops it in its correct
        # FIFO position and discards it there, same as any other already-
        # resolved future, rather than needing to specifically detect or
        # count "orphans" at all. This queue is the sole source of truth;
        # there is deliberately no separate "the current pending call"
        # slot to keep in sync with it.
        self._dispatch_queue: "deque[asyncio.Future]" = deque()
        # Guards self._dispatch_queue specifically. The "wx's single-
        # threaded event dispatch serializes append/pop, no lock needed"
        # reasoning above only holds if EVERY mutation happens on the GUI
        # thread -- but _fail_pending() (via _mark_dead()) is also
        # reachable from the background asyncio thread, via
        # _await_ready()'s and _run_script()'s own timeout paths and via
        # close(). A GUI-thread _on_script_result() doing its
        # "if not queue: return" check followed by popleft() is not atomic
        # against a concurrent background-thread _fail_pending() draining
        # the same deque between those two steps -- confirmed reachable,
        # not just theoretical: a script timeout racing a real (slightly
        # delayed) SCRIPT_RESULT event hits exactly this window. A plain
        # threading.Lock, not asyncio.Lock, since the background side is
        # not necessarily running on this adapter's event loop when it
        # takes the lock.
        self._dispatch_queue_lock = threading.Lock()
        # Armed on the GUI thread, immediately before LoadURL() actually
        # runs (not when goto() is called) -- see _on_loaded for why.
        self._nav_generation = 0
        self._armed_nav_generation = 0
        self._pending_goto: Optional[tuple] = None  # (generation, future)

        self._console_handlers: list[Callable[[ConsoleMessage], None]] = []
        self._pageerror_handlers: list[Callable[[Exception], None]] = []
        self._warned_no_presenter = False
        self._native_mute_warned = False

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

    async def set_muted(self, muted: bool) -> None:
        """Mutes/unmutes the WHOLE WebView's audio output natively --
        see the module-level comment above _gtk_native_set_muted() for
        why this replaced per-site JS mute() calls, and the residual,
        un-live-tested risk on MSW. Deliberately does NOT await
        _await_ready()/go through the script-result FIFO queue like
        evaluate() -- this is the whole point of the native path being
        fast: no page round-trip to wait on at all, just a same-thread
        native call once dispatched to the GUI thread. Fire-and-forget
        from the caller's perspective; does not block on the GUI thread
        actually running it.

        GetNativeBackend() itself must run inside do_mute(), on the GUI
        thread, same as every other widget touch in this class -- a real
        bug caught by a bug-hunter review pass: calling it here instead
        (on the calling asyncio thread) is exactly the segfault-on-a-
        destroyed-widget hazard this module's own docstring warns about
        for RunScriptAsync, and was reproduced live (destroy the WebView,
        then call GetNativeBackend() from off the GUI thread -> SIGSEGV,
        no Python exception)."""
        if not self._alive:
            return

        def do_mute():
            if not self._alive:
                return
            native = self.webview.GetNativeBackend()
            if native is None:
                return
            if not _native_set_muted(native, muted) and not self._native_mute_warned:
                self._native_mute_warned = True
                logger.warning(
                    "native audio mute unavailable on this platform/WebView build -- "
                    "TX audio will not be muted"
                )

        wx.CallAfter(do_mute)

    async def mute_before_teardown(self, muted: bool = True) -> None:
        """Like set_muted(), but AWAITED end-to-end and does not gate on
        self._alive -- for WebViewHost.destroy_page() to call right
        before close()/Destroy(), so the mute is guaranteed to actually
        run before the widget goes away.

        A real bug caught by a bug-hunter review pass: destroy_page()
        used to call the ordinary set_muted() followed immediately by
        close() (which synchronously clears self._alive with no await in
        between) -- set_muted()'s fire-and-forget wx.CallAfter(do_mute)
        never got a chance to actually run before do_mute's own
        `if not self._alive: return` saw it already cleared. Reproduced
        live: the mute call was a guaranteed no-op every time, not just
        a race. This version is awaited by the caller instead, so the
        GUI thread has genuinely run it before destroy_page() proceeds
        to close()/Destroy()."""
        fut: "asyncio.Future" = self._loop.create_future()

        def do_mute():
            native = self.webview.GetNativeBackend()
            if native is not None:
                _native_set_muted(native, muted)
            self._loop.call_soon_threadsafe(_safe_set_result, fut, None)

        wx.CallAfter(do_mute)
        await fut

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
                    # fut may already be cancelled if the caller gave up
                    # while this was still queued for the GUI thread --
                    # setting an exception on an already-done future is a
                    # silent no-op via _safe_set_exception, which is
                    # exactly right: nobody is listening any more, and
                    # (unlike the success path just below) no completion
                    # event is ever coming for a dispatch that never
                    # happened, so nothing needs to go on the queue.
                    self._loop.call_soon_threadsafe(
                        _safe_set_exception, fut, BrowserError(f"RunScriptAsync failed: {e}")
                    )
                    return
                # RunScriptAsync succeeded -- a real
                # wxEVT_WEBVIEW_SCRIPT_RESULT is now guaranteed for this
                # dispatch, whether or not the caller is still around to
                # receive it (it may already be cancelled -- see
                # self._dispatch_queue's own doc comment for why that's
                # fine to just enqueue anyway rather than needing to
                # special-case it here). Appended on the GUI thread, same
                # as _on_script_result()'s pop -- wx's single-threaded
                # event dispatch serializes the two, no lock needed, and
                # unlike a single "pending future" slot plus a separate
                # orphan counter (an earlier version of this fix), a
                # single FIFO queue can't fall out of sync with itself:
                # every dispatch that actually happened is in it exactly
                # once, in order, whether or not it was later abandoned.
                with self._dispatch_queue_lock:
                    self._dispatch_queue.append(fut)

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
            # A plain CancelledError (the caller itself was cancelled, not
            # a timeout) propagates through unhandled -- nothing to clean
            # up here. fut is already in self._dispatch_queue (or about
            # to be, once do_run() reaches the GUI thread) regardless of
            # cancellation, and _on_script_result() discards it in its
            # correct FIFO position once its real result arrives, same as
            # any other already-done future it might pop.

    def _mark_dead(self, reason: str, notify: bool = True) -> None:
        was_alive = self._alive
        self._alive = False
        self._fail_pending(BrowserError(f"page adapter dead: {reason}"))
        if notify and was_alive and not self._dead_reported and self._on_dead is not None:
            self._dead_reported = True
            cb = self._on_dead
            wx.CallAfter(cb, reason)

    def _fail_pending(self, exc: BrowserError) -> None:
        with self._dispatch_queue_lock:
            pending = list(self._dispatch_queue)
            self._dispatch_queue.clear()
        for fut in pending:
            if not fut.done():
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
        with self._dispatch_queue_lock:
            if not self._dispatch_queue:
                # Nothing was dispatched that could produce this --
                # shouldn't happen (every RunScriptAsync call has a
                # matching queue entry) unless a concurrent
                # _fail_pending() (background thread, via _mark_dead())
                # just drained it -- silently dropping an unexplained
                # event is safer than raising out of a wx event handler
                # either way.
                return
            fut = self._dispatch_queue.popleft()
        if fut.done():
            # The caller that dispatched this one was cancelled before
            # its result came back (see _dispatch_queue's doc comment) --
            # this event is exactly that abandoned call's real, late
            # result. Nothing is listening any more; discard it and let
            # the NEXT event (either another abandoned call further back
            # in the queue, or the currently live one) take its place in
            # line.
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

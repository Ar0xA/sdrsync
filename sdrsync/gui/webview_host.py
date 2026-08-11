"""Bridges SyncEngine's background asyncio thread to the wx GUI thread for
WebView creation/destruction and the audio-unlock click.

GUI REWRITE (spec §6.1): the WebView is embedded INLINE inside
ReceiverHost/ReceiverLive while docked, rather than owning a separate
persistent popup wx.Frame the way the pre-rewrite app did (see git
history before the gui-pixel-perfect-redesign branch for that version).
attach() gives WebViewHost the real parent panel (constructed once, at
MainFrame build time, before the engine exists) that create_page()/
destroy_page() add/remove the WebView widget from via a plain sizer --
mirrors the Browser/Page split Playwright had (WebViewHost =
Browser-lifetime, the WxPageAdapter-wrapped WebView = Page-lifetime),
just parented differently now.

spec §9 (compact bar / undock) moves the WebView to a second frame (or
a hidden one) instead -- see reparent() below. attach()'s `_parent` is
therefore not fixed for the process lifetime the way it used to be;
reparent() is the only thing allowed to change it after construction.

The audio-unlock click (WxPageAdapter._simulate_click, browser_shim.py)
only needs webview.ClientToScreen() to resolve to real on-screen pixels
and the WebView's own top-level window to be topmost at that instant --
neither requires the WebView to live in a dedicated frame, which is
what makes inline embedding work with zero changes to browser_shim.py.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Callable, Optional

import wx
import wx.html2

from sdrsync.browser.backend import target_backend
from sdrsync.config import MIN_WEBVIEW_HEIGHT, MIN_WEBVIEW_WIDTH
from sdrsync.websdr.browser_shim import WxPageAdapter

logger = logging.getLogger("sdrsync.gui.webview_host")


def _display_index_for(point: wx.Point) -> int:
    index = wx.Display.GetFromPoint(point)
    return index if index != wx.NOT_FOUND else 0


def has_display_at(point: wx.Point) -> bool:
    """Whether `point` currently lands on any connected display -- used
    to detect a remembered position from a monitor that's since gone
    away (a docking station unplugged, a resolution/arrangement change),
    so the caller can fall back to a display that still exists instead of
    positioning the frame somewhere unreachable."""
    return wx.Display.GetFromPoint(point) != wx.NOT_FOUND


def clamp_size_to_display(size: wx.Size, near: wx.Point) -> wx.Size:
    """Clamp a requested frame size to the client area of the display
    nearest `near` (with a hard floor), so a size saved on a larger/
    second monitor -- or a stray huge value in a hand-edited
    config.json -- can't produce a window that's partly or fully
    unreachable once it lands. `near` must be the point the frame is
    actually about to be positioned at (see WebViewHost._on_screen_pos)
    -- clamping against a fixed/wrong display would defeat a size that's
    perfectly valid on the monitor the window will really appear on.
    config.py's load()-time validation only enforces the floor (it has
    no way to know the screen); this is the other half of that same
    bound, applied wherever a size actually reaches the frame."""
    try:
        available = wx.Display(_display_index_for(near)).GetClientArea().GetSize()
    except Exception:
        return size
    width = max(MIN_WEBVIEW_WIDTH, min(size.width, available.width))
    height = max(MIN_WEBVIEW_HEIGHT, min(size.height, available.height))
    return wx.Size(width, height)


# SetWindowPos() flags/constants (winuser.h). HWND_TOPMOST (-1) "places
# the window above all non-topmost windows [and] maintains its topmost
# position even when it is deactivated"; HWND_NOTOPMOST (-2) "places the
# window above all non-topmost windows (that is, behind all topmost
# windows). This flag has no effect if the window is already a
# non-topmost window." -- that last sentence is why the pair has to be
# used in that order; see _kick_to_top(). HWND_TOP (0) is deliberately no
# longer used anywhere here: it's the one that needs SetForegroundWindow
# permission, and being silently refused is how the maximized-window bug
# happened.
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SW_RESTORE = 9


_user32_cache = None


def _user32():
    """user32.dll via ctypes with the entry points used below given
    explicit signatures, or None when that isn't available (any non-win32
    platform -- the Linux/WSL target included). The argtypes matter: with
    ctypes' defaults an HWND is marshalled as a C int, which truncates on
    64-bit Windows."""
    global _user32_cache
    if _user32_cache is not None:
        return _user32_cache
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.BOOL,
        ]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        _user32_cache = user32
        return user32
    except Exception as e:  # pragma: no cover -- win32-only, defensive
        logger.debug("user32 unavailable, falling back to wx Raise(): %s", e)
        return None


def _current_thread_id() -> int:
    """GetCurrentThreadId() (kernel32, not user32) -- the wx GUI thread's
    id, which is also the thread that owns both of this app's frames.
    Returns 0 if it can't be obtained, which callers treat as "skip the
    AttachThreadInput step"."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        return int(kernel32.GetCurrentThreadId())
    except Exception as e:  # pragma: no cover -- win32-only, defensive
        logger.debug("GetCurrentThreadId unavailable: %s", e)
        return 0


def _kick_to_top(user32, hwnd) -> None:
    """Raises `hwnd` to the top of the ordinary (non-topmost) window band
    -- above every other normal window on the desktop, including a
    maximized one -- without activating it, without stealing focus, and
    (crucially) without needing SetForegroundWindow permission.

    SetWindowPos's own MSDN page states the permission requirement only
    for the "bring a window to the top" case ("To use SetWindowPos to
    bring a window to the top, the process that owns the window must have
    SetForegroundWindow permission"), i.e. HWND_TOP. Moving a window
    between the topmost and non-topmost *bands* carries no such
    documented requirement -- that's what every always-on-top utility
    does from the background. So HWND_TOPMOST followed immediately by
    HWND_NOTOPMOST lands the window at the top of the normal band by a
    route that can't be silently refused the way a bare HWND_TOP can.
    The order is forced by the docs: HWND_NOTOPMOST alone "has no effect
    if the window is already a non-topmost window", so it only does the
    raising after the HWND_TOPMOST call has made the window topmost.

    The HWND_NOTOPMOST half is in a finally: if anything goes wrong in
    between, the frame must NOT be left permanently always-on-top. Safe
    for these two frames specifically because neither is ever
    legitimately topmost -- nothing here has a topmost state to lose.
    """
    flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
    try:
        user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, flags)
    finally:
        user32.SetWindowPos(hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0, flags)


class _foreground_input_attached:
    """Context manager around the classic AttachThreadInput technique:
    while this app's GUI thread shares an input queue with the thread
    that owns the *current* foreground window, this process satisfies
    SetForegroundWindow's "the calling process is the foreground process"
    / focus-ownership tests rather than being treated as a background app
    trying to steal focus.

    Per the AttachThreadInput docs the two threads "share ... input states
    (such as keyboard states and the current focus window)" until
    detached, so the attachment is held for as short a window as possible
    (a handful of SetWindowPos/SetForegroundWindow calls) and detached in
    a finally -- leaving this process' input queue permanently welded to
    another app's would be a real hazard if that app ever hung. No-ops
    (and stays a no-op on exit) when there is no foreground window, when
    the foreground window is already one of ours, or when the attach call
    itself fails -- the docs list several ways it legitimately can (no
    message queue on either thread, a journal record hook installed,
    another desktop)."""

    def __init__(self, user32) -> None:
        self._user32 = user32
        self._ours = 0
        self._theirs = 0
        self._attached = False

    def __enter__(self) -> "_foreground_input_attached":
        try:
            foreground = self._user32.GetForegroundWindow()
            if not foreground:
                return self
            self._theirs = int(self._user32.GetWindowThreadProcessId(foreground, None))
            self._ours = _current_thread_id()
            if self._theirs and self._ours and self._theirs != self._ours:
                self._attached = bool(
                    self._user32.AttachThreadInput(self._ours, self._theirs, True)
                )
                if not self._attached:
                    logger.debug(
                        "AttachThreadInput(%s -> %s) refused; continuing unattached",
                        self._ours, self._theirs,
                    )
        except Exception as e:  # pragma: no cover -- win32-only, defensive
            logger.debug("AttachThreadInput setup failed (non-fatal): %s", e)
        return self

    def __exit__(self, *_exc) -> bool:
        if self._attached:
            try:
                self._user32.AttachThreadInput(self._ours, self._theirs, False)
            except Exception as e:  # pragma: no cover -- win32-only, defensive
                logger.debug("AttachThreadInput detach failed: %s", e)
            self._attached = False
        return False


def bring_pair_to_front(top: "wx.TopLevelWindow", below: Optional["wx.TopLevelWindow"] = None) -> None:
    """Brings `top` to the front of the whole desktop and leaves `below`
    (if given) parked immediately underneath it -- so the user sees BOTH
    of this app's windows, `top` over `below`, with `below` never pushed
    *down* past some third-party window.

    The win32 semantics this leans on, taken from the current MSDN pages
    for SetWindowPos/SetForegroundWindow (each of the three earlier
    attempts at this failed on one of them):

    1. SetWindowPos's `hWndInsertAfter` is "a handle to the window to
       PRECEDE the positioned window in the Z order" -- the positioned
       window (`hWnd`) ends up *below* `hWndInsertAfter`. So putting the
       WebSDR frame over MainFrame is SetWindowPos(MainFrame,
       hWndInsertAfter=WebSDR frame) -- the reverse of the intuitive
       reading, and the reason an earlier attempt put the WebSDR page
       *under* the control panel.
    2. "If an application is not in the foreground, and should be in the
       foreground, it must call the SetForegroundWindow function", and
       "to use SetWindowPos to bring a window to the top, the process
       that owns the window must have SetForegroundWindow permission".
       So SWP_NOACTIVATE alone can never lift this app out from behind an
       unrelated foreground app -- a z-order-only call is not enough.
    3. SetForegroundWindow is permitted when "the calling process is the
       foreground process" or "the calling process received the last
       input event". Both hold here, because this runs synchronously out
       of the user's own click on MainFrame's Connect button. If it is
       refused anyway, the documented consequence is just a flashing
       taskbar button -- and step 4 still leaves the two windows in the
       right order relative to each other.
    4. `below` is only ever moved UP (to directly under `top`), never
       down. Nothing here can bury MainFrame behind another application,
       which is what wx's Raise() (SetForegroundWindow on wxMSW, applied
       to the WebSDR frame alone) was doing on a later connect.

    Attempt 4 (points 1-4 above: HWND_TOP + SetForegroundWindow + the
    `below` fix-up) was confirmed live to work against ordinary
    background windows, but still lost to a *maximized* foreground app --
    the WebSDR window came up behind it. Two documented reasons, both
    fixed here rather than guessed at:

    5. HWND_TOP is exactly the case SetWindowPos's own page hedges: "To
       use SetWindowPos to bring a window to the top, the process that
       owns the window must have SetForegroundWindow permission" -- and
       SetForegroundWindow's page ends its list of qualifying conditions
       with "It is possible for a process to be denied the right to set
       the foreground window even if it meets these conditions". A
       refused HWND_TOP is silent (SetWindowPos still returns success for
       the move/size part). _kick_to_top()'s HWND_TOPMOST ->
       HWND_NOTOPMOST pair reaches the same place through the band
       machinery instead, which carries no such documented permission
       requirement -- so the z-order half now holds even when the
       foreground half is refused.
    6. AttachThreadInput: for the duration of the calls below this app's
       GUI thread shares an input queue (and therefore focus state) with
       whatever thread owns the current foreground window, which is the
       long-standing way of satisfying SetForegroundWindow's foreground/
       last-input-event conditions when the strict reading of them would
       otherwise fail. See _foreground_input_attached for the caveats.

    Ordering below: `below` is kicked up first, then `top` (so `top` ends
    above it), then the foreground grab, then the exact `below`-under-
    `top` fix-up as the last word on relative order.
    """
    user32 = _user32()
    if user32 is None:
        # Non-win32: no equivalent primitive; Raise() at least restores a
        # sane in-app z-order.
        try:
            if below is not None:
                below.Raise()
            top.Raise()
        except Exception as e:  # pragma: no cover -- defensive
            logger.debug("Raise() fallback failed: %s", e)
        return
    try:
        top_hwnd = top.GetHandle()
        below_hwnd = below.GetHandle() if below is not None else None
        with _foreground_input_attached(user32):
            if user32.IsIconic(top_hwnd):
                # A minimized window has no useful z-order; restoring it
                # is a precondition for any of the rest to be visible.
                user32.ShowWindow(top_hwnd, _SW_RESTORE)
            if below_hwnd is not None:
                _kick_to_top(user32, below_hwnd)
            _kick_to_top(user32, top_hwnd)
            if not user32.SetForegroundWindow(top_hwnd):
                # Not fatal any more: _kick_to_top() has already put both
                # windows above the rest of the desktop, so the user sees
                # them; only keyboard focus stays where it was.
                logger.info(
                    "SetForegroundWindow refused (foreground lock) -- windows raised "
                    "above the desktop anyway, focus left with the other app"
                )
            if below_hwnd is not None:
                # ...and MainFrame directly beneath it (see point 1 above
                # for why the arguments read backwards).
                user32.SetWindowPos(
                    below_hwnd, top_hwnd, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
                )
    except Exception as e:  # pragma: no cover -- win32-only, defensive
        logger.debug("bring_pair_to_front failed (non-fatal): %s", e)


def _raise_without_activating(win: "wx.TopLevelWindow") -> None:
    """Puts `win` at the top of the z-order without touching activation,
    focus or the desktop-wide foreground. Used right before the
    audio-unlock click: wx.UIActionSimulator injects a real OS click at a
    screen *point*, which is delivered to whatever window happens to be
    topmost there -- if something else covers the host frame, the click
    lands in that other app instead (activating it -- which visibly
    shoves this app's windows behind it -- and leaving the autoplay
    gesture unsatisfied).

    Uses _kick_to_top() rather than a plain HWND_TOP SetWindowPos for the
    same reason bring_pair_to_front() does: HWND_TOP is documented to
    require SetForegroundWindow permission, and this runs from the
    engine's own timing (a CallAfter, well after the user's click), which
    is exactly when that permission is least certain to still be held --
    the case where the raise silently no-ops and the injected click lands
    in a maximized window behind this one. The band route needs no such
    permission and still never touches activation or focus."""
    user32 = _user32()
    if user32 is None:
        return
    try:
        _kick_to_top(user32, win.GetHandle())
    except Exception as e:  # pragma: no cover -- win32-only, defensive
        logger.debug("_raise_without_activating failed (non-fatal): %s", e)


class WebViewHost:
    """Construct once, in the wx App's OnInit(), before MainFrame exists
    (matches the pre-rewrite ordering, so SDRSyncApp.OnInit() and
    sync/engine.py's WebViewHost usage need no changes). attach() is
    then called once MainFrame has built ReceiverLive's content-host
    panel, before the engine's background thread starts -- create_page()/
    destroy_page() are no-ops-that-error if called before attach()
    happens, which would be a real construction-order bug, not a state
    this app is ever meant to reach at runtime.
    """

    def __init__(self) -> None:
        self._parent: Optional["wx.Window"] = None
        self._current_webview: Optional["wx.html2.WebView"] = None

    def attach(self, parent: "wx.Window") -> None:
        """GUI-thread only. `parent` is ReceiverLive's content-host panel
        (a plain wx.Panel with its own one-slot vertical sizer) -- see
        receiver_live.py. Called once by MainFrame right after
        ReceiverHost/ReceiverLive are built."""
        assert wx.IsMainThread()
        self._parent = parent

    def reparent(self, new_parent: "wx.Window") -> None:
        """GUI-thread only, synchronous. Moves the live WebView (if any)
        to `new_parent` in place -- spec §9's undock/dock requires this
        explicitly ("reparent the WebView, do not recreate it (reloading
        loses the audio session)"). A no-op if there's currently no
        WebView (e.g. undocking before any WebSDR session started).

        wx.Window.Reparent() issues a native SetParent() call on MSW --
        the same primitive wx itself uses internally for things like
        moving a control between notebook pages -- so this should carry
        the underlying WebView2 HWND across cleanly. Unverified for a
        WebView2 control specifically until confirmed live with real
        playing audio; if Reparent() ever proves unreliable here, the
        fallback is a one-time destroy_page()+create_page() into the
        new parent (a visible reload -- worse than spec, but keeps
        undock functional) rather than silently leaving the widget in a
        broken state."""
        assert wx.IsMainThread()
        webview = self._current_webview
        if webview is None:
            self._parent = new_parent
            return
        old_parent = webview.GetParent()
        old_sizer = old_parent.GetSizer() if old_parent is not None else None
        if old_sizer is not None:
            old_sizer.Detach(webview)
        webview.Reparent(new_parent)
        sizer = new_parent.GetSizer()
        if sizer is None:
            sizer = wx.BoxSizer(wx.VERTICAL)
            new_parent.SetSizer(sizer)
        sizer.Add(webview, 1, wx.EXPAND)
        new_parent.Layout()
        if old_parent is not None:
            old_parent.Layout()
        self._parent = new_parent

    def present(self, on_screen: bool) -> None:
        """GUI-thread only. Passed to WxPageAdapter as its
        on_screen_presenter, called around the audio-unlock click
        (browser_shim.py's _simulate_click). Docked-only model (spec §9
        is out of scope) -- there is no off-screen resting state to
        restore to any more, so the False branch is a no-op. The True
        branch raises the WebView's own top-level window (MainFrame) so
        the simulated click's screen-coordinate delivery lands on it
        rather than whatever else may be covering the desktop at that
        instant. Deliberately z-order only (no activation/foreground
        grab) -- see _raise_without_activating()."""
        assert wx.IsMainThread()
        if not on_screen or self._parent is None:
            return
        top = self._parent.GetTopLevelParent()
        if top is not None:
            _raise_without_activating(top)

    async def create_page(
        self,
        loop: "asyncio.AbstractEventLoop",
        on_dead: Optional[Callable[[str], None]] = None,
    ) -> WxPageAdapter:
        """Creates a new WebView child widget inside the attached parent
        panel and wraps it in a WxPageAdapter. Callable from any thread."""
        fut: "asyncio.Future" = loop.create_future()

        def do_create():
            try:
                if self._parent is None:
                    raise RuntimeError("WebViewHost.attach() was never called")
                webview = wx.html2.WebView.New(self._parent, backend=target_backend())
                sizer = self._parent.GetSizer()
                if sizer is None:
                    sizer = wx.BoxSizer(wx.VERTICAL)
                    self._parent.SetSizer(sizer)
                sizer.Add(webview, 1, wx.EXPAND)
                self._parent.Layout()
                self._current_webview = webview
                adapter = WxPageAdapter(
                    webview, loop=loop, on_screen_presenter=self.present, on_dead=on_dead,
                )
            except Exception as e:
                loop.call_soon_threadsafe(_safe_set_exception, fut, e)
                return
            loop.call_soon_threadsafe(_safe_set_result, fut, adapter)

        wx.CallAfter(do_create)
        return await fut

    async def destroy_page(self, page: WxPageAdapter, loop: "asyncio.AbstractEventLoop") -> None:
        """Callable from any thread. Marks the adapter dead first (via
        close()) -- RunScriptAsync on an already-Destroy()'d WebView
        segfaults the whole process, confirmed during Block B, so
        ordering here is load-bearing, not stylistic.

        Awaits the actual GUI-thread Destroy(), symmetric to create_page()
        -- a bare wx.CallAfter() with no completion signal let the caller
        (SyncEngine._stop_websdr()) consider a session fully torn down,
        and a subsequent Switch start a brand new create_page() for the
        replacement, before the old widget had actually been destroyed.
        Live-reported (pre-rewrite): an intermittent race where Switch
        WebSDR left the old widget's slot hidden (audio from the new
        session still audible, nothing visible) -- the old CallAfter and
        the new one could reach the GUI thread in either order once a
        round trip through the engine's status-queue/GUI-timer polling
        separated them, since neither was awaited end-to-end."""
        await page.close()

        fut: "asyncio.Future" = loop.create_future()

        def do_destroy():
            if self._current_webview is page.webview:
                self._current_webview = None
            try:
                if self._parent is not None:
                    sizer = self._parent.GetSizer()
                    if sizer is not None:
                        sizer.Detach(page.webview)
                page.webview.Destroy()
                if self._parent is not None:
                    self._parent.Layout()
            except Exception as e:
                logger.debug("Non-fatal error destroying WebView widget: %s", e)
            loop.call_soon_threadsafe(_safe_set_result, fut, None)

        wx.CallAfter(do_destroy)
        await fut


def _safe_set_result(fut: "asyncio.Future", value) -> None:
    if not fut.done():
        fut.set_result(value)


def _safe_set_exception(fut: "asyncio.Future", exc: Exception) -> None:
    if not fut.done():
        fut.set_exception(exc)

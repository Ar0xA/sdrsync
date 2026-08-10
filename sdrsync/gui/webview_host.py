"""Bridges SyncEngine's background asyncio thread to the wx GUI thread for
WebView creation/destruction and the audio-unlock on-screen/off-screen
dance.

Mirrors the Browser/Page split Playwright had: one persistent, offscreen
host wx.Frame lives for the whole app session (the Browser-lifetime
analogue -- created once, never destroyed until the app closes), and a
WxPageAdapter-wrapped wx.html2.WebView child widget is created/destroyed
inside it per WebSDR connect/switch/disconnect (the Page-lifetime
analogue). See the v5 migration plan's Block A/B/C notes for why a
per-connection top-level Frame was rejected (structurally reintroduces
the "MainLoop needs a synchronous frame before it starts" issue Block A
hit) in favor of this shape.
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
from sdrsync.resources import ICON_PATH
from sdrsync.websdr.browser_shim import WxPageAdapter

logger = logging.getLogger("sdrsync.gui.webview_host")

# Mirrors the off-screen position the Playwright-based engine already used
# for "headless" mode (a real, positioned window, not Chromium's actual
# headless mode, which has no audio output at all -- see the removed
# _describe_browser_launch_error/engine.py comment history for why).
# Confirmed to still work (audio survives the move, via SetPosition())
# under WSLg's XWayland bridge on Linux -- see project_brief.md's
# 2026-08-07 Linux spike. Known, unsolved limitation: a native (non-
# XWayland) Wayland compositor may restrict a client's ability to
# reposition its own window this way; not something that can be tested
# in this environment, so it's documented here rather than "fixed".
OFF_SCREEN_POS = wx.Point(-32000, -32000)
# Also doubles as the "visible mode" resting position -- any genuine
# on-screen position works equally well for both the audio-unlock click
# (wx.UIActionSimulator needs real on-screen delivery) and for a user who
# wants to actually see the WebSDR page.
ON_SCREEN_POS = wx.Point(50, 50)
# Bumped from the original 1000x700 -- reported as "kind of small" for
# comfortably reading a WebSDR waterfall. This is only the fallback for a
# driver_type AppSettings.webview_sizes has no remembered size for yet
# (first run, or a site type never connected to before); see
# gui/app.py's _restore_webview_geometry().
VISIBLE_SIZE = wx.Size(1280, 900)


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


def on_screen_pos_for_monitor(reference_window: "wx.Window") -> wx.Point:
    """A resting/visible position on the same display as reference_window
    (MainFrame, "the base app window") -- used the first time a
    driver_type has no AppSettings.webview_positions entry of its own
    (a fresh install, or a driver_type never connected to before), so
    the WebSDR window doesn't default onto a fixed, possibly-wrong
    monitor. Anchored near that display's own client-area origin (the
    same (50, 50)-style offset ON_SCREEN_POS uses, just relative to the
    right monitor instead of always the primary one)."""
    try:
        index = wx.Display.GetFromWindow(reference_window)
        if index == wx.NOT_FOUND:
            index = 0
        origin = wx.Display(index).GetClientArea().GetTopLeft()
    except Exception:
        return ON_SCREEN_POS
    return wx.Point(origin.x + 50, origin.y + 50)


class WebViewHost:
    """Owns the persistent host frame. Construct once, in the wx App's
    OnInit(), before the SyncEngine's background thread starts.

    "headless" here means the same thing AppSettings.headless always has:
    off-screen-but-shown (for real audio -- see OFF_SCREEN_POS above), not
    an actually-hidden window. present(False) restores whichever of
    on-screen/off-screen the current headless setting calls "resting" --
    it must NOT hardcode off-screen, since it's also called after every
    audio-unlock click (see WxPageAdapter._simulate_click's finally
    block), which would otherwise silently undo a user's "show the
    window" choice on every single connection."""

    def __init__(self, headless: bool = False) -> None:
        self.frame = wx.Frame(
            None, title="SDRSync WebSDR",
            style=wx.DEFAULT_FRAME_STYLE | (wx.FRAME_NO_TASKBAR if headless else 0),
        )
        self._current_webview: Optional["wx.html2.WebView"] = None
        # Set by MainFrame once it exists (WebViewHost is constructed
        # before it, in SDRSyncApp.OnInit()) -- see _on_frame_close_requested.
        self.on_close_requested: Optional[Callable[[], None]] = None
        self.frame.Bind(wx.EVT_SIZE, self._on_frame_size)
        if ICON_PATH.exists():
            try:
                icon = wx.Icon(str(ICON_PATH), wx.BITMAP_TYPE_ICO)
                if icon.IsOk():
                    self.frame.SetIcon(icon)
                else:
                    logger.warning("App icon at %s failed to load (not IsOk())", ICON_PATH)
            except Exception as e:
                logger.warning("Could not load app icon from %s (%s)", ICON_PATH, e)
        else:
            logger.warning("App icon not found at %s", ICON_PATH)
        # Default resting on-screen point until gui/app.py's
        # _restore_webview_geometry() picks a better one (a remembered
        # position, or the main app window's own monitor) -- there is no
        # MainFrame yet at this point in startup for either of those to
        # reference.
        self._on_screen_pos = ON_SCREEN_POS
        self.frame.SetSize(clamp_size_to_display(VISIBLE_SIZE, self._on_screen_pos))
        self._headless = headless
        self.frame.SetPosition(self._rest_pos())
        # Since headless=False now genuinely shows this frame on-screen
        # (not just briefly, for the audio-unlock click), it has a normal
        # close box -- but nothing else in this app expects this frame
        # itself to ever actually be destroyed before the whole app closes
        # (WxPageAdapter/create_page/present all assume self.frame stays
        # alive for the whole session). So the close is always vetoed;
        # MainFrame._on_close() tears this frame down via frame.Destroy()
        # directly, which doesn't fire EVT_CLOSE, so that shutdown path is
        # unaffected. A user clicking this window's own X, though, clearly
        # means "I'm done with this WebSDR" -- routed to on_close_requested
        # (see above) so it behaves like Disconnect WebSDR instead of the
        # window just silently refusing to close.
        self.frame.Bind(wx.EVT_CLOSE, self._on_frame_close_requested)
        # Real, shown (not Show(False)/Iconize()) -- WebView2 suspends/
        # throttles rendering and timers for genuinely hidden windows;
        # positioning off-screen while still "shown" is what keeps audio
        # and JS timers running reliably (confirmed during Block A/B).
        self.frame.Show(True)

    def _rest_pos(self) -> wx.Point:
        return OFF_SCREEN_POS if self._headless else self._on_screen_pos

    def _on_frame_close_requested(self, evt: "wx.CloseEvent") -> None:
        evt.Veto()
        if self.on_close_requested is not None:
            self.on_close_requested()

    def _on_frame_size(self, event: wx.SizeEvent) -> None:
        if self._current_webview is not None:
            self._current_webview.SetSize(self.frame.GetClientSize())
        event.Skip()

    def set_headless(self, headless: bool) -> None:
        """GUI-thread only. Call before starting a WebSDR connection to
        pick up the current AppSettings.headless value -- picked up fresh
        per-connect, not live-toggled while already connected (out of
        scope for now)."""
        assert wx.IsMainThread()
        self._headless = headless
        self.frame.SetPosition(self._rest_pos())
        # A shown, visible-mode window is a real, separate top-level
        # window with its own taskbar button and Alt-Tab entry -- users
        # need to be able to switch to it directly to click the page
        # itself (audio-unlock, and now reverse sync). Headless keeps
        # wx.FRAME_NO_TASKBAR (it's parked off-screen, nothing to switch
        # to). wxMSW supports toggling this style on an already-shown
        # frame at runtime (internally re-shows the window); confirmed
        # live on this platform, not assumed from docs alone.
        currently_no_taskbar = bool(self.frame.GetWindowStyleFlag() & wx.FRAME_NO_TASKBAR)
        if currently_no_taskbar != headless:
            self.frame.ToggleWindowStyle(wx.FRAME_NO_TASKBAR)

    def set_size(self, width: int, height: int) -> None:
        """GUI-thread only. Resizes the persistent host frame -- used by
        gui/app.py to restore a per-driver_type remembered size on
        connect/switch. Clamped against whichever display self._on_screen_pos
        currently points at -- call set_on_screen_position() first if
        restoring both together, so the clamp matches where the frame is
        actually about to sit."""
        assert wx.IsMainThread()
        self.frame.SetSize(clamp_size_to_display(wx.Size(width, height), self._on_screen_pos))

    def set_on_screen_position(self, point: wx.Point) -> None:
        """GUI-thread only. Updates the resting on-screen point (used by
        _rest_pos()/present() from here on) and repositions the frame
        immediately if it's not currently in headless/off-screen resting
        state -- used by gui/app.py to restore a per-driver_type
        remembered position, or fall back to on_screen_pos_for_monitor()
        the first time a driver_type has none."""
        assert wx.IsMainThread()
        self._on_screen_pos = point
        self.frame.SetPosition(self._rest_pos())

    def present(self, on_screen: bool) -> None:
        """GUI-thread only. Passed to WxPageAdapter as its
        on_screen_presenter -- called around the audio-unlock click."""
        assert wx.IsMainThread()
        self.frame.SetPosition(self._on_screen_pos if on_screen else self._rest_pos())
        if on_screen:
            # The simulated click that follows is delivered to whatever
            # window is topmost at that screen point -- make sure that's
            # this one. Deliberately z-order only (no activation/
            # foreground grab): see _raise_without_activating().
            _raise_without_activating(self.frame)

    def bring_to_front_over(self, base: "wx.TopLevelWindow") -> None:
        """GUI-thread only. Brings this app forward on the desktop with
        the WebSDR frame sitting directly on top of `base` (MainFrame,
        the control panel the user just clicked Connect in) -- see
        bring_pair_to_front() for the win32 semantics and why neither a
        bare Show(), nor Raise(), nor a z-order-only SetWindowPos got
        this right on its own.

        Call this AFTER set_headless()/set_size()/set_on_screen_position()
        for a connection, not before: toggling wx.FRAME_NO_TASKBAR
        re-shows the frame internally, which resets the z-order this
        establishes.

        In headless mode there is nothing to put on top of anything (the
        frame is parked off-screen and has no taskbar button), so only
        `base` is brought forward -- otherwise the app would hand focus
        to an invisible window."""
        assert wx.IsMainThread()
        if self._headless or not self.frame.IsShown():
            bring_pair_to_front(base)
            return
        bring_pair_to_front(self.frame, base)

    def set_frame_visible(self, visible: bool) -> None:
        """GUI-thread only. Hides/shows the persistent host frame
        outright -- unlike headless mode (still Show()'n, just
        off-screen, to keep WebView2 rendering/audio/timers alive for a
        LIVE connection -- see __init__'s docstring), this is for when
        there is no WebSDR connection at all: nothing needs to keep
        running, so a genuinely hidden window is safe here and avoids an
        empty frame sitting on screen (or in the taskbar/Alt-Tab) between
        sessions. Always called with the frame in this idle, no-child-
        webview state -- gui/app.py shows it again before starting a new
        connection, well before any page/audio-unlock activity begins.

        Deliberately does NOT touch z-order: on a connect the frame is
        shown before its headless style, size and position are settled,
        and toggling wx.FRAME_NO_TASKBAR internally re-shows the frame,
        which would throw away any ordering established here. gui/app.py
        calls bring_to_front_over() once, after all of that."""
        assert wx.IsMainThread()
        self.frame.Show(visible)

    async def create_page(
        self,
        loop: "asyncio.AbstractEventLoop",
        on_dead: Optional[Callable[[str], None]] = None,
    ) -> WxPageAdapter:
        """Creates a new WebView child widget inside the host frame and
        wraps it in a WxPageAdapter. Callable from any thread."""
        fut: "asyncio.Future" = loop.create_future()

        def do_create():
            try:
                webview = wx.html2.WebView.New(self.frame, backend=target_backend())
                # WebView.New() defaults to wx.DefaultSize, which does NOT
                # fill the parent frame on its own (no sizer is used here,
                # deliberately, since only one webview is ever live at a
                # time) -- without this, the widget can sit at a small
                # default size while the frame's own background shows
                # through everywhere else, which is indistinguishable at a
                # glance from the separate WebView2-repaint issue
                # _nudge_repaint() addresses. _on_frame_size() keeps this
                # in sync if the frame is resized later.
                webview.SetSize(self.frame.GetClientSize())
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

    async def destroy_page(self, page: WxPageAdapter) -> None:
        """Callable from any thread. Marks the adapter dead first (via
        close()) -- RunScriptAsync on an already-Destroy()'d WebView
        segfaults the whole process, confirmed during Block B, so
        ordering here is load-bearing, not stylistic."""
        await page.close()

        def do_destroy():
            if self._current_webview is page.webview:
                self._current_webview = None
            try:
                page.webview.Destroy()
            except Exception as e:
                logger.debug("Non-fatal error destroying WebView widget: %s", e)

        wx.CallAfter(do_destroy)


def _safe_set_result(fut: "asyncio.Future", value) -> None:
    if not fut.done():
        fut.set_result(value)


def _safe_set_exception(fut: "asyncio.Future", exc: Exception) -> None:
    if not fut.done():
        fut.set_exception(exc)

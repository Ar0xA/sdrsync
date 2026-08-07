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
from typing import Callable, Optional

import wx
import wx.html2

from sdrsync.browser.backend import target_backend
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
VISIBLE_SIZE = wx.Size(1000, 700)


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
            style=wx.DEFAULT_FRAME_STYLE | wx.FRAME_NO_TASKBAR,
        )
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
        self.frame.SetSize(VISIBLE_SIZE)
        self._headless = headless
        self.frame.SetPosition(self._rest_pos())
        # Since headless=False now genuinely shows this frame on-screen
        # (not just briefly, for the audio-unlock click), it has a normal
        # close box -- but nothing else in this app expects it to be
        # user-closable (WxPageAdapter/create_page/present all assume
        # self.frame stays alive for the whole app session). Veto a
        # user-initiated close; MainFrame._on_close() tears this frame
        # down via frame.Destroy() directly, which doesn't fire EVT_CLOSE,
        # so that shutdown path is unaffected.
        self.frame.Bind(wx.EVT_CLOSE, lambda evt: evt.Veto())
        # Real, shown (not Show(False)/Iconize()) -- WebView2 suspends/
        # throttles rendering and timers for genuinely hidden windows;
        # positioning off-screen while still "shown" is what keeps audio
        # and JS timers running reliably (confirmed during Block A/B).
        self.frame.Show(True)

    def _rest_pos(self) -> wx.Point:
        return OFF_SCREEN_POS if self._headless else ON_SCREEN_POS

    def set_headless(self, headless: bool) -> None:
        """GUI-thread only. Call before starting a WebSDR connection to
        pick up the current AppSettings.headless value -- picked up fresh
        per-connect, not live-toggled while already connected (out of
        scope for now)."""
        assert wx.IsMainThread()
        self._headless = headless
        self.frame.SetPosition(self._rest_pos())

    def present(self, on_screen: bool) -> None:
        """GUI-thread only. Passed to WxPageAdapter as its
        on_screen_presenter -- called around the audio-unlock click."""
        assert wx.IsMainThread()
        self.frame.SetPosition(ON_SCREEN_POS if on_screen else self._rest_pos())

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

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

from sdrsync.websdr.browser_shim import WxPageAdapter

logger = logging.getLogger("sdrsync.gui.webview_host")

# Mirrors the off-screen position the Playwright-based engine already used
# for "headless" mode (a real, positioned window, not Chromium's actual
# headless mode, which has no audio output at all -- see the removed
# _describe_browser_launch_error/engine.py comment history for why).
OFF_SCREEN_POS = wx.Point(-32000, -32000)
# Used only briefly, during the audio-unlock click -- wx.UIActionSimulator
# needs a real on-screen position for the OS to deliver input to.
ON_SCREEN_POS = wx.Point(0, 0)


class WebViewHost:
    """Owns the persistent offscreen host frame. Construct once, in the
    wx App's OnInit(), before the SyncEngine's background thread starts."""

    def __init__(self) -> None:
        self.frame = wx.Frame(
            None, title="SDRSync (hidden WebSDR host)",
            style=wx.DEFAULT_FRAME_STYLE | wx.FRAME_NO_TASKBAR,
        )
        self.frame.SetPosition(OFF_SCREEN_POS)
        # Real, shown (not Show(False)/Iconize()) -- WebView2 suspends/
        # throttles rendering and timers for genuinely hidden windows;
        # positioning off-screen while still "shown" is what keeps audio
        # and JS timers running reliably (confirmed during Block A/B).
        self.frame.Show(True)

    def present(self, on_screen: bool) -> None:
        """GUI-thread only. Passed to WxPageAdapter as its
        on_screen_presenter -- called around the audio-unlock click."""
        assert wx.IsMainThread()
        self.frame.SetPosition(ON_SCREEN_POS if on_screen else OFF_SCREEN_POS)

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
                webview = wx.html2.WebView.New(self.frame, backend=wx.html2.WebViewBackendEdge)
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

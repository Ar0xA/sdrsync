"""ReceiverHost (spec §6) -- swaps between ReceiverIdle (two copy
states) and ReceiverLive (inline WebView), eager-create + Show/Hide,
same pattern as SettingsHost.
"""
from __future__ import annotations

import wx

from . import theme
from .receiver_idle import ReceiverIdle
from .receiver_live import ReceiverLive
from .state import AppState


class ReceiverHost(wx.Panel):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.BG)
        self._sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self._sizer)

        self.idle = ReceiverIdle(self)
        self.live = ReceiverLive(self)
        self._sizer.Add(self.idle, 1, wx.EXPAND)
        self._sizer.Add(self.live, 1, wx.EXPAND)
        self.live.Hide()

        self._mode = "idle"

    def refresh(self, state: AppState) -> None:
        # Live as soon as a WebSDR session has been requested (sdr_active),
        # not only once fully confirmed connected (sdr_connected) -- the
        # WebView must already be visible with a real size for WebView2 to
        # initialize; see AppState.sdr_active's own docstring.
        if state.rig_connected and state.sdr_active:
            mode = "live"
        else:
            mode = "idle"
            self.idle.set_variant("rig_down" if not state.rig_connected else "no_sdr")
        if mode != self._mode:
            self.idle.Show(mode == "idle")
            self.live.Show(mode == "live")
            self._mode = mode
            self.Layout()
        if mode == "live":
            self.live.set_site_name(state.site)

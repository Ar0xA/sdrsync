"""SettingsHost -- shows at most one of the three settings panels at a
time (spec §5 intro: "Exactly zero or one panel is ever visible"),
hidden entirely when none is open.
"""
from __future__ import annotations

from typing import Optional

import wx

from . import theme


class SettingsHost(wx.Panel):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.BG)
        self._sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self._sizer)
        self._panels: dict[str, wx.Window] = {}
        self._open_key: Optional[str] = None
        self.Hide()

    def add_panel(self, key: str, panel: wx.Window) -> None:
        self._panels[key] = panel
        self._sizer.Add(panel, 1, wx.EXPAND)
        panel.Hide()

    def show_panel(self, key: Optional[str]) -> None:
        if key == self._open_key:
            return
        for k, panel in self._panels.items():
            panel.Show(k == key)
        self._open_key = key
        self.Show(key is not None)
        top = self.GetTopLevelParent()
        self.Layout()
        if top is not None:
            top.Layout()

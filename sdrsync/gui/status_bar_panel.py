"""StatusBarPanel (spec §8) -- fixed 28px single-line status band:
rig dot + "{rig} · {sync} · {site}" + Open log folder.
"""
from __future__ import annotations

from typing import Callable, Optional

import wx

from . import theme
from .fonts import value_font_at
from .strip_panel import _Dot
from .widgets import FlatButton


class StatusBarPanel(wx.Panel):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.PANEL_ALT)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize(wx.Size(-1, self.FromDIP(theme.STATUS_BAR_HEIGHT)))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda evt: None)

        self.on_open_log_folder: Optional[Callable[[], None]] = None

        pad = self.FromDIP(14)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.dot = _Dot(self, size_dip=5)
        sizer.Add(self.dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, pad)

        self.text = wx.StaticText(self, label="")
        self.text.SetFont(value_font_at(8))
        self.text.SetForegroundColour(theme.MUTED)
        self.text.SetBackgroundColour(theme.PANEL_ALT)
        sizer.Add(self.text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(5))

        sizer.AddStretchSpacer(1)

        self.log_btn = FlatButton(self, "Open log folder")
        self.log_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_open_log_folder and self.on_open_log_folder())
        sizer.Add(self.log_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)

        self.SetSizer(sizer)

    def _on_paint(self, _evt: wx.PaintEvent) -> None:
        dc = wx.BufferedPaintDC(self)
        dc.SetBackground(wx.Brush(theme.PANEL_ALT))
        dc.Clear()
        theme.draw_hairline(dc, self.GetClientRect(), "top")

    def set_dot_mode(self, mode: str) -> None:
        self.dot.set_mode(mode)

    _COLOUR_MODES = {"error": theme.TRANSMIT, "connected": theme.RECEIVE, "muted": theme.MUTED}

    def set_text(self, text: str, colour_mode: str = "muted") -> None:
        colour = self._COLOUR_MODES[colour_mode]
        if self.text.GetForegroundColour() != colour:
            self.text.SetForegroundColour(colour)
            self.text.Refresh()
        if self.text.GetLabel() != text:
            self.text.SetLabel(text)
            self.Layout()

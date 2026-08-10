"""ReceiverLive (spec §6.1) -- the 26px header strip + inline WebView."""
from __future__ import annotations

import wx

from . import theme
from .fonts import label_font


class ReceiverLive(wx.Panel):
    HEADER_HEIGHT = 26

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.BG)

        sizer = wx.BoxSizer(wx.VERTICAL)

        self._header = wx.Panel(self, style=wx.TAB_TRAVERSAL)
        self._header.SetBackgroundColour(theme.BG)
        self._header.SetMinSize(wx.Size(-1, self.FromDIP(self.HEADER_HEIGHT)))
        self._header.Bind(wx.EVT_PAINT, self._on_header_paint)
        self._header.Bind(wx.EVT_ERASE_BACKGROUND, lambda evt: None)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)
        pad = self.FromDIP(8)
        self.title_text = wx.StaticText(self._header, label="")
        self.title_text.SetFont(label_font())
        self.title_text.SetForegroundColour(theme.FAINT)
        self.title_text.SetBackgroundColour(theme.BG)
        header_sizer.Add(self.title_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, pad)
        header_sizer.AddStretchSpacer(1)
        self.latency_text = wx.StaticText(self._header, label="")
        self.latency_text.SetFont(label_font())
        self.latency_text.SetForegroundColour(theme.FAINT)
        self.latency_text.SetBackgroundColour(theme.BG)
        header_sizer.Add(self.latency_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)
        self._header.SetSizer(header_sizer)
        sizer.Add(self._header, 0, wx.EXPAND)

        # The WebView's actual parent -- a plain panel with its own
        # one-slot sizer, handed to WebViewHost.attach() so create_page()/
        # destroy_page() can Add()/Detach() the wx.html2.WebView here
        # without disturbing the header above it.
        self.webview_parent = wx.Panel(self, style=wx.TAB_TRAVERSAL)
        self.webview_parent.SetBackgroundColour(theme.BG)
        self.webview_parent.SetSizer(wx.BoxSizer(wx.VERTICAL))
        sizer.Add(self.webview_parent, 1, wx.EXPAND)

        self.SetSizer(sizer)

    def _on_header_paint(self, _evt: wx.PaintEvent) -> None:
        dc = wx.BufferedPaintDC(self._header)
        dc.SetBackground(wx.Brush(theme.BG))
        dc.Clear()
        theme.draw_hairline(dc, self._header.GetClientRect(), "bottom")

    def set_site_name(self, site_name: str) -> None:
        text = theme.caps(f"Embedded receiver · {site_name}")
        if self.title_text.GetLabel() != text:
            self.title_text.SetLabel(text)
            self._header.Layout()

    def set_latency_text(self, text: str) -> None:
        if self.latency_text.GetLabel() != text:
            self.latency_text.SetLabel(text)
            self._header.Layout()

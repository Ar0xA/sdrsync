"""SectionBar (spec §4) -- the 30px band with the three panel toggles
(TRANSCEIVER/SITES/BEHAVIOUR) and the right-aligned fold hint.
"""
from __future__ import annotations

import wx

from . import theme
from .fonts import label_font
from .state import AppState
from .widgets import ToggleButton

PANELS = ("transceiver", "sites", "behaviour")
_PANEL_LABELS = {"transceiver": "TRANSCEIVER", "sites": "SITES", "behaviour": "BEHAVIOUR"}


class SectionBar(wx.Panel):
    """Presentation only, like StripPanel: exposes panel_buttons (dict
    keyed by "transceiver"/"sites"/"behaviour") for the owner to Bind();
    owns no panel-visibility logic itself (MainFrame's SettingsHost
    does), only the pressed/open highlighting driven by
    AppState.open_panel.
    """

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.PANEL_ALT)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize(wx.Size(-1, self.FromDIP(theme.SECTION_BAR_HEIGHT)))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda evt: None)

        pad = self.FromDIP(16)
        gap = self.FromDIP(26)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.panel_buttons: dict[str, ToggleButton] = {}
        first = True
        for key in PANELS:
            btn = ToggleButton(self, _PANEL_LABELS[key], bordered=False)
            self.panel_buttons[key] = btn
            sizer.Add(btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, pad if first else gap)
            first = False

        sizer.AddStretchSpacer(1)

        self.hint_text = wx.StaticText(self, label="")
        self.hint_text.SetFont(label_font())
        self.hint_text.SetForegroundColour(theme.FAINT)
        self.hint_text.SetBackgroundColour(theme.PANEL_ALT)
        sizer.Add(self.hint_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, pad)

        self.SetSizer(sizer)

    def _on_paint(self, _evt: wx.PaintEvent) -> None:
        dc = wx.BufferedPaintDC(self)
        dc.SetBackground(wx.Brush(theme.PANEL_ALT))
        dc.Clear()
        theme.draw_hairline(dc, self.GetClientRect(), "bottom")

    def refresh(self, state: AppState) -> None:
        for key, btn in self.panel_buttons.items():
            btn.SetValue(state.open_panel == key)

        if state.open_panel is not None:
            hint = "click again to fold"
        elif state.rig_connected:
            hint = "folded"
        else:
            hint = "set up the rig to begin"
        if self.hint_text.GetLabel() != theme.caps(hint):
            self.hint_text.SetLabel(theme.caps(hint))
            self.Layout()

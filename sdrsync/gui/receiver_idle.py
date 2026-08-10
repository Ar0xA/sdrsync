"""ReceiverIdle (spec §6.2) -- the two idle-state copy screens shown
when there's nothing live to receive: rig down ("Connect the
transceiver") and rig up with no WebSDR ("Choose a WebSDR").
"""
from __future__ import annotations

from typing import Callable, Optional

import wx

from . import theme
from .fonts import body_font, heading_font, label_font
from .widgets import FlatButton

# Centred column, "max 46 characters wide" (spec §6.2) -- approximated
# as a pixel width at body_font's size rather than a literal character
# count, since proportional-font character widths vary.
COLUMN_WIDTH_DIP = 340

_COPY = {
    "rig_down": {
        "kicker": "Step one",
        "heading": "Connect the transceiver",
        "body": (
            "SDRSync polls rigctld for frequency, mode and PTT, then steers the "
            "receiver to match. Once the rig is up this panel steps aside and the "
            "receiver takes the window."
        ),
        "primary": "Connect rig",
    },
    "no_sdr": {
        "kicker": "Receiver",
        "heading": "Choose a WebSDR",
        "body": (
            "Pick a saved site from the strip above, or paste a URL. The receiver "
            "docks here and follows the rig."
        ),
        "primary": "Connect WebSDR",
    },
}


class ReceiverIdle(wx.Panel):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.BG)
        self.on_primary: Optional[Callable[[], None]] = None
        self.on_secondary: Optional[Callable[[], None]] = None

        column = wx.BoxSizer(wx.VERTICAL)

        self.kicker_text = wx.StaticText(self, label="", style=wx.ALIGN_CENTRE_HORIZONTAL)
        self.kicker_text.SetFont(label_font())
        self.kicker_text.SetForegroundColour(theme.MUTED)
        self.kicker_text.SetBackgroundColour(theme.BG)
        column.Add(self.kicker_text, 0, wx.ALIGN_CENTER | wx.BOTTOM, self.FromDIP(9))

        self.heading_text = wx.StaticText(self, label="", style=wx.ALIGN_CENTRE_HORIZONTAL)
        self.heading_text.SetFont(heading_font())
        self.heading_text.SetForegroundColour(theme.TEXT)
        self.heading_text.SetBackgroundColour(theme.BG)
        column.Add(self.heading_text, 0, wx.ALIGN_CENTER | wx.BOTTOM, self.FromDIP(14))

        self.body_text = wx.StaticText(
            self, label="", style=wx.ALIGN_CENTRE_HORIZONTAL, size=(self.FromDIP(COLUMN_WIDTH_DIP), -1),
        )
        self.body_text.SetFont(body_font())
        self.body_text.SetForegroundColour(theme.MUTED)
        self.body_text.SetBackgroundColour(theme.BG)
        column.Add(self.body_text, 0, wx.ALIGN_CENTER | wx.BOTTOM, self.FromDIP(18))

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.primary_btn = FlatButton(self, "", is_primary=True)
        self.primary_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_primary and self.on_primary())
        self.secondary_btn = FlatButton(self, "Transceiver settings")
        self.secondary_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_secondary and self.on_secondary())
        btn_row.Add(self.primary_btn, 0, wx.RIGHT, self.FromDIP(9))
        btn_row.Add(self.secondary_btn, 0)
        column.Add(btn_row, 0, wx.ALIGN_CENTER)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.AddStretchSpacer(1)
        outer.Add(column, 0, wx.ALIGN_CENTER)
        outer.AddStretchSpacer(1)
        self.SetSizer(outer)

        self.set_variant("rig_down")

    def set_variant(self, variant: str) -> None:
        copy = _COPY[variant]
        self.kicker_text.SetLabel(theme.caps(copy["kicker"]))
        self.heading_text.SetLabel(copy["heading"])
        self.body_text.SetLabel(copy["body"])
        self.body_text.Wrap(self.FromDIP(COLUMN_WIDTH_DIP))
        self.primary_btn.SetLabel(copy["primary"])
        self.Layout()

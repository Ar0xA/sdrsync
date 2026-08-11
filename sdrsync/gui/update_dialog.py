"""UpdateDialog -- the startup "a newer version is available" popup.
Same kicker/heading/body column language as receiver_idle.py's idle
screens, reused here for a consistent look rather than inventing a
second visual style for what is otherwise the app's only real dialog.
"""
from __future__ import annotations

import webbrowser

import wx

from . import theme
from .fonts import body_font, heading_font, label_font
from .widgets import CheckBox, FlatButton

COLUMN_WIDTH_DIP = 340


class UpdateDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, current_version: str, latest_version: str, release_url: str) -> None:
        super().__init__(parent, title="Update available", style=wx.DEFAULT_DIALOG_STYLE)
        self.SetBackgroundColour(theme.BG)
        self._release_url = release_url

        pad = self.FromDIP(28)
        column = wx.BoxSizer(wx.VERTICAL)

        kicker = wx.StaticText(self, label=theme.caps("Update available"), style=wx.ALIGN_CENTRE_HORIZONTAL)
        kicker.SetFont(label_font())
        kicker.SetForegroundColour(theme.MUTED)
        kicker.SetBackgroundColour(theme.BG)
        column.Add(kicker, 0, wx.ALIGN_CENTER | wx.BOTTOM, self.FromDIP(9))

        heading = wx.StaticText(self, label=f"SDRSync {latest_version}", style=wx.ALIGN_CENTRE_HORIZONTAL)
        heading.SetFont(heading_font())
        heading.SetForegroundColour(theme.TEXT)
        heading.SetBackgroundColour(theme.BG)
        column.Add(heading, 0, wx.ALIGN_CENTER | wx.BOTTOM, self.FromDIP(14))

        body = wx.StaticText(
            self, label=f"You're running {current_version}. See what's new on GitHub.",
            style=wx.ALIGN_CENTRE_HORIZONTAL, size=(self.FromDIP(COLUMN_WIDTH_DIP), -1),
        )
        body.SetFont(body_font())
        body.SetForegroundColour(theme.MUTED)
        body.SetBackgroundColour(theme.BG)
        body.Wrap(self.FromDIP(COLUMN_WIDTH_DIP))
        column.Add(body, 0, wx.ALIGN_CENTER | wx.BOTTOM, self.FromDIP(18))

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.view_btn = FlatButton(self, "View release", is_primary=True)
        self.view_btn.Bind(wx.EVT_BUTTON, self._on_view_release)
        self.close_btn = FlatButton(self, "Close")
        # EndModal(), not Close() -- this dialog is always shown via
        # ShowModal(), which specifically waits for EndModal() to
        # return a value; Close()'s default EVT_CLOSE handling (a plain
        # Destroy()) leaves that call hanging.
        self.close_btn.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE))
        btn_row.Add(self.view_btn, 0, wx.RIGHT, self.FromDIP(9))
        btn_row.Add(self.close_btn, 0)
        column.Add(btn_row, 0, wx.ALIGN_CENTER | wx.BOTTOM, self.FromDIP(18))

        self.ignore_check = CheckBox(self, "Don't show this again for this version")
        column.Add(self.ignore_check, 0, wx.ALIGN_CENTER)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(column, 1, wx.EXPAND | wx.ALL, pad)
        self.SetSizer(outer)
        outer.Fit(self)
        self.CentreOnParent()

    def _on_view_release(self, _evt: wx.CommandEvent) -> None:
        webbrowser.open(self._release_url)
        self.EndModal(wx.ID_CLOSE)

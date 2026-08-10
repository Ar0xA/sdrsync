"""Behaviour settings panel (spec §5.3)."""
from __future__ import annotations

from typing import Callable, Optional

import wx

from .. import theme
from ..fonts import label_font, value_font
from ..widgets import CheckBox
from ...config import AppSettings

SYNC_DIRECTION_ONE_WAY = "Rig -> WebSDR"
SYNC_DIRECTION_BIDIRECTIONAL = "Rig <-> WebSDR (reverse within range)"
SYNC_DIRECTION_CHOICES = [SYNC_DIRECTION_ONE_WAY, SYNC_DIRECTION_BIDIRECTIONAL]


def _kicker(parent: wx.Window, text: str) -> wx.StaticText:
    w = wx.StaticText(parent, label=theme.caps(text))
    w.SetFont(label_font())
    w.SetForegroundColour(theme.MUTED)
    w.SetBackgroundColour(theme.BG)
    return w


class BehaviourPanel(wx.Panel):
    """Sync Direction (session-only, mirrors the old Hold checkbox's own
    never-persisted default) drives engine.set_reverse_sync_held() once
    the engine is reconnected (phase 6) -- selecting "Rig -> WebSDR"
    means held=True (one-way only), bidirectional means held=False.
    Audio device is an inert ["System default"] placeholder: nothing in
    this codebase enumerates system audio devices today (confirmed via
    grep), and WebView2/WebKit expose no per-instance output-device
    routing hook even if it did -- real enumeration is future work, not
    this rewrite.
    """

    def __init__(self, parent: wx.Window, settings: AppSettings) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.settings = settings
        self.SetBackgroundColour(theme.BG)
        self.on_sync_tx_vfo_changed: Optional[Callable[[bool], None]] = None
        self.on_sync_direction_changed: Optional[Callable[[bool], None]] = None  # bool = held

        pad_top, pad_side, pad_bottom = self.FromDIP(18), self.FromDIP(16), self.FromDIP(20)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.AddSpacer(pad_top)

        grid = wx.FlexGridSizer(cols=4, gap=wx.Size(self.FromDIP(14), self.FromDIP(5)))
        for col in range(4):
            grid.AddGrowableCol(col)

        def field(label_text: str, control: wx.Window) -> wx.BoxSizer:
            box = wx.BoxSizer(wx.VERTICAL)
            box.Add(_kicker(self, label_text), 0, wx.BOTTOM, self.FromDIP(5))
            box.Add(control, 0, wx.EXPAND)
            return box

        self.cw_offset_ctrl = wx.SpinCtrl(self, min=-2000, max=2000, initial=settings.cw_offset_hz)
        self.cw_offset_ctrl.SetFont(value_font())
        self.cw_offset_ctrl.Bind(wx.EVT_SPINCTRL, self._on_cw_offset_changed)
        grid.Add(field("CW offset (Hz)", self.cw_offset_ctrl), 1, wx.EXPAND)

        idle_val = settings.websdr_idle_disconnect_min if settings.websdr_idle_disconnect_min is not None else 0
        self.idle_disconnect_ctrl = wx.SpinCtrl(self, min=0, max=600, initial=idle_val)
        self.idle_disconnect_ctrl.SetFont(value_font())
        self.idle_disconnect_ctrl.Bind(wx.EVT_SPINCTRL, self._on_idle_disconnect_changed)
        grid.Add(field("Idle disconnect (min)", self.idle_disconnect_ctrl), 1, wx.EXPAND)

        self.audio_device_choice = wx.Choice(self, choices=["System default"])
        self.audio_device_choice.SetFont(value_font())
        self.audio_device_choice.SetSelection(0)
        self.audio_device_choice.Enable(False)
        self.audio_device_choice.SetToolTip("Device selection isn't implemented yet -- audio follows the OS default")
        grid.Add(field("Audio device", self.audio_device_choice), 1, wx.EXPAND)

        self.sync_direction_choice = wx.Choice(self, choices=SYNC_DIRECTION_CHOICES)
        self.sync_direction_choice.SetFont(value_font())
        self.sync_direction_choice.SetSelection(0)
        self.sync_direction_choice.Bind(wx.EVT_CHOICE, self._on_sync_direction_changed)
        grid.Add(field("Sync direction", self.sync_direction_choice), 1, wx.EXPAND)

        outer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad_side)
        outer.AddSpacer(self.FromDIP(14))

        checks = wx.FlexGridSizer(cols=2, gap=wx.Size(self.FromDIP(28), self.FromDIP(9)))
        checks.AddGrowableCol(0)
        checks.AddGrowableCol(1)

        self.sync_tx_vfo_check = CheckBox(self, "Sync TX VFO (follow split transmit frequency)")
        self.sync_tx_vfo_check.SetValue(settings.sync_tx_vfo)
        self.sync_tx_vfo_check.Bind(wx.EVT_CHECKBOX, self._on_sync_tx_vfo_toggled)
        checks.Add(self.sync_tx_vfo_check, 0)

        self.keep_on_top_check = CheckBox(self, "Keep compact bar always on top")
        self.keep_on_top_check.SetValue(settings.keep_compact_bar_on_top)
        self.keep_on_top_check.Bind(wx.EVT_CHECKBOX, self._on_keep_on_top_toggled)
        checks.Add(self.keep_on_top_check, 0)

        self.hide_when_undocked_check = CheckBox(self, "Hide receiver window when undocked (audio still plays)")
        self.hide_when_undocked_check.SetValue(settings.hide_receiver_when_undocked)
        self.hide_when_undocked_check.Bind(wx.EVT_CHECKBOX, self._on_hide_when_undocked_toggled)
        checks.Add(self.hide_when_undocked_check, 0)

        outer.Add(checks, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad_side)
        outer.AddSpacer(pad_bottom)
        self.SetSizer(outer)

    def _on_cw_offset_changed(self, _evt: wx.CommandEvent) -> None:
        self.settings.cw_offset_hz = self.cw_offset_ctrl.GetValue()
        self.settings.save()

    def _on_idle_disconnect_changed(self, _evt: wx.CommandEvent) -> None:
        value = self.idle_disconnect_ctrl.GetValue()
        self.settings.websdr_idle_disconnect_min = value if value > 0 else None
        self.settings.save()

    def _on_sync_tx_vfo_toggled(self, _evt: wx.CommandEvent) -> None:
        value = self.sync_tx_vfo_check.GetValue()
        self.settings.sync_tx_vfo = value
        self.settings.save()
        if self.on_sync_tx_vfo_changed is not None:
            self.on_sync_tx_vfo_changed(value)

    def _on_keep_on_top_toggled(self, _evt: wx.CommandEvent) -> None:
        self.settings.keep_compact_bar_on_top = self.keep_on_top_check.GetValue()
        self.settings.save()

    def _on_hide_when_undocked_toggled(self, _evt: wx.CommandEvent) -> None:
        self.settings.hide_receiver_when_undocked = self.hide_when_undocked_check.GetValue()
        self.settings.save()

    def _on_sync_direction_changed(self, _evt: wx.CommandEvent) -> None:
        held = self.sync_direction_choice.GetStringSelection() == SYNC_DIRECTION_ONE_WAY
        if self.on_sync_direction_changed is not None:
            self.on_sync_direction_changed(held)

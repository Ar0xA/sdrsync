"""Behaviour settings panel (spec §5.3)."""
from __future__ import annotations

import sys
from typing import Callable, Optional

import wx

from .. import theme
from ..fonts import label_font, value_font
from ..widgets import CheckBox, FlatButton
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
        # Narrower than the default best-size -- 5 digits plus a sign
        # covers the whole -2000..2000 range, no need for the wider
        # default width.
        self.cw_offset_ctrl.SetMinSize(wx.Size(self.FromDIP(70), -1))
        self.cw_offset_ctrl.Bind(wx.EVT_SPINCTRL, self._on_cw_offset_changed)
        # EVT_SPINCTRL fires for the up/down arrows, but a value typed
        # directly into the text portion only registers once focus
        # leaves the control -- an explicit Apply button lets the
        # operator force it to take effect immediately instead, without
        # having to click elsewhere first.
        self.cw_offset_apply_btn = FlatButton(self, "Apply")
        self.cw_offset_apply_btn.Bind(wx.EVT_BUTTON, self._on_cw_offset_changed)
        cw_offset_row = wx.BoxSizer(wx.HORIZONTAL)
        cw_offset_row.Add(self.cw_offset_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        cw_offset_row.Add(self.cw_offset_apply_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(6))
        grid.Add(field("WebSDR CW offset (Hz)", cw_offset_row), 1, wx.EXPAND)

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
        # Must mirror the engine's own never-persisted default
        # (SyncEngine._reverse_sync_held = False, i.e. bidirectional) --
        # this dropdown never gets an init-time reconciliation call, so
        # whatever index is selected here IS the displayed state for the
        # entire session until the user changes it themselves.
        self.sync_direction_choice.SetSelection(SYNC_DIRECTION_CHOICES.index(SYNC_DIRECTION_BIDIRECTIONAL))
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

        self.auto_click_audio_unlock_check = CheckBox(
            self, "Mouse hijack to enable WebSDR audio (Linux only)",
        )
        self.auto_click_audio_unlock_check.SetValue(settings.auto_click_audio_unlock)
        if sys.platform == "win32":
            # Inert there: WebView2's own --autoplay-policy flag (see
            # browser/backend.py) means audio never gets gated behind a
            # click in the first place on Windows, so this setting has
            # nothing to turn on or off -- greyed out rather than left
            # clickable-but-meaningless.
            self.auto_click_audio_unlock_check.Enable(False)
            self.auto_click_audio_unlock_check.SetToolTip(
                "Not applicable on Windows -- WebView2 never gates WebSDR audio behind "
                "a click in the first place, so there's nothing here to turn off."
            )
        else:
            self.auto_click_audio_unlock_check.SetToolTip(
                "Some WebSDR sites require a real click to start audio on Linux -- sdrsync "
                "moves your actual mouse cursor and clicks automatically when this is on. "
                "Turning it off means you'll need to click those sites' own Start/Play "
                "controls yourself."
            )
        self.auto_click_audio_unlock_check.Bind(wx.EVT_CHECKBOX, self._on_auto_click_audio_unlock_toggled)
        checks.Add(self.auto_click_audio_unlock_check, 0)

        self.force_ssb_to_data_mode_check = CheckBox(self, "Force SSB to data mode")
        self.force_ssb_to_data_mode_check.SetValue(settings.force_ssb_to_data_mode)
        self.force_ssb_to_data_mode_check.SetToolTip(
            "Reverse sync (WebSDR -> rig): when the WebSDR page is in USB or LSB, set the "
            "rig's DATA-mode variant instead of plain USB/LSB (e.g. PKTUSB/PKTLSB on "
            "rigctld, DATA-U/DATA-L on flrig) -- so the rig is always keyed via its data "
            "input, never the mic, for a plain SSB-looking WebSDR page."
        )
        self.force_ssb_to_data_mode_check.Bind(wx.EVT_CHECKBOX, self._on_force_ssb_to_data_mode_toggled)
        checks.Add(self.force_ssb_to_data_mode_check, 0)

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

    def _on_auto_click_audio_unlock_toggled(self, _evt: wx.CommandEvent) -> None:
        self.settings.auto_click_audio_unlock = self.auto_click_audio_unlock_check.GetValue()
        self.settings.save()

    def _on_force_ssb_to_data_mode_toggled(self, _evt: wx.CommandEvent) -> None:
        self.settings.force_ssb_to_data_mode = self.force_ssb_to_data_mode_check.GetValue()
        self.settings.save()

    def _on_sync_direction_changed(self, _evt: wx.CommandEvent) -> None:
        held = self.sync_direction_choice.GetStringSelection() == SYNC_DIRECTION_ONE_WAY
        if self.on_sync_direction_changed is not None:
            self.on_sync_direction_changed(held)

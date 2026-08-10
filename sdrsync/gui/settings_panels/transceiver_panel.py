"""Transceiver settings panel (spec §5.1) + a mock-rig injection
addendum (no spec equivalent -- kept for the no-hardware testing path,
shown only while "Use mock rig" is checked, same rule the old
Mock Rig Control panel used).
"""
from __future__ import annotations

from typing import Callable, Optional

import wx

from .. import theme
from ..fonts import label_font, value_font
from ..widgets import CheckBox, FlatButton
from ...config import MAX_POLL_INTERVAL_S, MIN_POLL_INTERVAL_S, AppSettings, clamp_reverse_sync_bounds

# Fixed display order -- config.RIG_BACKENDS is a set (validation-only),
# and only these two backends actually exist in sync/engine.py today;
# the spec's illustrative list (Hamlib direct/Omni-Rig/FlexRadio
# SmartSDR) names backends that aren't implemented, so offering them
# would be a dead, actively-misleading choice -- not offered.
RIG_BACKEND_CHOICES = ["rigctld", "flrig"]


def _parse_optional_hz(text: str) -> tuple[Optional[int], bool]:
    text = text.strip()
    if not text:
        return None, True
    try:
        return int(text), True
    except ValueError:
        return None, False


def _labeled(parent: wx.Window, text: str) -> wx.StaticText:
    label = wx.StaticText(parent, label=theme.caps(text))
    label.SetFont(label_font())
    label.SetForegroundColour(theme.MUTED)
    return label


class TransceiverPanel(wx.Panel):
    """spec §5.1. Reads/writes AppSettings directly and immediately
    (same "save right after mutating" discipline as the rest of the
    app). Test/Connect are disabled until MainFrame.set_engine_hooks()
    is called (phase 6 wires the real engine in) -- see
    project_brief.md's GUI rewrite progress log.
    """

    def __init__(self, parent: wx.Window, settings: AppSettings) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.settings = settings
        self.SetBackgroundColour(theme.BG)
        self.on_test: Optional[Callable[[], None]] = None
        self.on_connect: Optional[Callable[[], None]] = None

        outer = wx.BoxSizer(wx.VERTICAL)
        pad_top, pad_side, pad_bottom = self.FromDIP(18), self.FromDIP(16), self.FromDIP(20)

        row1 = wx.FlexGridSizer(rows=1, cols=6, gap=wx.Size(self.FromDIP(14), 0))
        for col in range(5):
            row1.AddGrowableCol(col)

        def field(label_text: str, control: wx.Window) -> wx.BoxSizer:
            box = wx.BoxSizer(wx.VERTICAL)
            box.Add(_labeled(self, label_text), 0, wx.BOTTOM, self.FromDIP(5))
            box.Add(control, 0, wx.EXPAND)
            return box

        self.backend_choice = wx.Choice(self, choices=RIG_BACKEND_CHOICES)
        self.backend_choice.SetFont(value_font())
        self.backend_choice.SetStringSelection(
            settings.rig_backend if settings.rig_backend in RIG_BACKEND_CHOICES else RIG_BACKEND_CHOICES[0]
        )
        self.backend_choice.Bind(wx.EVT_CHOICE, self._on_backend_changed)
        row1.Add(field("Backend", self.backend_choice), 1, wx.EXPAND)

        self.host_entry = wx.TextCtrl(self)
        self.host_entry.SetFont(value_font())
        row1.Add(field("Host", self.host_entry), 1, wx.EXPAND)

        self.port_entry = wx.SpinCtrl(self, min=1, max=65535)
        self.port_entry.SetFont(value_font())
        row1.Add(field("Port", self.port_entry), 1, wx.EXPAND)
        # Populate BEFORE binding change handlers -- SetValue() fires
        # EVT_TEXT/EVT_SPINCTRL immediately, and binding first meant the
        # host field's SetValue() read the port field's still-default
        # (uninitialized) value and persisted it, corrupting the real
        # port default before the port field's own SetValue() ran right
        # after (confirmed live: rigctld_port ended up saved as 1, the
        # SpinCtrl's min, instead of AppSettings' 4532 default).
        self._populate_host_port_for_backend(self.backend_choice.GetStringSelection())
        self.host_entry.Bind(wx.EVT_TEXT, self._on_host_port_edited)
        self.port_entry.Bind(wx.EVT_SPINCTRL, self._on_host_port_edited)
        self.port_entry.Bind(wx.EVT_TEXT, self._on_host_port_edited)

        self.poll_interval_ctrl = wx.SpinCtrlDouble(
            self, min=MIN_POLL_INTERVAL_S, max=MAX_POLL_INTERVAL_S, inc=0.05, initial=settings.poll_interval_s,
        )
        self.poll_interval_ctrl.SetDigits(2)
        self.poll_interval_ctrl.SetFont(value_font())
        self.poll_interval_ctrl.Bind(wx.EVT_SPINCTRLDOUBLE, self._on_poll_interval_changed)
        row1.Add(field("Poll interval (s)", self.poll_interval_ctrl), 1, wx.EXPAND)

        range_box = wx.BoxSizer(wx.HORIZONTAL)
        self.reverse_sync_min_entry = wx.TextCtrl(
            self, value=str(settings.reverse_sync_min_hz) if settings.reverse_sync_min_hz is not None else "",
            style=wx.TE_PROCESS_ENTER,
        )
        self.reverse_sync_max_entry = wx.TextCtrl(
            self, value=str(settings.reverse_sync_max_hz) if settings.reverse_sync_max_hz is not None else "",
            style=wx.TE_PROCESS_ENTER,
        )
        for entry in (self.reverse_sync_min_entry, self.reverse_sync_max_entry):
            entry.SetFont(value_font())
            entry.Bind(wx.EVT_TEXT_ENTER, self._on_reverse_sync_range_commit)
            entry.Bind(wx.EVT_KILL_FOCUS, self._on_reverse_sync_range_commit)
        range_box.Add(self.reverse_sync_min_entry, 1, wx.ALIGN_CENTER_VERTICAL)
        to_label = wx.StaticText(self, label="to")
        to_label.SetFont(value_font())
        to_label.SetForegroundColour(theme.MUTED)
        range_box.Add(to_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, self.FromDIP(5))
        range_box.Add(self.reverse_sync_max_entry, 1, wx.ALIGN_CENTER_VERTICAL)
        row1.Add(field("Reverse-sync range (Hz)", range_box), 1, wx.EXPAND)

        btn_box = wx.BoxSizer(wx.HORIZONTAL)
        self.test_btn = FlatButton(self, "Test")
        self.connect_btn = FlatButton(self, "Connect", is_primary=True)
        self.test_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_test and self.on_test())
        self.connect_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_connect and self.on_connect())
        # GUI REWRITE IN PROGRESS: real engine wiring lands in phase 6.
        self.test_btn.Enable(False)
        self.connect_btn.Enable(False)
        self.test_btn.SetToolTip("Wired up in a later phase of the GUI rewrite")
        self.connect_btn.SetToolTip("Wired up in a later phase of the GUI rewrite")
        btn_box.Add(self.test_btn, 0, wx.RIGHT, self.FromDIP(9))
        btn_box.Add(self.connect_btn, 0)
        row1.Add(field("", btn_box), 0, wx.ALIGN_BOTTOM)

        outer.Add(row1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 0)

        row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.mock_rig_check = CheckBox(self, "Use mock rig (embedded, for testing)")
        self.mock_rig_check.SetValue(settings.use_mock_rig)
        self.mock_rig_check.Bind(wx.EVT_CHECKBOX, self._on_mock_rig_toggled)
        row2.Add(self.mock_rig_check, 0, wx.ALIGN_CENTER_VERTICAL)
        self.poll_status_text = wx.StaticText(self, label="")
        self.poll_status_text.SetFont(label_font())
        self.poll_status_text.SetForegroundColour(theme.FAINT)
        row2.Add(self.poll_status_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(18))
        outer.Add(row2, 0, wx.TOP, self.FromDIP(14))

        self._mock_panel = _MockRigPanel(self)
        outer.Add(self._mock_panel, 0, wx.EXPAND | wx.TOP, self.FromDIP(14))
        self._mock_panel.Show(settings.use_mock_rig)

        # spec §5's "18px top / 16px sides / 20px bottom" padding, via a
        # side border on the outer sizer plus explicit top/bottom spacers
        # (a border alone can't give top and bottom different widths).
        border_sizer = wx.BoxSizer(wx.VERTICAL)
        border_sizer.Add(outer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad_side)
        self.SetSizer(border_sizer)
        border_sizer.AddSpacer(pad_bottom)
        outer.InsertSpacer(0, pad_top)

        self._refresh_poll_status()

    def _populate_host_port_for_backend(self, backend: str) -> None:
        if backend == "flrig":
            self.host_entry.SetValue(self.settings.flrig_host)
            self.port_entry.SetValue(self.settings.flrig_port)
        else:
            self.host_entry.SetValue(self.settings.rigctld_host)
            self.port_entry.SetValue(self.settings.rigctld_port)

    def _on_backend_changed(self, _evt: wx.CommandEvent) -> None:
        backend = self.backend_choice.GetStringSelection()
        self.settings.rig_backend = backend
        self.settings.save()
        self._populate_host_port_for_backend(backend)

    def _on_host_port_edited(self, _evt: wx.CommandEvent) -> None:
        backend = self.backend_choice.GetStringSelection()
        host = self.host_entry.GetValue().strip()
        port = self.port_entry.GetValue()
        if backend == "flrig":
            self.settings.flrig_host, self.settings.flrig_port = host, port
        else:
            self.settings.rigctld_host, self.settings.rigctld_port = host, port
        self.settings.save()

    def _on_poll_interval_changed(self, _evt: wx.CommandEvent) -> None:
        self.settings.poll_interval_s = self.poll_interval_ctrl.GetValue()
        self.settings.save()
        self._refresh_poll_status()

    def _on_reverse_sync_range_commit(self, evt: wx.Event) -> None:
        min_hz, min_ok = _parse_optional_hz(self.reverse_sync_min_entry.GetValue())
        max_hz, max_ok = _parse_optional_hz(self.reverse_sync_max_entry.GetValue())
        if min_ok and max_ok:
            min_hz, max_hz = clamp_reverse_sync_bounds(min_hz, max_hz)
            self.settings.reverse_sync_min_hz = min_hz
            self.settings.reverse_sync_max_hz = max_hz
            self.settings.save()
            self.reverse_sync_min_entry.SetValue(str(min_hz) if min_hz is not None else "")
            self.reverse_sync_max_entry.SetValue(str(max_hz) if max_hz is not None else "")
        if isinstance(evt, wx.FocusEvent):
            evt.Skip()

    def _on_mock_rig_toggled(self, _evt: wx.CommandEvent) -> None:
        self.settings.use_mock_rig = self.mock_rig_check.GetValue()
        self.settings.save()
        self.host_entry.Enable(not self.settings.use_mock_rig)
        self._mock_panel.Show(self.settings.use_mock_rig)
        self._refresh_poll_status()
        self.Layout()
        top = self.GetTopLevelParent()
        if top is not None:
            top.Layout()

    def _refresh_poll_status(self) -> None:
        text = f"polling every {self.settings.poll_interval_s:.2f} s -- frequency, mode, PTT"
        self.poll_status_text.SetLabel(text)


class _MockRigPanel(wx.Panel):
    """Manual freq/mode/passband/PTT injection -- no spec equivalent;
    kept as the no-hardware testing path (see TransceiverPanel's
    docstring). Wiring to the real engine lands in phase 6."""

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.PANEL_ALT)
        self.on_set_freq: Optional[Callable[[str], None]] = None
        self.on_set_mode: Optional[Callable[[str, str], None]] = None
        self.on_ptt_toggled: Optional[Callable[[bool], None]] = None

        outer = wx.BoxSizer(wx.VERTICAL)
        header = _labeled(self, "Mock rig control")
        outer.Add(header, 0, wx.ALL, self.FromDIP(9))

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.freq_entry = wx.TextCtrl(self, value="14074000", size=(self.FromDIP(110), -1))
        self.freq_entry.SetFont(value_font())
        set_freq_btn = FlatButton(self, "Set Freq")
        set_freq_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_set_freq and self.on_set_freq(self.freq_entry.GetValue()))
        row.Add(self.freq_entry, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(5))
        row.Add(set_freq_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(18))

        self.mode_choice = wx.Choice(self, choices=["USB", "LSB", "CW", "AM", "FM"])
        self.mode_choice.SetFont(value_font())
        self.mode_choice.SetSelection(0)
        self.passband_entry = wx.TextCtrl(self, value="2400", size=(self.FromDIP(90), -1))
        self.passband_entry.SetFont(value_font())
        set_mode_btn = FlatButton(self, "Set Mode")
        set_mode_btn.Bind(
            wx.EVT_BUTTON,
            lambda evt: self.on_set_mode
            and self.on_set_mode(self.mode_choice.GetStringSelection(), self.passband_entry.GetValue()),
        )
        row.Add(self.mode_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(5))
        row.Add(self.passband_entry, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(5))
        row.Add(set_mode_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(18))

        self.ptt_check = CheckBox(self, "PTT (transmitting)")
        self.ptt_check.Bind(wx.EVT_CHECKBOX, lambda evt: self.on_ptt_toggled and self.on_ptt_toggled(self.ptt_check.GetValue()))
        row.Add(self.ptt_check, 0, wx.ALIGN_CENTER_VERTICAL)

        outer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(9))
        self.SetSizer(outer)

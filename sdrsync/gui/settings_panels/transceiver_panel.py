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
        self._rig_active = False
        # See _populate_host_port_for_backend()'s docstring -- must exist
        # before either host_entry or port_entry can fire an event.
        self._populating_host_port = False

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
        port_box = wx.BoxSizer(wx.VERTICAL)
        self.port_label = _labeled(self, "Port")
        port_box.Add(self.port_label, 0, wx.BOTTOM, self.FromDIP(5))
        port_box.Add(self.port_entry, 0, wx.EXPAND)
        row1.Add(port_box, 1, wx.EXPAND)
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
        # Match the WebSDR strip's own Connect/Disconnect button look
        # (user-reported: they read as different background colours,
        # since this panel's own background is BG but the strip's is
        # SURFACE, and a FlatButton with no fill just blends into
        # whichever panel hosts it) rather than this panel's BG.
        self.connect_btn.set_bg_override(theme.SURFACE)
        self.test_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_test and self.on_test())
        self.connect_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_connect and self.on_connect())
        btn_box.Add(self.test_btn, 0, wx.RIGHT, self.FromDIP(9))
        btn_box.Add(self.connect_btn, 0)
        row1.Add(field("", btn_box), 0, wx.ALIGN_BOTTOM)

        outer.Add(row1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 0)

        # WebSDR CW offset and Transceiver CW pitch are summed into one
        # effective offset (SyncEngine._effective_cw_offset_hz()) -- kept
        # side by side, not split across tabs, so that relationship is
        # visible instead of surprising (previously on the Behaviour tab
        # alone, which is how a user ended up asking why their offset's
        # "0" line actually sat at 750Hz -- their rig's own CW pitch,
        # set here, but invisible from the offset field's own tab).
        row1b = wx.BoxSizer(wx.HORIZONTAL)

        self.cw_pitch_ctrl = wx.SpinCtrl(self, min=-2000, max=2000, initial=settings.transceiver_cw_pitch_hz)
        self.cw_pitch_ctrl.SetFont(value_font())
        self.cw_pitch_ctrl.SetMinSize(wx.Size(self.FromDIP(70), -1))
        self.cw_pitch_ctrl.SetToolTip(
            "The rig's OWN CW pitch/sidetone setting (from its menu), entered here "
            "manually -- sdrsync never queries the rig for this. Added to the "
            "WebSDR CW offset alongside it, so this field can hold the rig's "
            "usual pitch (often a few hundred Hz) while that one stays at 0 for "
            "\"no additional correction needed\"."
        )
        self.cw_pitch_ctrl.Bind(wx.EVT_SPINCTRL, self._on_cw_pitch_changed)
        row1b.Add(field("Transceiver CW pitch (Hz)", self.cw_pitch_ctrl), 0, wx.EXPAND)

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
        row1b.Add(field("WebSDR CW offset (Hz)", cw_offset_row), 0, wx.EXPAND | wx.LEFT, self.FromDIP(14))

        self.cw_offset_total_text = wx.StaticText(self, label="")
        self.cw_offset_total_text.SetFont(label_font())
        self.cw_offset_total_text.SetForegroundColour(theme.FAINT)
        row1b.Add(field("= effective CW offset", self.cw_offset_total_text), 0, wx.EXPAND | wx.LEFT, self.FromDIP(14))
        self._refresh_cw_offset_total()

        outer.Add(row1b, 0, wx.TOP, self.FromDIP(14))

        row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.mock_rig_check = CheckBox(self, "Use mock rig (embedded, for testing)")
        self.mock_rig_check.SetValue(settings.use_mock_rig)
        self.mock_rig_check.Bind(wx.EVT_CHECKBOX, self._on_mock_rig_toggled)
        row2.Add(self.mock_rig_check, 0, wx.ALIGN_CENTER_VERTICAL)
        self.poll_status_text = wx.StaticText(self, label="")
        self.poll_status_text.SetFont(label_font())
        self.poll_status_text.SetForegroundColour(theme.FAINT)
        row2.Add(self.poll_status_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(18))
        # No spec-mandated slot for a Test result -- placed on its own
        # row so it never fights the connection-state text above for the
        # same line (spec §5.1's two-state text is about polling, not
        # about a one-shot Test click).
        self.test_status_text = wx.StaticText(self, label="")
        self.test_status_text.SetFont(label_font())
        self.test_status_text.SetForegroundColour(theme.FAINT)
        outer.Add(row2, 0, wx.TOP, self.FromDIP(14))
        outer.Add(self.test_status_text, 0, wx.TOP, self.FromDIP(5))

        self.mock_panel = _MockRigPanel(self)
        outer.Add(self.mock_panel, 0, wx.EXPAND | wx.TOP, self.FromDIP(14))
        self.mock_panel.Show(settings.use_mock_rig)

        # spec §5's "18px top / 16px sides / 20px bottom" padding, via a
        # side border on the outer sizer plus explicit top/bottom spacers
        # (a border alone can't give top and bottom different widths).
        border_sizer = wx.BoxSizer(wx.VERTICAL)
        border_sizer.Add(outer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad_side)
        self.SetSizer(border_sizer)
        border_sizer.AddSpacer(pad_bottom)
        outer.InsertSpacer(0, pad_top)

        self._refresh_poll_status()

    def set_connection_state(
        self, active: bool, busy_label: Optional[str] = None, connecting: bool = False,
    ) -> None:
        """Called by MainFrame from real StatusSnapshots (and
        optimistically, with a busy_label, right after a click) --
        active=True means a rig session has been started (may still be
        connecting), matching StatusSnapshot.rig_active.

        connecting=True (rig_active but not yet rig_connected) keeps the
        button labeled "Connecting..." instead of immediately jumping to
        "Disconnect" -- previously the label flipped to "Disconnect" the
        instant the session started, which reads as "already connected"
        even while the engine is still silently retrying the handshake
        for up to RIG_CONNECT_TIMEOUT_S (30s). That false-positive label
        was confirmed live as the actual source of a user's confusion
        ("mock rig off, couldn't figure out why it wouldn't connect") --
        the button said Disconnect but nothing else in the UI ever
        showed a connected rig. Left enabled (unlike busy_label) so a
        stuck attempt can still be aborted by clicking it, which routes
        to the same stop_rig_from_other_thread() call Disconnect uses."""
        if busy_label is not None:
            label = busy_label
        elif connecting:
            label = "Connecting..."
        else:
            label = "Disconnect" if active else "Connect"
        self.connect_btn.SetLabel(label)
        self.connect_btn.Enable(busy_label is None)
        # Gold (primary) at rest for Connect, reddish (danger) at rest
        # for Disconnect (user-reported preference -- previously
        # Disconnect flipped to the plain grey/BORDER style, only
        # visually gold transiently on hover). Hover/press feedback
        # stays the universal gold either way, unchanged.
        self.connect_btn.SetPrimary(not active)
        self.connect_btn.SetDanger(active)
        editable = not active
        self.backend_choice.Enable(editable)
        self.port_entry.Enable(editable)
        self.host_entry.Enable(editable and not self.mock_rig_check.GetValue())
        self.mock_rig_check.Enable(editable)
        self._rig_active = active
        self._refresh_poll_status()

    def set_test_result(self, ok: bool, message: str) -> None:
        self.test_btn.Enable(True)
        self.test_btn.SetLabel("Test")
        self.test_status_text.SetLabel(("OK: " if ok else "FAIL: ") + message)

    def begin_test(self) -> None:
        self.test_btn.Enable(False)
        self.test_btn.SetLabel("Testing...")
        self.test_status_text.SetLabel("")

    def _populate_host_port_for_backend(self, backend: str) -> None:
        # Guarded, not just "populate before binding" (see __init__'s own
        # comment on that first fix) -- THIS call site runs at runtime,
        # long after both fields' handlers are already bound, so the same
        # hazard applies here too: SetValue() fires EVT_TEXT/EVT_SPINCTRL
        # synchronously, and host_entry's SetValue() landing before
        # port_entry's own runs _on_host_port_edited with the NEW
        # backend's host but the OLD backend's still-displayed port --
        # which then gets saved as the new backend's port, clobbering it
        # with the wrong backend's value before port_entry.SetValue() ever
        # gets a chance to correct it (confirmed live: switching flrig ->
        # rigctld left rigctld_port holding flrig's port instead of 4532).
        # flrig's port is an XML-RPC endpoint, not rigctld's plain TCP
        # port -- labeling it "Port" the same as rigctld's reads as if
        # it's the same kind of thing, which it isn't (user-reported).
        port_label_text = "XML-RPC Port" if backend == "flrig" else "Port"
        if self.port_label.GetLabel() != theme.caps(port_label_text):
            self.port_label.SetLabel(theme.caps(port_label_text))
            self.Layout()

        self._populating_host_port = True
        try:
            if backend == "flrig":
                self.host_entry.SetValue(self.settings.flrig_host)
                self.port_entry.SetValue(self.settings.flrig_port)
            else:
                self.host_entry.SetValue(self.settings.rigctld_host)
                self.port_entry.SetValue(self.settings.rigctld_port)
        finally:
            self._populating_host_port = False

    def _on_backend_changed(self, _evt: wx.CommandEvent) -> None:
        backend = self.backend_choice.GetStringSelection()
        self.settings.rig_backend = backend
        self.settings.save()
        self._populate_host_port_for_backend(backend)
        self._refresh_poll_status()

    def _on_host_port_edited(self, _evt: wx.CommandEvent) -> None:
        if self._populating_host_port:
            return
        backend = self.backend_choice.GetStringSelection()
        host = self.host_entry.GetValue().strip()
        port = self.port_entry.GetValue()
        if backend == "flrig":
            self.settings.flrig_host, self.settings.flrig_port = host, port
        else:
            self.settings.rigctld_host, self.settings.rigctld_port = host, port
        self.settings.save()

    def _on_cw_offset_changed(self, _evt: wx.CommandEvent) -> None:
        self.settings.cw_offset_hz = self.cw_offset_ctrl.GetValue()
        self.settings.save()
        self._refresh_cw_offset_total()

    def _on_cw_pitch_changed(self, _evt: wx.CommandEvent) -> None:
        self.settings.transceiver_cw_pitch_hz = self.cw_pitch_ctrl.GetValue()
        self.settings.save()
        self._refresh_cw_offset_total()

    def _refresh_cw_offset_total(self) -> None:
        total = self.cw_offset_ctrl.GetValue() + self.cw_pitch_ctrl.GetValue()
        self.cw_offset_total_text.SetLabel(f"{total:+d} Hz")

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
        self.mock_panel.Show(self.settings.use_mock_rig)
        self._refresh_poll_status()
        self.Layout()
        top = self.GetTopLevelParent()
        if top is not None:
            top.Layout()

    def _refresh_poll_status(self) -> None:
        # spec §5.1's literal two-state text -- connection-state-dependent,
        # not just a poll-interval readout (a gap found during the phase
        # 10 polish pass: this used to unconditionally show the "polling
        # every..." text even while disconnected, which doesn't match
        # either reference screenshot).
        if self._rig_active:
            text = f"polling every {self.settings.poll_interval_s:.2f} s -- frequency, mode, PTT"
        else:
            text = f"{self.backend_choice.GetStringSelection()} not reachable until connected"
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
        header_row = wx.BoxSizer(wx.HORIZONTAL)
        header_row.Add(_labeled(self, "Mock rig control"), 0, wx.ALIGN_CENTER_VERTICAL)
        # Native TextCtrl/Choice's default disabled rendering is too
        # subtle against this theme's near-white PANEL_ALT background to
        # read as "disabled" at a glance (user-reported live) -- this
        # hint plus set_enabled()'s explicit foreground-colour dimming
        # below make the state legible without fighting the OS-native
        # disabled style further.
        self.hint_text = wx.StaticText(self, label="connect a WebSDR to enable")
        self.hint_text.SetFont(label_font())
        self.hint_text.SetForegroundColour(theme.FAINT)
        header_row.Add(self.hint_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(9))
        outer.Add(header_row, 0, wx.ALL, self.FromDIP(9))

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
        self.set_enabled(False)

    def set_enabled(self, enabled: bool) -> None:
        """Greys out the freq/mode/PTT controls while no WebSDR is
        connected -- pushing a mock rig frequency/mode/PTT has nothing
        to reach until then, so an enabled control here would just look
        broken (user-reported live: "why isn't anything happening").

        Also explicitly dims the native TextCtrl/Choice foreground text
        and shows a hint -- their default OS-native disabled rendering
        was reported as too subtle to notice against this theme's
        near-white background (FlatButton/CheckBox's own 45%-opacity
        disabled style, spec §7, is unaffected and already clear)."""
        for w in (self.freq_entry, self.mode_choice, self.passband_entry, self.ptt_check):
            w.Enable(enabled)
        for child in self.GetChildren():
            if isinstance(child, FlatButton):
                child.Enable(enabled)
        text_colour = theme.TEXT if enabled else theme.FAINT
        # wx.Choice's own disabled background didn't read as "disabled"
        # in this theme either (user-reported live) -- SURFACE while
        # disabled, plain white (its normal native background) once
        # re-enabled.
        bg_colour = wx.WHITE if enabled else theme.SURFACE
        for w in (self.freq_entry, self.mode_choice, self.passband_entry):
            w.SetForegroundColour(text_colour)
            w.SetBackgroundColour(bg_colour)
            w.Refresh()
        self.hint_text.Show(not enabled)
        self.Layout()

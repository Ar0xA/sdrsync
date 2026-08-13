"""StripPanel (spec §3) -- the 46px always-visible top chrome band: rig
dot, RX/TX VFO, mode, PTT tag, pause/mute toggles, site chooser,
connect/disconnect, and an (inert this rewrite -- spec §9 is out of
scope) undock icon.
"""
from __future__ import annotations

import wx

from . import theme
from .fonts import label_font, value_font
from .format import fmt_delta, fmt_hz
from .state import AppState, ptt_tag_state
from .widgets import CheckBox, FlatButton, ToggleButton, _OwnerDrawnMixin


class _Dot(wx.Window):
    """6px owner-drawn rig-connection dot (spec §3 item 1 / §8's
    StatusBarPanel dot, same rules): filled ACCENT while connected and
    syncing, filled MUTED while connected but paused, unfilled FAINT
    outline while disconnected."""

    SIZE_DIP = 6  # spec §3 default; StatusBarPanel passes size_dip=5 (spec §8)

    def __init__(self, parent: wx.Window, size_dip: float = SIZE_DIP) -> None:
        super().__init__(parent, size=wx.Size(-1, -1), style=wx.BORDER_NONE)
        self._mode = "disconnected"  # "syncing" | "paused" | "disconnected"
        self._size_dip = size_dip
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize(wx.Size(self.FromDIP(size_dip + 4), self.FromDIP(size_dip + 4)))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda evt: None)

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self.Refresh()

    def _on_paint(self, _evt: wx.PaintEvent) -> None:
        dc = wx.BufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        size = self.FromDIP(self._size_dip)
        rect = self.GetClientRect()
        cx, cy = rect.width / 2, rect.height / 2
        path = gc.CreatePath()
        path.AddEllipse(cx - size / 2, cy - size / 2, size, size)
        if self._mode == "syncing":
            colour = theme.ACCENT
            gc.SetBrush(gc.CreateBrush(wx.Brush(colour)))
            gc.FillPath(path)
        elif self._mode == "paused":
            colour = theme.MUTED
            gc.SetBrush(gc.CreateBrush(wx.Brush(colour)))
            gc.FillPath(path)
        else:
            gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(theme.FAINT).Width(1)))
            gc.StrokePath(path)


class _LabelValue(wx.Panel):
    """One "LABEL value" group (spec §3 items 2-4: RX VFO / TX VFO /
    MODE) -- a caps label (label_font/MUTED) above/beside a value
    (value_font), matching the strip's single-row layout."""

    def __init__(self, parent: wx.Window, label: str) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(parent.GetBackgroundColour())
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._label = wx.StaticText(self, label=theme.caps(label))
        self._label.SetFont(label_font())
        self._label.SetForegroundColour(theme.MUTED)
        self._label.SetBackgroundColour(self.GetBackgroundColour())
        self._value = wx.StaticText(self, label="-")
        self._value.SetFont(value_font())
        self._value.SetForegroundColour(theme.TEXT)
        self._value.SetBackgroundColour(self.GetBackgroundColour())
        sizer.Add(self._label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(5))
        sizer.Add(self._value, 0, wx.ALIGN_CENTER_VERTICAL)
        self.SetSizer(sizer)

    def set_label_text(self, text: str) -> None:
        if self._label.GetLabel() != theme.caps(text):
            self._label.SetLabel(theme.caps(text))
            self.Layout()

    def set_value(self, text: str, colour: wx.Colour) -> None:
        # SetLabel() alone doesn't make an ancestor sizer reflow -- only
        # a subsequent Layout() picks up the new best size. Rarely
        # visible on RX/TX VFO (fmt_hz's fixed-width zero-padded groups
        # barely change width) but MODE's text ("USB"/"LSB"/"-"/...)
        # genuinely varies, and on StripPanel this was masked by
        # incidental full-window Layout() calls elsewhere (opening a
        # settings panel, etc.) -- CompactFrame (spec §9), a small
        # fixed-size window with no such incidental relayout, exposed it
        # directly: MODE showed a single clipped letter. Same root cause
        # and fix as widgets.py's _relayout_after_label_change.
        if self._value.GetLabel() != text:
            self._value.SetLabel(text)
            self.Layout()
            top = self.GetTopLevelParent()
            if top is not None:
                top.Layout()
        if self._value.GetForegroundColour() != colour:
            self._value.SetForegroundColour(colour)
        self._value.Refresh()


class _PttTag(_OwnerDrawnMixin, wx.Control):
    """spec §3 item 5: owner-drawn rounded-rect PTT tag. Blinks border +
    text between 100% and 40% alpha on a 1.1s timer while transmitting."""

    BLINK_MS = 1100

    def __init__(self, parent: wx.Window) -> None:
        wx.Control.__init__(self, parent, style=wx.BORDER_NONE)
        self._init_owner_drawn()
        self.SetFont(label_font())
        self._ptt_state = "offline"  # "offline" | "idle" | "receive" | "transmit"
        self._blink_on = True
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_blink, self._timer)
        self._recompute_min_size()

    def AcceptsFocus(self) -> bool:  # noqa: N802
        return False

    def set_state(self, state: str) -> None:
        if state == self._ptt_state:
            return
        self._ptt_state = state
        if state == "transmit":
            self._blink_on = True
            if not self._timer.IsRunning():
                self._timer.Start(self.BLINK_MS)
        else:
            self._timer.Stop()
            self._blink_on = True
        self.Refresh()

    def _on_blink(self, _evt: wx.TimerEvent) -> None:
        self._blink_on = not self._blink_on
        self.Refresh()

    def _label(self) -> str:
        return {"offline": "OFFLINE", "idle": "IDLE", "receive": "RECEIVE", "transmit": "TRANSMIT"}[self._ptt_state]

    def _colour(self) -> wx.Colour:
        # User-requested semantic colouring (a deliberate deviation from
        # spec §3 item 5's literal FAINT/FAINT/ACCENT_TEXT treatment,
        # confirmed live): grey while offline, green while actually
        # receiving, red while transmitting.
        if self._ptt_state == "transmit":
            if not self._blink_on:
                return theme.with_alpha(theme.TRANSMIT, 102)  # 40%
            return theme.TRANSMIT
        if self._ptt_state == "receive":
            return theme.RECEIVE
        return theme.FAINT

    def _recompute_min_size(self) -> None:
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        w, h = dc.GetTextExtent(theme.caps("TRANSMIT"))
        pad_x, pad_y = self.FromDIP(7), self.FromDIP(2)
        self.SetMinSize(wx.Size(w + pad_x * 2, h + pad_y * 2))

    def _activate(self) -> None:
        pass  # display-only, not clickable

    def _paint(self, gc: "wx.GraphicsContext", rect: wx.Rect) -> None:
        colour = self._colour()
        radius = self.FromDIP(theme.RADIUS)
        path = gc.CreatePath()
        path.AddRoundedRectangle(0.5, 0.5, max(rect.width - 1, 0), max(rect.height - 1, 0), radius)
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(colour).Width(1)))
        gc.StrokePath(path)
        gc.SetFont(self.GetFont(), colour)
        text = theme.caps(self._label())
        tw, th = gc.GetTextExtent(text)
        gc.DrawText(text, (rect.width - tw) / 2, (rect.height - th) / 2)


class _VDivider(wx.Window):
    """20px-tall vertical hairline divider (spec §3 items 6/9)."""

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, size=wx.Size(-1, -1), style=wx.BORDER_NONE)
        self.SetMinSize(wx.Size(self.FromDIP(1), self.FromDIP(20)))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda evt: None)

    def _on_paint(self, _evt: wx.PaintEvent) -> None:
        dc = wx.BufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        dc.SetPen(wx.Pen(theme.DIVIDER, 1))
        rect = self.GetClientRect()
        x = rect.width // 2
        dc.DrawLine(x, rect.GetTop(), x, rect.GetBottom() + 1)


class _UndockButton(_OwnerDrawnMixin, wx.Control):
    """28x26 flat icon button (spec §3 item 12). Posts a real
    wx.EVT_BUTTON-shaped event on click, same pattern FlatButton uses,
    so MainFrame binds it exactly like every other strip control --
    see app.py's _on_undock_clicked (spec §9's compact bar)."""

    def __init__(self, parent: wx.Window) -> None:
        wx.Control.__init__(self, parent, style=wx.BORDER_NONE)
        self._init_owner_drawn()
        self.SetMinSize(wx.Size(self.FromDIP(28), self.FromDIP(26)))
        self.SetToolTip("Pop receiver out to its own window")

    def _activate(self) -> None:
        evt = wx.CommandEvent(wx.wxEVT_BUTTON, self.GetId())
        evt.SetEventObject(self)
        wx.PostEvent(self, evt)

    def _paint(self, gc: "wx.GraphicsContext", rect: wx.Rect) -> None:
        radius = self.FromDIP(theme.RADIUS)
        if self._hover:
            path = gc.CreatePath()
            path.AddRoundedRectangle(0.5, 0.5, max(rect.width - 1, 0), max(rect.height - 1, 0), radius)
            gc.SetBrush(gc.CreateBrush(wx.Brush(theme.ACCENT_TINT)))
            gc.FillPath(path)
        glyph_colour = theme.ACCENT_TEXT if self._hover else theme.MUTED
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(glyph_colour).Width(1.4)))
        # A minimal external-link glyph: a box with an arrow breaking out
        # of its top-right corner (Lucide external-link, hand-drawn --
        # no icon-font/SVG dependency added for one glyph).
        cx, cy = rect.width / 2, rect.height / 2
        s = min(rect.width, rect.height) * 0.34
        box = gc.CreatePath()
        box.AddRectangle(cx - s * 0.9, cy - s * 0.2, s * 1.5, s * 1.5)
        gc.StrokePath(box)
        arrow = gc.CreatePath()
        arrow.MoveToPoint(cx - s * 0.1, cy - s * 1.1)
        arrow.AddLineToPoint(cx + s * 1.1, cy - s * 1.1)
        arrow.AddLineToPoint(cx + s * 1.1, cy + s * 0.1)
        gc.StrokePath(arrow)
        diag = gc.CreatePath()
        diag.MoveToPoint(cx - s * 0.3, cy + s * 0.5)
        diag.AddLineToPoint(cx + s * 1.1, cy - s * 1.1)
        gc.StrokePath(diag)
        self._draw_focus_ring(gc, rect, radius)


class _LinkToggle(_OwnerDrawnMixin, wx.Control):
    """Small horizontal chain-link icon between RX VFO and TX VFO
    (user-requested, no spec equivalent). Toggles the same
    AppState.sync_tx_vfo/AppSettings.sync_tx_vfo the Behaviour panel's
    "Sync TX VFO" checkbox already controls -- this is a second,
    quicker access point for it, not a new setting; StripPanel.refresh()
    already dims/relabels TX VFO off that same field regardless of
    which control changed it. Checked (default) = linked, MUTED glyph.
    Unchecked = unlinked, glyph turns TRANSMIT red with a diagonal
    strike, matching the app's existing "red = not doing the normal
    thing" colour language (PTT tag, status bar errors)."""

    def __init__(self, parent: wx.Window) -> None:
        wx.Control.__init__(self, parent, style=wx.BORDER_NONE)
        self._init_owner_drawn()
        self._checked = True
        self.SetMinSize(wx.Size(self.FromDIP(28), self.FromDIP(20)))
        self._update_tooltip()

    def SetValue(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self._update_tooltip()
        self.Refresh()

    def GetValue(self) -> bool:
        return self._checked

    def _update_tooltip(self) -> None:
        self.SetToolTip(
            "TX VFO linked to RX (click to unlink)" if self._checked
            else "TX VFO unlinked from RX (click to relink)"
        )

    def _activate(self) -> None:
        self._checked = not self._checked
        self._update_tooltip()
        evt = wx.CommandEvent(wx.wxEVT_TOGGLEBUTTON, self.GetId())
        evt.SetEventObject(self)
        evt.SetInt(1 if self._checked else 0)
        wx.PostEvent(self, evt)

    def _paint(self, gc: "wx.GraphicsContext", rect: wx.Rect) -> None:
        if not self.IsEnabled():
            colour = theme.with_alpha(theme.FAINT, 115)
        elif not self._checked:
            colour = theme.TRANSMIT
        elif self._hover:
            colour = theme.ACCENT_TEXT
        else:
            colour = theme.MUTED
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(colour).Width(1.4)))
        cx, cy = rect.width / 2, rect.height / 2
        link_w, link_h = rect.width * 0.32, rect.height * 0.55
        left = gc.CreatePath()
        left.AddRoundedRectangle(cx - link_w * 0.95, cy - link_h / 2, link_w, link_h, link_h / 2)
        gc.StrokePath(left)
        right = gc.CreatePath()
        right.AddRoundedRectangle(cx - link_w * 0.05, cy - link_h / 2, link_w, link_h, link_h / 2)
        gc.StrokePath(right)
        if not self._checked:
            diag = gc.CreatePath()
            diag.MoveToPoint(rect.width * 0.12, rect.height * 0.85)
            diag.AddLineToPoint(rect.width * 0.88, rect.height * 0.15)
            gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(colour).Width(1.6)))
            gc.StrokePath(diag)
        self._draw_focus_ring(gc, rect, self.FromDIP(theme.RADIUS))


class StripPanel(wx.Panel):
    """spec §3. Presentation only: reads AppState via refresh(state), and
    exposes its interactive children (pause_btn, mute_btn, site_choice,
    connect_btn) as public attributes for the owner (MainFrame) to Bind()
    -- same "owner wires handlers to child widgets" convention the rest
    of this codebase already uses.
    """

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.SURFACE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize(wx.Size(-1, self.FromDIP(theme.STRIP_HEIGHT)))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda evt: None)

        pad = self.FromDIP(14)
        gap = self.FromDIP(11)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.rig_dot = _Dot(self)
        sizer.Add(self.rig_dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, pad)

        self.rx_vfo = _LabelValue(self, "RX VFO")
        sizer.Add(self.rx_vfo, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.link_toggle = _LinkToggle(self)
        sizer.Add(self.link_toggle, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(5))

        self.tx_vfo = _LabelValue(self, "TX VFO")
        sizer.Add(self.tx_vfo, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(5))

        self.mode_group = _LabelValue(self, "MODE")
        sizer.Add(self.mode_group, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.ptt_tag = _PttTag(self)
        sizer.Add(self.ptt_tag, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        sizer.Add(_VDivider(self), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.pause_btn = ToggleButton(self, "Pause sync")
        sizer.Add(self.pause_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.mute_btn = CheckBox(self, "mute tx", emphasize_when_checked=True)
        sizer.Add(self.mute_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        sizer.Add(_VDivider(self), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.site_choice = wx.Choice(self)
        self.site_choice.SetFont(value_font())
        self.site_choice.SetMinSize(wx.Size(self.FromDIP(130), -1))
        sizer.Add(self.site_choice, 1, wx.EXPAND | wx.LEFT, gap)

        self.connect_btn = FlatButton(self, "Connect", is_primary=True)
        sizer.Add(self.connect_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.undock_btn = _UndockButton(self)
        sizer.Add(self.undock_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, gap)

        self.SetSizer(sizer)

    def _on_paint(self, _evt: wx.PaintEvent) -> None:
        dc = wx.BufferedPaintDC(self)
        dc.SetBackground(wx.Brush(theme.SURFACE))
        dc.Clear()
        theme.draw_hairline(dc, self.GetClientRect(), "bottom")

    def refresh(self, state: AppState) -> None:
        self.rig_dot.set_mode("syncing" if (state.rig_connected and not state.paused) else
                               "paused" if (state.rig_connected and state.paused) else "disconnected")

        self.rx_vfo.set_value(fmt_hz(state.rx_hz) if state.rig_connected else "-", theme.TEXT)

        self.link_toggle.SetValue(state.sync_tx_vfo)

        if not state.sync_tx_vfo:
            self.tx_vfo.set_label_text("TX VFO (not synced)")
            tx_colour = theme.FAINT
        else:
            self.tx_vfo.set_label_text("TX VFO")
            tx_colour = theme.ACCENT_TEXT if state.ptt else theme.TEXT
        self.tx_vfo.set_value(fmt_hz(state.tx_hz) if state.rig_connected else "-", tx_colour)

        self.mode_group.set_value(state.mode if state.rig_connected else "-", theme.TEXT)

        self.ptt_tag.set_state(ptt_tag_state(state))

        self.pause_btn.SetValue(state.paused)
        if state.paused:
            delta = fmt_delta(state.rx_hz, state.sdr_hz)
            self.pause_btn.SetLabel(f"Sync paused · {delta}")
        else:
            self.pause_btn.SetLabel("Pause sync")

        self.mute_btn.SetValue(state.mute_on_tx)

        # Three states, not two -- while a WebSDR session is active, the
        # button reads "Disconnect" only if the dropdown still points at
        # the site that's actually loaded; if the user has since picked
        # a DIFFERENT site, it reads "Load" instead (matches
        # _on_strip_connect_clicked's own same-site check exactly, which
        # already supports switching in one click -- this was previously
        # always "Disconnect" regardless of the dropdown, so switching
        # sites silently required a manual disconnect-then-reconnect).
        if not state.sdr_active:
            self.connect_btn.SetLabel("Connect")
            self.connect_btn.SetPrimary(True)
        else:
            same_site = self.site_choice.GetStringSelection() == state.site
            self.connect_btn.SetLabel("Disconnect" if same_site else "Load")
            self.connect_btn.SetPrimary(not same_site)

        self.Layout()

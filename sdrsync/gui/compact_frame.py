"""CompactFrame (spec §9) -- the 720x92 always-on-top-optional control
bar shown while undocked. Reuses StripPanel's own private widgets
(_Dot/_PttTag/_VDivider/_LabelValue) the same way status_bar_panel.py
already does, rather than reimplementing them.
"""
from __future__ import annotations

from typing import Callable, Optional

import wx

from sdrsync.config import AppSettings

from . import theme
from .fonts import big_freq_font, value_font_at
from .format import fmt_hz_split
from .state import AppState, ptt_tag_state
from .strip_panel import _Dot, _LabelValue, _PttTag, _VDivider
from .widgets import CheckBox, FlatButton, ToggleButton

FRAME_SIZE = (720, 92)


class _BigFreq(wx.Panel):
    """Compact bar's frequency readout (spec §9): the kHz group in the
    22pt Cormorant Garamond big-frequency face, the trailing Hz
    remainder smaller and MUTED alongside it -- the one place
    big_freq_font() is used (built in phase 1 of the GUI rewrite,
    unused until now)."""

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(parent.GetBackgroundColour())
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._khz = wx.StaticText(self, label="-")
        self._khz.SetFont(big_freq_font())
        self._khz.SetForegroundColour(theme.TEXT)
        self._khz.SetBackgroundColour(self.GetBackgroundColour())
        self._hz = wx.StaticText(self, label="")
        self._hz.SetFont(value_font_at(13))
        self._hz.SetForegroundColour(theme.MUTED)
        self._hz.SetBackgroundColour(self.GetBackgroundColour())
        sizer.Add(self._khz, 0, wx.ALIGN_BOTTOM)
        sizer.Add(self._hz, 0, wx.ALIGN_BOTTOM | wx.LEFT, self.FromDIP(2))
        self.SetSizer(sizer)

    def set_hz(self, hz: Optional[int]) -> None:
        khz_text, hz_text = fmt_hz_split(hz) if hz is not None else ("-", "")
        changed = False
        if self._khz.GetLabel() != khz_text:
            self._khz.SetLabel(khz_text)
            changed = True
        if self._hz.GetLabel() != hz_text:
            self._hz.SetLabel(hz_text)
            changed = True
        if not changed:
            return
        # See _LabelValue.set_value()'s comment (strip_panel.py) -- same
        # bug class: SetLabel() alone doesn't reflow CompactFrame's row
        # sizer, so the "-" placeholder's narrow allocated width stuck
        # around and clipped the real value to one character.
        self.Layout()
        top = self.GetTopLevelParent()
        if top is not None:
            top.Layout()


class CompactFrame(wx.Frame):
    """spec §9. Presentation only, same convention as StripPanel: owns
    no undock/dock/engine logic itself, just exposes pause_btn/mute_btn/
    dock_btn for MainFrame to Bind()."""

    def __init__(self, parent: Optional[wx.Window], settings: AppSettings) -> None:
        style = wx.CAPTION | wx.CLOSE_BOX | wx.FRAME_NO_TASKBAR
        if settings.keep_compact_bar_on_top:
            style |= wx.STAY_ON_TOP
        super().__init__(parent, title="SDRSync", style=style)
        self.SetBackgroundColour(theme.SURFACE)

        self.on_dock: Optional[Callable[[], None]] = None

        pad_v, pad_h = self.FromDIP(12), self.FromDIP(16)
        gap = self.FromDIP(11)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.rig_dot = _Dot(self)
        sizer.Add(self.rig_dot, 0, wx.ALIGN_CENTER_VERTICAL)

        self.freq = _BigFreq(self)
        sizer.Add(self.freq, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        sizer.Add(_VDivider(self), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.mode_group = _LabelValue(self, "MODE")
        sizer.Add(self.mode_group, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.ptt_tag = _PttTag(self)
        sizer.Add(self.ptt_tag, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        sizer.AddStretchSpacer(1)

        self.pause_btn = ToggleButton(self, "Pause sync")
        sizer.Add(self.pause_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.mute_btn = CheckBox(self, "mute tx", emphasize_when_checked=True)
        sizer.Add(self.mute_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        self.dock_btn = FlatButton(self, "Dock", is_primary=True)
        self.dock_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_dock and self.on_dock())
        sizer.Add(self.dock_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, gap)

        # Asymmetric 12 (top/bottom) x 16 (left/right) padding (spec §9)
        # -- same two-sizer border trick TransceiverPanel uses, since a
        # single Add() border applies one value to every flagged side.
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(sizer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad_h)
        outer.InsertSpacer(0, pad_v)
        outer.AddSpacer(pad_v)
        self.SetSizer(outer)
        # Spec §9's "size 720 x 92" is the *bar* -- i.e. the client area
        # the row above is laid out in -- so it has to be applied with
        # SetClientSize(), never as the wx.Frame size= argument. A
        # frame's size includes its decorations, and wx subtracts those
        # to get the client area the sizer actually gets to fill.
        #
        # This mattered enormously on Linux/GTK: under WSLg there is no
        # server-side-decorating window manager, so GTK3 falls back to
        # client-side decorations -- a title bar *plus* an invisible
        # resize/shadow border, all inside the X11 window. wx measured
        # those at 76 x 97 px. Asking for a 720x92 frame therefore left
        # a client area of 644 x -5, clamped to 644x1, and the sizer
        # dutifully laid every child out at height 0: the window mapped
        # at the right size and painted its own SURFACE background, but
        # was otherwise completely blank. win32's decorations are only
        # ~16x39 so the same bug merely squeezed the bar to a 53px-tall
        # client there -- visible but wrong, and enough of the row still
        # fit that it looked fine. Both platforms are correct now.
        #
        # Min/max are then taken from the resulting *frame* size, since
        # wx's SetMinSize/SetMaxSize on a top-level window are in frame
        # coordinates too -- setting them to FRAME_SIZE directly was the
        # same units mistake, and would have re-clamped the window back
        # down to a 1px client on GTK.
        self.SetClientSize(wx.Size(*FRAME_SIZE))
        self.SetMinSize(self.GetSize())
        self.SetMaxSize(self.GetSize())
        self.Layout()

    def refresh(self, state: AppState) -> None:
        if not state.rig_connected:
            self.rig_dot.set_mode("disconnected")
        elif state.paused:
            self.rig_dot.set_mode("paused")
        else:
            self.rig_dot.set_mode("syncing")
        self.ptt_tag.set_state(ptt_tag_state(state))
        self.freq.set_hz(state.rx_hz if state.rig_connected else None)
        self.mode_group.set_value(state.mode if state.rig_connected else "-", theme.TEXT)
        self.pause_btn.SetValue(state.paused)
        self.mute_btn.SetValue(state.mute_on_tx)

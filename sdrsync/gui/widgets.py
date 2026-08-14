"""Owner-drawn FlatButton / ToggleButton / CheckBox (spec §7).

wxPython's native buttons/checkboxes can't be themed to this design on
Windows, so these are hand-painted wx.Control subclasses using
wx.BufferedPaintDC (flicker-free) + wx.GraphicsContext (clean
anti-aliased stroked rounded rects and alpha fills, needed for the
hover/pressed states and the PTT-tag blink -- plain wx.DC rounded-rect +
alpha is unreliable cross-platform). Borders are always *stroked*, never
filled, per the spec's "accent as stroke only" rule.

Each control posts a real wx.EVT_BUTTON / wx.EVT_CHECKBOX /
wx.EVT_TOGGLEBUTTON -shaped CommandEvent on activation, so call sites
Bind() exactly as they would to the native control it replaces --
minimizing churn when migrating existing wx.Button/wx.CheckBox call
sites in app.py.
"""
from __future__ import annotations

from typing import Optional

import wx

from . import theme
from .fonts import value_font


class _OwnerDrawnMixin:
    """Shared hover/pressed/focus/disabled/checked state tracking and
    buffered-paint plumbing. Mixed into wx.Control subclasses below;
    subclasses implement _paint(gc, rect) and _activate()."""

    def _init_owner_drawn(self) -> None:
        self._hover = False
        self._pressed = False
        self._checked = False
        # Normally None: _on_paint() clears to the immediate parent's own
        # background colour, so the control blends into whatever panel
        # hosts it. Some call sites want a control to look identical
        # regardless of which panel it happens to live in instead (e.g.
        # TransceiverPanel's Connect/Disconnect button matching the
        # WebSDR strip's Connect/Disconnect button -- user-reported: the
        # two read as different background colours purely because their
        # parent panels use different background tokens, BG vs SURFACE).
        self._bg_override: Optional[wx.Colour] = None
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda evt: None)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_SET_FOCUS, self._on_focus_change)
        self.Bind(wx.EVT_KILL_FOCUS, self._on_focus_change)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

    def AcceptsFocus(self) -> bool:  # noqa: N802 - wx virtual method name
        return self.IsEnabled()

    def AcceptsFocusFromKeyboard(self) -> bool:  # noqa: N802
        return self.IsEnabled()

    def _set_flag(self, name: str, value: bool) -> None:
        if getattr(self, name) != value:
            setattr(self, name, value)
            self.Refresh()

    def _relayout_after_label_change(self) -> None:
        """SetMinSize() alone doesn't make an existing sizer reflow --
        it only takes effect on the NEXT Layout() pass. Without this, a
        label change to a wider string (e.g. "Test" -> "Testing...")
        left the control painting at its old, now-too-small rect, so
        the new text overflowed past the button's border (confirmed
        live). Walks up to the top-level frame, mirroring the same
        pattern already used for other dynamic-size changes (see
        TransceiverPanel._on_mock_rig_toggled), since a mid-tree
        Layout() alone doesn't always reach every affected sizer."""
        parent = self.GetParent()
        if parent is not None:
            parent.Layout()
        top = self.GetTopLevelParent()
        if top is not None:
            top.Layout()

    def _on_enter(self, evt: wx.MouseEvent) -> None:
        self._set_flag("_hover", True)
        evt.Skip()

    def _on_leave(self, evt: wx.MouseEvent) -> None:
        self._set_flag("_hover", False)
        self._set_flag("_pressed", False)
        evt.Skip()

    def _on_left_down(self, evt: wx.MouseEvent) -> None:
        if not self.IsEnabled():
            return
        if not self.HasFocus():
            self.SetFocus()
        self._set_flag("_pressed", True)
        evt.Skip()

    def _on_left_up(self, evt: wx.MouseEvent) -> None:
        if not self.IsEnabled():
            return
        was_pressed = self._pressed
        self._set_flag("_pressed", False)
        if was_pressed and wx.Rect(self.GetClientSize()).Contains(evt.GetPosition()):
            self._activate()
        evt.Skip()

    def _on_focus_change(self, evt: wx.FocusEvent) -> None:
        self.Refresh()
        evt.Skip()

    def _on_key_down(self, evt: wx.KeyEvent) -> None:
        if not self.IsEnabled():
            evt.Skip()
            return
        if evt.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._activate()
        else:
            evt.Skip()

    def set_bg_override(self, colour: Optional[wx.Colour]) -> None:
        """Force this control's painted background to `colour` instead of
        the immediate parent's background colour (pass None to go back to
        the default blend-with-parent behaviour)."""
        self._bg_override = colour
        self.Refresh()

    def _on_paint(self, evt: wx.PaintEvent) -> None:
        dc = wx.BufferedPaintDC(self)
        if self._bg_override is not None:
            parent_bg = self._bg_override
        else:
            parent_bg = self.GetParent().GetBackgroundColour() if self.GetParent() else theme.BG
        dc.SetBackground(wx.Brush(parent_bg))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        self._paint(gc, self.GetClientRect())

    def _paint(self, gc: "wx.GraphicsContext", rect: wx.Rect) -> None:
        raise NotImplementedError

    def _activate(self) -> None:
        raise NotImplementedError

    def _draw_focus_ring(self, gc: "wx.GraphicsContext", rect: wx.Rect, radius: float) -> None:
        if not self.HasFocus():
            return
        offset = self.FromDIP(2)
        path = gc.CreatePath()
        path.AddRoundedRectangle(
            -offset + 0.5, -offset + 0.5,
            rect.width + offset * 2 - 1, rect.height + offset * 2 - 1,
            radius + offset,
        )
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(theme.ACCENT).Width(2)))
        gc.StrokePath(path)


class FlatButton(_OwnerDrawnMixin, wx.Control):
    """spec §7 FlatButton. is_primary=True is the "outline only, never
    filled" primary variant (ACCENT border, ACCENT_TEXT label)."""

    def __init__(self, parent: wx.Window, label: str = "", is_primary: bool = False, **kwargs) -> None:
        style = kwargs.pop("style", 0)
        wx.Control.__init__(self, parent, style=wx.BORDER_NONE | style, **kwargs)
        self._init_owner_drawn()
        self._label = label
        self.is_primary = is_primary
        # "Danger" variant (TRANSMIT red border/label at rest) -- added
        # for Connect/Disconnect buttons' Disconnect state (user-reported:
        # wanted a persistent reddish look there, distinct from the gold
        # Connect/Load state, while keeping the universal gold hover/
        # press feedback unchanged -- "mouse over yellow is fine").
        # Mutually exclusive with is_primary; is_primary wins if both are
        # ever set True, since no call site needs both at once.
        self.is_danger = False
        self.SetFont(value_font())
        self._recompute_min_size()

    def SetLabel(self, label: str) -> None:  # noqa: N802
        if label == self._label:
            return
        self._label = label
        self._recompute_min_size()
        self._relayout_after_label_change()
        self.Refresh()

    def GetLabel(self) -> str:  # noqa: N802
        return self._label

    def SetPrimary(self, is_primary: bool) -> None:
        if is_primary == self.is_primary:
            return
        self.is_primary = is_primary
        self.Refresh()

    def SetDanger(self, is_danger: bool) -> None:
        if is_danger == self.is_danger:
            return
        self.is_danger = is_danger
        self.Refresh()

    def _rest_colour(self) -> wx.Colour:
        """Border/label colour when not hovered/pressed/disabled."""
        if self.is_primary:
            return theme.ACCENT
        if self.is_danger:
            return theme.TRANSMIT
        return theme.BORDER

    def _recompute_min_size(self) -> None:
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        w, h = dc.GetTextExtent(self._label or "Button")
        pad_x, pad_y = self.FromDIP(11), self.FromDIP(5)
        self.SetMinSize(wx.Size(w + pad_x * 2, h + pad_y * 2))
        self.InvalidateBestSize()

    def _activate(self) -> None:
        evt = wx.CommandEvent(wx.wxEVT_BUTTON, self.GetId())
        evt.SetEventObject(self)
        wx.PostEvent(self, evt)

    def _fill_and_border(self):
        base_border = self._rest_colour()
        if not self.IsEnabled():
            return theme.with_alpha(base_border, theme.DISABLED_ALPHA), None
        if self._pressed:
            return theme.ACCENT, theme.with_alpha(theme.ACCENT, 41)  # 16%
        if self._hover:
            return theme.ACCENT, theme.ACCENT_TINT
        return base_border, None

    def _text_colour(self):
        if self.is_primary:
            colour = theme.ACCENT_TEXT
        elif self.is_danger:
            colour = theme.TRANSMIT
        else:
            colour = theme.TEXT
        if not self.IsEnabled():
            return theme.with_alpha(colour, theme.DISABLED_ALPHA)
        return colour

    def _paint(self, gc: "wx.GraphicsContext", rect: wx.Rect) -> None:
        radius = self.FromDIP(theme.RADIUS)
        border_colour, fill_colour = self._fill_and_border()

        path = gc.CreatePath()
        path.AddRoundedRectangle(0.5, 0.5, max(rect.width - 1, 0), max(rect.height - 1, 0), radius)
        if fill_colour is not None:
            gc.SetBrush(gc.CreateBrush(wx.Brush(fill_colour)))
            gc.FillPath(path)
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(border_colour).Width(1)))
        gc.StrokePath(path)

        self._draw_focus_ring(gc, rect, radius)

        gc.SetFont(self.GetFont(), self._text_colour())
        tw, th = gc.GetTextExtent(self._label)
        gc.DrawText(self._label, (rect.width - tw) / 2, (rect.height - th) / 2)


class ToggleButton(FlatButton):
    """spec §7 ToggleButton: FlatButton plus a latched checked state,
    rendered as the primary variant when checked. bordered=False gives
    the borderless ALL-CAPS variant used by SectionBar's panel toggles."""

    def __init__(self, parent: wx.Window, label: str = "", bordered: bool = True, **kwargs) -> None:
        self.bordered = bordered
        super().__init__(parent, label=label, **kwargs)

    def SetValue(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self.Refresh()

    def GetValue(self) -> bool:
        return self._checked

    def _activate(self) -> None:
        self._checked = not self._checked
        evt = wx.CommandEvent(wx.wxEVT_TOGGLEBUTTON, self.GetId())
        evt.SetEventObject(self)
        evt.SetInt(1 if self._checked else 0)
        wx.PostEvent(self, evt)

    def _fill_and_border(self):
        if not self.bordered:
            return None, None
        self.is_primary = self._checked
        return super()._fill_and_border()

    def _text_colour(self):
        if not self.bordered:
            # SectionBar variant (spec §4): MUTED normally, ACCENT_TEXT
            # when its panel is open (checked) or on hover.
            colour = theme.ACCENT_TEXT if (self._checked or self._hover) else theme.MUTED
        else:
            colour = theme.ACCENT_TEXT if self._checked else theme.TEXT
        if not self.IsEnabled():
            return theme.with_alpha(colour, theme.DISABLED_ALPHA)
        return colour

    def _paint(self, gc: "wx.GraphicsContext", rect: wx.Rect) -> None:
        if not self.bordered:
            gc.SetFont(self.GetFont(), self._text_colour())
            tw, th = gc.GetTextExtent(self._label)
            gc.DrawText(self._label, (rect.width - tw) / 2, (rect.height - th) / 2)
            if self.HasFocus():
                self._draw_focus_ring(gc, rect, self.FromDIP(theme.RADIUS))
            return
        super()._paint(gc, rect)


class CheckBox(_OwnerDrawnMixin, wx.Control):
    """spec §7 CheckBox: 14px drawn box + 7px gap + label, whole row
    clickable (the hit-test covers the full control rect, not just the
    box)."""

    BOX_SIZE_DIP = 14
    GAP_DIP = 7

    def __init__(self, parent: wx.Window, label: str = "", emphasize_when_checked: bool = False, **kwargs) -> None:
        style = kwargs.pop("style", 0)
        wx.Control.__init__(self, parent, style=wx.BORDER_NONE | style, **kwargs)
        self._init_owner_drawn()
        self._label = label
        # Opt-in (default False, preserving every existing CheckBox's look
        # unchanged): dims the label to MUTED gray while unchecked and
        # lifts it to ACCENT_TEXT while checked, instead of the flat TEXT
        # colour every other CheckBox uses regardless of state. For a
        # checkbox whose checked state has a live, ongoing effect (e.g.
        # "Mute on TX" actually muting audio right now), the box's own
        # small tint/checkmark wasn't obvious enough on its own -- see
        # strip_panel.py's mute_btn.
        self.emphasize_when_checked = emphasize_when_checked
        self.SetFont(value_font())
        self._recompute_min_size()

    def SetValue(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self.Refresh()

    def GetValue(self) -> bool:
        return self._checked

    def SetLabel(self, label: str) -> None:  # noqa: N802
        if label == self._label:
            return
        self._label = label
        self._recompute_min_size()
        self._relayout_after_label_change()
        self.Refresh()

    def GetLabel(self) -> str:  # noqa: N802
        return self._label

    def _text_colour(self):
        if self.emphasize_when_checked:
            colour = theme.ACCENT_TEXT if self._checked else theme.MUTED
        else:
            colour = theme.TEXT
        if not self.IsEnabled():
            return theme.with_alpha(colour, theme.DISABLED_ALPHA)
        return colour

    def _recompute_min_size(self) -> None:
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        w, h = dc.GetTextExtent(self._label)
        box = self.FromDIP(self.BOX_SIZE_DIP)
        gap = self.FromDIP(self.GAP_DIP)
        self.SetMinSize(wx.Size(box + gap + w, max(box, h)))
        self.InvalidateBestSize()

    def _activate(self) -> None:
        self._checked = not self._checked
        self.Refresh()
        evt = wx.CommandEvent(wx.wxEVT_CHECKBOX, self.GetId())
        evt.SetEventObject(self)
        evt.SetInt(1 if self._checked else 0)
        wx.PostEvent(self, evt)

    def _paint(self, gc: "wx.GraphicsContext", rect: wx.Rect) -> None:
        box = self.FromDIP(self.BOX_SIZE_DIP)
        gap = self.FromDIP(self.GAP_DIP)
        box_y = (rect.height - box) / 2
        radius = self.FromDIP(2)

        path = gc.CreatePath()
        path.AddRoundedRectangle(0.5, box_y + 0.5, box - 1, box - 1, radius)
        if self._checked:
            fill = theme.with_alpha(theme.ACCENT_TINT, 255)
            gc.SetBrush(gc.CreateBrush(wx.Brush(fill)))
            gc.FillPath(path)
        border = theme.with_alpha(theme.BORDER, theme.DISABLED_ALPHA) if not self.IsEnabled() else theme.BORDER
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(border).Width(1)))
        gc.StrokePath(path)

        if self._checked:
            check_colour = theme.with_alpha(theme.ACCENT, theme.DISABLED_ALPHA) if not self.IsEnabled() else theme.ACCENT
            gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(check_colour).Width(2)))
            check_path = gc.CreatePath()
            x0, y0 = box * 0.22, box_y + box * 0.52
            x1, y1 = box * 0.42, box_y + box * 0.72
            x2, y2 = box * 0.80, box_y + box * 0.28
            check_path.MoveToPoint(x0, y0)
            check_path.AddLineToPoint(x1, y1)
            check_path.AddLineToPoint(x2, y2)
            gc.StrokePath(check_path)

        if self.HasFocus():
            self._draw_focus_ring(gc, wx.Rect(0, int(box_y), box, box), radius)

        gc.SetFont(self.GetFont(), self._text_colour())
        _, th = gc.GetTextExtent(self._label)
        gc.DrawText(self._label, box + gap, (rect.height - th) / 2)

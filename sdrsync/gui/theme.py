"""Design tokens for the pixel-perfect GUI rewrite.

Values transcribed verbatim from GUI/SDRSync-wxpython-spec.md §1 (colour
tokens) and the spacing scale from the same section. Every pixel value
routes through dip() (wx.Window.FromDIP) at the call site, not here,
since FromDIP needs a live wx.Window to query the current display's
scale factor.
"""
from __future__ import annotations

import wx

BG = wx.Colour(0xF3, 0xF2, 0xF2)
SURFACE = wx.Colour(0xEA, 0xE9, 0xE9)
TEXT = wx.Colour(0x20, 0x1F, 0x1D)
MUTED = wx.Colour(0x7D, 0x79, 0x79)
FAINT = wx.Colour(0x9B, 0x97, 0x97)
DIVIDER = wx.Colour(0xD2, 0xD0, 0xCE)
BORDER = wx.Colour(0xBA, 0xB6, 0xB6)
ACCENT = wx.Colour(0xB6, 0x82, 0x35)
ACCENT_TEXT = wx.Colour(0x7D, 0x54, 0x11)
ACCENT_TINT = wx.Colour(0xFF, 0xF3, 0xE4)
PANEL_ALT = wx.Colour(0xF8, 0xF4, 0xF4)
# Not in spec §1's literal palette (which treats OFFLINE/RECEIVE as the
# same FAINT grey) -- added at the user's explicit request for a
# semantic RX/TX indicator: green while actually receiving, red while
# transmitting, keeping the app's muted/low-saturation tonal weight
# rather than a bright/saturated red or green.
RECEIVE = wx.Colour(0x3F, 0x8C, 0x4A)
TRANSMIT = wx.Colour(0xB0, 0x3A, 0x2E)

# Spacing scale (spec §1) and control corner radius (spec §1: "4 px").
SPACING = (5, 9, 14, 18, 28)
RADIUS = 4

# Band heights (spec §2).
STRIP_HEIGHT = 46
SECTION_BAR_HEIGHT = 30
STATUS_BAR_HEIGHT = 28

# Frame geometry (spec §2).
FRAME_SIZE = (1360, 812)
FRAME_MIN_SIZE = (1100, 640)


def with_alpha(colour: wx.Colour, alpha: int) -> wx.Colour:
    """colour at a given 0-255 alpha (e.g. the pressed-state 16% accent
    fill, or the disabled-state 45% opacity, spec §7)."""
    return wx.Colour(colour.Red(), colour.Green(), colour.Blue(), alpha)


def draw_hairline(dc: "wx.DC", rect: wx.Rect, edge: str = "bottom") -> None:
    """Draw a 1px DIVIDER line on one edge of rect.

    Spec §2: every chrome band draws its own hairline in EVT_PAINT rather
    than relying on wx.StaticLine, which renders as OS grey and ignores
    DIVIDER. edge is one of "top"/"bottom".
    """
    dc.SetPen(wx.Pen(DIVIDER, 1))
    y = rect.GetTop() if edge == "top" else rect.GetBottom()
    dc.DrawLine(rect.GetLeft(), y, rect.GetRight() + 1, y)


def caps(text: str) -> str:
    """Fake letter-spacing for ALL CAPS label/kicker text (spec §1 note:
    '" ".join()' when real letter-spacing isn't available)."""
    return " ".join(text.upper())

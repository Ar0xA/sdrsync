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
# Darkened from the original spec §1 values (0x7D7979/0x9B9797) at the
# user's explicit request, in two rounds -- the first round (MUTED to
# 0x5C5858, ~6.3:1) fixed the field captions (RX VFO/TX VFO/MODE, every
# settings-panel label) fine, but FAINT only moved to MUTED's old value
# (~3.9:1) and was still reported hard to read for site URLs and status/
# hint text (e.g. "rigctld not reachable until connected") -- both now
# pushed further. Still grey, not TEXT's near-black, per "doesn't have to
# be black text, but more contrast".
MUTED = wx.Colour(0x4A, 0x46, 0x46)  # ~8.4:1 against BG
FAINT = wx.Colour(0x5C, 0x58, 0x58)  # ~6.3:1 against BG (was MUTED's 1st-round value)
# Disabled-control opacity (spec §7 was 45%/115 -- also reported hard to
# read, e.g. the Sites panel's "Edit" button on a non-editable curated
# site). 63% keeps disabled controls visibly de-emphasized relative to
# enabled ones while landing at ~4.5:1 contrast (was ~2.7:1 at 45%).
DISABLED_ALPHA = 160
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

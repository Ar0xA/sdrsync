"""Font loading for the pixel-perfect GUI rewrite (spec §1's font table).

wx exposes no OpenType feature-tag API on any platform, so there is no
way to directly request Lora's tnum/lnum (tabular lining figures)
through wx.Font. Instead, load_fonts() runs a one-time runtime self-test
(render every digit, compare widths) and transparently substitutes the
documented fallback chain (Cascadia Mono -> DejaVu Sans Mono -> teletype
default) for VALUE-role text only, if Lora's own default figure style
turns out not to be tabular through this rendering path. Non-value roles
(labels, headings, body) don't need tabular figures and always use
Lora/Cormorant Garamond directly once AddPrivateFont succeeds.
"""
from __future__ import annotations

import logging

import wx

from .. import resources

logger = logging.getLogger(__name__)

_LORA_FILE = "Lora-Regular.ttf"
_CORMORANT_FILE = "CormorantGaramond-Regular.ttf"
LORA_FACE = "Lora"
CORMORANT_FACE = "Cormorant Garamond"

# Spec §1: documented fallback order when tabular figures aren't available.
_FALLBACK_MONO_CANDIDATES = ("Cascadia Mono", "DejaVu Sans Mono")

_loaded = False
_value_face = LORA_FACE
_font_cache: dict[tuple, wx.Font] = {}


def load_fonts() -> None:
    """Load the vendored TTFs and resolve the tabular-figure fallback.

    Call once, early in SDRSyncApp.OnInit(), before any frame is built.
    Idempotent, and non-fatal on a broken install (missing font files or
    a failed AddPrivateFont just widen the fallback chain, logged as a
    warning -- the app must still start).
    """
    global _loaded, _value_face
    if _loaded:
        return
    _loaded = True

    for filename in (_LORA_FILE, _CORMORANT_FILE):
        path = resources.FONTS_DIR / filename
        if not path.exists():
            logger.warning("Font file missing, falling back to system fonts: %s", path)
            continue
        if not wx.Font.AddPrivateFont(str(path)):
            logger.warning("wx.Font.AddPrivateFont failed for: %s", path)

    _value_face = _resolve_value_face()


def _digit_widths(face: str, point_size: int) -> list[int]:
    font = wx.Font(wx.FontInfo(point_size).FaceName(face))
    bmp = wx.Bitmap(1, 1)
    dc = wx.MemoryDC(bmp)
    dc.SetFont(font)
    widths = [dc.GetTextExtent(d)[0] for d in "0123456789"]
    dc.SelectObject(wx.NullBitmap)
    return widths


def _is_tabular(face: str, point_size: int = 10) -> bool:
    try:
        widths = _digit_widths(face, point_size)
    except Exception:
        return False
    if not widths or 0 in widths:
        return False
    return (max(widths) - min(widths)) <= 1  # 1px epsilon at 96 DPI


def _resolve_value_face() -> str:
    if _is_tabular(LORA_FACE):
        return LORA_FACE
    for candidate in _FALLBACK_MONO_CANDIDATES:
        if _is_tabular(candidate):
            logger.info("Lora digits are not tabular here; using %s for value text.", candidate)
            return candidate
    logger.warning("No tabular-figure font found on this system; frequency digits may jitter.")
    return wx.Font(wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL).GetFaceName() or LORA_FACE


def _cached(key: tuple, builder) -> wx.Font:
    font = _font_cache.get(key)
    if font is None:
        font = builder()
        _font_cache[key] = font
    return font


def value_font() -> wx.Font:
    """10pt — frequencies, mode, values (spec §1 table row 1)."""
    if not _loaded:
        load_fonts()
    return _cached(("value",), lambda: wx.Font(wx.FontInfo(10).FaceName(_value_face)))


def value_font_at(point_size: float) -> wx.Font:
    """Value-role (tabular-figure-resolved) face at a non-default size --
    e.g. StatusBarPanel's 8pt line (spec §8)."""
    if not _loaded:
        load_fonts()
    return _cached(("value", point_size), lambda: wx.Font(wx.FontInfo(point_size).FaceName(_value_face)))


def big_freq_font() -> wx.Font:
    """22pt Cormorant Garamond, regular weight — compact-bar big frequency."""
    if not _loaded:
        load_fonts()
    return _cached(("big_freq",), lambda: wx.Font(wx.FontInfo(22).FaceName(CORMORANT_FACE)))


def label_font() -> wx.Font:
    """7.5pt Lora — labels/kickers (rendered ALL CAPS via theme.caps() at call sites)."""
    if not _loaded:
        load_fonts()
    return _cached(("label",), lambda: wx.Font(wx.FontInfo(7.5).FaceName(LORA_FACE)))


def heading_font() -> wx.Font:
    """26pt Cormorant Garamond, regular weight — idle-screen headings."""
    if not _loaded:
        load_fonts()
    return _cached(("heading",), lambda: wx.Font(wx.FontInfo(26).FaceName(CORMORANT_FACE)))


def body_font() -> wx.Font:
    """10pt Lora — idle-screen body paragraph."""
    if not _loaded:
        load_fonts()
    return _cached(("body",), lambda: wx.Font(wx.FontInfo(10).FaceName(LORA_FACE)))

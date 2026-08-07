"""Locates bundled non-code resources (currently just icon.ico) in a way
that works both from source and from a PyInstaller build.

Under PyInstaller, this module's own __file__ does NOT correspond to a
real file on disk -- pure-Python modules are bundled inside a PYZ
archive, not copied out as loose .py files (confirmed: a --onedir build's
dist/SDRSync/_internal/sdrsync/ directory does not exist). So a
naive `Path(__file__).parent` only works when running from source;
sys._MEIPASS is PyInstaller's own extraction-root pointer, set only in a
frozen build, and is the correct base in that case. The build must pass
--add-data "sdrsync/icon.ico;sdrsync" so icon.ico ends up at
_MEIPASS/sdrsync/icon.ico -- a plain --add-data "sdrsync/icon.ico;."
(dropping it at _MEIPASS/icon.ico) will NOT be found by this module.
See project_brief.md's packaging notes for the full build command.
"""
from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys._MEIPASS) / "sdrsync"  # type: ignore[attr-defined]
else:
    _BASE_DIR = Path(__file__).resolve().parent

ICON_PATH = _BASE_DIR / "icon.ico"

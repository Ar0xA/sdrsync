"""Shared status type for rig-control backends (rigctld, flrig, ...).

Mirrors sdrsync/websdr/base.py's role for the WebSDR side: a small
backend-agnostic dataclass, kept separate from any one backend's client
module so other backends don't need a sibling-module import to use it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RigState:
    freq_hz: Optional[int]
    mode: Optional[str]
    passband_hz: Optional[int]
    ptt: Optional[bool]

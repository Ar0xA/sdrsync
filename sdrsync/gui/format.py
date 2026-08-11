"""Formatting helpers (spec §11) — pure functions, no wx dependency."""
from __future__ import annotations


def fmt_hz(hz: int) -> str:
    """14074500 -> "14.074.500" """
    mhz, rest = divmod(abs(hz), 1_000_000)
    khz, hzr = divmod(rest, 1_000)
    sign = "-" if hz < 0 else ""
    return f"{sign}{mhz}.{khz:03d}.{hzr:03d}"


def fmt_delta(a: int, b: int) -> str:
    """-> "+60 Hz" / "−140 Hz" """
    d = a - b
    return f"{'+' if d >= 0 else '−'}{abs(d)} Hz"


def fmt_hz_split(hz: int) -> tuple[str, str]:
    """14074500 -> ("14.074", "500") -- kHz group / Hz remainder, for
    the compact bar's two-size frequency display (spec §9): the kHz
    group renders in the big frequency font, the Hz remainder smaller
    and muted alongside it."""
    mhz, rest = divmod(abs(hz), 1_000_000)
    khz, hzr = divmod(rest, 1_000)
    sign = "-" if hz < 0 else ""
    return f"{sign}{mhz}.{khz:03d}", f"{hzr:03d}"

"""sdrsync.gui.format's fmt_hz/fmt_delta (spec §11) -- pure functions,
no wx dependency, so importable without a display."""
from sdrsync.gui.format import fmt_delta, fmt_hz


def test_fmt_hz_basic():
    assert fmt_hz(14_074_500) == "14.074.500"


def test_fmt_hz_zero():
    assert fmt_hz(0) == "0.000.000"


def test_fmt_hz_negative():
    assert fmt_hz(-14_074_500) == "-14.074.500"


def test_fmt_hz_sub_khz():
    assert fmt_hz(500) == "0.000.500"


def test_fmt_delta_positive():
    assert fmt_delta(14_074_560, 14_074_500) == "+60 Hz"


def test_fmt_delta_negative():
    assert fmt_delta(14_074_360, 14_074_500) == "−140 Hz"


def test_fmt_delta_zero():
    assert fmt_delta(14_074_500, 14_074_500) == "+0 Hz"

"""update_check's version comparison -- pure functions, no network."""
from sdrsync.update_check import is_newer


def test_is_newer_true():
    assert is_newer("2.1.0", "2.0.2")


def test_is_newer_false_equal():
    assert not is_newer("2.0.2", "2.0.2")


def test_is_newer_false_older():
    assert not is_newer("2.0.1", "2.0.2")


def test_is_newer_v_prefix():
    assert is_newer("v2.1.0", "2.0.2")


def test_is_newer_current_has_v_prefix_too():
    assert is_newer("v2.1.0", "v2.0.2")


def test_is_newer_minor_vs_major():
    assert is_newer("3.0.0", "2.9.9")


def test_is_newer_different_length():
    assert is_newer("2.1.0.1", "2.1.0")
    assert not is_newer("2.1", "2.1.0")


def test_is_newer_prerelease_suffix_digits_taken():
    # "2.1.0-rc1" -> (2, 1, 0) since the suffix's leading digits (none,
    # the "-" breaks the digit run immediately) are dropped rather than
    # raising.
    assert not is_newer("2.1.0-rc1", "2.1.0")

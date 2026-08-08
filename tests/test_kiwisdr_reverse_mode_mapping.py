from sdrsync.websdr.kiwisdr import map_kiwi_mode_to_hamlib


def test_usb_maps_to_usb():
    assert map_kiwi_mode_to_hamlib("USB") == "USB"


def test_lsb_maps_to_lsb():
    assert map_kiwi_mode_to_hamlib("LSB") == "LSB"


def test_cw_maps_to_cw():
    assert map_kiwi_mode_to_hamlib("CW") == "CW"


def test_am_maps_to_am():
    assert map_kiwi_mode_to_hamlib("AM") == "AM"


def test_amw_also_maps_to_am_no_separate_hamlib_wide_mode():
    assert map_kiwi_mode_to_hamlib("AMW") == "AM"


def test_sam_maps_to_sam():
    assert map_kiwi_mode_to_hamlib("SAM") == "SAM"


def test_nbfm_maps_to_fm():
    assert map_kiwi_mode_to_hamlib("NBFM") == "FM"


def test_case_insensitive():
    assert map_kiwi_mode_to_hamlib("usb") == "USB"


def test_none_input_returns_none():
    assert map_kiwi_mode_to_hamlib(None) is None


def test_unknown_mode_returns_none():
    assert map_kiwi_mode_to_hamlib("IQ") is None

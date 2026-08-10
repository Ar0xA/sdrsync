from sdrsync.websdr.websdr_org import map_websdr_mode_to_hamlib


def test_usb_maps_to_usb():
    assert map_websdr_mode_to_hamlib("USB") == "USB"


def test_lsb_maps_to_lsb():
    assert map_websdr_mode_to_hamlib("LSB") == "LSB"


def test_cw_maps_to_cw():
    assert map_websdr_mode_to_hamlib("CW") == "CW"


def test_am_maps_to_am():
    assert map_websdr_mode_to_hamlib("AM") == "AM"


def test_amsync_maps_to_sam():
    assert map_websdr_mode_to_hamlib("AMSYNC") == "SAM"


def test_fm_maps_to_fm():
    assert map_websdr_mode_to_hamlib("FM") == "FM"


def test_case_insensitive():
    assert map_websdr_mode_to_hamlib("usb") == "USB"


def test_none_input_returns_none():
    assert map_websdr_mode_to_hamlib(None) is None


def test_unknown_mode_returns_none():
    assert map_websdr_mode_to_hamlib("DSTAR") is None

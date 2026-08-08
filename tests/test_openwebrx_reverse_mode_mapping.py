from sdrsync.websdr.openwebrx import map_openwebrx_mode_to_hamlib


def test_usb_maps_to_usb():
    assert map_openwebrx_mode_to_hamlib("USB") == "USB"


def test_lsb_maps_to_lsb():
    assert map_openwebrx_mode_to_hamlib("LSB") == "LSB"


def test_cw_maps_to_cw():
    assert map_openwebrx_mode_to_hamlib("CW") == "CW"


def test_am_maps_to_am():
    assert map_openwebrx_mode_to_hamlib("AM") == "AM"


def test_nfm_maps_to_fm():
    assert map_openwebrx_mode_to_hamlib("NFM") == "FM"


def test_wfm_maps_to_wfm():
    assert map_openwebrx_mode_to_hamlib("WFM") == "WFM"


def test_case_insensitive():
    assert map_openwebrx_mode_to_hamlib("usb") == "USB"


def test_none_input_returns_none():
    assert map_openwebrx_mode_to_hamlib(None) is None


def test_unknown_mode_returns_none():
    assert map_openwebrx_mode_to_hamlib("DIGITAL") is None

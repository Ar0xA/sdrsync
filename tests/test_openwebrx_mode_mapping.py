from sdrsync.websdr.openwebrx import map_hamlib_mode_openwebrx


def test_direct_mode_mapping():
    assert map_hamlib_mode_openwebrx("USB") == "usb"
    assert map_hamlib_mode_openwebrx("LSB") == "lsb"
    assert map_hamlib_mode_openwebrx("CW") == "cw"
    assert map_hamlib_mode_openwebrx("AM") == "am"
    assert map_hamlib_mode_openwebrx("FM") == "nfm"
    assert map_hamlib_mode_openwebrx("WFM") == "wfm"


def test_case_insensitive():
    assert map_hamlib_mode_openwebrx("usb") == "usb"
    assert map_hamlib_mode_openwebrx("Cw") == "cw"


def test_cwr_maps_to_cw():
    assert map_hamlib_mode_openwebrx("CWR") == "cw"


def test_packet_modes_map_to_their_sideband():
    assert map_hamlib_mode_openwebrx("PKTUSB") == "usb"
    assert map_hamlib_mode_openwebrx("PKTLSB") == "lsb"


def test_unknown_mode_returns_none():
    assert map_hamlib_mode_openwebrx("DSTAR") is None

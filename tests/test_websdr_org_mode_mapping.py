from sdrsync.websdr.websdr_org import map_hamlib_mode


def test_usb_wide_passband_stays_wide():
    assert map_hamlib_mode("USB", 2700) == "USB"


def test_usb_narrow_passband_gets_narrow_variant():
    assert map_hamlib_mode("USB", 1800) == "USBN"


def test_lsb_case_insensitive():
    assert map_hamlib_mode("lsb", 2700) == "LSB"


def test_am_narrow_threshold_differs_from_usb():
    # 5000 Hz is "wide" for USB/LSB (>2000) but "narrow" for AM (<6000)
    assert map_hamlib_mode("AM", 5000) == "AMN"
    assert map_hamlib_mode("USB", 5000) == "USB"


def test_cw_and_cwr_both_map_to_cw_no_narrow_variant():
    assert map_hamlib_mode("CW", 500) == "CW"
    assert map_hamlib_mode("CWR", 500) == "CW"


def test_packet_modes_map_to_their_sideband():
    assert map_hamlib_mode("PKTUSB", 2700) == "USB"
    assert map_hamlib_mode("PKTLSB", 2700) == "LSB"


def test_no_passband_defaults_to_wide():
    assert map_hamlib_mode("USB", None) == "USB"


def test_unknown_mode_returns_none():
    assert map_hamlib_mode("DSTAR", 2700) is None


def test_data_modes_map_to_their_sideband():
    assert map_hamlib_mode("DATA-U", 2700) == "USB"
    assert map_hamlib_mode("DATA-U", 1800) == "USBN"
    assert map_hamlib_mode("DATA-L", 2700) == "LSB"
    assert map_hamlib_mode("DATA-L", 1800) == "LSBN"


def test_cw_u_and_cw_l_map_to_plain_cw_no_narrow_variant():
    assert map_hamlib_mode("CW-U", 500) == "CW"
    assert map_hamlib_mode("CW-L", 500) == "CW"

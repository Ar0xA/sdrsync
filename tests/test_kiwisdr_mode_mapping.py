from sdrsync.websdr.kiwisdr import map_hamlib_mode_kiwi


def test_usb_wide_passband_stays_wide():
    assert map_hamlib_mode_kiwi("USB", 2700) == "usb"


def test_usb_narrow_passband_gets_narrow_variant():
    assert map_hamlib_mode_kiwi("USB", 1800) == "usn"


def test_lsb_case_insensitive():
    assert map_hamlib_mode_kiwi("lsb", 2700) == "lsb"


def test_lsb_narrow_passband_gets_narrow_variant():
    assert map_hamlib_mode_kiwi("LSB", 1800) == "lsn"


def test_am_narrow_and_wide_thresholds():
    assert map_hamlib_mode_kiwi("AM", 1800) == "amn"
    assert map_hamlib_mode_kiwi("AM", 5000) == "am"
    assert map_hamlib_mode_kiwi("AM", 9000) == "amw"


def test_cw_and_cwr_both_map_to_cw_with_narrow_variant():
    assert map_hamlib_mode_kiwi("CW", 500) == "cwn"
    assert map_hamlib_mode_kiwi("CWR", 500) == "cwn"
    assert map_hamlib_mode_kiwi("CW", 2700) == "cw"


def test_packet_modes_map_to_their_sideband():
    assert map_hamlib_mode_kiwi("PKTUSB", 2700) == "usb"
    assert map_hamlib_mode_kiwi("PKTLSB", 2700) == "lsb"


def test_fm_modes_map_to_nbfm_narrow_or_wide():
    assert map_hamlib_mode_kiwi("FM", 1800) == "nnfm"
    assert map_hamlib_mode_kiwi("WFM", 12000) == "nbfm"


def test_sam_has_no_narrow_variant():
    assert map_hamlib_mode_kiwi("SAM", 1800) == "sam"
    assert map_hamlib_mode_kiwi("SAM", None) == "sam"


def test_no_passband_defaults_to_wide():
    assert map_hamlib_mode_kiwi("USB", None) == "usb"


def test_unknown_mode_returns_none():
    assert map_hamlib_mode_kiwi("DSTAR", 2700) is None


def test_data_modes_map_to_their_sideband():
    assert map_hamlib_mode_kiwi("DATA-U", 2700) == "usb"
    assert map_hamlib_mode_kiwi("DATA-U", 1800) == "usn"
    assert map_hamlib_mode_kiwi("DATA-L", 2700) == "lsb"
    assert map_hamlib_mode_kiwi("DATA-L", 1800) == "lsn"


def test_cw_u_and_cw_l_map_to_plain_cw():
    assert map_hamlib_mode_kiwi("CW-U", 500) == "cwn"
    assert map_hamlib_mode_kiwi("CW-L", 500) == "cwn"
    assert map_hamlib_mode_kiwi("CW-U", 2700) == "cw"
    assert map_hamlib_mode_kiwi("CW-L", 2700) == "cw"

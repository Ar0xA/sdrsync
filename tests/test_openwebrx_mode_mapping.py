from sdrsync.websdr.openwebrx import map_hamlib_mode_openwebrx, passband_edges


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


def test_data_modes_map_to_their_sideband():
    assert map_hamlib_mode_openwebrx("DATA-U") == "usb"
    assert map_hamlib_mode_openwebrx("DATA-L") == "lsb"


def test_cw_u_and_cw_l_map_to_plain_cw():
    assert map_hamlib_mode_openwebrx("CW-U") == "cw"
    assert map_hamlib_mode_openwebrx("CW-L") == "cw"


# --- passband_edges() -----------------------------------------------------
# Unlike websdr_org.py/kiwisdr.py, OpenWebRX has no static per-mode
# default table at all (it's entirely server-sent at runtime -- see
# module docstring), so passband_edges() takes the live default as an
# explicit argument instead of looking one up internally. These tests
# use made-up-but-plausible default_bandpass values (there's no "real"
# reference to confirm against without a live instance) -- what's under
# test is the fixed-edge-vs-symmetric CONVENTION (_SIDEBAND), which IS a
# universal, hardcoded ham-radio/DSP fact, not this driver's guess.


def test_usb_low_edge_stays_fixed_width_added_above():
    assert passband_edges("usb", 2400, (300, 2700)) == (300, 2700)  # exactly the given default
    assert passband_edges("usb", 1200, (300, 2700)) == (300, 1500)


def test_lsb_high_edge_stays_fixed_width_added_below():
    assert passband_edges("lsb", 2400, (-2700, -300)) == (-2700, -300)
    assert passband_edges("lsb", 1200, (-2700, -300)) == (-1500, -300)


def test_am_nfm_wfm_symmetric_about_the_dial():
    assert passband_edges("am", 9000, (-4500, 4500)) == (-4500, 4500)
    assert passband_edges("nfm", 12000, (-6000, 6000)) == (-6000, 6000)
    assert passband_edges("wfm", 150000, (-75000, 75000)) == (-75000, 75000)


def test_cw_symmetric_about_whatever_centre_the_live_default_gives():
    """No hardcoded CW centre here (unlike websdr_org.py's -750 Hz or
    kiwisdr.py's +500 Hz) -- the centre comes entirely from
    default_bandpass, since this site's own value isn't known without a
    live instance."""
    assert passband_edges("cw", 400, (300, 700)) == (300, 700)  # centre +500, matches given default
    assert passband_edges("cw", 200, (300, 700)) == (400, 600)  # narrower, same +500 centre


def test_none_width_or_missing_default_says_nothing_about_the_filter():
    assert passband_edges("usb", None, (300, 2700)) is None
    assert passband_edges("usb", 0, (300, 2700)) is None
    assert passband_edges("usb", -100, (300, 2700)) is None
    assert passband_edges("usb", 2400, None) is None  # live default unreadable


def test_unknown_mode_has_no_edges():
    assert passband_edges("iq", 2400, (-5000, 5000)) is None

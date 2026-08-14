from sdrsync.websdr.websdr_org import map_hamlib_mode, passband_edges


def test_usb_maps_to_usb():
    assert map_hamlib_mode("USB") == "USB"


def test_lsb_case_insensitive():
    assert map_hamlib_mode("lsb") == "LSB"


def test_am_maps_to_am():
    assert map_hamlib_mode("AM") == "AM"


def test_sam_maps_to_amsync():
    assert map_hamlib_mode("SAM") == "AMSYNC"


def test_cw_and_cwr_both_map_to_cw():
    assert map_hamlib_mode("CW") == "CW"
    assert map_hamlib_mode("CWR") == "CW"


def test_cw_u_and_cw_l_map_to_plain_cw():
    assert map_hamlib_mode("CW-U") == "CW"
    assert map_hamlib_mode("CW-L") == "CW"


def test_packet_modes_map_to_their_sideband():
    assert map_hamlib_mode("PKTUSB") == "USB"
    assert map_hamlib_mode("PKTLSB") == "LSB"


def test_data_modes_map_to_their_sideband():
    assert map_hamlib_mode("DATA-U") == "USB"
    assert map_hamlib_mode("DATA-L") == "LSB"


def test_fm_and_wfm_both_map_to_fm():
    assert map_hamlib_mode("FM") == "FM"
    assert map_hamlib_mode("WFM") == "FM"


def test_unknown_mode_returns_none():
    assert map_hamlib_mode("DSTAR") is None


# --- passband_edges() -----------------------------------------------------
# Reference points taken directly from the live site's own set_mode()
# preset table (see module docstring): USB (0.3, 2.7), LSB (-2.7, -0.3),
# AM/AMSYNC (-4.5, 4.5), CW (-0.95, -0.55), FM (-5, 5) -- all in kHz there,
# Hz here (passband_edges() itself works in Hz; only _push_mode_to_page()
# converts to kHz at the page.evaluate() boundary).


def test_usb_low_edge_stays_fixed_width_added_above():
    assert passband_edges("USB", 2400) == (300, 2700)  # matches the site's own USB default exactly
    assert passband_edges("USB", 1200) == (300, 1500)  # narrower rig filter -> narrower filter, same low edge


def test_lsb_high_edge_stays_fixed_width_added_below():
    assert passband_edges("LSB", 2400) == (-2700, -300)  # matches the site's own LSB default exactly
    assert passband_edges("LSB", 1200) == (-1500, -300)


def test_am_symmetric_about_the_dial():
    assert passband_edges("AM", 9000) == (-4500, 4500)  # matches the site's own AM default exactly


def test_amsync_never_gets_a_custom_filter():
    """AMSYNC deliberately has no entry in _DEFAULT_EDGES_HZ -- bug-hunter
    finding: some real websdr.org builds (Hack Green, confirmed) have no
    "AMSYNC" case in their own set_mode() JS switch at all, so bypassing
    it via window.setmf() would silently ask an unsupported build to
    receive in a mode it never implements. Always None here so
    _push_mode_to_page() falls back to the switch-gated window.set_mode()
    call, the same no-op-if-unsupported behavior this had before any
    passband syncing existed."""
    assert passband_edges("AMSYNC", 6000) is None
    assert passband_edges("AMSYNC", None) is None


def test_fm_symmetric_about_the_dial():
    assert passband_edges("FM", 10000) == (-5000, 5000)  # matches the site's own FM default exactly


def test_cw_symmetric_about_750hz_below_the_dial_not_the_dial_itself():
    """The site's own CW preset (-0.95, -0.55 kHz) is centred at -750 Hz,
    not 0 -- a real, site-specific quirk (its built-in ~750 Hz CW sidetone
    pitch), not a bug. Confirmed from websdr-base.js's own set_mode()."""
    assert passband_edges("CW", 400) == (-950, -550)  # matches the site's own CW default exactly
    assert passband_edges("CW", 200) == (-850, -650)  # narrower, same -750 Hz centre


def test_cw_width_is_capped_below_the_sites_own_1000hz_iscw_threshold():
    """Bug-hunter finding, confirmed against the live site's own JS: its
    iscw() check is `hi-lo < 1.0` (kHz), not a mode check -- a rig CW
    filter at or above 1000 Hz would silently flip the site OUT of its
    own CW frequency-display/click-to-tune handling. A common real rig
    CW filter (1200 Hz) must not reach the site uncapped."""
    lo, hi = passband_edges("CW", 1200)
    assert hi - lo < 1000
    # Still centred on the site's own -750 Hz CW filter centre, just narrower.
    assert (lo + hi) / 2 == -750


def test_none_or_non_positive_width_says_nothing_about_the_filter():
    assert passband_edges("USB", None) is None
    assert passband_edges("USB", 0) is None
    assert passband_edges("USB", -100) is None


def test_unknown_mode_has_no_edges():
    assert passband_edges("DSTAR", 2400) is None

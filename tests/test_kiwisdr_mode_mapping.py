from sdrsync.websdr.kiwisdr import _base_mode_of, map_hamlib_mode_kiwi, passband_edges


def test_usb_maps_to_usb():
    assert map_hamlib_mode_kiwi("USB") == "usb"


def test_lsb_case_insensitive():
    assert map_hamlib_mode_kiwi("lsb") == "lsb"


def test_am_maps_to_am():
    assert map_hamlib_mode_kiwi("AM") == "am"


def test_cw_and_cwr_both_map_to_cw():
    assert map_hamlib_mode_kiwi("CW") == "cw"
    assert map_hamlib_mode_kiwi("CWR") == "cw"


def test_cw_u_and_cw_l_map_to_plain_cw():
    assert map_hamlib_mode_kiwi("CW-U") == "cw"
    assert map_hamlib_mode_kiwi("CW-L") == "cw"


def test_packet_modes_map_to_their_sideband():
    assert map_hamlib_mode_kiwi("PKTUSB") == "usb"
    assert map_hamlib_mode_kiwi("PKTLSB") == "lsb"


def test_data_modes_map_to_their_sideband():
    assert map_hamlib_mode_kiwi("DATA-U") == "usb"
    assert map_hamlib_mode_kiwi("DATA-L") == "lsb"


def test_fm_and_wfm_both_map_to_nbfm():
    assert map_hamlib_mode_kiwi("FM") == "nbfm"
    assert map_hamlib_mode_kiwi("WFM") == "nbfm"


def test_sam_maps_to_sam():
    assert map_hamlib_mode_kiwi("SAM") == "sam"


def test_unknown_mode_returns_none():
    assert map_hamlib_mode_kiwi("DSTAR") is None


def test_base_mode_of_strips_narrow_variants():
    # This is what get_status() relies on to display "USB"/"LSB" instead
    # of the raw KiwiSDR-internal "usn"/"lsn" narrow-filter mode strings
    # ext_get_mode() returns verbatim.
    assert _base_mode_of("usn") == "usb"
    assert _base_mode_of("lsn") == "lsb"
    assert _base_mode_of("cwn") == "cw"
    assert _base_mode_of("amn") == "am"
    assert _base_mode_of("nnfm") == "nbfm"


def test_base_mode_of_normalizes_wide_am_too():
    """"amw" (wide AM) must also normalize to "am" -- it has no separate
    hamlib mode, so leaving it unnormalized would silently break reverse
    sync for a receiver sitting in wide AM (map_kiwi_mode_to_hamlib has
    no "AMW" key, only "AM")."""
    assert _base_mode_of("amw") == "am"


def test_base_mode_of_leaves_base_modes_and_unknowns_unchanged():
    assert _base_mode_of("usb") == "usb"
    assert _base_mode_of("lsb") == "lsb"
    assert _base_mode_of("sam") == "sam"
    assert _base_mode_of("iq") == "iq"


# --- passband_edges() -----------------------------------------------------
# Reference points taken directly from the live site's own
# passbands_fallback table (see module docstring): usb (300, 2700), lsb
# (-2700, -300), am/sam (-4900, 4900), cw (300, 700), nbfm (-6000, 6000)
# -- all in Hz already (no kHz conversion needed for ext_tune(), unlike
# websdr_org.py's setmf()).


def test_usb_low_edge_stays_fixed_width_added_above():
    assert passband_edges("usb", 2400) == (300, 2700)  # matches the site's own USB default exactly
    assert passband_edges("usb", 1200) == (300, 1500)


def test_lsb_high_edge_stays_fixed_width_added_below():
    assert passband_edges("lsb", 2400) == (-2700, -300)  # matches the site's own LSB default exactly
    assert passband_edges("lsb", 1200) == (-1500, -300)


def test_am_and_sam_symmetric_about_the_dial():
    assert passband_edges("am", 9800) == (-4900, 4900)  # matches the site's own AM default exactly
    assert passband_edges("sam", 6000) == (-3000, 3000)


def test_nbfm_symmetric_about_the_dial():
    assert passband_edges("nbfm", 12000) == (-6000, 6000)  # matches the site's own NBFM default exactly


def test_cw_symmetric_about_500hz_above_the_dial_not_the_dial_itself():
    """The site's own CW default (300, 700 Hz) is centred at +500 Hz, not
    0 -- a real, site-specific quirk (this site's own ~500 Hz CW sidetone
    convention), confirmed from kiwisdr.min.js's own passbands_fallback
    table. Deliberately a DIFFERENT sign/value from websdr_org.py's own
    -750 Hz CW convention -- independently confirmed per site, not
    copy-pasted."""
    assert passband_edges("cw", 400) == (300, 700)  # matches the site's own CW default exactly
    assert passband_edges("cw", 200) == (400, 600)  # narrower, same +500 Hz centre


def test_none_or_non_positive_width_says_nothing_about_the_filter():
    assert passband_edges("usb", None) is None
    assert passband_edges("usb", 0) is None
    assert passband_edges("usb", -100) is None


def test_unknown_mode_has_no_edges():
    assert passband_edges("iq", 2400) is None

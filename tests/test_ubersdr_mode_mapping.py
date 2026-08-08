"""hamlib mode -> UberSDR mode id, and a rig passband -> UberSDR filter edges.

UberSDR differs from the other three families in taking the filter as two real
numbers rather than as a choice between narrow/wide mode strings, so the mapping
splits in two: which mode, and which edges. Both are pure and both are here.
"""
from sdrsync.websdr.ubersdr import _FALLBACK_MODES, map_hamlib_mode_ubersdr, passband_edges


def test_ssb_modes_map_straight_across():
    assert map_hamlib_mode_ubersdr("USB") == "usb"
    assert map_hamlib_mode_ubersdr("LSB") == "lsb"


def test_mode_names_are_case_insensitive():
    assert map_hamlib_mode_ubersdr("usb") == "usb"
    assert map_hamlib_mode_ubersdr("Lsb") == "lsb"


def test_data_modes_map_to_their_sideband():
    assert map_hamlib_mode_ubersdr("PKTUSB") == "usb"
    assert map_hamlib_mode_ubersdr("DATA-U") == "usb"
    assert map_hamlib_mode_ubersdr("PKTLSB") == "lsb"
    assert map_hamlib_mode_ubersdr("DATA-L") == "lsb"


def test_cw_keeps_its_sideband_rather_than_collapsing():
    # UberSDR has both, and the letter says which sideband the tone is on. Mapping
    # CWR onto cwu would put the tone on the wrong side of the carrier -- audible,
    # and the whole point of the distinction.
    assert map_hamlib_mode_ubersdr("CW") == "cwu"
    assert map_hamlib_mode_ubersdr("CWR") == "cwl"
    assert map_hamlib_mode_ubersdr("CW-U") == "cwu"
    assert map_hamlib_mode_ubersdr("CW-L") == "cwl"


def test_am_and_synchronous_am_are_different_modes():
    assert map_hamlib_mode_ubersdr("AM") == "am"
    assert map_hamlib_mode_ubersdr("AMS") == "sam"
    assert map_hamlib_mode_ubersdr("SAM") == "sam"


def test_fm_narrow_and_wide_are_different_modes():
    # UberSDR's nfm and fm differ in their default filter (±5 kHz vs ±8 kHz), so a
    # rig reporting wide FM should not land on the narrow one.
    assert map_hamlib_mode_ubersdr("FM") == "nfm"
    assert map_hamlib_mode_ubersdr("WFM") == "fm"


def test_unknown_mode_is_none_rather_than_a_guess():
    # The caller logs it and carries on syncing frequency -- the same contract the
    # other drivers have for a mode their site cannot do.
    assert map_hamlib_mode_ubersdr("DRM") is None
    assert map_hamlib_mode_ubersdr("") is None


# --- the filter -----------------------------------------------------------

def test_usb_width_is_added_above_the_modes_own_low_edge():
    # 50 Hz, not 0: the receiver's own default keeps the filter off the carrier,
    # and a rig's "2400 Hz" means 2400 Hz of speech, not 2400 Hz starting at DC.
    assert passband_edges("usb", 2400) == (50, 2450)


def test_lsb_is_the_mirror_of_usb():
    assert passband_edges("lsb", 2400) == (-2450, -50)


def test_symmetric_modes_are_split_either_side_of_the_dial():
    # Halved, not doubled: a 500 Hz CW filter is -250..+250. Passing the width to
    # both edges would make the receiver twice as wide as the rig.
    assert passband_edges("cwu", 500) == (-250, 250)
    assert passband_edges("cwl", 500) == (-250, 250)
    assert passband_edges("am", 6000) == (-3000, 3000)


def test_no_width_means_the_mode_decides():
    # The rig did not say, so nothing is said about the filter and the receiver
    # keeps its own default for the mode.
    assert passband_edges("usb", None) is None
    assert passband_edges("usb", 0) is None
    assert passband_edges("usb", -100) is None


def test_a_width_wider_than_the_receiver_allows_becomes_the_widest_it_has():
    # "As wide as I am" is the honest reading, and it is what the receiver would
    # do anyway -- except that sending the raw number would be *refused*, since
    # absolute values out of range are errors rather than clamps.
    low, high = passband_edges("fm", 40000)
    assert (low, high) == (_FALLBACK_MODES["fm"]["min"], _FALLBACK_MODES["fm"]["max"])


def test_a_width_too_small_to_be_a_filter_is_left_to_the_mode():
    # Clamping can collapse the two edges onto each other, and a filter of no
    # width is not a filter.
    assert passband_edges("usb", 1) is None


def test_an_unknown_mode_has_no_edges():
    assert passband_edges("drm", 2400) is None


def test_the_receivers_own_table_is_used_when_given():
    # Read from the page at attach, so a receiver whose limits differ from this
    # build's assumptions is still driven correctly rather than being sent values
    # it will refuse.
    live = {"usb": {"low": 100, "high": 2800, "min": 0, "max": 3000, "sideband": "upper"}}
    assert passband_edges("usb", 2400, live) == (100, 2500)
    # And its limits win over the built-in ones.
    assert passband_edges("usb", 5000, live) == (100, 3000)

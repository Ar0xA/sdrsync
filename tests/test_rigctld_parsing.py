from sdrsync.rig.rigctld import parse_freq_response, parse_mode_response, parse_ptt_response


def test_parse_freq_valid():
    assert parse_freq_response("14074000\n") == 14074000


def test_parse_freq_none_input():
    assert parse_freq_response(None) is None


def test_parse_freq_garbage():
    assert parse_freq_response("RPRT -1") is None


def test_parse_mode_valid():
    assert parse_mode_response("USB\n", "2400\n") == ("USB", 2400)


def test_parse_mode_empty_mode():
    assert parse_mode_response("", "2400") is None


def test_parse_mode_non_numeric_passband():
    assert parse_mode_response("USB", "wide") is None


def test_parse_ptt_rx():
    assert parse_ptt_response("0\n") is False


def test_parse_ptt_tx_variants():
    assert parse_ptt_response("1") is True
    assert parse_ptt_response("2") is True
    assert parse_ptt_response("3") is True


def test_parse_ptt_unknown():
    assert parse_ptt_response("9") is None


def test_parse_ptt_none_input():
    assert parse_ptt_response(None) is None

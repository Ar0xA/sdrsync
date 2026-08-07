from sdrsync.rig.flrig import (
    parse_bandwidth_response,
    parse_freq_response,
    parse_mode_response,
    parse_ptt_response,
)


def test_parse_freq_valid():
    assert parse_freq_response("14070000") == 14070000


def test_parse_freq_none_input():
    assert parse_freq_response(None) is None


def test_parse_freq_non_string_input():
    assert parse_freq_response(14070000) is None  # real flrig always sends a string


def test_parse_freq_garbage():
    assert parse_freq_response("not a number") is None


def test_parse_mode_valid():
    assert parse_mode_response("USB") == "USB"


def test_parse_mode_strips_whitespace():
    assert parse_mode_response(" USB \n") == "USB"


def test_parse_mode_empty_string():
    assert parse_mode_response("") is None


def test_parse_mode_non_string_input():
    assert parse_mode_response(None) is None


def test_parse_bandwidth_single_table_shape():
    assert parse_bandwidth_response(["2400", ""]) == 2400


def test_parse_bandwidth_dual_dsp_shape_uses_only_element_zero():
    assert parse_bandwidth_response(["1800", "2800"]) == 1800


def test_parse_bandwidth_non_numeric_element_falls_back_to_none():
    assert parse_bandwidth_response(["wide", ""]) is None


def test_parse_bandwidth_empty_list():
    assert parse_bandwidth_response([]) is None


def test_parse_bandwidth_non_list_input():
    assert parse_bandwidth_response("2400") is None
    assert parse_bandwidth_response(None) is None


def test_parse_ptt_rx():
    assert parse_ptt_response(0) is False


def test_parse_ptt_tx():
    assert parse_ptt_response(1) is True


def test_parse_ptt_other_nonzero_int_is_tx():
    assert parse_ptt_response(2) is True


def test_parse_ptt_non_int_input():
    assert parse_ptt_response("1") is None
    assert parse_ptt_response(None) is None


def test_parse_ptt_bool_input_is_rejected_not_silently_coerced():
    # XML-RPC booleans unmarshal as Python bool, which is an int subclass --
    # explicitly guarded against so True/False from a genuinely
    # bool-returning method can't silently pass as 1/0 here.
    assert parse_ptt_response(True) is None
    assert parse_ptt_response(False) is None

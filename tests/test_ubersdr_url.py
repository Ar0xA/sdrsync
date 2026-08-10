"""Which page the UberSDR driver navigates to.

Only UberSDR's v2 interface carries the control API this driver speaks -- the
older interface has nothing equivalent -- but the address an operator publishes,
and therefore the one they will paste, is the root. So the driver resolves one to
the other itself rather than making that the user's problem.
"""
from sdrsync.websdr.ubersdr import UberSDRDriver, v2_page_url


def test_a_root_url_becomes_the_v2_interface():
    assert v2_page_url("https://rx.example/") == "https://rx.example/v2/"
    assert v2_page_url("https://rx.example") == "https://rx.example/v2/"


def test_a_url_already_pointing_at_v2_is_left_alone():
    assert v2_page_url("https://rx.example/v2/") == "https://rx.example/v2/"


def test_a_v2_url_without_its_trailing_slash_gets_one():
    # Not cosmetic: relative script and asset paths on that page resolve against
    # the directory, and without the slash the browser resolves them one level up.
    assert v2_page_url("https://rx.example/v2") == "https://rx.example/v2/"


def test_a_port_and_a_path_prefix_both_survive():
    assert v2_page_url("http://rx.example:8073/") == "http://rx.example:8073/v2/"
    assert v2_page_url("https://host/receiver/") == "https://host/receiver/v2/"


def test_a_share_link_keeps_its_query():
    # UberSDR's own share links carry the frequency, mode and passband in the
    # query string, and pasting one should land where it says.
    assert (
        v2_page_url("https://rx.example/?freq=7100000&mode=lsb")
        == "https://rx.example/v2/?freq=7100000&mode=lsb"
    )


def test_a_deeper_v2_url_is_not_rewritten():
    # Already inside the v2 interface. Whatever it points at, it is not this
    # function's business to move it.
    assert v2_page_url("https://rx.example/v2/index.html") == "https://rx.example/v2/index.html"


def test_a_bare_host_gets_a_scheme():
    # Paste-friendliness: a hostname with no scheme is a URL somebody meant.
    assert v2_page_url("rx.example") == "http://rx.example/v2/"


def test_the_driver_resolves_it_at_construction():
    # So every log line, error message and navigation in the driver names the page
    # it is actually driving rather than the one it was handed.
    assert UberSDRDriver("https://rx.example/").url == "https://rx.example/v2/"

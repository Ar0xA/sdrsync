"""ptt_tag_state (sdrsync/gui/state.py) -- pure function, no wx dependency,
so importable without a display.

Regression coverage for: the PTT tag used to read "receive" as soon as the
rig connected, even with no WebSDR site connected at all (sdr_connected
was never consulted)."""
from sdrsync.gui.state import AppState, ptt_tag_state


def test_offline_when_rig_not_connected():
    assert ptt_tag_state(AppState(rig_connected=False)) == "offline"


def test_transmit_wins_over_everything():
    state = AppState(rig_connected=True, ptt=True, sdr_connected=False)
    assert ptt_tag_state(state) == "transmit"


def test_idle_when_rig_connected_but_no_websdr():
    # This is the bug: rig connected, no WebSDR site connected yet --
    # used to incorrectly report "receive".
    state = AppState(rig_connected=True, ptt=False, sdr_connected=False)
    assert ptt_tag_state(state) == "idle"


def test_idle_when_websdr_connected_but_audio_reported_inactive():
    state = AppState(rig_connected=True, ptt=False, sdr_connected=True, sdr_audio_active=False)
    assert ptt_tag_state(state) == "idle"


def test_receive_when_rig_and_websdr_connected_audio_unknown():
    # audio_active=None (driver didn't report) shouldn't block "receive" --
    # only an explicit False does.
    state = AppState(rig_connected=True, ptt=False, sdr_connected=True, sdr_audio_active=None)
    assert ptt_tag_state(state) == "receive"


def test_receive_when_rig_and_websdr_and_audio_all_active():
    state = AppState(rig_connected=True, ptt=False, sdr_connected=True, sdr_audio_active=True)
    assert ptt_tag_state(state) == "receive"

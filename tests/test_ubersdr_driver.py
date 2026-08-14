"""What the UberSDR driver actually sends, and what it does with the answer.

The page's control API answers every command with either the state it set or an
error carrying a *reason*, which is the whole advantage of it over calling a
site's internals and reading the result back. These tests pin the three places
that advantage is spent: PTT silence going to `duck` rather than `mute`, a
refusal being reported instead of assumed-successful, and a refused filter not
costing us the mode change.

They cover what the driver *asks for*, which is everything checkable without the
receiver's own code to hand. That the page *understands* it was established once,
by running the driver's agent against UberSDR's real bridge implementation in
node — see the protocol-audit notes in the driver's own comments.

The stub stands in for the browser page. It matches on the JS the driver
evaluates -- white-box, like the other drivers' tests, because the alternative is
a real browser.
"""
import asyncio
import json

from sdrsync.websdr import browser_shim
from sdrsync.websdr import ubersdr as ubersdr_module
from sdrsync.websdr.ubersdr import UberSDRDriver


class StubPage:
    """Answers the driver's evaluate() calls and records the commands it sent."""

    def __init__(self, replies=None, topics=None):
        # command name -> reply dict, as the page's `result` carries it.
        self.replies = replies or {}
        self.topics = topics or {}
        self.sent = []          # [(type, fields)] in order
        self.agent_present = True
        self.ready = True
        self.closed = False
        self.mouse = self
        self.click_calls = []   # [(x, y)] in order

    async def evaluate(self, js, *args):
        if ".call(" in js:
            fields = args[0]
            msg_type, payload = fields["type"], fields["fields"]
            self.sent.append((msg_type, payload))
            name = payload.get("name")
            reply = self.replies.get(name, {"ok": True, "value": {}})
            if isinstance(reply, list):
                reply = reply.pop(0) if reply else {"ok": True, "value": {}}
            return {"id": len(self.sent), "done": True, "res": reply}
        if "topics:" in js:                       # the get_status() read
            if not self.agent_present:
                return None
            return {"ready": self.ready, "closed": self.closed,
                    "refused": None, "topics": self.topics}
        if ".topics[" in js:
            return self.topics.get(js.split("'")[1])
        return True

    async def click(self, x, y):
        self.click_calls.append((x, y))

    def on(self, event, handler):
        return None


def attached(page, **kw):
    """A driver in the state attach() leaves it in, without a browser."""
    driver = UberSDRDriver("https://rx.example/", **kw)
    driver._page = page
    driver._attached = True
    return driver


def run(coro):
    return asyncio.run(coro)


# --- PTT silence ----------------------------------------------------------

def test_ptt_silence_uses_duck_and_never_touches_the_operators_mute():
    # The distinction the API draws, and the reason it exists: `mute` is the
    # operator's own setting, remembered by the browser view and shown on its mute
    # button. Using it for transmit would leave that mute behind to undo if sdrsync
    # died mid-transmission -- `duck` leaves nothing.
    page = StubPage()
    driver = attached(page)
    run(driver.set_muted(True))
    assert page.sent == [("command", {"name": "duck", "args": {"ducked": True}})]
    run(driver.set_muted(False))
    assert page.sent[-1] == ("command", {"name": "duck", "args": {"ducked": False}})


def test_a_receiver_without_duck_still_goes_quiet_for_transmit():
    # Feature-detected on the announce's capability list, as the API instructs. A
    # receiver that has no transient-silence command still has to be silent while
    # the rig is transmitting -- RX audio over your own transmission is the worse
    # failure, so the operator's own mute is used and said out loud once.
    page = StubPage()
    driver = attached(page)
    driver._capabilities = ("tune", "mode", "passband", "mute")
    driver._can_duck = False
    run(driver.set_muted(True))
    assert page.sent == [("command", {"name": "mute", "args": {"muted": True}})]


def test_duck_is_used_when_the_receiver_reports_it():
    page = StubPage()
    driver = attached(page)
    driver._capabilities = ("tune", "mute", "duck")
    driver._can_duck = True
    run(driver.set_muted(True))
    assert page.sent[-1][1]["name"] == "duck"


def test_silence_is_absolute_rather_than_a_toggle():
    # PTT arrives as "transmitting: true/false". A toggle desynchronises
    # permanently the first time a message is missed, and then unmutes on every
    # transmission.
    page = StubPage()
    driver = attached(page)
    run(driver.set_muted(True))
    run(driver.set_muted(True))
    assert all(payload["args"]["ducked"] is True for _t, payload in page.sent)


# --- tuning ---------------------------------------------------------------

def test_tune_sends_an_absolute_frequency_and_asks_for_the_view_to_follow():
    page = StubPage()
    driver = attached(page)
    assert run(driver.tune_hz(14074000)) is True
    assert page.sent == [("command", {
        "name": "tune", "args": {"frequency": 14074000, "ensureVisible": True},
    })]


def test_the_cw_offset_applies_only_in_cw():
    page = StubPage()
    driver = attached(page, cw_offset_hz=700)
    run(driver.tune_hz(7030000))
    assert page.sent[-1][1]["args"]["frequency"] == 7030000, "not CW yet"
    driver._current_mode = "cwu"
    run(driver.tune_hz(7030000))
    assert page.sent[-1][1]["args"]["frequency"] == 7030700


def test_a_refused_frequency_is_reported_as_not_applied():
    # The engine only latches "sent" when the driver says it applied -- otherwise
    # it would never retry once the receiver could take it.
    page = StubPage(replies={"tune": {
        "ok": False,
        "error": {"code": "bad_args", "message": "frequency 40000000 is outside 10000-30000000"},
    }})
    driver = attached(page)
    assert run(driver.tune_hz(40000000)) is False
    status = run(driver.get_status())
    assert "outside" in (status.last_error or ""), "the receiver's own reason is kept"


def test_nothing_is_sent_while_unattached():
    page = StubPage()
    driver = UberSDRDriver("https://rx.example/")
    driver._page = page
    assert run(driver.tune_hz(14074000)) is False
    assert run(driver.set_mode("USB", 2400)) is False
    run(driver.set_muted(True))
    assert page.sent == []


# --- mode and filter ------------------------------------------------------

def test_mode_and_filter_go_in_one_command():
    # Separately, the receiver passes audibly through the new mode's default width
    # on its way to the one asked for.
    page = StubPage()
    driver = attached(page)
    assert run(driver.set_mode("USB", 2400)) is True
    assert page.sent == [("command", {
        "name": "tune",
        "args": {"mode": "usb", "bandwidthLow": 50, "bandwidthHigh": 2450},
    })]


def test_a_mode_with_no_reported_filter_says_nothing_about_the_filter():
    page = StubPage()
    driver = attached(page)
    assert run(driver.set_mode("LSB", None)) is True
    assert page.sent[-1][1]["args"] == {"mode": "lsb"}


def test_an_unmapped_mode_is_skipped_without_sending_anything():
    page = StubPage()
    driver = attached(page)
    assert run(driver.set_mode("DRM", 6000)) is False
    assert page.sent == []
    assert "no UberSDR equivalent" in (run(driver.get_status()).last_error or "")


def test_a_refused_filter_does_not_cost_us_the_mode_change():
    # A receiver whose limits are tighter than we guessed refuses the passband. The
    # mode is the more important half of the request, so it is retried alone rather
    # than the whole thing being reported as failed.
    page = StubPage(replies={"tune": [
        {"ok": False, "error": {"code": "bad_args", "message": "passband too wide for usb"}},
        {"ok": True, "value": {"mode": "usb"}},
    ]})
    driver = attached(page)
    assert run(driver.set_mode("USB", 5900)) is True
    assert len(page.sent) == 2
    assert "bandwidthLow" in page.sent[0][1]["args"]
    assert page.sent[1][1]["args"] == {"mode": "usb"}, "the retry is the mode alone"
    assert run(driver.get_status()).last_error is None


def test_a_refused_mode_is_reported_as_not_applied():
    page = StubPage(replies={"tune": {
        "ok": False, "error": {"code": "bad_args", "message": 'no mode "usb"'},
    }})
    driver = attached(page)
    assert run(driver.set_mode("USB", None)) is False


# --- status ---------------------------------------------------------------

def test_status_comes_from_the_subscribed_topics():
    page = StubPage(topics={
        "tuning": {"frequency": 14074000, "mode": "usb"},
        "session": {"running": True},
        "audio": {"ducked": False},
    })
    driver = attached(page)
    status = run(driver.get_status())
    assert (status.connected, status.freq_hz, status.mode) == (True, 14074000, "USB")
    assert status.audio_active is True


def test_a_ducked_receiver_is_not_reported_as_playing():
    # It is silent, and saying otherwise is a lie the user can hear -- this is the
    # state sdrsync itself puts the receiver in while the rig transmits.
    page = StubPage(topics={
        "tuning": {"frequency": 14074000, "mode": "usb"},
        "session": {"running": True},
        "audio": {"ducked": True},
    })
    assert run(attached(page).get_status()).audio_active is False


def test_a_page_that_reloaded_drops_attachment_so_the_engine_re_attaches():
    # The agent goes with the page. Continuing to send would be driving a page
    # that is no longer listening, and every command would look like it worked.
    page = StubPage()
    page.agent_present = False
    driver = attached(page)
    status = run(driver.get_status())
    assert status.connected is False
    assert driver.attached is False
    assert "reloaded" in (status.last_error or "")


def test_the_page_closing_the_connection_also_drops_attachment():
    page = StubPage(topics={"tuning": {}})
    page.closed = True
    driver = attached(page)
    assert run(driver.get_status()).connected is False
    assert driver.attached is False


def test_status_while_unattached_carries_the_reason_rather_than_being_blank():
    driver = UberSDRDriver("https://rx.example/")
    driver._last_attach_error = "the browser bridge is switched off"
    status = run(driver.get_status())
    assert status.connected is False
    assert status.last_error == "the browser bridge is switched off"


# --- reverse sync (WebSDR -> rig, v11) -------------------------------------

def test_reverse_sync_maps_status_mode_back_to_hamlib():
    driver = attached(StubPage())
    status = run(driver.get_status())  # unattached-shaped is fine; only .mode matters
    for ubersdr_mode, hamlib_mode in [
        ("USB", "USB"), ("LSB", "LSB"), ("AM", "AM"), ("SAM", "SAM"),
        ("NFM", "FM"), ("FM", "WFM"), ("CWU", "CW"), ("CWL", "CWR"),
    ]:
        status.mode = ubersdr_mode
        assert driver.hamlib_mode_from_status(status) == hamlib_mode


def test_reverse_sync_reports_none_for_an_unmapped_mode():
    driver = attached(StubPage())
    status = run(driver.get_status())
    status.mode = "DRM"
    assert driver.hamlib_mode_from_status(status) is None


def test_reverse_sync_un_applies_the_cw_offset():
    # Symmetric to test_the_cw_offset_applies_only_in_cw() above: the same
    # offset that gets added going out to the receiver must come back off
    # going the other way, or every reverse-synced CW frequency would drift.
    driver = attached(StubPage(), cw_offset_hz=700)
    status = run(driver.get_status())
    status.freq_hz, status.mode = 7030700, "CWU"
    assert driver.rig_freq_from_status(status) == 7030000
    status.mode = "CWL"
    assert driver.rig_freq_from_status(status) == 7030000


def test_reverse_sync_leaves_non_cw_frequency_untouched():
    driver = attached(StubPage(), cw_offset_hz=700)
    status = run(driver.get_status())
    status.freq_hz, status.mode = 14074000, "USB"
    assert driver.rig_freq_from_status(status) == 14074000


def test_reverse_sync_reads_the_driver_own_offset_not_the_last_pushed_mode():
    # rig_freq_from_status() must key off the OBSERVED mode in this status
    # snapshot, not self._current_mode (the driver's own last-pushed mode) --
    # otherwise a receiver retuned by someone else on the page (exactly what
    # reverse sync exists to observe) would apply the wrong offset.
    driver = attached(StubPage(), cw_offset_hz=700)
    driver._current_mode = "usb"
    status = run(driver.get_status())
    status.freq_hz, status.mode = 7030700, "CWU"
    assert driver.rig_freq_from_status(status) == 7030000


def test_reverse_sync_frequency_is_none_when_status_has_none():
    driver = attached(StubPage())
    status = run(driver.get_status())
    status.freq_hz, status.mode = None, "USB"
    assert driver.rig_freq_from_status(status) is None


# --- goodbye --------------------------------------------------------------

def test_close_says_goodbye_so_the_client_slot_is_freed():
    # The page holds at most eight clients and evicts the stalest to make room. A
    # session that reconnects repeatedly without saying bye would push out
    # somebody's browser extension.
    page = StubPage()
    driver = attached(page)
    run(driver.close())
    assert driver.attached is False


def test_the_reported_json_is_what_the_page_would_receive():
    # The driver's fields are handed to the page as JSON. Anything unserialisable
    # would fail inside the browser shim rather than here, so it is checked here.
    page = StubPage()
    driver = attached(page)
    run(driver.tune_hz(14074000))
    run(driver.set_mode("CW", 500))
    for _type, payload in page.sent:
        json.dumps(payload)


# --- audio unlock opt-out ---------------------------------------------------

def test_stop_listen_cycle_skipped_when_disabled_via_settings():
    # Behaviour panel's "Mouse hijack to enable WebSDR audio" checkbox --
    # off means the operator will click the receiver's own Stop/Listen
    # controls themselves. Only gates the EXTRA Stop->Listen cycle, not
    # the initial Start-button click (see _start_audio's/__init__'s own
    # comments): session.running=True from the very start here so the
    # driver's own click loop never even reaches for .start__go, isolating
    # this test to just the Stop->Listen gate.
    class PageThatMustNotBeQueried(StubPage):
        async def evaluate(self, js, *args):
            if "querySelector" in js:
                raise AssertionError("Stop/Listen click must not run when disabled")
            return await super().evaluate(js, *args)

    page = PageThatMustNotBeQueried(topics={"session": {"running": True}})
    driver = attached(page, auto_click_audio_unlock=False)

    run(driver._start_audio())

    assert page.click_calls == []


# --- Stop->Listen recovery ---------------------------------------------------

class _StopSucceedsListenCoveredStubPage(StubPage):
    """Simulates: Stop is clicked successfully, but the topbar Listen
    control is never found/hit-testable afterward (e.g. the page fell
    back to its own full-viewport .start__go overlay, covering the
    topbar) -- and THAT overlay is itself clickable and recovers the
    session."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._stopped = False

    async def evaluate(self, js, *args):
        if "querySelector" in js:
            selector = args[0]
            if selector == '[title="Stop listening"]' and not self._stopped:
                return {"x": 10.4, "y": 20.6}
            if selector == '[title="Start listening"]':
                return None  # never found -- simulates being covered
            if selector == ".start__go" and self._stopped:
                return {"x": 30.4, "y": 40.6}
            return None
        return await super().evaluate(js, *args)

    async def click(self, x, y):
        await super().click(x, y)
        if (x, y) == (10, 21):
            self._stopped = True
            self.topics["session"] = {"running": False}
        elif (x, y) == (30, 41):
            self.topics["session"] = {"running": True}


def test_stop_listen_cycle_recovers_via_start_overlay_when_listen_not_found(monkeypatch):
    # Regression coverage (third-party bug-hunt review, reproduced with a
    # standalone script): previously, if the Listen click failed to find
    # a hit-testable target, _try_stop_listen_cycle returned False having
    # already clicked Stop -- leaving the receiver powered OFF with no
    # further attempt, which is worse than not touching it at all.
    monkeypatch.setattr(ubersdr_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.05)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)
    page = _StopSucceedsListenCoveredStubPage(topics={"session": {"running": True}})
    driver = attached(page)

    result = run(driver._try_stop_listen_cycle())

    assert result is True
    assert page.click_calls == [(10, 21), (30, 41)]


def test_stop_listen_cycle_reports_failure_when_recovery_also_fails(monkeypatch):
    # If even the .start__go recovery can't be found, the cycle must
    # still report failure honestly (not claim success) -- covered
    # separately from the happy-recovery case above so a regression in
    # either branch is caught independently.
    monkeypatch.setattr(ubersdr_module, "AUDIO_UNLOCK_WATCH_ATTEMPT_S", 0.05)
    monkeypatch.setattr(browser_shim, "CLICK_ELEMENT_POLL_INTERVAL_S", 0.01)

    class _NoRecoveryStubPage(_StopSucceedsListenCoveredStubPage):
        async def evaluate(self, js, *args):
            if "querySelector" in js and args[0] == ".start__go":
                return None
            return await super().evaluate(js, *args)

    page = _NoRecoveryStubPage(topics={"session": {"running": True}})
    driver = attached(page)

    result = run(driver._try_stop_listen_cycle())

    assert result is False

"""The rig and WebSDR subsystems have independent lifecycles: picking a
different WebSDR must never touch the rigctld connection. The reverse is
NOT symmetric (deliberately, since v9): stopping the rig also stops an
active WebSDR session, since WebSDR has nothing driving its sync without
a rig (it can't even be started without one -- see gui/app.py's Connect
gating). Uses stub objects instead of a real browser/socket, same pattern
as test_engine_mode_independence.py.

StubWebViewHost stands in for gui/webview_host.py's WebViewHost --
satisfies SyncEngine's WebViewHost Protocol (create_page/destroy_page)
without touching wx at all, so these tests stay fast and headless.
"""
import asyncio
import queue
import time

import sdrsync.sync.engine as engine_module
from sdrsync.config import AppSettings, WebSDRSite
from sdrsync.rig.rigctld import RigState
from sdrsync.sync.engine import SyncEngine
from sdrsync.websdr.base import WebSDRStatus


class StubDriver:
    def __init__(self, url: str, cw_offset_hz: int = 0) -> None:
        self.url = url
        self.attached = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        return True

    async def set_muted(self, muted: bool) -> None:
        pass

    async def get_status(self) -> WebSDRStatus:
        return WebSDRStatus(connected=True)

    def hamlib_mode_from_status(self, status: WebSDRStatus):
        return None

    def rig_freq_from_status(self, status: WebSDRStatus):
        return None


class StubRig:
    """Just enough of RigctldClient's interface to prove close() was (or
    wasn't) called."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class StubPage:
    """Sentinel page -- nothing in these tests touches its content, only
    identity (via StubWebViewHost's created/destroyed lists)."""

    def __init__(self) -> None:
        self.on_dead = None

    def set_on_dead(self, on_dead) -> None:
        self.on_dead = on_dead


class StubWebViewHost:
    """Satisfies SyncEngine's WebViewHost Protocol. Tracks created/
    destroyed pages so tests can assert on WebView lifecycle without a
    real wx.App/WebView existing."""

    def __init__(self) -> None:
        self.created: list[StubPage] = []
        self.destroyed: list[StubPage] = []

    async def create_page(self, loop, on_dead=None):
        page = StubPage()
        self.created.append(page)
        return page

    async def destroy_page(self, page, loop=None) -> None:
        self.destroyed.append(page)


async def _noop_attach_supervisor(page) -> None:
    """Stands in for the real attach-retry loop -- returns immediately so
    the test doesn't need a real attach cycle."""
    return


def make_engine() -> SyncEngine:
    settings = AppSettings()
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=StubWebViewHost())
    engine._attach_supervisor = _noop_attach_supervisor
    return engine


def test_start_websdr_does_not_touch_inactive_rig(monkeypatch):
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    asyncio.run(engine._start_websdr(site))

    assert engine._websdr_active is True
    assert engine._rig_active is False
    assert engine._rig is None


def test_switching_site_swaps_driver_and_resets_latches_without_touching_rig(monkeypatch):
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    monkeypatch.setitem(engine_module.DRIVERS, "kiwisdr", StubDriver)

    engine = make_engine()
    site_a = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    site_b = WebSDRSite(name="B", url="http://b.invalid/", driver_type="kiwisdr")

    async def run():
        await engine._start_websdr(site_a)
        original_driver = engine._driver
        assert engine.site is site_a
        original_rig = engine._rig  # None -- rig was never started

        engine._last_sent_freq = 14074000
        engine._last_sent_mode_key = ("USB", 2700)
        engine._last_ptt = False

        await engine._start_websdr(site_b)  # the "switch" -- same call as connect

        assert original_driver.closed is True
        assert engine.site is site_b
        assert engine._driver is not original_driver
        assert engine._driver.url == "http://b.invalid/"
        assert engine._last_sent_freq is None
        assert engine._last_sent_mode_key is None
        assert engine._last_ptt is None
        assert engine._rig is original_rig  # still untouched
        # Switching sites must tear down the first WebView, not leak it.
        assert len(engine._webview_host.created) == 2
        assert len(engine._webview_host.destroyed) == 1

    asyncio.run(run())


def test_switch_websdr_reuses_the_same_page_without_destroying_it(monkeypatch):
    # _switch_websdr() is the actual "Switch" path now -- unlike
    # _start_websdr()'s own "if active, stop first" behavior exercised by
    # test_switching_site_swaps_driver_and_resets_latches_without_touching_rig
    # above, this must NOT create a second WebView or destroy the first:
    # that destroy-and-recreate cycle was the root of a string of Win32
    # z-order/visibility bugs against the always-visible WebSDR window.
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    monkeypatch.setitem(engine_module.DRIVERS, "kiwisdr", StubDriver)

    engine = make_engine()
    site_a = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    site_b = WebSDRSite(name="B", url="http://b.invalid/", driver_type="kiwisdr")

    async def run():
        await engine._start_websdr(site_a)
        original_driver = engine._driver
        original_page = engine._page

        engine._last_sent_freq = 14074000
        engine._last_sent_mode_key = ("USB", 2700)
        engine._last_ptt = False

        await engine._switch_websdr(site_b)

        assert original_driver.closed is True
        assert engine.site is site_b
        assert engine._driver is not original_driver
        assert engine._driver.url == "http://b.invalid/"
        assert engine._last_sent_freq is None
        assert engine._last_sent_mode_key is None
        assert engine._last_ptt is None
        assert engine._websdr_active is True
        # The whole point: same page, nothing created or destroyed.
        assert engine._page is original_page
        assert len(engine._webview_host.created) == 1
        assert len(engine._webview_host.destroyed) == 0

    asyncio.run(run())


def test_switch_websdr_falls_back_to_a_full_start_with_no_existing_page(monkeypatch):
    # The GUI only ever calls this while already active, but a defensive
    # fallback matters more than an assertion here would.
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    asyncio.run(engine._switch_websdr(site))

    assert engine._websdr_active is True
    assert engine.site is site
    assert len(engine._webview_host.created) == 1


def test_switch_websdr_rebinds_on_dead_so_a_post_switch_crash_still_recovers(monkeypatch):
    # Regression guard for the exact bug set_on_dead() exists to prevent:
    # the dead-callback closure captures a generation number by value, and
    # _switch_websdr() bumps that generation without going through
    # create_page() (which is what normally re-wires on_dead for a new
    # generation). Without the explicit rebind, a WebView crash AFTER a
    # switch would report the pre-switch generation, _handle_page_dead()
    # would see it doesn't match the current one, and silently ignore a
    # real crash instead of recovering from it.
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    monkeypatch.setitem(engine_module.DRIVERS, "kiwisdr", StubDriver)
    engine = make_engine()
    site_a = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    site_b = WebSDRSite(name="B", url="http://b.invalid/", driver_type="kiwisdr")

    recreated = []
    real_start = engine._start_websdr

    async def spy_start(site, user_initiated=True):
        recreated.append(site)
        await real_start(site, user_initiated=user_initiated)

    engine._start_websdr = spy_start

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site_a)
        recreated.clear()  # only the post-switch recovery below matters
        page = engine._page
        await engine._switch_websdr(site_b)
        assert page.on_dead is not None
        # Simulate the page dying after the switch -- same call shape
        # WxPageAdapter uses (a single reason string). Deliberately calling
        # the REBOUND callback itself (not engine._on_page_dead() with an
        # explicit generation, as the other page-death tests do) -- the
        # whole point is proving the closure captured the post-switch
        # generation on its own.
        page.on_dead("script timeout")
        # _on_page_dead schedules _handle_page_dead via call_soon_threadsafe,
        # which itself schedules _start_websdr() as a new task -- two
        # sleeps to let both run, same as test_page_death_recreates_the_
        # websdr_session above.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert recreated == [site_b], "a post-switch crash must recover the CURRENT site, not be ignored as stale"

    asyncio.run(run())


def test_stop_websdr_does_not_touch_rig(monkeypatch):
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    engine._rig_active = True  # pretend the rig subsystem is up
    rig_stub = StubRig()
    engine._rig = rig_stub

    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        await engine._start_websdr(site)
        assert engine._websdr_active is True
        await engine._stop_websdr()
        assert engine._websdr_active is False
        assert engine.site is None
        assert len(engine._webview_host.destroyed) == 1
        # The whole point: stopping WebSDR never touches the rig.
        assert engine._rig is rig_stub
        assert rig_stub.closed is False
        assert engine._rig_active is True

    asyncio.run(run())


def test_stop_rig_also_stops_an_active_websdr_session(monkeypatch):
    """v9: deliberate one-directional exception to the independent-
    lifecycles principle -- see the module docstring above."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        await engine._start_websdr(site)
        driver = engine._driver
        engine._rig_active = True
        rig_stub = StubRig()
        engine._rig = rig_stub

        await engine._stop_rig()

        assert engine._rig_active is False
        assert engine._rig is None
        assert rig_stub.closed is True
        assert engine._websdr_active is False
        assert engine._driver is None
        assert driver.closed is True

    asyncio.run(run())


def test_stop_rig_leaves_an_inactive_websdr_session_alone(monkeypatch):
    """The cascade only fires when WebSDR is actually active -- confirms
    _stop_rig() doesn't unconditionally call _stop_websdr() in a way that
    would raise/misbehave when there's nothing to stop."""
    engine = make_engine()

    async def run():
        engine._rig_active = True
        rig_stub = StubRig()
        engine._rig = rig_stub

        await engine._stop_rig()  # must not raise

        assert engine._rig_active is False
        assert engine._websdr_active is False

    asyncio.run(run())


def test_thread_safe_entry_points_are_noop_before_run():
    """_loop is only set once run() starts; calling any thread-safe entry
    point before that (or after a full stop) must not raise, and must not
    schedule work that's never awaited."""
    settings = AppSettings()
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=StubWebViewHost())
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    engine.start_websdr_from_other_thread(site)  # must not raise
    engine.stop_websdr_from_other_thread()
    engine.start_rig_from_other_thread("rigctld", "127.0.0.1", 4532, True)
    engine.stop_rig_from_other_thread()


def test_page_death_recreates_the_websdr_session(monkeypatch):
    """A WebView dying (script timeout with no way to cancel it -- see
    WxPageAdapter's on_dead) must trigger a fresh _start_websdr(), the
    same recovery path an explicit reconnect takes -- this was a real gap
    the Block B implementation review found (nothing previously called
    the callback at all)."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        assert len(engine._webview_host.created) == 1
        generation = engine._websdr_generation

        engine._on_page_dead(generation, "test-induced death")
        # _on_page_dead schedules _handle_page_dead via call_soon_threadsafe
        # -- let the loop process it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(engine._webview_host.created) == 2
        assert engine._websdr_active is True

    asyncio.run(run())


def test_stale_page_death_notification_is_ignored(monkeypatch):
    """A dead-notification tagged with an old generation (from a page
    that's already been replaced/torn down) must not trigger a spurious
    recreation of whatever's active now."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        stale_generation = engine._websdr_generation
        await engine._stop_websdr()

        engine._on_page_dead(stale_generation, "stale")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert engine._websdr_active is False
        assert len(engine._webview_host.created) == 1  # no spurious recreation

    asyncio.run(run())


class GatedWebViewHost(StubWebViewHost):
    """Like StubWebViewHost, but create_page() can be made to block until
    the test releases it -- lets a test suspend a coroutine PRECISELY
    inside the create_page() await, the exact race window
    _start_websdr()'s post-create_page() generation re-check guards
    against (a Disconnect racing a page-death recovery)."""

    def __init__(self) -> None:
        super().__init__()
        self._gate: "asyncio.Event | None" = None

    def block_next_create(self) -> None:
        self._gate = asyncio.Event()

    def release(self) -> None:
        if self._gate is not None:
            self._gate.set()

    async def create_page(self, loop, on_dead=None):
        if self._gate is not None:
            await self._gate.wait()
            self._gate = None
        return await super().create_page(loop, on_dead=on_dead)


def test_page_death_recovery_racing_a_disconnect_does_not_resurrect_the_session(monkeypatch):
    """_handle_page_dead()'s own generation check only guards its
    SCHEDULING -- the _start_websdr() coroutine it fires off has its own
    await (create_page()) that a real Disconnect can complete underneath.
    Without _start_websdr() re-checking the generation itself after that
    await, this silently undoes the Disconnect: engine state ends up
    websdr_active=True, pointed at a page the user already tore down."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    settings = AppSettings()
    host = GatedWebViewHost()
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=host)
    engine._attach_supervisor = _noop_attach_supervisor
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)  # first create_page() is ungated -- goes through immediately
        assert engine._websdr_active is True
        generation = engine._websdr_generation

        host.block_next_create()
        engine._on_page_dead(generation, "test-induced death")
        await asyncio.sleep(0)  # on_dead's call_soon_threadsafe -> _handle_page_dead runs
        await asyncio.sleep(0)  # the new _start_websdr(user_initiated=False) task starts,
        #                         reaches create_page(), suspends on the gate

        # A real Disconnect races in and completes FIRST, while the
        # recovery attempt above is still suspended.
        await engine._stop_websdr()
        assert engine._websdr_active is False
        assert engine.site is None

        # Now let the stale recovery attempt finish.
        host.release()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The Disconnect must not have been silently undone.
        assert engine._websdr_active is False
        assert engine.site is None
        assert engine._page is None
        assert engine._driver is None
        # The orphaned page the stale recovery created must have been
        # destroyed, not leaked -- 1 for the real Disconnect's page, 1 for
        # the stale recovery's own (immediately-superseded) page.
        assert len(host.created) == 2
        assert len(host.destroyed) == 2

    asyncio.run(run())


def test_rig_disconnect_during_an_in_flight_websdr_connect_is_not_undone(monkeypatch):
    """_start_websdr()'s post-await generation re-check only catches a
    concurrent _stop_rig() if something actually bumps the generation
    while _websdr_active is still False (i.e. before the in-flight start
    has committed) -- _stop_rig()'s own cascade only fires once
    _websdr_active is True, which it isn't yet during create_page(). Must
    bump the generation unconditionally, or a rig Disconnect landing in
    that window is silently undone once the in-flight start resumes."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    settings = AppSettings()
    host = GatedWebViewHost()
    engine = SyncEngine(settings, status_queue=queue.Queue(), webview_host=host)
    engine._attach_supervisor = _noop_attach_supervisor
    engine._rig_active = True
    rig_stub = StubRig()
    engine._rig = rig_stub
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        host.block_next_create()
        start_task = asyncio.ensure_future(engine._start_websdr(site))
        await asyncio.sleep(0)  # let it reach and suspend inside create_page()

        # A real rig Disconnect races in and completes first.
        await engine._stop_rig()
        assert engine._rig_active is False
        assert engine._rig is None

        host.release()
        await start_task

        # The Disconnect must not have been silently undone.
        assert engine._websdr_active is False
        assert engine.site is None
        assert engine._page is None
        assert engine._driver is None
        assert len(host.created) == 1
        assert len(host.destroyed) == 1  # the orphaned page must not leak

    asyncio.run(run())


class GatedDriver(StubDriver):
    """StubDriver whose close() can fire a one-shot callback -- lets a
    test simulate a concurrent second _switch_websdr() completing and
    installing its own state PRECISELY while this call is suspended
    inside its own 'await old_driver.close()' step, the exact race window
    the post-await generation re-check guards against."""

    def __init__(self, url: str, cw_offset_hz: int = 0) -> None:
        super().__init__(url, cw_offset_hz)
        self.on_close = None

    async def close(self) -> None:
        if self.on_close is not None:
            cb, self.on_close = self.on_close, None
            cb()
        await super().close()


def test_switch_websdr_racing_a_second_switch_does_not_clobber_the_winner(monkeypatch):
    """Two overlapping _switch_websdr() calls (nothing disables the Sites
    panel's Load button mid-switch, so a double-click or Load-then-Load
    on two rows reaches this) must not both install their own attach_task
    -- the loser overwriting (not cancelling) the winner's attach_task
    would leave two supervisors driving the same page forever."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", GatedDriver)
    monkeypatch.setitem(engine_module.DRIVERS, "kiwisdr", StubDriver)
    engine = make_engine()
    site_a = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    site_b = WebSDRSite(name="B", url="http://b.invalid/", driver_type="kiwisdr")
    winner_site = WebSDRSite(name="Winner", url="http://winner.invalid/", driver_type="kiwisdr")

    async def run():
        await engine._start_websdr(site_a)
        original_driver = engine._driver  # a GatedDriver

        winner_driver = StubDriver("http://winner.invalid/")
        winner_task = asyncio.ensure_future(_noop_attach_supervisor(None))

        def simulate_concurrent_winner():
            # What a second, faster _switch_websdr() call would have
            # installed while this call was suspended closing the old
            # driver -- mutating engine state directly (rather than
            # racing a second real _switch_websdr() call, which would
            # itself contend on the SAME old_driver.close() this callback
            # fires from) keeps the test deterministic.
            engine._websdr_generation += 1
            engine.site = winner_site
            engine._driver = winner_driver
            engine._attach_task = winner_task

        original_driver.on_close = simulate_concurrent_winner

        await engine._switch_websdr(site_b)  # the stale, slower switch

        # Must not have overwritten the "winner"'s state.
        assert engine.site is winner_site
        assert engine._driver is winner_driver
        assert engine._attach_task is winner_task

        winner_task.cancel()
        try:
            await winner_task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


# ----------------------------------------------------------------------
# v14 attach-supervisor redesign (Item 5) and idle disconnect (Item 7).
# ----------------------------------------------------------------------


class ScriptedAttachDriver:
    """Driver whose attach() outcome the test controls, and whose
    `attached` flag the test can flip between supervisor iterations to
    simulate a real drop."""

    def __init__(self, url: str = "http://a.invalid/", cw_offset_hz: int = 0) -> None:
        self.url = url
        self.attached = False
        self.attach_calls = 0
        self.attach_raises: Exception | None = None

    async def attach(self, page) -> None:
        self.attach_calls += 1
        if self.attach_raises is not None:
            raise self.attach_raises
        self.attached = True

    async def close(self) -> None:
        pass

    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        return True

    async def set_mode(self, hamlib_mode: str, passband_hz) -> bool:
        return True

    async def set_muted(self, muted: bool) -> None:
        pass

    async def get_status(self) -> WebSDRStatus:
        return WebSDRStatus(connected=self.attached)


def _run_supervisor(engine, driver, iterations, on_sleep=None):
    """Drives _attach_supervisor() for a bounded number of loop
    iterations with no real sleeping: _sleep_or_stop is replaced by a
    recorder that also ends the loop once `iterations` sleeps have
    happened. on_sleep(n) runs after each sleep, letting a test change
    the world between iterations (e.g. drop the attachment)."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if on_sleep is not None:
            on_sleep(len(delays))
        if len(delays) >= iterations:
            engine._websdr_active = False

    engine._sleep_or_stop = fake_sleep
    engine._driver = driver
    engine._websdr_active = True
    # The real method off the class, not engine._attach_supervisor --
    # make_engine() replaces that instance attribute with a no-op stub
    # for the lifecycle tests above, and these tests are specifically
    # about the supervisor itself.
    asyncio.run(SyncEngine._attach_supervisor(engine, StubPage()))
    return delays


def test_flapping_attachment_keeps_growing_the_failure_counter():
    """v14: an attach that succeeds and drops again before ATTACH_STABLE_S
    is a flap -- which is exactly what a server-side fair-use kick looks
    like from here. The old code reset the counter the instant attach()
    returned, so the backoff never grew and the client hammered the site
    forever at the base delay."""
    engine = make_engine()
    driver = ScriptedAttachDriver()

    def drop_it(_n):
        driver.attached = False  # the far end kicks us right back off

    _run_supervisor(engine, driver, iterations=4, on_sleep=drop_it)

    assert driver.attach_calls == 2
    assert engine._attach_failures == 2  # grew across the flap, not reset by it


def test_attachment_that_stays_up_past_the_stable_window_resets_the_ladder():
    engine = make_engine()
    driver = ScriptedAttachDriver()
    engine._attach_failures = 4  # a rough patch before this attach
    failures_after_attach = []

    def age_the_attachment(n):
        if n == 1:
            # The attach in iteration 1 succeeded; pretend it has now been
            # up for longer than ATTACH_STABLE_S.
            failures_after_attach.append(engine._attach_failures)
            engine._attach_attached_at -= engine_module.ATTACH_STABLE_S + 1.0

    _run_supervisor(engine, driver, iterations=2, on_sleep=age_the_attachment)

    # A bare successful attach does NOT forgive the ladder...
    assert failures_after_attach == [4]
    # ...but staying up past the stable window does.
    assert engine._attach_failures == 0
    assert engine._attach_attached_at is None


def test_attach_retry_delay_is_jittered_within_bounds():
    engine = make_engine()
    engine._attach_failures = 1
    delays = [engine._attach_retry_delay() for _ in range(50)]

    low, high = engine_module.ATTACH_RETRY_JITTER
    base = engine_module.ATTACH_RETRY_BASE_DELAY_S
    assert all(base * low <= d <= base * high for d in delays)
    assert len(set(delays)) > 1  # actually jittered, not a constant


def test_attach_retry_delay_ladder_doubles_up_to_the_extended_ceiling():
    engine = make_engine()
    low, high = engine_module.ATTACH_RETRY_JITTER
    for failures, undelayed in ((1, 2.0), (2, 4.0), (5, 32.0), (20, engine_module.ATTACH_RETRY_MAX_DELAY_S)):
        engine._attach_failures = failures
        delay = engine._attach_retry_delay()
        assert undelayed * low <= delay <= undelayed * high
    assert engine_module.ATTACH_RETRY_MAX_DELAY_S == 300.0  # 5 minutes, not 30 seconds


def test_deep_ladder_skips_the_expensive_attach_when_the_cheap_check_fails(monkeypatch):
    """Once the ladder is deep, one cheap HTTP GET stands in for a full
    page load + JS wait + WebSocket against a site that is simply down."""
    engine = make_engine()
    engine.site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    engine._attach_failures = engine_module.ATTACH_PREFLIGHT_GATE_FAILURES
    driver = ScriptedAttachDriver()

    checked = []

    async def fake_check(url, timeout=None):
        checked.append(url)
        return False, f"Could not reach WebSDR site {url}"

    monkeypatch.setattr(engine_module, "check_websdr_url", fake_check)
    _run_supervisor(engine, driver, iterations=1)

    assert checked == ["http://a.invalid/"]
    assert driver.attach_calls == 0  # the expensive attempt was skipped entirely
    # A failed reachability check IS a failed attempt at the far end, so
    # the ladder keeps growing rather than freezing at this rung.
    assert engine._attach_failures == engine_module.ATTACH_PREFLIGHT_GATE_FAILURES + 1


def test_shallow_ladder_does_not_run_the_cheap_check(monkeypatch):
    """The gate is for deep retries only -- a first/second attempt must
    not pay for an extra HTTP round trip."""
    engine = make_engine()
    engine.site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    engine._attach_failures = engine_module.ATTACH_PREFLIGHT_GATE_FAILURES - 1
    driver = ScriptedAttachDriver()

    checked = []

    async def fake_check(url, timeout=None):
        checked.append(url)
        return False, "down"

    monkeypatch.setattr(engine_module, "check_websdr_url", fake_check)
    _run_supervisor(engine, driver, iterations=1)

    assert checked == []
    assert driver.attach_calls == 1


def test_still_retrying_status_appears_once_the_ladder_is_deep():
    engine = make_engine()
    engine._attach_failures = engine_module.ATTACH_STILL_TRYING_FAILURES - 2
    engine._register_attach_failure()
    assert engine._attach_status_message is None  # not yet worth surfacing

    engine._register_attach_failure()
    message = engine._attach_status_message
    assert message is not None
    assert "still retrying" in message  # never reads like a crash or a give-up


def test_page_death_does_not_reset_the_attach_failure_ladder(monkeypatch):
    """The laundering path this closes: _handle_page_dead ->
    _start_websdr() previously started a brand-new supervisor task whose
    failure count began at zero, so a site failing every few seconds was
    retried forever at the base delay."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        engine._attach_failures = 4
        generation = engine._websdr_generation

        engine._on_page_dead(generation, "test-induced death")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(engine._webview_host.created) == 2  # still recovers...
        assert engine._attach_failures == 4  # ...without forgiving the ladder

    asyncio.run(run())


def test_user_initiated_start_does_reset_the_attach_failure_ladder(monkeypatch):
    """Contrast to the test above: a real Connect/Switch click is a fresh
    human decision and gets a clean ladder."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        engine._attach_failures = 4
        engine._attach_status_message = "WebSDR site not responding..."
        await engine._start_websdr(site)  # defaults to user_initiated=True
        assert engine._attach_failures == 0
        assert engine._attach_status_message is None

    asyncio.run(run())


# ---------------------------------------------------------------- idle disconnect


class IdleStubRig:
    """Enough of RigClient for _tick(): state is mutated by the test to
    simulate the operator touching (or not touching) the radio.
    `reachable` simulates the CAT link itself dropping (USB unplugged,
    rigctld killed, PC slept)."""

    def __init__(self, state: RigState) -> None:
        self.state = state
        self.reachable = True

    async def ensure_connected(self) -> bool:
        return self.reachable

    async def get_state(self) -> RigState:
        return self.state

    async def close(self) -> None:
        pass


def _make_idle_engine(monkeypatch, idle_minutes):
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    engine.settings.websdr_idle_disconnect_min = idle_minutes
    engine._rig = IdleStubRig(RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False))
    engine._rig_active = True
    return engine


def test_rig_activity_updates_the_idle_timer(monkeypatch):
    engine = _make_idle_engine(monkeypatch, idle_minutes=60)

    asyncio.run(engine._tick())  # seeds the trackers
    engine._last_rig_activity_at -= 100.0
    stale = engine._last_rig_activity_at

    asyncio.run(engine._tick())  # nothing changed -- idle time keeps accruing
    assert engine._last_rig_activity_at == stale

    engine._rig.state = RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False)
    asyncio.run(engine._tick())
    assert engine._last_rig_activity_at > stale


def test_ptt_alone_counts_as_rig_activity(monkeypatch):
    """Someone calling CQ on one frequency for an hour is not idle."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=60)
    asyncio.run(engine._tick())
    engine._last_rig_activity_at -= 100.0
    stale = engine._last_rig_activity_at

    engine._rig.state = RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=True)
    asyncio.run(engine._tick())
    assert engine._last_rig_activity_at > stale


def test_idle_threshold_disconnects_the_websdr_and_arms_auto_resume(monkeypatch):
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()  # seeds the activity trackers
        assert engine._websdr_active is True

        engine._last_rig_activity_at -= 61.0  # one minute of nothing
        await engine._tick()

        assert engine._websdr_active is False
        assert engine._websdr_idle_stopped is True
        assert engine._idle_stopped_site == site  # captured before self.site was cleared
        assert len(engine._webview_host.destroyed) == 1

    asyncio.run(run())


def test_rig_activity_after_an_idle_stop_reconnects_automatically(monkeypatch):
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()
        assert engine._websdr_idle_stopped is True

        # The operator touches the VFO.
        engine._rig.state = RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()

        assert engine._websdr_active is True
        assert engine._websdr_idle_stopped is False
        assert engine._idle_stopped_site is None
        assert engine.site == site
        assert len(engine._webview_host.created) == 2

    asyncio.run(run())


def test_user_disconnect_after_an_idle_stop_does_not_auto_resume(monkeypatch):
    """A manual Disconnect must never leave the auto-resume machinery
    armed -- the user asked for it off, and silently reconnecting to
    someone else's receiver moments later is exactly the behavior this
    whole round exists to prevent."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()
        assert engine._websdr_idle_stopped is True

        await engine._stop_websdr()  # what stop_websdr_from_other_thread runs
        assert engine._websdr_idle_stopped is False
        assert engine._idle_stopped_site is None

        engine._rig.state = RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()

        assert engine._websdr_active is False
        assert len(engine._webview_host.created) == 1  # no silent reconnection

    asyncio.run(run())


def test_idle_disconnect_disabled_never_fires(monkeypatch):
    for disabled_value in (None, 0):
        engine = _make_idle_engine(monkeypatch, idle_minutes=disabled_value)
        site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

        async def run():
            engine._loop = asyncio.get_running_loop()
            await engine._start_websdr(site)
            await engine._tick()
            engine._last_rig_activity_at -= 60 * 60 * 24  # a whole day of nothing
            await engine._tick()
            assert engine._websdr_active is True
            assert engine._websdr_idle_stopped is False

        asyncio.run(run())


def test_idle_stop_is_published_as_a_state_not_an_error(monkeypatch):
    """The GUI distinguishes 'disconnected (idle)' from a failure purely
    by this flag -- an idle release must never render through the red
    error label."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()

    asyncio.run(run())

    snapshots = []
    while not engine.status_queue.empty():
        snapshots.append(engine.status_queue.get_nowait())
    last = snapshots[-1]
    assert last.websdr_idle_stopped is True
    assert last.websdr_active is False
    assert last.websdr is None or not last.websdr.last_error


# ------------------------------------------- idle disconnect: correctness fixes


def test_manual_connect_after_a_long_idle_period_is_not_undone_one_tick_later(monkeypatch):
    """The idle clock is keyed on RIG activity, which keeps running while
    no WebSDR session exists. Without stamping it on a user-initiated
    start, _idle_disconnect_due() is already True the instant a manual
    Connect comes up and the very next tick tears it straight back down
    (~200ms at the default poll interval) -- i.e. the user cannot connect
    by hand at all. Also covers the first-ever Connect on an app left
    open past the threshold with the rig parked on one frequency."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        # The rig is already connected and being polled -- deliberately
        # established BEFORE the idle window, so the not-connected ->
        # connected activity edge is long past and cannot mask this.
        await engine._tick()
        # An hour of the rig sitting untouched before the user ever clicks
        # Connect.
        engine._last_rig_activity_at -= 3600.0

        await engine._start_websdr(site)  # the Connect click itself
        assert engine._websdr_active is True

        await engine._tick()
        assert engine._websdr_active is True  # still up, not idle-stopped
        assert engine._websdr_idle_stopped is False

        await engine._tick()
        assert engine._websdr_active is True

    asyncio.run(run())


def test_rig_disconnect_while_idle_stopped_disarms_auto_resume(monkeypatch):
    """_stop_websdr()'s docstring promises every non-idle stop path
    disarms auto-resume, but the rig-stop cascade is guarded by
    `if self._websdr_active` -- which is already False while idle-stopped,
    so the cascade is skipped and the bookkeeping survived an explicit
    rig Disconnect. The app would then silently reopen a session on
    someone else's receiver hours later, on the first rig activity, with
    no Connect click anywhere."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()
        assert engine._websdr_idle_stopped is True
        assert engine._websdr_active is False  # which is why the cascade is skipped

        await engine._stop_rig()  # the Transceiver panel's own Disconnect
        assert engine._websdr_idle_stopped is False
        assert engine._idle_stopped_site is None

        # Hours later the user reconnects the rig and starts using it.
        engine._rig = IdleStubRig(RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False))
        engine._rig_active = True
        await engine._tick()
        await engine._tick()

        assert engine._websdr_active is False
        assert len(engine._webview_host.created) == 1  # no session it never asked for

    asyncio.run(run())


def test_a_failed_idle_resume_stays_armed_for_a_later_retry(monkeypatch):
    """_resume_websdr_after_idle() clears the idle flags BEFORE awaiting
    _start_websdr(), which has early-return failure paths. If one fires,
    both flags are already cleared and _websdr_active is False -- nothing
    can ever retry, and WebSDR sync is silently over until a human
    notices. Re-arming makes the next rig-activity tick try again."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()
        assert engine._websdr_idle_stopped is True

        # The WebView cannot be created this time (the real failure path
        # this guards -- create_page() raising).
        working_create_page = engine._webview_host.create_page

        async def failing_create_page(loop, on_dead=None):
            raise RuntimeError("no WebView available")

        engine._webview_host.create_page = failing_create_page
        engine._rig.state = RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()

        assert engine._websdr_active is False
        assert engine._websdr_idle_stopped is True  # re-armed, not silently abandoned
        assert engine._idle_stopped_site == site

        # ...and once the underlying problem clears, a later activity tick
        # really does recover on its own.
        engine._webview_host.create_page = working_create_page
        engine._idle_resume_retry_at = 0.0  # skip the anti-spam gap
        engine._rig.state = RigState(freq_hz=14300000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()

        assert engine._websdr_active is True
        assert engine._websdr_idle_stopped is False

    asyncio.run(run())


def test_a_repeatedly_failing_idle_resume_is_rate_limited(monkeypatch):
    """The retry above is driven by rig activity, and spinning a VFO
    produces activity several times a second -- so a resume that fails
    every time must not mean several page-creation attempts per second."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()

        attempts = []

        async def failing_create_page(loop, on_dead=None):
            attempts.append(1)
            raise RuntimeError("no WebView available")

        engine._webview_host.create_page = failing_create_page
        for n in range(10):  # ten ticks of the operator working the dial
            engine._rig.state = RigState(
                freq_hz=14200000 + n * 1000, mode="USB", passband_hz=2700, ptt=False
            )
            await engine._tick()

        assert len(attempts) == 1  # not ten
        assert engine._websdr_idle_stopped is True  # still armed for a real retry

    asyncio.run(run())


def test_an_unreachable_rig_still_idle_disconnects(monkeypatch):
    """A rig that dropped is arguably the STRONGEST "nobody is here"
    signal -- but every not-connected path in _tick() returned above the
    idle check, so the session held a volunteer receiver's audio slot
    forever. Note this is the path a rig that drops AFTER connecting once
    takes: _rig_connect_deadline is cleared on first success, so the
    30s give-up branch can never fire again and it retries indefinitely."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        assert engine._rig_connect_deadline is None  # connected once already

        engine._rig.reachable = False  # USB unplugged / rigctld killed / PC slept
        await engine._tick()
        assert engine._websdr_active is True  # not yet due

        engine._last_rig_activity_at -= 61.0
        await engine._tick()

        assert engine._websdr_active is False
        assert engine._websdr_idle_stopped is True
        assert engine._idle_stopped_site == site

    asyncio.run(run())


def test_a_rig_that_comes_back_unchanged_still_counts_as_activity(monkeypatch):
    """The trap in fixing the above: _idle_last_freq/_idle_last_mode/
    _idle_last_ptt keep their pre-drop values across the outage, so a rig
    that returns reporting exactly what it showed before (replugged
    without touching the dial, PC resumed from sleep) produces no
    detectable change and the session would never come back -- trading
    "holds the slot forever" for "never resumes". The not-connected ->
    connected transition itself has to count."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    parked = RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False)

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()

        engine._rig.reachable = False
        engine._last_rig_activity_at -= 61.0
        await engine._tick()
        assert engine._websdr_idle_stopped is True

        # Back, reporting the IDENTICAL state it had before the outage.
        engine._rig.state = parked
        engine._rig.reachable = True
        await engine._tick()

        assert engine._websdr_active is True
        assert engine._websdr_idle_stopped is False
        assert len(engine._webview_host.created) == 2

    asyncio.run(run())


def test_dropped_rig_reads_do_not_keep_resetting_the_idle_timer(monkeypatch):
    """Every RigState field is Optional, and None means a DROPPED READ,
    not a value the operator moved something to. A plain != counted each
    drop as activity and each recovery as a second one, so a marginal
    USB-serial/CAT link reset the idle timer continuously and
    idle-disconnect could never fire -- on exactly the unattended,
    flaky-link setup it matters most for."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    good = RigState(freq_hz=14074000, mode="USB", passband_hz=2700, ptt=False)
    dropped = RigState(freq_hz=None, mode=None, passband_hz=None, ptt=None)

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()  # seeds the trackers from a good read

        engine._last_rig_activity_at -= 59.0  # not yet due
        stale = engine._last_rig_activity_at

        for _ in range(5):  # the link flapping, nobody at the radio
            engine._rig.state = dropped
            await engine._tick()
            engine._rig.state = good
            await engine._tick()

        assert engine._last_rig_activity_at == stale  # never reset by the flapping
        assert engine._websdr_active is True

        engine._last_rig_activity_at -= 2.0  # now past the threshold
        await engine._tick()
        assert engine._websdr_active is False  # idle disconnect still fires

    asyncio.run(run())


def test_a_real_change_seen_after_a_dropped_read_still_counts(monkeypatch):
    """The other half: ignoring None must not make the engine deaf to a
    genuine change that happens to arrive after a dropped read."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=60)

    asyncio.run(engine._tick())
    engine._last_rig_activity_at -= 100.0
    stale = engine._last_rig_activity_at

    engine._rig.state = RigState(freq_hz=None, mode=None, passband_hz=None, ptt=None)
    asyncio.run(engine._tick())
    assert engine._last_rig_activity_at == stale

    engine._rig.state = RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False)
    asyncio.run(engine._tick())
    assert engine._last_rig_activity_at > stale


def test_a_partial_read_does_not_mask_a_change_on_another_axis(monkeypatch):
    """Compared per field, so a dropped frequency read cannot swallow a
    real mode change arriving in the same poll."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=60)

    asyncio.run(engine._tick())
    engine._last_rig_activity_at -= 100.0
    stale = engine._last_rig_activity_at

    engine._rig.state = RigState(freq_hz=None, mode="CW", passband_hz=500, ptt=False)
    asyncio.run(engine._tick())
    assert engine._last_rig_activity_at > stale
    # ...and the dropped frequency did NOT overwrite the remembered one,
    # so returning to it later is correctly seen as no change.
    assert engine._idle_last_freq == 14074000


# ------------------------------------------- per-attachment state and site staleness


def test_teardown_clears_the_attach_timestamp_so_the_next_page_attaches_immediately(monkeypatch):
    """_attach_attached_at is per-ATTACHMENT state and must not outlive a
    teardown -- unlike _attach_failures, which deliberately does. Left
    set, the next session's supervisor sees it non-None on its very first
    iteration (when the new driver naturally isn't attached yet), takes
    the flap branch, charges a bogus failure and sleeps a backoff delay
    BEFORE ever trying to attach the new page -- delaying recovery from
    an unrelated WebView crash and mislabelling it in the log."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        engine._attach_attached_at = time.monotonic()  # a live attachment
        await engine._stop_websdr()
        assert engine._attach_attached_at is None

    asyncio.run(run())

    # The next session's supervisor really does attempt a real attach on
    # its first pass, rather than charging a flap and backing off first.
    driver = ScriptedAttachDriver()
    _run_supervisor(engine, driver, iterations=1)
    assert driver.attach_calls == 1
    assert engine._attach_failures == 0


def test_a_webview_crash_does_not_charge_a_flap_against_the_replacement_page(monkeypatch):
    """End-to-end version of the above, over the real crash-recovery path
    (_handle_page_dead -> _start_websdr(user_initiated=False), which
    deliberately does NOT reset attach state -- so the teardown is the
    only thing that can clear the per-attachment half)."""
    monkeypatch.setitem(engine_module.DRIVERS, "websdr_org", StubDriver)
    engine = make_engine()
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        engine._attach_attached_at = time.monotonic()  # attached and healthy...
        engine._on_page_dead(engine._websdr_generation, "script timeout")  # ...then the WebView dies
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(engine._webview_host.created) == 2  # recovered
        assert engine._attach_attached_at is None  # no phantom attachment carried over
        assert engine._attach_failures == 0  # the crash is not a flap

    asyncio.run(run())


def test_attach_retry_delay_exponent_is_clamped(monkeypatch):
    """Unbounded exponent growth is not merely wasteful: at ~1024
    consecutive failures `ATTACH_RETRY_BASE_DELAY_S * (2 ** (n - 1))`
    raises OverflowError converting the int to float, which propagates
    out of _register_attach_failure() and kills the attach supervisor
    task outright -- after which nothing ever retries, silently. At the
    300s ceiling that is only ~3.5 days against a site that has been
    removed, on an app explicitly meant to run unattended indefinitely."""
    engine = make_engine()
    low, high = engine_module.ATTACH_RETRY_JITTER
    ceiling = engine_module.ATTACH_RETRY_MAX_DELAY_S

    for failures in (1024, 5000):
        engine._attach_failures = failures
        delay = engine._attach_retry_delay()  # must not raise
        assert ceiling * low <= delay <= ceiling * high

    # The cap is chosen well past the rung where the ladder already pins
    # to its ceiling, so it can never act as a second, lower cap.
    engine._attach_failures = engine_module.BACKOFF_EXPONENT_CAP
    assert engine._attach_retry_delay() >= ceiling * low


def test_idle_resume_uses_the_sites_current_definition(monkeypatch):
    """_idle_stopped_site is a value snapshot that can sit unused for
    hours. If the user corrects that site's definition meanwhile, the
    resume must use the CURRENT one, not the stale capture."""
    monkeypatch.setitem(engine_module.DRIVERS, "kiwisdr", StubDriver)
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    engine.settings.user_sites = [
        {"name": "A", "url": "http://a.invalid/", "driver_type": "websdr_org"}
    ]

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()
        assert engine._idle_stopped_site_was_listed is True

        # The user fixes the driver type while the session is idle-stopped.
        engine.settings.user_sites = [
            {"name": "A (kiwi)", "url": "http://a.invalid/", "driver_type": "kiwisdr"}
        ]
        engine._rig.state = RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()

        assert engine._websdr_active is True
        assert engine.site.driver_type == "kiwisdr"
        assert engine.site.name == "A (kiwi)"

    asyncio.run(run())


def test_idle_resume_disarms_if_the_site_was_deleted_meanwhile(monkeypatch):
    """Reconnecting to a definition the user has since deleted would be
    acting on data they removed -- disarm and say why instead."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="A", url="http://a.invalid/", driver_type="websdr_org")
    engine.settings.user_sites = [
        {"name": "A", "url": "http://a.invalid/", "driver_type": "websdr_org"}
    ]

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()
        assert engine._idle_stopped_site_was_listed is True

        engine.settings.user_sites = []  # deleted from the dropdown
        engine._rig.state = RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()

        assert engine._websdr_active is False
        assert engine._websdr_idle_stopped is False  # disarmed, not left half-armed
        assert engine._idle_stopped_site is None
        assert len(engine._webview_host.created) == 1

    asyncio.run(run())


def test_an_unsaved_custom_url_still_resumes_after_an_idle_stop(monkeypatch):
    """The trap in the deletion check: a one-off Custom URL is in no site
    list to begin with, so "not found at resume time" cannot mean
    "deleted" on its own -- otherwise every unsaved custom session would
    be stranded by the idle feature."""
    engine = _make_idle_engine(monkeypatch, idle_minutes=1)
    site = WebSDRSite(name="Custom", url="http://custom.invalid/", driver_type="websdr_org")
    assert engine.settings.user_sites == []  # never saved to the list

    async def run():
        engine._loop = asyncio.get_running_loop()
        await engine._start_websdr(site)
        await engine._tick()
        engine._last_rig_activity_at -= 61.0
        await engine._tick()
        assert engine._idle_stopped_site_was_listed is False

        engine._rig.state = RigState(freq_hz=14200000, mode="USB", passband_hz=2700, ptt=False)
        await engine._tick()

        assert engine._websdr_active is True
        assert engine.site == site  # the snapshot is all there is, and it is correct

    asyncio.run(run())

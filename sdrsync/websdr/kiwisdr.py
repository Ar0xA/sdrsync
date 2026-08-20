"""Driver for the KiwiSDR software family (e.g. *.proxy.kiwisdr.com instances).

Strategy: same as the websdr.org driver -- drive the real page's own control
functions (confirmed by reading the live site's actual JS source and
calling them live in a real browser tab) via Playwright's page.evaluate,
rather than reimplementing KiwiSDR's websocket wire protocol.

Confirmed global functions/objects on the page:
    ext_tune(freq_dial_kHz, mode, zoom, zlevel, low_cut, high_cut, opt)
        -- omitted args are left as JS `undefined` and treated as "don't
        change this": ext_tune(khz) alone retunes frequency only (mode
        unaffected); ext_tune(undefined, mode) alone changes mode only
        (frequency unaffected). Confirmed live against a real instance.
        low_cut/high_cut (Hz, relative to the dial) are the real
        underlying filter-edge primitive -- confirmed from the live JS
        source: ext_tune() only calls window.ext_set_passband(low_cut,
        high_cut) when BOTH are given (isArg(low_cut) && isArg(high_cut)),
        which clamps them into that mode's real filter.low_cut_limit/
        high_cut_limit and applies them to demod.low_cut/high_cut
        directly -- an exact filter width, not the coarse binary narrow/
        wide mode-string choice this driver used before. Per-mode
        defaults (used here only as the "which edge is fixed while width
        varies" reference, via passband_edges() below) come from the
        page's own passbands_fallback table -- kiwi_passbands(mode)
        checks a live, per-instance cfg.passbands override first, so an
        individual receiver's admin-configured defaults can differ from
        this reference; the fixed-edge CONVENTION itself is assumed
        universal, not independently verified against every possible
        instance config.
    ext_get_freq_kHz()  -- returns a STRING like "14074.00", not a number.
    ext_get_mode()      -- returns the current mode as a lowercase string.
    ws_snd              -- the sound WebSocket; ws_snd.readyState === 1
                            (OPEN) is the reliable "ready" signal.
    toggle_or_set_mute(1|0) -- mutes/unmutes local playback volume.
    .id-play-button-container -- audio-unlock overlay the page shows
        itself (onclick -> play_button_click_cb() -> AudioContext.resume())
        whenever its own test AudioContext isn't already "running" --
        confirmed live in KiwiSDR's actual JS source (test_audio_suspended()
        in kiwisdr.min.js). A CSS CLASS, not a DOM id, despite the "id-"
        prefix in its name -- confirmed live that w3_psa() (the page's own
        markup-string parser behind w3_div()) only ever emits a class=
        attribute from this string, never a real id=. See attach()'s own
        comment for why this matters on WebKitGTK/Linux specifically.

Unlike PA3FWM, a KiwiSDR instance is a single continuous-range receiver --
there is no setband()-equivalent and no band table. That does NOT mean
tune/mode requests can't silently fail though: public instances can drop
ws_snd out from under you (user-count/time limits), and admin-configured
frequency ranges can silently clamp/reject a request -- so tune_hz/set_mode
both check ws_snd.readyState and verify via a readback, the same
"only report success if it actually happened" discipline as websdr_org.py.
"""
from __future__ import annotations

import asyncio
import logging
import math
import sys
from typing import Optional

from sdrsync.websdr.browser_shim import BrowserError as PlaywrightError
from sdrsync.websdr.browser_shim import PageLike as Page
from sdrsync.websdr.browser_shim import click_element_if_present, element_is_present

from sdrsync.websdr.base import WebSDRIncompatibleError, WebSDRStatus, is_finite_frequency

logger = logging.getLogger("sdrsync.websdr.kiwisdr")

LOAD_TIMEOUT_MS = 15000
FREQ_VERIFY_TOLERANCE_HZ = 10
# Per-attempt timeout for _watch_for_audio_unlock_overlay()'s retry loop,
# NOT a total give-up deadline -- confirmed live that the overlay's own
# trigger (a WebSocket "camp" message handler / early UI-init call inside
# the page's JS) can fire well after ws_snd/demodulators are ready, and
# even a generous-feeling one-shot 20s window wasn't always enough (a real
# observed case: still not present after 20s, then appeared on screen
# later anyway). The watcher keeps retrying for as long as this driver
# stays attached, so this only controls how often it re-checks.
AUDIO_UNLOCK_WATCH_ATTEMPT_S = 5.0
# Settle time after a click before re-probing to confirm the overlay
# actually went away (see _watch_for_audio_unlock_overlay's own comment
# on why a dispatched click alone isn't proof of success) -- KiwiSDR's
# own fade-out is w3_opacity(...,0) then w3_hide() 1100ms later, so this
# needs to be a bit longer than that.
AUDIO_UNLOCK_OVERLAY_FADE_S = 1.2
# How many consecutive "clicked, but the overlay is still there" outcomes
# before the watcher stops clicking and switches to probe-only (see
# _watch_for_audio_unlock_overlay's own comment). A require_id receiver
# makes every click a permanent no-op -- confirmed live -- so with no cap
# the watcher clicked forever: a bug-hunter review pass measured a real
# OS-level mouse click (via wx.UIActionSimulator) roughly every
# AUDIO_UNLOCK_WATCH_ATTEMPT_S + AUDIO_UNLOCK_OVERLAY_FADE_S seconds,
# indefinitely, actively hijacking the cursor the operator needs to type
# their ID into the page in the first place.
AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER = 3
# How often the probe-only phase re-checks once clicking has given up --
# no more urgency once we're just waiting for the operator (or the page
# itself) to clear the overlay some other way.
AUDIO_UNLOCK_PROBE_ONLY_INTERVAL_S = 5.0

# hamlib mode name -> kiwi base mode. Filter width is a separate concern
# now -- see passband_edges() below -- so this no longer picks a narrow-
# variant mode string; every write goes through the base mode plus an
# exact low_cut/high_cut via ext_tune(), never a "usn"/"lsn"/etc. string.
_MODE_MAP: dict[str, str] = {
    "USB": "usb",
    "PKTUSB": "usb",
    "DATA-U": "usb",
    "LSB": "lsb",
    "PKTLSB": "lsb",
    "DATA-L": "lsb",
    "CW": "cw",
    "CWR": "cw",
    "CW-U": "cw",
    "CW-L": "cw",
    "AM": "am",
    "SAM": "sam",
    "FM": "nbfm",
    "WFM": "nbfm",
}


def map_hamlib_mode_kiwi(hamlib_mode: str) -> Optional[str]:
    """Pure mapping from a hamlib mode name to a KiwiSDR base mode
    string. Returns None if there's no known mapping (caller should skip
    mode sync and log, not raise). Filter width is passband_edges()'s
    job now, not folded into this mapping."""
    return _MODE_MAP.get(hamlib_mode.upper())


# KiwiSDR's own narrow/wide-variant mode strings (from its live
# passbands_fallback table -- see module docstring) -> their base mode.
# Used only for _base_mode_of()'s defensive normalization of whatever
# get_status() reads back from the page: this driver no longer SENDS any
# narrow-suffixed string itself (passband_edges() replaces the old
# narrow/wide binary choice), but the page's own reported mode could
# still show one from some other state. Not derivable from a simple
# suffix rule -- most of these don't follow "+n" (nbfm -> nnfm, not
# "nbfmn") -- so listed explicitly, same reasoning _MODE_MAP's old
# narrow-variant column had.
_NARROW_TO_BASE: dict[str, str] = {
    "usn": "usb",
    "lsn": "lsb",
    "cwn": "cw",
    "amn": "am",
    "amw": "am",
    "nnfm": "nbfm",
}


def _base_mode_of(kiwi_mode: str) -> str:
    """Strip a KiwiSDR narrow/wide-variant mode string down to its base.
    Falls back to returning the string unchanged if it's not a known
    variant (covers 'sam' and any mode this driver doesn't map to)."""
    return _NARROW_TO_BASE.get(kiwi_mode, kiwi_mode)


# Reference edges (Hz, relative to the dial/carrier) taken directly from
# the live site's own passbands_fallback JS table (see module docstring)
# -- confirmed against kiwisdr.min.js:
#   usb   {lo:300,  hi:2700}
#   lsb   {lo:-2700, hi:-300}
#   cw    {lo:300,  hi:700}   (centre +500 Hz -- NOT the dial, and NOT
#                               websdr_org.py's own -750 Hz CW convention;
#                               a genuinely different, independently
#                               confirmed per-site value, not copy-pasted)
#   am    {lo:-4900, hi:4900}
#   sam   {lo:-4900, hi:4900}  (same table entry as am on this site)
#   nbfm  {lo:-6000, hi:6000}
# Used both as the "which edge stays fixed while width varies" convention
# below and as passband_edges()'s per-mode existence check.
_DEFAULT_EDGES_HZ: dict[str, tuple[float, float]] = {
    "usb": (300, 2700),
    "lsb": (-2700, -300),
    "cw": (300, 700),
    "am": (-4900, 4900),
    "sam": (-4900, 4900),
    "nbfm": (-6000, 6000),
}


def passband_edges(base_mode: str, width_hz: Optional[int]) -> Optional[tuple[float, float]]:
    """The ext_tune()/ext_set_passband() filter edges to ask for, in Hz
    relative to the dial -- KiwiSDR's own units, no kHz conversion needed
    (unlike websdr_org.py's setmf()).

    None means "say nothing about the filter" (low_cut/high_cut both
    omitted from the ext_tune() call), which falls back to the site's own
    default for that mode -- the right answer when the rig did not
    report a passband. No client-side ceiling clamp: ext_set_passband()
    already clamps into that mode's real filter.low_cut_limit/
    high_cut_limit itself, so sending an oversized width and letting the
    site do the clamping is the honest reading of "as wide as I am"
    (mirrors ubersdr.py's and websdr_org.py's identically-named
    functions' own reasoning).

    Mirrors websdr_org.py's passband_edges(), adapted to this site's own
    fixed-edge conventions (see _DEFAULT_EDGES_HZ above):

        usb   the low edge stays fixed where the site's own USB default
              puts it (300 Hz, clear of the carrier) and the width is
              added above it.
        lsb   the mirror of that (high edge fixed at -300 Hz).
        am/sam/nbfm   symmetric about the dial, like the site's own
              defaults for these modes -- half the width each side.
        cw    symmetric about +500 Hz, NOT the dial -- this site's own
              hardcoded CW filter centre (the midpoint of its real
              300/700 Hz default), a different sidetone-pitch convention
              from websdr_org.py's -750 Hz one.
    """
    defaults = _DEFAULT_EDGES_HZ.get(base_mode)
    if defaults is None or not width_hz or width_hz <= 0:
        return None
    if base_mode == "usb":
        low = defaults[0]
        return low, low + width_hz
    if base_mode == "lsb":
        high = defaults[1]
        return high - width_hz, high
    if base_mode == "cw":
        center = (defaults[0] + defaults[1]) / 2
        return center - width_hz / 2, center + width_hz / 2
    # am, sam, nbfm -- symmetric about the dial, like the site's own defaults.
    return -width_hz / 2, width_hz / 2


# get_status()'s already-normalized mode string (base_mode_of(...).upper(),
# the exact post-normalization set -- USB/LSB/CW/AM/AMW/SAM/NBFM) ->
# canonical hamlib mode name. NOT mechanically derived from _MODE_MAP
# above (many-to-one there); since the input here is already
# base-collapsed and uppercased, _base_mode_of() would be a no-op if
# re-applied, so this maps directly from the confirmed uppercase set.
_REVERSE_MODE_MAP: dict[str, str] = {
    "USB": "USB",
    "LSB": "LSB",
    "CW": "CW",
    "AM": "AM",
    "AMW": "AM",  # no separate hamlib wide-AM mode exists
    "SAM": "SAM",
    "NBFM": "FM",
}


def map_kiwi_mode_to_hamlib(kiwi_mode: Optional[str]) -> Optional[str]:
    """Pure reverse mapping: KiwiSDR's get_status()-normalized mode string
    (already base-collapsed and uppercased) -> a canonical hamlib mode
    name. None if unknown (caller should skip the reverse push and log,
    not raise)."""
    if kiwi_mode is None:
        return None
    return _REVERSE_MODE_MAP.get(kiwi_mode.upper())


class KiwiSDRDriver:
    """WebSDRDriver implementation for the KiwiSDR software family.

    Does not own the Playwright Page/Browser lifecycle -- see
    WebsdrOrgDriver's docstring, same contract.
    """

    FINGERPRINT_MARKERS = ("kiwisdr.min.js",)
    # CW and CWR both forward-map to page mode "cw"/"cwn" (see _MODE_MAP
    # above) and _REVERSE_MODE_MAP only has "CW" -> "CW" -- ambiguous.
    CW_VARIANT_IS_AMBIGUOUS = True

    def __init__(self, url: str, cw_offset_hz: int = 0, auto_click_audio_unlock: bool = True) -> None:
        self.url = url
        self.cw_offset_hz = cw_offset_hz
        self._auto_click_audio_unlock = auto_click_audio_unlock

        self._page: Optional[Page] = None
        self._current_mode: Optional[str] = None
        self._attached = False
        self._listeners_registered = False
        self._last_attach_error: Optional[str] = None
        self._last_page_error: Optional[str] = None
        self._last_tune_error: Optional[str] = None
        self._last_mode_error: Optional[str] = None
        # Tracks the last hamlib mode that had no KiwiSDR equivalent, so the
        # "no equivalent" warning logs once per new occurrence instead of
        # every poll tick the engine retries set_mode() -- mirrors
        # openwebrx.py's _last_out_of_range_key rate-limiting for the same
        # class of problem (repeated per-tick log spam).
        self._last_unmapped_mode: Optional[str] = None
        # Background audio-unlock watcher (see attach()) -- tracked so
        # close() can cancel a still-pending one rather than leaving it
        # running against a page this driver no longer owns.
        self._audio_unlock_task: Optional[asyncio.Task] = None

    @property
    def attached(self) -> bool:
        return self._attached

    # ------------------------------------------------------------------
    async def attach(self, page: Page) -> None:
        self._page = page
        if not self._listeners_registered:
            page.on("console", self._on_console)
            page.on("pageerror", self._on_pageerror)
            self._listeners_registered = True

        try:
            await page.goto(self.url, timeout=LOAD_TIMEOUT_MS)
            await page.wait_for_function(
                "typeof window.ext_tune === 'function' "
                "&& typeof window.ext_get_mode === 'function' "
                "&& typeof window.ext_get_freq_kHz === 'function' "
                "&& window.ws_snd && window.ws_snd.readyState === 1 "
                # ws_snd opening doesn't mean the page is ready to be
                # retuned yet -- the page's own default demodulator is
                # created slightly *after* the sound socket opens (its own
                # init callback), and calling ext_tune() before that exists
                # throws inside the page's own JS (demodulators[0] is
                # undefined). Confirmed empirically: readyState hits 1 up
                # to ~1-2s before demodulators.length goes from 0 to 1.
                "&& typeof demodulators !== 'undefined' && demodulators.length > 0",
                timeout=LOAD_TIMEOUT_MS,
            )
        except PlaywrightError as e:
            self._last_attach_error = (
                f"Page at {self.url} did not behave like a compatible KiwiSDR "
                f"within {LOAD_TIMEOUT_MS}ms (control functions and/or open sound "
                f"socket not found): {e!r}"
            )
            raise WebSDRIncompatibleError(self._last_attach_error) from e

        # Best-effort audio unlock: confirmed live in KiwiSDR's own JS
        # (test_audio_suspended()) that the page shows a "Click to start
        # KiwiSDR" overlay -- .id-play-button-container (a CSS CLASS, not a
        # DOM id, despite the name -- confirmed live via w3_psa(), the
        # page's own markup-string parser, which only ever emits a class=
        # attribute from this string; querying it as an ID selector
        # matched nothing, which is why the very first live test of this
        # watcher never found/clicked anything at all), onclick ->
        # play_button_click_cb() -> audio_context.resume() -- whenever ITS
        # OWN test AudioContext isn't already "running" at that check. On
        # WebKitGTK (confirmed live on a native, non-WSL2 Linux desktop) a
        # fresh AudioContext is NOT auto-"running", so this overlay
        # genuinely appears and, unlike websdr_org.py's page, nothing else
        # on this page ever calls resume() -- so tuning/mode control all
        # worked while audio stayed permanently silent until this got
        # clicked.
        #
        # Run as a background task, NOT awaited here: confirmed live that
        # the overlay's own trigger can fire well after this point (a real
        # observed case: still not present ~8s after session start), so
        # awaiting it inline with a short timeout would either miss a
        # late-appearing overlay or make attach() (and the GUI's
        # "connected" status) hang around waiting for something that isn't
        # guaranteed to happen promptly. A previous still-pending watcher
        # (e.g. a fast reattach) is cancelled first so it can't click
        # against a page this attach() call is about to take back over.
        #
        # Skipped entirely on Windows, not just left to no-op: confirmed
        # from KiwiSDR's own source (test_audio_suspended() above) that
        # this overlay is gated on the exact same AudioContext.state
        # check ensure_webview_backend() (browser/backend.py) already
        # guarantees "running" for via --autoplay-policy=
        # no-user-gesture-required -- so the overlay provably never
        # renders there, and watching for one that can never appear would
        # only ever be wasted work on Windows (v2.2.6 behavior confirmed
        # working there; this must not regress it).
        if self._audio_unlock_task is not None:
            self._audio_unlock_task.cancel()
        if sys.platform != "win32" and self._auto_click_audio_unlock:
            self._audio_unlock_task = asyncio.create_task(self._watch_for_audio_unlock_overlay(page))

        self._attached = True
        self._current_mode = None
        self._last_attach_error = None
        self._last_page_error = None
        self._last_tune_error = None
        self._last_mode_error = None
        self._last_unmapped_mode = None

    async def _watch_for_audio_unlock_overlay(self, page: Page) -> None:
        """Background task launched from attach() -- see its own comment
        for why this isn't just awaited inline. Loops in
        AUDIO_UNLOCK_WATCH_ATTEMPT_S-sized attempts for as long as this
        driver stays attached (bounded only by close()/a reattach
        cancelling this task), rather than giving up after one fixed
        window: confirmed live that the overlay's own trigger can appear
        well past any window that seemed generous in testing (a real
        observed case: still not present a full 20s after session start,
        and the overlay DID show up on screen after that). Never raises
        out to the event loop's default task-exception logging: a page-
        closed/navigated-away BrowserError here is routine (the operator
        disconnected or switched sites while this was still watching),
        not a real error.

        After AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER consecutive clicks that
        didn't actually clear the overlay (the require_id case -- see
        below), stops clicking and switches to probe-only: still watches
        for the overlay to disappear (e.g. the operator typed their ID
        and dismissed it themselves), just without touching the mouse any
        more. Found by a bug-hunter review pass: with no cap, this loop
        clicked forever on a require_id receiver -- a real OS-level mouse
        click roughly every 6s, indefinitely, actively hijacking the
        cursor the operator needs to use to type into the very page this
        was trying to help with."""
        logger.debug("Audio-unlock overlay watcher started for %s", self.url)
        consecutive_ineffective_clicks = 0
        clicking = True
        try:
            while self._attached:
                # Re-checked every loop iteration, not just at spawn time
                # (engine.py syncs this live from settings each tick) --
                # a bug-hunter review pass found unchecking "Mouse hijack
                # to enable WebSDR audio" mid-session had no effect on an
                # already-running watcher, the same staleness bug
                # cw_offset_hz had. A short sleep, not a busy spin, since
                # the box being off is the steady state once toggled.
                if not self._auto_click_audio_unlock:
                    await asyncio.sleep(1.0)
                    continue

                if not clicking:
                    await asyncio.sleep(AUDIO_UNLOCK_PROBE_ONLY_INTERVAL_S)
                    if not await element_is_present(page, ".id-play-button-container"):
                        logger.info(
                            "Audio-unlock overlay at %s cleared (probe-only, not by our own "
                            "click)", self.url,
                        )
                        return
                    continue

                clicked = await click_element_if_present(
                    page, ".id-play-button-container", timeout_s=AUDIO_UNLOCK_WATCH_ATTEMPT_S,
                )
                if not clicked:
                    continue
                # A dispatched click is not proof it did anything --
                # confirmed live in KiwiSDR's own JS: a receiver with
                # cfg.require_id set renders this exact overlay class
                # with onclick='' (an identification text field takes
                # its place instead) until the operator types something
                # in, so clicking it is a genuine no-op. Only trust
                # success once a re-probe confirms the overlay is
                # actually gone -- KiwiSDR's own fade-out
                # (w3_opacity(...,0) then w3_hide() 1100ms later) needs
                # a moment to finish first.
                await asyncio.sleep(AUDIO_UNLOCK_OVERLAY_FADE_S)
                if await element_is_present(page, ".id-play-button-container"):
                    consecutive_ineffective_clicks += 1
                    if consecutive_ineffective_clicks >= AUDIO_UNLOCK_CLICK_GIVE_UP_AFTER:
                        clicking = False
                        logger.info(
                            "Audio-unlock overlay at %s did not clear after %d clicks -- "
                            "likely a require_id receiver (the operator needs to enter an "
                            "ID/callsign on the page itself); no longer clicking, will keep "
                            "watching", self.url, consecutive_ineffective_clicks,
                        )
                        continue
                    logger.debug(
                        "Audio-unlock overlay at %s still present after being clicked "
                        "(likely a require_id receiver -- the operator needs to enter an "
                        "ID/callsign on the page itself); will keep watching", self.url,
                    )
                    continue
                logger.info("Audio-unlock overlay clicked at %s", self.url)
                return
            logger.debug("Audio-unlock overlay watcher stopped (no longer attached) at %s", self.url)
        except asyncio.CancelledError:
            raise
        except PlaywrightError as e:
            logger.debug("Audio-unlock overlay watcher stopped early (non-fatal): %s", e)

    # ------------------------------------------------------------------
    async def tune_hz(self, freq_hz: int, verify: bool = True) -> bool:
        """Returns True only if actually applied and verified via readback.

        verify is accepted for WebSDRDriver Protocol parity but unused
        here -- this driver's readback verification is synchronous and
        inline (below), not a delayed background task, so there's nothing
        for verify=False to skip."""
        if not self._attached or not is_finite_frequency(freq_hz):
            return False
        effective_hz = freq_hz
        if self._current_mode is not None and _base_mode_of(self._current_mode) == "cw":
            effective_hz += self.cw_offset_hz

        try:
            result = await self._page.evaluate(
                "(khz) => { "
                "if (!window.ws_snd || window.ws_snd.readyState !== 1) return 'not_ready'; "
                "window.ext_tune(khz); "
                "return 'ok'; "
                "}",
                effective_hz / 1000.0,
            )
        except PlaywrightError as e:
            self._last_tune_error = f"ext_tune() failed: {e}"
            logger.warning(self._last_tune_error)
            return False

        if result == "not_ready":
            self._last_tune_error = "KiwiSDR sound socket is not open (disconnected mid-session?)"
            logger.warning(self._last_tune_error)
            self._attached = False
            return False

        actual_hz = await self._read_freq_hz()
        if actual_hz is None:
            self._last_tune_error = "Could not read back frequency to verify ext_tune()"
            logger.warning(self._last_tune_error)
            return False
        if abs(actual_hz - effective_hz) > FREQ_VERIFY_TOLERANCE_HZ:
            self._last_tune_error = (
                f"KiwiSDR did not apply requested frequency: wanted {effective_hz/1000:.3f} kHz, "
                f"reads {actual_hz/1000:.3f} kHz (out of the receiver's configured range?)"
            )
            logger.warning(self._last_tune_error)
            return False

        self._last_tune_error = None
        return True

    async def _read_freq_hz(self) -> Optional[int]:
        try:
            khz_str = await self._page.evaluate("() => window.ext_get_freq_kHz()")
            return int(round(float(khz_str) * 1000))
        except (PlaywrightError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    async def set_mode(self, hamlib_mode: str, passband_hz: Optional[int]) -> bool:
        """Returns True only if actually applied and verified via readback.

        passband_hz drives an EXACT filter width via ext_tune()'s own
        low_cut/high_cut params (see passband_edges()) rather than
        picking between a coarse narrow/wide mode-string variant -- so
        the rig's real filter width is what actually gets set."""
        if not self._attached:
            return False
        kiwi_mode = map_hamlib_mode_kiwi(hamlib_mode)
        if kiwi_mode is None:
            self._last_mode_error = (
                f"hamlib mode {hamlib_mode!r} has no KiwiSDR equivalent; frequency sync continues"
            )
            if hamlib_mode != self._last_unmapped_mode:
                self._last_unmapped_mode = hamlib_mode
                logger.warning(self._last_mode_error)
            else:
                logger.debug(self._last_mode_error)
            return False

        # Bug-hunter finding, confirmed against the live site's own JS:
        # ext_tune()'s mode branch (freqmode_set_dsp_kHz) only recreates
        # the demodulator -- which is what actually resets the filter to
        # a mode's default -- when the mode STRING changes
        # (`mode!=cur_mode`). Since this driver only ever sends the base
        # mode string now (no more narrow/wide variant strings), an
        # unchanged mode with lo=hi=null is a COMPLETE no-op for the
        # filter, not "use the site's own default" -- so turning
        # AppSettings.sync_passband_from_rig off (or a rig that stops
        # reporting a passband) left whatever custom width was last sent
        # in place forever. Falling back to our own reference default
        # here (same table passband_edges() itself anchors on) makes
        # every set_mode() call explicit about the filter it wants,
        # regardless of whether the mode string happens to be changing.
        edges = passband_edges(kiwi_mode, passband_hz)
        if edges is None:
            edges = _DEFAULT_EDGES_HZ.get(kiwi_mode)
        lo, hi = edges if edges is not None else (None, None)
        try:
            result = await self._page.evaluate(
                "(a) => { "
                "if (!window.ws_snd || window.ws_snd.readyState !== 1) return 'not_ready'; "
                "window.ext_tune(undefined, a.mode, undefined, undefined, a.lo, a.hi); "
                "return 'ok'; "
                "}",
                {"mode": kiwi_mode, "lo": lo, "hi": hi},
            )
        except PlaywrightError as e:
            self._last_mode_error = f"ext_tune(mode, lo, hi) failed: {e}"
            logger.warning(self._last_mode_error)
            return False

        if result == "not_ready":
            self._last_mode_error = "KiwiSDR sound socket is not open (disconnected mid-session?)"
            logger.warning(self._last_mode_error)
            self._attached = False
            return False

        actual_mode = await self._read_mode()
        if actual_mode != kiwi_mode:
            self._last_mode_error = (
                f"KiwiSDR did not apply requested mode: wanted {kiwi_mode!r}, reads {actual_mode!r}"
            )
            logger.warning(self._last_mode_error)
            return False

        self._current_mode = kiwi_mode
        self._last_mode_error = None
        self._last_unmapped_mode = None
        return True

    async def _read_mode(self) -> Optional[str]:
        try:
            return await self._page.evaluate("() => window.ext_get_mode()")
        except PlaywrightError:
            return None

    # Mute-on-TX is handled natively at the page-adapter level now (v15,
    # WxPageAdapter.set_muted() in browser_shim.py) -- see its module
    # comment for why: a native WebView-level mute is instant and site-
    # independent, unlike this KiwiSDR-specific toggle_or_set_mute() JS
    # call, which added up to ~1s of perceptible lag depending on
    # KiwiSDR's own mute implementation.

    def _reverse_effective_hz(
        self, observed_hz: Optional[int], observed_hamlib_mode: Optional[str]
    ) -> Optional[int]:
        """Un-applies cw_offset_hz for the reverse direction (WebSDR ->
        rig), symmetric to tune_hz()'s forward application above. Takes
        the mode as an explicit argument, sourced from the SAME status
        snapshot map_kiwi_mode_to_hamlib() derived it from -- NOT
        self._current_mode, which is stale by construction for reverse
        sync (see websdr_org.py's identical helper for the full
        reasoning)."""
        if observed_hz is None:
            return None
        if observed_hamlib_mode == "CW":
            return observed_hz - self.cw_offset_hz
        return observed_hz

    def hamlib_mode_from_status(self, status: WebSDRStatus) -> Optional[str]:
        return map_kiwi_mode_to_hamlib(status.mode)

    def rig_freq_from_status(self, status: WebSDRStatus) -> Optional[int]:
        return self._reverse_effective_hz(status.freq_hz, self.hamlib_mode_from_status(status))

    # ------------------------------------------------------------------
    def _combined_error(self) -> Optional[str]:
        parts = [
            e for e in (
                self._last_attach_error, self._last_page_error,
                self._last_tune_error, self._last_mode_error,
            ) if e
        ]
        return " | ".join(parts) if parts else None

    async def get_status(self) -> WebSDRStatus:
        if not self._attached:
            return WebSDRStatus(connected=False, last_error=self._combined_error())
        try:
            data = await self._page.evaluate(
                "() => ({freq: window.ext_get_freq_kHz ? window.ext_get_freq_kHz() : null, "
                "mode: window.ext_get_mode ? window.ext_get_mode() : null, "
                "audio: window.ws_snd ? window.ws_snd.readyState === 1 : null})"
            )
            freq_str = data.get("freq")
            mode_str = data.get("mode")
            # ext_get_freq_kHz() can legitimately return "nan" (e.g. right
            # after attach, before any tune has been sent yet) -- that's
            # "frequency not known yet", not a fatal error, so it must not
            # raise out of this try block and flip self._attached off.
            freq_hz: Optional[int] = None
            if freq_str is not None:
                freq_khz = float(freq_str)
                if is_finite_frequency(freq_khz):
                    freq_hz = int(round(freq_khz * 1000))
            return WebSDRStatus(
                connected=True,
                freq_hz=freq_hz,
                # Displayed mode is normalized to its base (non-narrow) form
                # -- ext_get_mode() returns KiwiSDR's own internal string
                # verbatim (e.g. "usn"/"lsn" for a narrow-filter USB/LSB
                # selection), which reads as a different mode than the
                # hamlib USB/LSB it actually corresponds to if shown raw.
                mode=_base_mode_of(mode_str).upper() if mode_str else None,
                # Proxy: "is the sound socket open", not a true audio-is-
                # actually-playing signal -- no confirmed AudioContext
                # global for KiwiSDR yet (see project_brief.md).
                audio_active=data.get("audio"),
                last_error=self._combined_error(),
            )
        except (PlaywrightError, TypeError, ValueError) as e:
            self._attached = False
            self._last_page_error = str(e)
            return WebSDRStatus(connected=False, last_error=self._combined_error())

    async def close(self) -> None:
        if self._audio_unlock_task is not None:
            self._audio_unlock_task.cancel()
            self._audio_unlock_task = None
        self._attached = False
        self._page = None

    # ------------------------------------------------------------------
    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            logger.warning("[KiwiSDR page console:%s] %s", msg.type, msg.text)

    def _on_pageerror(self, exc) -> None:
        self._last_page_error = f"Unhandled JS error on KiwiSDR page: {exc}"
        logger.error(self._last_page_error)

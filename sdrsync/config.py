"""Configuration: known WebSDR sites and persisted user settings."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("sdrsync.config")

CONFIG_DIR = Path.home() / ".sdrsync"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Bounds for AppSettings.poll_interval_s -- see the field's docstring.
# Defined here (not in gui/app.py) so load() can enforce them too, not
# just the GUI's SpinCtrlDouble widget.
MIN_POLL_INTERVAL_S = 0.05
MAX_POLL_INTERVAL_S = 5.0

# Floor for AppSettings.webview_sizes entries (the WebSDR popup) --
# defined here (not in gui/webview_host.py) so
# load() can enforce it on a hand-edited config.json too, not just the
# GUI. gui/webview_host.py imports these same two constants rather than
# redefining them, so the two layers can't drift apart (same split as
# MIN/MAX_POLL_INTERVAL_S above). There's no matching upper bound here --
# the sane ceiling is "this machine's screen", which only the wx-aware
# GUI layer can know; see gui/webview_host.py's display-clamping logic.
MIN_WEBVIEW_WIDTH = 400
MIN_WEBVIEW_HEIGHT = 300


@dataclass(frozen=True)
class WebSDRSite:
    """A selectable WebSDR instance and which driver knows how to control it."""
    name: str
    url: str
    driver_type: str


# Starter list for the dropdown. Add more sites here as they're supported;
# driver_type must match a key in sdrsync.websdr.registry.DRIVERS.
#
# NOTE: these are all third-party public receivers, not ours -- they can go
# offline, move, or (as already happened once with the original KiwiSDR
# entry, which had a server-side bug breaking set_mode()) develop instance-
# specific quirks. Treat this list as needing occasional revalidation, not
# a permanent fixture.
KNOWN_SITES: list[WebSDRSite] = [
    WebSDRSite(
        name="Twente wide-band (websdr.org)",
        url="http://websdr.ewi.utwente.nl:8901/",
        driver_type="websdr_org",
    ),
    WebSDRSite(
        name="KiwiSDR example (VK5ARG)",
        url="http://kiwisdr.areg.org.au:8073/",
        driver_type="kiwisdr",
    ),
    WebSDRSite(
        # 80m HF band (~2.65-4.65 MHz), confirmed live -- the previous
        # example (sdr2.justjakob.de) was VHF-only (2m, ~144-146 MHz),
        # which made every ordinary HF rig frequency report as "out of
        # range" by default. OpenWebRX instances with true full-HF
        # (0-30 MHz) single-profile coverage are uncommon (most consumer
        # SDR hardware this software runs on doesn't have that much
        # instantaneous bandwidth), so this is a deliberately narrower but
        # actually-useful-for-HF-testing default rather than a wideband one
        # like the websdr.org/KiwiSDR entries above.
        name="OpenWebRX example (OH6KK, 80m)",
        url="http://oh6kk.dy.fi:8073/",
        driver_type="openwebrx",
    ),
    WebSDRSite(
        # UberSDR exposes a documented control API rather than requiring us to
        # call its internals, so this driver is a client of that API -- see
        # sdrsync/websdr/ubersdr.py. The root URL is what an operator publishes;
        # the driver navigates to the v2 interface itself, which is where the API
        # lives.
        name="UberSDR example (M9PSY, full HF)",
        url="https://m9psy.tunnel.ubersdr.org/",
        driver_type="ubersdr",
    ),
]


@dataclass
class AppSettings:
    """User-editable settings, persisted as JSON in the user's home dir."""
    rigctld_host: str = "127.0.0.1"
    rigctld_port: int = 4532
    flrig_host: str = "127.0.0.1"
    flrig_port: int = 12345
    # Which rig-control backend is active -- "rigctld" or "flrig". Each
    # backend keeps its own host/port pair above so switching back and
    # forth in the GUI never loses either one's settings.
    rig_backend: str = "rigctld"
    last_site_url: str = field(default_factory=lambda: KNOWN_SITES[0].url)
    # Set alongside last_site_url so a Custom URL site (not present in
    # KNOWN_SITES) can be reconstructed on restart instead of silently
    # reverting to KNOWN_SITES[0]. Empty string means "not a custom site".
    last_site_driver_type: str = ""
    # WebSDR sites the user saved to the dropdown via the "Save to list"
    # button (a proven-working Custom URL), each dict:
    # {"name": str, "url": str, "driver_type": str}. Separate from
    # KNOWN_SITES, which are the app's built-in defaults and aren't
    # user-editable.
    user_sites: list = field(default_factory=list)
    # WebSDR sites loaded via the "Manage sites..." dialog's Load-from-file
    # or Load-from-URL actions (same dict shape as user_sites). Both share
    # this one bucket (not split by source) -- a fresh load fully replaces
    # whatever was here before. Kept separate from user_sites since these
    # come from outside the app and haven't been proven-working through
    # Detect/Connect the way a saved user_sites entry has.
    imported_sites: list = field(default_factory=list)
    # WebSDR sites loaded via the "Manage sites..." dialog's "Update from
    # GitHub" action (same dict shape). Separate from imported_sites so a
    # repeatable auto-refresh never clobbers a manual file/URL load or vice
    # versa. Replace-all on each Update -- a site the maintainer removes
    # upstream disappears locally too, since this isn't user-owned data.
    curated_sites: list = field(default_factory=list)
    cw_offset_hz: int = 0
    # Transceiver settings tab: the rig's OWN CW pitch/sidetone setting
    # (e.g. from its menu), entered manually -- nothing queries the rig
    # for this automatically (rigctld's hamlib CWPITCH level is real and
    # readable, but the user explicitly wants a plain editable field, not
    # an auto-query). Added to cw_offset_hz (see sync/engine.py's
    # _tick(), which sums the two into one value pushed to
    # driver.cw_offset_hz) rather than kept as a second, separately-
    # threaded offset -- reuses all of cw_offset_hz's existing live-sync/
    # reverse-sync-reseed handling for free. Most rigs default this to a
    # few hundred Hz (a common one, live-confirmed by the user's own
    # rig: 750 Hz) -- entering it here lets the WebSDR CW offset field
    # stay at 0 for "no additional correction needed" instead of the
    # operator having to fold the rig's own pitch into that number by
    # hand.
    transceiver_cw_pitch_hz: int = 0
    # Reverse sync (WebSDR -> rig): when the WebSDR page is in USB/LSB,
    # send the rig's DATA-mode variant instead of plain USB/LSB (e.g.
    # PKTUSB/PKTLSB on rigctld, DATA-U/DATA-L on flrig) -- for operators
    # who always want the rig keyed via its data input rather than the
    # mic, regardless of what a plain SSB WebSDR page shows. Default off:
    # this changes what mode actually gets set on the rig, not just a
    # display/UI preference. See sync/engine.py's
    # _reverse_sync_data_mode_for() for the actual per-backend mode
    # strings and their sourcing.
    force_ssb_to_data_mode: bool = False
    mute_on_tx: bool = True
    # GUI rewrite (spec §5.3): purely a display-gate today -- the strip's
    # TX VFO readout mirrors RX VFO regardless (no real split/second-VFO
    # rig polling exists), but when this is off the TX VFO label dims and
    # relabels to "TX VFO (not synced)". Kept as a real, persisted
    # setting even though it has no functional effect yet, since it's a
    # user-visible display preference in its own right.
    sync_tx_vfo: bool = True
    # GUI rewrite (spec §5.3): both inert until the compact-bar/undock
    # feature itself is built (out of scope for the pixel-perfect
    # rewrite) -- persisted now so the Behaviour panel's field list
    # matches the spec exactly and the values are ready for undock to
    # read once it exists.
    hide_receiver_when_undocked: bool = False
    keep_compact_bar_on_top: bool = True
    # Despite the name, this does NOT use Chromium's real --headless mode --
    # that has no audio output at all (confirmed: neither the lightweight
    # headless-shell binary nor the full binary's own headless mode play
    # sound). Instead sync/engine.py launches a normal headed browser
    # positioned far off-screen when this is True, keeping real audio intact
    # while showing no visible window.
    headless: bool = False
    use_mock_rig: bool = False
    # How often the sync loop polls rigctld and pushes to the WebSDR, in
    # seconds. Default matches the previously-hardcoded
    # sync/engine.py POLL_INTERVAL_S. Note this shares FREQ_DEBOUNCE_S's
    # 0.2s window (sync/engine.py) -- an interval at or above that makes
    # the debounce satisfiable in a single tick, so the GUI clamps this to
    # a sane range (MIN_POLL_INTERVAL_S/MAX_POLL_INTERVAL_S below) rather
    # than letting a user pick a value that defeats jitter filtering by
    # accident. That range is also enforced here at load() time, not just
    # in the GUI widget -- a hand-edited 0 or negative value would
    # otherwise reach asyncio.wait_for(timeout=...) as a busy loop.
    poll_interval_s: float = 0.2
    # Reverse sync (WebSDR -> rig, v11) frequency safety bound. None means
    # unrestricted on that side -- either can be set independently (e.g.
    # min_hz alone rejects anything below it with no upper bound). Any
    # frequency the WebSDR page reports outside this range is rejected
    # rather than written to the rig; see sync/engine.py's
    # _reverse_sync_tick(). Default unrestricted on both, matching every
    # other reverse-sync safety control in this app (Hold toggle
    # defaults off too) -- this is an opt-in guard, not a forced-on
    # restriction, since a sane default range doesn't exist across every
    # rig/band/country this app might be used in.
    reverse_sync_min_hz: Optional[int] = None
    reverse_sync_max_hz: Optional[int] = None
    # Minutes of no rig activity (no frequency/mode/PTT change at all)
    # after which the WebSDR session is disconnected, releasing the audio
    # slot back to what is usually a volunteer-run public receiver with a
    # small number of concurrent listeners. None or <= 0 disables it
    # entirely (hold the connection forever, the pre-v14 behavior). The
    # session reconnects automatically the moment the rig is touched
    # again -- see sync/engine.py's _tick(). Defaults to ON (unlike the
    # reverse-sync guards, which default off): the cost of being wrong
    # here is a few seconds of reconnect delay, while the cost of the
    # old always-on behavior is borne by someone else's hardware.
    websdr_idle_disconnect_min: Optional[int] = 60
    # Remembers the main control-panel window's own POSITION across
    # restarts. See gui/app.py's _restore_main_window_geometry(). None
    # means "never saved yet" (fresh install, or an old config from
    # before this existed); a saved position that no longer lands on any
    # connected display (e.g. a docking station's monitor unplugged
    # since it was saved) is also treated as absent.
    #
    # No matching main_window_size: this window has a fixed default/min
    # size (theme.FRAME_SIZE/FRAME_MIN_SIZE) and is otherwise genuinely
    # user-resizable, with ReceiverHost (proportion=1) absorbing any
    # extra space -- nothing here needs remembering across restarts.
    main_window_position: Optional[list] = None
    # Set when the user checks "Don't show this again for this version"
    # on the startup update-available popup (see update_check.py /
    # gui/app.py's _apply_update_check_result). Compared against the
    # *specific* latest tag each check, not a bare bool -- so dismissing
    # v2.1.0 silences only v2.1.0; the popup returns the moment a newer
    # release ships.
    dismissed_update_version: Optional[str] = None
    # Linux/WebKitGTK-only: some WebSDR families gate their own audio
    # behind a real trusted click on a specific page control (a browser
    # engine requirement, not something a documented control API can
    # bypass -- see websdr_org.py's/kiwisdr.py's/openwebrx.py's/
    # ubersdr.py's own audio-unlock code for the specifics of each).
    # sdrsync satisfies this by moving the real OS mouse cursor and
    # clicking (wx.UIActionSimulator) -- default on, since without it
    # audio silently never starts on Linux, but user-visible/disruptive
    # enough (it moves your actual mouse) that it's an opt-out, not just
    # a fait accompli. No effect on Windows/WebView2, which never gates
    # audio behind a click in the first place (sdrsync's own
    # --autoplay-policy flag there -- see browser/backend.py).
    auto_click_audio_unlock: bool = True

    @classmethod
    def load(cls) -> "AppSettings":
        if not CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning(
                    "Ignoring %s: expected a JSON object at the top level, got %s; using defaults",
                    CONFIG_FILE, type(data).__name__,
                )
                return cls()
            known_fields = {f for f in cls.__dataclass_fields__}
            filtered = {k: v for k, v in data.items() if k in known_fields}
            _validate_scalars(filtered)
            _validate_site_list(filtered, "user_sites")
            _validate_site_list(filtered, "imported_sites")
            _validate_site_list(filtered, "curated_sites")
            _validate_rig_backend(filtered)
            _clamp_poll_interval(filtered)
            _clamp_reverse_sync_range(filtered)
            _clamp_idle_disconnect(filtered)
            _validate_main_window_position(filtered)
            return cls(**filtered)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("Could not load %s (%s); using defaults", CONFIG_FILE, e)
            return cls()

    def save(self) -> None:
        """Write-temp-then-replace so a crash mid-write can't truncate the
        real config file -- os.replace() is atomic on both Windows and
        POSIX as long as source/dest are on the same volume, which they
        always are here (same directory)."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(asdict(self), indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, CONFIG_FILE)
        except OSError as e:
            logger.warning("Could not save settings to %s (%s)", CONFIG_FILE, e)


# Expected scalar type per AppSettings field that a hand-edited config.json
# could plausibly get wrong (e.g. a quoted port number). Deliberately a
# hardcoded map rather than introspecting dataclass field types, since
# `from __future__ import annotations` turns those into unevaluated strings.
# Extend this alongside any new scalar AppSettings field.
_SCALAR_TYPES: dict[str, "type | tuple[type, ...]"] = {
    "rigctld_host": str,
    "rigctld_port": int,
    "flrig_host": str,
    "flrig_port": int,
    "rig_backend": str,
    "last_site_url": str,
    "last_site_driver_type": str,
    "cw_offset_hz": int,
    "transceiver_cw_pitch_hz": int,
    "force_ssb_to_data_mode": bool,
    "mute_on_tx": bool,
    "sync_tx_vfo": bool,
    "hide_receiver_when_undocked": bool,
    "keep_compact_bar_on_top": bool,
    "headless": bool,
    "use_mock_rig": bool,
    "auto_click_audio_unlock": bool,
    "poll_interval_s": (int, float),
    "reverse_sync_min_hz": (int, type(None)),
    "reverse_sync_max_hz": (int, type(None)),
    "websdr_idle_disconnect_min": (int, type(None)),
    "dismissed_update_version": (str, type(None)),
}

RIG_BACKENDS = {"rigctld", "flrig"}


def _type_name(expected_type: "type | tuple[type, ...]") -> str:
    if isinstance(expected_type, tuple):
        return " or ".join(t.__name__ for t in expected_type)
    return expected_type.__name__


def _clamp_poll_interval(filtered: dict[str, Any]) -> None:
    """Clamp a valid-but-out-of-range poll_interval_s (e.g. a hand-edited
    0 or negative value) into [MIN_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S]
    -- an unclamped non-positive value reaches asyncio.wait_for(timeout=...)
    in sync/engine.py's poll loop as a busy loop. Runs after
    _validate_scalars, so by this point the value (if present) is known
    to be an int/float, not e.g. a string."""
    if "poll_interval_s" not in filtered:
        return
    value = filtered["poll_interval_s"]
    clamped = min(max(value, MIN_POLL_INTERVAL_S), MAX_POLL_INTERVAL_S)
    if clamped != value:
        logger.warning(
            "Clamping poll_interval_s in %s from %r to %r (allowed range [%s, %s])",
            CONFIG_FILE, value, clamped, MIN_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S,
        )
        filtered["poll_interval_s"] = clamped


def clamp_reverse_sync_bounds(
    min_hz: Optional[int], max_hz: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    """Pure sanity-check of a reverse-sync min/max Hz pair: takes the two
    bounds, returns the corrected pair. Shared by _clamp_reverse_sync_range()
    below (a hand-edited config.json) and gui/app.py's range entry fields
    (typed input), so the two layers enforcing this safety bound can't
    drift apart.

    A NEGATIVE bound can never be satisfied by a real frequency, so it
    would silently reject every reverse-sync push forever with no way for
    the user to tell why -- that one bound is dropped to None
    (unrestricted) rather than left as a value nothing can pass.

    An INVERTED range (min > max, both otherwise valid) is SWAPPED, not
    dropped. This is deliberately the opposite correction direction from
    _clamp_poll_interval's fail-open philosophy, because the risk is
    reversed: for poll_interval_s a too-tight value breaks the app, so
    the safe direction is permissive; here LOOSENING is the unsafe
    direction, since this range bounds what a public WebSDR page may
    retune a real transmitter to. Resetting both bounds to None would
    silently leave NO guard at all -- with only a log warning nobody
    reads -- while the user still believes reverse sync is confined to,
    say, HF. An honest transposition (30000000/1800000 for a meant
    1.8-30 MHz) is overwhelmingly the likely cause, so swapping keeps
    *a* range enforced instead of quietly removing the guard."""
    if min_hz is not None and min_hz < 0:
        logger.warning("Ignoring negative reverse-sync minimum %r; unrestricted on that side", min_hz)
        min_hz = None
    if max_hz is not None and max_hz < 0:
        logger.warning("Ignoring negative reverse-sync maximum %r; unrestricted on that side", max_hz)
        max_hz = None
    if min_hz is not None and max_hz is not None and min_hz > max_hz:
        logger.warning(
            "Reverse-sync range %r..%r is inverted -- swapping to %r..%r rather than dropping the guard",
            min_hz, max_hz, max_hz, min_hz,
        )
        min_hz, max_hz = max_hz, min_hz
    return min_hz, max_hz


def _clamp_reverse_sync_range(filtered: dict[str, Any]) -> None:
    """Apply clamp_reverse_sync_bounds() to a loaded config dict, in
    place. Runs after _validate_scalars, so by this point each bound (if
    present) is already known to be an int or None. Only writes a key
    back when its value actually changed, so an absent key stays absent
    and falls through to the dataclass default."""
    min_hz = filtered.get("reverse_sync_min_hz")
    max_hz = filtered.get("reverse_sync_max_hz")
    new_min, new_max = clamp_reverse_sync_bounds(min_hz, max_hz)
    if new_min != min_hz:
        filtered["reverse_sync_min_hz"] = new_min
    if new_max != max_hz:
        filtered["reverse_sync_max_hz"] = new_max


def clamp_idle_disconnect_min(value: Optional[int]) -> Optional[int]:
    """Pure sanity-check of websdr_idle_disconnect_min: takes the value,
    returns the corrected one. Shared by _clamp_idle_disconnect() below (a
    hand-edited config.json) and gui/app.py's entry field (typed input),
    so the two layers can't drift apart -- same split as
    clamp_reverse_sync_bounds().

    A NEGATIVE value is dropped to None (feature OFF), which is the
    DELIBERATELY OPPOSITE correction direction from
    clamp_reverse_sync_bounds()'s "keep *a* guard rather than silently
    remove it": there, loosening was the unsafe direction (that range
    bounds what a public page may do to a real transmitter). Here the
    unsafe direction is the other way round -- a bad value read as
    "disconnect more eagerly" would tear down a working WebSDR session
    under the user mid-QSO, whereas failing to idle-disconnect only
    restores the app's previous (merely impolite) behavior. Fail toward
    "off"."""
    if value is not None and value < 0:
        logger.warning(
            "Ignoring negative websdr_idle_disconnect_min %r; idle disconnect disabled", value
        )
        return None
    return value


def _clamp_idle_disconnect(filtered: dict[str, Any]) -> None:
    """Apply clamp_idle_disconnect_min() to a loaded config dict, in place.
    Runs after _validate_scalars, so the value (if present) is already
    known to be an int or None. Only writes the key back when the value
    actually changed, so an absent key stays absent and falls through to
    the dataclass default."""
    if "websdr_idle_disconnect_min" not in filtered:
        return
    value = filtered["websdr_idle_disconnect_min"]
    clamped = clamp_idle_disconnect_min(value)
    if clamped != value:
        filtered["websdr_idle_disconnect_min"] = clamped


def _validate_rig_backend(filtered: dict[str, Any]) -> None:
    """Drop an unrecognized rig_backend value back to the default. Runs
    after _validate_scalars, so by this point the value (if present) is
    already known to be a str -- this only needs to check set membership,
    not type, mirroring _clamp_poll_interval's layering."""
    if "rig_backend" not in filtered:
        return
    value = filtered["rig_backend"]
    if value not in RIG_BACKENDS:
        logger.warning(
            "Ignoring invalid rig_backend %r in %s (expected one of %s); using default",
            value, CONFIG_FILE, sorted(RIG_BACKENDS),
        )
        del filtered["rig_backend"]


def _validate_scalars(filtered: dict[str, Any]) -> None:
    """Drop (in place) any scalar field whose JSON value has the wrong
    type, so a bad value falls back to the dataclass default instead of
    silently sitting in the field with the wrong type (dataclasses don't
    validate types at construction time)."""
    for key, expected_type in _SCALAR_TYPES.items():
        if key not in filtered:
            continue
        value = filtered[key]
        is_valid = isinstance(value, bool) if expected_type is bool else (
            isinstance(value, expected_type) and not isinstance(value, bool)
        )
        if not is_valid:
            logger.warning(
                "Ignoring invalid value for %r in %s (expected %s, got %r); using default",
                key, CONFIG_FILE, _type_name(expected_type), value,
            )
            del filtered[key]


def _validate_site_entry(entry: Any) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    name, url, driver_type = entry.get("name"), entry.get("url"), entry.get("driver_type")
    if not (isinstance(name, str) and name and isinstance(url, str) and url
            and isinstance(driver_type, str) and driver_type):
        return None
    return {"name": name, "url": url, "driver_type": driver_type}


def _validate_site_list(filtered: dict[str, Any], key: str) -> None:
    """Drop (in place) any malformed entry in the given site-list field
    (user_sites/imported_sites/curated_sites -- all the same dict shape)
    rather than crashing downstream (gui/app.py does unguarded dict access
    on these). This is a lenient, shape-only check (non-empty strings),
    the same tier for all three fields -- the stricter check (driver_type
    actually registered, name/URL collisions) belongs at fetch/import time
    in sitesource.py, not here, since re-validating already-persisted data
    against DRIVERS on every load() could silently delete entries if
    DRIVERS ever changed shape, and config.py must stay free of the
    websdr/registry -> browser_shim -> wx import chain. Note: since each
    bucket is rewritten wholesale from in-memory objects on save, a
    skipped entry here is silently dropped on the app's next save --
    logged at WARNING, not DEBUG, so that's visible to the user."""
    if key not in filtered:
        return
    raw = filtered[key]
    if not isinstance(raw, list):
        logger.warning("Ignoring invalid %s value in %s (expected a list): %r", key, CONFIG_FILE, raw)
        del filtered[key]
        return
    valid_sites = []
    for entry in raw:
        validated = _validate_site_entry(entry)
        if validated is None:
            logger.warning("Skipping malformed %s entry in %s: %r", key, CONFIG_FILE, entry)
        else:
            valid_sites.append(validated)
    filtered[key] = valid_sites


def _validate_xy_pair_entry(value: Any) -> Optional[list[int]]:
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        return None
    x, y = value
    is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
    if not (is_int(x) and is_int(y)):
        return None
    return [x, y]


def _validate_main_window_position(filtered: dict[str, Any]) -> None:
    if "main_window_position" not in filtered:
        return
    value = filtered["main_window_position"]
    if value is None:
        return
    pos = _validate_xy_pair_entry(value)
    if pos is None:
        logger.warning("Ignoring malformed main_window_position in %s: %r", CONFIG_FILE, value)
        del filtered["main_window_position"]
    else:
        filtered["main_window_position"] = pos


def find_site_by_url(url: str) -> WebSDRSite | None:
    for site in KNOWN_SITES:
        if site.url == url:
            return site
    return None

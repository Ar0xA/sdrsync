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
    mute_on_tx: bool = True
    # v11: WebSDR page -> rig sync (the reverse of this app's original
    # rig -> WebSDR design). Default off -- a new rig-affecting behavior
    # must never silently activate for existing users on upgrade. PTT is
    # never reverse-synced regardless of this setting (see sync/engine.py).
    bidirectional_sync_enabled: bool = False
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
    "mute_on_tx": bool,
    "bidirectional_sync_enabled": bool,
    "headless": bool,
    "use_mock_rig": bool,
    "poll_interval_s": (int, float),
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


def find_site_by_url(url: str) -> WebSDRSite | None:
    for site in KNOWN_SITES:
        if site.url == url:
            return site
    return None

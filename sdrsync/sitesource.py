"""Fetch/load a WebSDR site list from a local file, an arbitrary URL, or
the maintainer-curated GitHub list, for the "Manage sites..." dialog.

Kept separate from preflight.py (scoped there to reachability checks, not
data fetching) and from config.py (a dependency-free leaf module that must
not import sdrsync.websdr.registry, since registry -> driver modules ->
browser_shim -> wx). This module already needs GUI-adjacent context
(called from the site manager dialog), so depending on registry here is
normal, not a layering violation -- and this is where the *strict*
external-data validation belongs: driver_type must be an actually-
registered driver, and name/URL collisions with the app's existing sites
are rejected. That's one tier stricter than config.py's lenient,
shape-only check for already-persisted data -- the trust boundary for
external data is at fetch time, not every app startup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from sdrsync.config import WebSDRSite
from sdrsync.gui_messages import GuiMessage
from sdrsync.websdr.registry import DRIVERS

logger = logging.getLogger("sdrsync.sitesource")

CURATED_LIST_URL = "https://raw.githubusercontent.com/Ar0xA/sdrsync/master/sites/websdr_sites.json"
FETCH_TIMEOUT_S = 5.0
# A site list is a handful of small JSON objects -- this is a generous cap
# against a misbehaving/malicious URL, not a realistic expected size.
MAX_RESPONSE_BYTES = 1_000_000


@dataclass
class SiteListFetchResult(GuiMessage):
    """Published on the status_queue after a background Load-from-URL or
    Update-from-GitHub fetch (never for the synchronous local-file load,
    which is small enough it doesn't need to hide latency behind a
    thread)."""
    bucket: str  # "imported" or "curated"
    sites: Optional[list[dict]]
    message: str


def _validate_entry(entry: Any, seen_names: set[str], seen_urls: set[str]) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    name, url, driver_type = entry.get("name"), entry.get("url"), entry.get("driver_type")
    if not (isinstance(name, str) and name and isinstance(url, str) and url
            and isinstance(driver_type, str) and driver_type):
        return None
    if driver_type not in DRIVERS:
        return None
    if name in seen_names or url in seen_urls:
        return None
    return {"name": name, "url": url, "driver_type": driver_type}


def validate_site_list(raw: Any, existing_sites: Sequence[WebSDRSite]) -> list[dict]:
    """Strict validator for an externally-sourced site list (a file, a
    URL, or the curated GitHub list). Rejects (skip + log, same pattern
    config.py's validators use) any entry with an unregistered
    driver_type, or whose name/url collides with existing_sites (the
    caller's KNOWN_SITES + user_sites) or with an earlier entry in the
    same batch -- lookup throughout gui/app.py is by name/url via
    next(...), which silently takes the first match, so an external list
    colliding with an existing entry could cause "select site X, connect
    to site Y". Returns [] rather than raising on a malformed top-level
    value."""
    if not isinstance(raw, list):
        logger.warning("Ignoring invalid site list (expected a list): %r", raw)
        return []
    seen_names = {s.name for s in existing_sites}
    seen_urls = {s.url for s in existing_sites}
    valid: list[dict] = []
    for entry in raw:
        validated = _validate_entry(entry, seen_names, seen_urls)
        if validated is None:
            logger.warning("Skipping invalid or colliding site list entry: %r", entry)
            continue
        valid.append(validated)
        seen_names.add(validated["name"])
        seen_urls.add(validated["url"])
    return valid


def load_site_list_from_file(path: str, existing_sites: Sequence[WebSDRSite]) -> tuple[Optional[list[dict]], str]:
    """Synchronous and GUI-thread-safe -- a local file is small, no need
    to hide latency behind a thread the way the URL variants below do."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"Could not read {path} ({e})"
    return _finalize(raw, existing_sites, str(path))


def _fetch_body_sync(url: str, timeout: float) -> tuple[Optional[str], str]:
    """Own fetch helper -- deliberately not a reuse of
    preflight._http_get_body_sync, which collapses every failure to a
    bare None (can't distinguish "offline" from a real 404, which matters
    here for confirming the curated JSON was actually pushed to GitHub)
    and has no response-size cap (fine for the app's own fixed
    reachability checks, not fine for a user-pasted URL)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return None, f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes, refusing to parse"
            return body.decode(errors="replace"), ""
    except urllib.error.HTTPError as e:
        return None, f"{url} returned HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"Could not reach {url} ({e.reason if hasattr(e, 'reason') else e})"


async def fetch_site_list(
    url: str, existing_sites: Sequence[WebSDRSite], timeout: float = FETCH_TIMEOUT_S,
) -> tuple[Optional[list[dict]], str]:
    """Async wrapper around a blocking urllib call via run_in_executor,
    matching preflight.py's check_websdr_url shape -- callers dispatch
    this via asyncio.run(...) on a background thread (the same
    threading.Thread + status_queue + GuiMessage pattern as
    _on_detect_clicked), never directly on the GUI thread."""
    loop = asyncio.get_running_loop()
    body, err = await loop.run_in_executor(None, _fetch_body_sync, url, timeout)
    if body is None:
        return None, err
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"{url} did not return valid JSON ({e})"
    return _finalize(raw, existing_sites, url)


def _finalize(raw: Any, existing_sites: Sequence[WebSDRSite], source: str) -> tuple[Optional[list[dict]], str]:
    """Shared result-shaping for both load_site_list_from_file and
    fetch_site_list. Deliberately distinguishes three outcomes rather than
    collapsing "raw was itself empty" and "every entry got rejected" into
    the same None-with-a-message result: a maintainer deliberately
    emptying the curated list is a valid, successful outcome (should
    clear the bucket, e.g. via curated_sites = []), not an error -- the
    original version conflated `not sites` for both cases, so a
    genuinely-empty upstream list could never actually clear
    curated_sites and always reported a spurious failure."""
    if not isinstance(raw, list):
        return None, f"{source} did not contain a JSON list"
    if not raw:
        return [], f"{source} has an empty site list"
    sites = validate_site_list(raw, existing_sites)
    if not sites:
        return None, f"No valid sites found at {source} (all {len(raw)} entries were rejected)"
    return sites, f"Loaded {len(sites)} site(s) from {source}"

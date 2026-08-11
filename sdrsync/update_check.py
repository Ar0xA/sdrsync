"""Checks GitHub's latest release against the running version, for the
startup update-available popup (gui/app.py). Deliberately synchronous
(a single small GET, no async needed) -- run on its own background
thread the same one-shot-network-check-then-status_queue pattern as
preflight.py/sitesource.py, just without needing an asyncio wrapper.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from sdrsync.gui_messages import GuiMessage

logger = logging.getLogger("sdrsync.update_check")

LATEST_RELEASE_API_URL = "https://api.github.com/repos/Ar0xA/sdrsync/releases/latest"
RELEASES_PAGE_URL = "https://github.com/Ar0xA/sdrsync/releases"
CHECK_TIMEOUT_S = 5.0


@dataclass
class UpdateCheckResult(GuiMessage):
    """Published on the status_queue after the background startup
    check. Both fields are None whenever there's nothing to show --
    check failed (offline, GitHub down, rate-limited) or the running
    version is already current/newer. Deliberately collapses those two
    cases: neither is worth surfacing to the user, so the caller only
    ever needs one truthy/falsy check, not a reason."""
    latest_version: Optional[str]
    release_url: Optional[str]


def _parse_version(v: str) -> tuple[int, ...]:
    """"v2.1.0" / "2.1.0" -> (2, 1, 0). A non-numeric trailing part
    (e.g. a "-rc1" pre-release suffix) has its digits taken and the
    rest dropped rather than raising, so a maintainer's tag that isn't
    strictly X.Y.Z can't crash the check -- worst case it just compares
    as if the suffix weren't there."""
    parts = []
    for p in v.lstrip("vV").split("."):
        digits = ""
        for ch in p:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return _parse_version(candidate) > _parse_version(current)


def check_for_update(current_version: str, timeout: float = CHECK_TIMEOUT_S) -> UpdateCheckResult:
    """Blocking -- call from a background thread only. Never raises:
    any failure (network, malformed response) is logged at debug level
    and reported as "nothing to show", the same as "already up to
    date" -- a failed version check is not itself something the user
    needs to see."""
    none_result = UpdateCheckResult(latest_version=None, release_url=None)
    # GitHub's API rejects requests with no User-Agent outright.
    req = urllib.request.Request(
        LATEST_RELEASE_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "SDRSync-update-check"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode(errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug("Update check failed (non-fatal): %s", e)
        return none_result
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return none_result
    if not is_newer(tag, current_version):
        return none_result
    url = data.get("html_url")
    return UpdateCheckResult(latest_version=tag, release_url=url if isinstance(url, str) and url else RELEASES_PAGE_URL)

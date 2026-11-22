"""Check GitHub for a newer release. Offline-tolerant by design.

Why this module is separate from the rest of the codebase
---------------------------------------------------------
Everything else in this project is strictly local: it reads agent logs and
never opens an outbound connection. Update checking is the single exception,
so it is isolated here (and in the one endpoint that calls it) rather than
sprinkled through the web layer. That keeps the "no network unless you look at
update status" claim auditable: grep for this module.

Policy
------
* **At most one request per day.** The result is cached next to the database
  with the timestamp that produced it; a fresh cache is served without touching
  the network. This is what makes the automatic startup check acceptable: one
  small request per day, not one per page load.
* **Failure is never fatal.** No network, DNS failure, timeout, rate limit,
  malformed JSON, GitHub outage -- every one returns a "could not check"
  payload. The dashboard must behave identically to a build with no network.
* **No redirects, no cookies, short timeout.** One GET, no tracking, nothing
  persisted from the response except the version tag and URL.
* **Explicit user agent.** GitHub's API rejects requests without one, and a
  named agent is the honest thing to send.

What is NOT sent: no machine ID, no usage data, no database contents, no
counts. The request is identical for every user -- it only asks "what is the
latest release tag".
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .paths import user_state_dir

logger = logging.getLogger("agent_token_radar.updates")

__all__ = [
    "RELEASES_API",
    "RELEASES_PAGE",
    "check_for_update",
    "parse_version",
    "is_newer",
    "read_cache",
    "clear_cache",
]

RELEASES_API = "https://api.github.com/repos/Zhucy123/agent-token-radar/releases/latest"
RELEASES_PAGE = "https://github.com/Zhucy123/agent-token-radar/releases/latest"

#: One request per day. The startup check reads this cache first, so opening the
#: dashboard ten times in an afternoon produces exactly one request.
CACHE_TTL_SECONDS = 24 * 60 * 60

#: Deliberately short: this runs on a background thread at startup and must
#: never make the app feel slow or hold a request open.
TIMEOUT_SECONDS = 6

#: GitHub requires a User-Agent. Sending the version is honest and lets the
#: server side tell builds apart if it ever matters.
def _user_agent(version: str) -> str:
    return f"agent-token-radar/{version} (+{RELEASES_PAGE})"


def _cache_path() -> Path:
    """Cache lives beside the database, never inside an agent's directory."""
    return user_state_dir() / "update-check.json"


def parse_version(tag: str) -> tuple[int, ...]:
    """``v0.0.2`` -> ``(0, 0, 2)``. Unparseable input yields ``()``.

    Deliberately tolerant: a tag like ``v1.2.3-beta.1`` takes the numeric
    prefix and ignores the pre-release suffix, because a beta should not be
    advertised as an update to a stable build.
    """
    if not tag:
        return ()
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", str(tag), re.IGNORECASE)
    if not m:
        return ()
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:  # pragma: no cover - regex already guarantees digits
        return ()


def is_newer(latest: str, current: str) -> bool:
    """True only when ``latest`` is a strictly higher version than ``current``.

    Compares numerically component by component and pads the shorter one, so
    ``0.0.10`` correctly beats ``0.0.9`` (string comparison would not).
    A pre-release tag is only newer if its numeric core is higher.
    """
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


def read_cache() -> dict[str, Any] | None:
    """The stored result, or ``None`` when missing/corrupt."""
    path = _cache_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_cache(payload: dict[str, Any]) -> None:
    """Best effort: a read-only install must still work, just without caching."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:  # pragma: no cover - depends on install layout
        logger.debug("could not write update cache", exc_info=True)


def clear_cache() -> None:
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass


def _fetch_latest(version: str) -> dict[str, Any]:
    """One GET to the GitHub releases API. Raises on any failure."""
    req = urllib.request.Request(
        RELEASES_API,
        headers={
            "User-Agent": _user_agent(version),
            "Accept": "application/vnd.github+json",
        },
    )
    # urlopen raises for non-2xx, so no status check is needed after the call.
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def check_for_update(
    current: str,
    *,
    force: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Return update status, using the daily cache unless ``force``.

    Parameters
    ----------
    current:
        The running version (``__version__``).
    force:
        Ignore the cache and ask GitHub now. Still writes the new result back,
        so a forced check restarts the 24h window.
    now:
        Injectable clock for tests.

    The return shape is stable and always JSON-serialisable. ``state`` is one
    of ``ok`` (we know the latest tag), ``up_to_date``, ``update_available``,
    ``unavailable`` (network or parse failure) -- the frontend never has to
    guess from missing keys.
    """
    stamp = time.time() if now is None else now
    cached = read_cache() if not force else None

    if cached and isinstance(cached.get("checked_at"), (int, float)):
        age = stamp - float(cached["checked_at"])
        # A clock that jumped backwards would otherwise disable checking for as
        # long as the skew lasts; treat a negative age as stale.
        if 0 <= age < CACHE_TTL_SECONDS and cached.get("latest_version"):
            return {**cached, "cached": True, "current_version": current}

    try:
        data = _fetch_latest(current)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, TimeoutError) as exc:
        # Every network/parse failure lands here. Return the last known good
        # answer if there is one, flagged as stale, so the UI can still show
        # something useful instead of an error box.
        logger.debug("update check failed: %s", exc)
        if cached and cached.get("latest_version"):
            return {**cached, "cached": True, "stale": True,
                    "current_version": current,
                    "state": "unavailable",
                    "reason": str(exc)[:200]}
        return {
            "state": "unavailable",
            "current_version": current,
            "reason": str(exc)[:200],
            "checked_at": stamp,
            "cached": False,
        }

    latest = str(data.get("tag_name") or "").strip()
    url = str(data.get("html_url") or RELEASES_PAGE).strip()
    if not latest:
        return {
            "state": "unavailable",
            "current_version": current,
            "reason": "no tag_name in response",
            "checked_at": stamp,
            "cached": False,
        }

    newer = is_newer(latest, current)
    payload = {
        "state": "update_available" if newer else "up_to_date",
        "current_version": current,
        "latest_version": latest,
        "latest_url": url,
        "published_at": data.get("published_at"),
        "release_name": data.get("name") or latest,
        "checked_at": stamp,
        "cached": False,
    }
    _write_cache(payload)
    return payload

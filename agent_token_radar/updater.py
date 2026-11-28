"""Apply an update in place: download the installer, run it, clean up.

Scope and limits
----------------
The installed build is a compiled PyInstaller bundle, not source, so "updating"
means running the vendor's own installer. There is no in-place patch to apply:
replacing a few .py files would do nothing, because those files do not exist in
the install directory -- they live inside the bundle.

What this can and cannot automate
---------------------------------
* **Download, launch, clean up**: fully automatic.
* **The elevation prompt**: NOT automatable. The installer writes to
  ``C:\\Program Files`` and declares ``PrivilegesRequired=admin``, so Windows
  shows a UAC dialog and waits for a human. Any claim otherwise would be a lie;
  the UI says so before the user clicks.
* **Restarting afterwards**: the installer closes the running app (it runs
  ``taskkill`` first) and relaunches it via its ``[Run]`` entry, so the app
  comes back on the new version by itself.

Safety
------
* The download goes to the per-user temp directory, never to the install
  directory (which a non-elevated process cannot write to anyway).
* The size from the release metadata is checked, so a truncated download is
  caught before anything is executed.
* Nothing is executed until the download has finished completely.
* The installer path is deleted on the next start as well, so a machine that
  was rebooted mid-update does not accumulate copies.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .paths import user_state_dir

logger = logging.getLogger("agent_token_radar.updater")

__all__ = [
    "is_installed_build",
    "download_installer",
    "launch_installer",
    "sweep_stale_downloads",
    "start_apply",
    "apply_state",
]

#: Long enough for an 18 MB installer on a slow line, short enough that a hung
#: connection does not pin a thread forever.
DOWNLOAD_TIMEOUT_SECONDS = 120

#: A release asset larger than this is not our installer; refuse rather than
#: fill the disk on a malformed response.
MAX_INSTALLER_BYTES = 200 * 1024 * 1024

_USER_AGENT = "agent-token-radar-updater/1 (+https://github.com/Zhucy123/agent-token-radar)"


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never onto a non-HTTPS URL.

    ``urlopen`` follows redirects by default. The scheme check on the original
    URL is not enough: a 302 to ``http://...`` would otherwise complete the
    download of an installer this process is about to execute.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not str(newurl).lower().startswith("https://"):
            raise UpdateError("redirect_not_https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(req: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler)
    return opener.open(req, timeout=timeout)


_lock = threading.Lock()
_state: dict[str, Any] = {"phase": "idle"}


def is_installed_build() -> bool:
    """True when running as the packaged desktop exe (not from a checkout)."""
    return bool(getattr(sys, "frozen", False))


def _download_dir() -> Path:
    """Where the installer is written. Per-user, so no elevation is needed."""
    base = user_state_dir() / "updates"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _installer_path() -> Path:
    return _download_dir() / "AgentTokenRadar-Setup.exe"


def sweep_stale_downloads() -> None:
    """Delete installers left behind by an interrupted update.

    Called at startup: if the app is running, any downloaded installer has
    either been used or was abandoned, and keeping it only wastes space.
    """
    try:
        path = _installer_path()
        if path.exists():
            path.unlink()
            logger.debug("removed stale installer %s", path)
    except OSError:  # pragma: no cover - depends on install layout
        logger.debug("could not remove stale installer", exc_info=True)


def download_installer(url: str, *, expected_size: int | None = None) -> dict[str, Any]:
    """Fetch the installer to the per-user download directory.

    Returns a dict with ``path`` and ``bytes``. Raises ``UpdateError`` on any
    failure so the caller can report a single, honest message.
    """
    if not url.lower().startswith("https://"):
        raise UpdateError("not_https")

    dest = _installer_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target, then move: a partial file must never be named
    # like a finished one, or a crash mid-download would look installable.
    tmp = dest.with_suffix(".part")
    digest = hashlib.sha256()
    written = 0

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            declared = resp.headers.get("Content-Length")
            limit = MAX_INSTALLER_BYTES
            if declared and declared.isdigit() and int(declared) > limit:
                raise UpdateError("too_large")
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise UpdateError("too_large")
                    digest.update(chunk)
                    fh.write(chunk)
    except UpdateError:
        # too_large is raised after some bytes have been written; a refused
        # redirect can also land here. The .part file must not linger either way.
        tmp.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError("download_failed", detail=str(exc)[:200]) from exc

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise UpdateError("empty_file")
    if expected_size and written != expected_size:
        # Not fatal on its own, but a mismatch usually means a proxy injected a
        # page or the transfer was cut short -- both worth refusing.
        tmp.unlink(missing_ok=True)
        raise UpdateError(
            "size_mismatch", expected=expected_size, actual=written)

    os.replace(tmp, dest)
    return {"path": str(dest), "bytes": written, "sha256": digest.hexdigest()}


def launch_installer(path: str) -> None:
    """Run the installer and let it take over.

    The installer elevates itself (PrivilegesRequired=admin), so this process
    does not try to elevate: that would show a second prompt for no reason. It
    also closes this app before copying files (its InitializeSetup runs
    taskkill), so we do not need to exit first -- but the caller should stop
    serving, because the process is about to be killed mid-request.
    """
    p = Path(path)
    if not p.is_file():
        raise UpdateError("missing_file")

    if os.name == "nt":
        # DETACHED_PROCESS: the installer must survive this process being
        # terminated by its own taskkill call, which would otherwise take the
        # child down with it.
        subprocess.Popen(
            [str(p)],
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    else:  # pragma: no cover - the installer is Windows-only
        subprocess.Popen([str(p)])


class UpdateError(Exception):
    """A user-facing update failure, identified by a language-independent code.

    The UI looks up ``code`` in the string table, so the same failure can be
    shown in Chinese or English. ``params`` are the template values for that
    string (byte counts, the underlying error, ...). ``str(self)`` is the
    code itself -- a last-resort detail, never a sentence in one language.
    """

    def __init__(self, code: str, **params: Any) -> None:
        self.code = code
        self.params = params
        super().__init__(code)


def apply_state() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _set(**kw: Any) -> None:
    with _lock:
        _state.update(kw)


def start_apply(installer_url: str, expected_size: int | None = None) -> dict[str, Any]:
    """Begin download+install on a daemon thread. Returns the initial state.

    Only one update may run at a time: a second click while the first is
    downloading would fight over the same file.
    """
    with _lock:
        if _state.get("phase") in ("downloading", "launching"):
            return dict(_state)
        _state.clear()
        _state.update({"phase": "downloading", "error": None})

    def _run() -> None:
        try:
            info = download_installer(installer_url, expected_size=expected_size)
            _set(phase="launching", path=info["path"], bytes=info["bytes"])
            launch_installer(info["path"])
            _set(phase="launched")
        except UpdateError as exc:
            _set(phase="failed", error=str(exc),
                 error_code=exc.code, error_params=exc.params)
        except Exception as exc:  # pragma: no cover - never kill the thread host
            logger.debug("update apply failed", exc_info=True)
            _set(phase="failed", error=str(exc)[:200])

    threading.Thread(target=_run, name="update-apply", daemon=True).start()
    return apply_state()

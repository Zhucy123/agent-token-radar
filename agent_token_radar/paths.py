"""Cross-platform, READ-ONLY path resolution.

All paths here are only ever *read*. Nothing in this module creates a file
inside a third-party agent's directory. The only directory this project ever
writes to is its own ``data/`` folder (see :func:`default_db_path`).

Windows / Linux conventions
---------------------------
Windows:  ``%USERPROFILE%`` , ``%APPDATA%`` , ``%LOCALAPPDATA%``
Linux:    ``$HOME`` , ``$XDG_CONFIG_HOME`` (``~/.config``) ,
          ``$XDG_DATA_HOME`` (``~/.local/share``)

Candidate roots are always returned as a *list*: the same tool may keep data
in several places (e.g. Cursor uses both Roaming and Local AppData), and an
adapter should merge them rather than pick one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "PROJECT_ROOT",
    "home",
    "config_dir",
    "local_data_dir",
    "agent_home",
    "agent_candidates",
    "default_db_path",
    "ensure_own_dirs",
    "user_state_dir",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def home() -> Path:
    """Best-effort user home on both Windows and Linux."""
    # Path.home() honours USERPROFILE on Windows and HOME on POSIX.
    try:
        return Path.home()
    except (RuntimeError, OSError):
        return Path(os.path.expanduser("~"))


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    return Path(raw).expanduser()


def config_dir() -> Path:
    """Roaming config root (``AppData/Roaming`` / ``$XDG_CONFIG_HOME``)."""
    if os.name == "nt":
        p = _env_path("APPDATA")
        if p:
            return p
    else:
        p = _env_path("XDG_CONFIG_HOME")
        if p:
            return p
        return home() / ".config"
    return home() / "AppData" / "Roaming"


def local_data_dir() -> Path:
    """Local data root (``AppData/Local`` / ``$XDG_DATA_HOME``)."""
    if os.name == "nt":
        p = _env_path("LOCALAPPDATA")
        if p:
            return p
    else:
        p = _env_path("XDG_DATA_HOME")
        if p:
            return p
        return home() / ".local" / "share"
    return home() / "AppData" / "Local"


def agent_home(
    dot_name: str,
    *,
    env: str | None = None,
    appdata_names: tuple[str, ...] = (),
) -> list[Path]:
    """Return every plausible home directory for one agent.

    Parameters
    ----------
    dot_name:
        Dot-directory under ``~`` (e.g. ``".codex"``).
    env:
        Environment override honoured by the tool (e.g. ``"DSH_HOME"``,
        ``"CODEX_HOME"``).
    appdata_names:
        Extra platform-specific dirs, e.g. ``("Codex",)`` under AppData.
    """
    out: list[Path] = []

    def add(p: Path | None) -> None:
        if p is not None and p not in out:
            out.append(p)

    if env:
        add(_env_path(env))
    add(home() / dot_name)
    for n in appdata_names:
        add(config_dir() / n)
        add(local_data_dir() / n)
    return out


def agent_candidates(
    dot_name: str,
    *,
    env: str | None = None,
    appdata_names: tuple[str, ...] = (),
    subpaths: tuple[str, ...] = (),
) -> list[Path]:
    """Convenience: agent homes joined with each relative subpath."""
    roots = agent_home(dot_name, env=env, appdata_names=appdata_names)
    if not subpaths:
        return roots
    return [r / s for r in roots for s in subpaths]


def _running_frozen() -> bool:
    """True inside a PyInstaller bundle (the Windows desktop exe).

    A frozen build resolves everything relative to ``sys._MEIPASS``, which lives
    inside the install directory -- typically ``Program Files``, where a normal
    user has no write access. Treat it like an installed package: user data
    belongs in the per-user state directory, not next to the executable.
    """
    return bool(getattr(sys, "frozen", False))


def _running_installed() -> bool:
    """True when this package was pip-installed rather than run from a checkout.

    Measured, not guessed: an installed package lives under a ``site-packages``
    (or ``dist-packages``) directory, while a checkout does not. A frozen bundle
    counts as installed for the same reason: its own directory is not ours to
    write to.
    """
    if _running_frozen():
        return True
    parts = {p.lower() for p in PROJECT_ROOT.parts}
    return bool(parts & {"site-packages", "dist-packages"})


def user_state_dir() -> Path:
    """Per-user directory this tool may write to, on any install layout."""
    if os.name == "nt":
        base = _env_path("LOCALAPPDATA") or (home() / "AppData" / "Local")
    else:
        base = _env_path("XDG_DATA_HOME") or (home() / ".local" / "share")
    return base / "agent-token-radar"


def default_db_path() -> Path:
    """The project's OWN database. Override with ``AGENT_TOKEN_RADAR_DB``."""
    p = _env_path("AGENT_TOKEN_RADAR_DB")
    if p:
        return p
    # Running from a checkout: keep the database beside the code so a clone is
    # self-contained and portable.
    if not _running_installed():
        return PROJECT_ROOT / "data" / "token_radar.db"
    # Installed via pip: ``PROJECT_ROOT`` is ``site-packages``, which is not a
    # writable place for user data (and a ``pip install --upgrade`` would delete
    # it). Use the platform's per-user data directory instead.
    return user_state_dir() / "token_radar.db"


def ensure_own_dirs() -> Path:
    """Create only OUR data directory. Never touches agent directories."""
    p = default_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

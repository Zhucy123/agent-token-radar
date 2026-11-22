"""Shared temp-directory helper for the agent-token-radar tests.

``tempfile.TemporaryDirectory`` builds its directory with mode ``0o700``. In
restricted Windows setups (and under the sandbox this project was verified in)
that mode lands as an ACL the creating process cannot then write into, so the
SQLite fixture fails with ``unable to open database file`` before any adapter
logic runs.

A plain ``mkdir`` of a unique name inherits the parent's permissions and works
everywhere. Tests must fail on the code under test, never on where they may put
a scratch file.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

#: Preferred parent: the project's own data directory (always writable by us).
_PROJECT_TMP = Path(__file__).resolve().parent.parent / "agent_token_radar" / "data" / "tmp"


def make_temp_dir(prefix: str = "atr_test_") -> Path:
    """Create and return a fresh writable directory; caller must clean it up."""
    roots = []
    try:
        _PROJECT_TMP.mkdir(parents=True, exist_ok=True)
        roots.append(_PROJECT_TMP)
    except OSError:
        pass
    roots.append(Path(__file__).resolve().parent)

    last: OSError | None = None
    for base in roots:
        for _ in range(8):
            candidate = base / f"{prefix}{uuid.uuid4().hex[:10]}"
            try:
                candidate.mkdir(mode=0o755)
            except FileExistsError:
                continue
            except OSError as e:  # noqa: PERF203 - tiny loop, clarity wins
                last = e
                break
            return candidate
    raise last or OSError("no writable temp directory")


def drop_temp_dir(path: Path | str | None) -> None:
    """Best-effort removal; a leftover scratch dir must never fail a test."""
    if not path:
        return
    shutil.rmtree(str(path), ignore_errors=True)

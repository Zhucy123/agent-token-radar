"""Small, dependency-free mapping helpers shared by the 5 adapters."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from ..base import UsageEvent


def safe_int(v: Any) -> int:
    """Coerce a token-ish value to a non-negative int; never raise."""
    try:
        n = int(v or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, n)


def first_int(d: Any, *keys: str) -> int:
    """Return the first integer found at ``d[key]`` among ``keys``."""
    if not isinstance(d, dict):
        return 0
    for k in keys:
        if k in d:
            n = safe_int(d[k])
            if n:
                return n
    return 0


def iso_to_ms(s: Any) -> int:
    """ISO8601 (``...Z`` or offset) -> UTC epoch milliseconds."""
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        n = int(s)
        # Heuristic: seconds vs milliseconds.
        return n * 1000 if 0 < n < 10_000_000_000 else n
    if not isinstance(s, str) or not s:
        return 0
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError):
        return 0


def seconds_to_ms(v: Any) -> int:
    """REAL epoch seconds (SQLite) -> UTC epoch milliseconds."""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return 0
    return int(f * 1000) if f > 0 else 0


#: DSH (and a few VS Code-derived tools) encode a path as a directory name by
#: replacing every non-alphanumeric character with ``~XXXX`` (its code point in
#: hex): ``D--UEProjects-Untitled~0020Game`` is ``D:\UEProjects\Untitled Game``.
#: The leading ``--`` is the drive/path separator marker.
_TILDE_ESCAPE = re.compile(r"~([0-9A-Fa-f]{4})")


def decode_dsh_project_dir(name: str) -> str:
    """Turn an encoded project directory name back into a readable path.

    The encoding replaces a path separator with a single ``-`` and every other
    special character with ``~XXXX``; a literal ``-`` in a name (as in
    ``deepseek-harness``) is written the same way, so it cannot be told apart
    from a separator. The result is therefore a **label**, normalised to forward
    slashes, and never fed back to the filesystem.

    ``--D-UEProjects-Untitled~0020Game--`` -> ``D:/UEProjects/Untitled Game``
    """
    if not name:
        return ""
    raw = name.strip()
    # Trailing ``--`` is the terminator, not part of the path.
    while raw.endswith("--"):
        raw = raw[:-2].rstrip("-")
    if not raw:
        return ""
    decoded = _TILDE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), raw)

    if decoded.startswith("--"):
        # ``--<drive>-<rest>`` marks an absolute Windows path.
        body = decoded[2:]
        head, sep, tail = body.partition("-")
        if sep and len(head) == 1 and head.isalpha():
            return f"{head}:/{tail.replace('-', '/')}".replace("//", "/")
    return decoded.replace("-", "/").replace("//", "/")


def project_label(cwd: Any) -> str:
    """Normalise a working directory into a stable project label.

    Every adapter records the same kind of value (an absolute path) but with
    different separators and case, so a single normalisation is what lets
    "the same project" mean one row instead of three. Only the label is
    normalised -- ``source_file`` keeps the real path.
    """
    if not isinstance(cwd, str):
        return ""
    text = cwd.strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    # Windows is case-insensitive and the agents disagree about drive case.
    if len(text) > 2 and text[1] == ":":
        text = text[0].upper() + text[1:]
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/") or "/"


def make_event(
    *,
    agent: str,
    event_key: str,
    source_file: str,
    model: str = "",
    provider: str = "",
    provider_source: str = "",
    served_model: str = "",
    session_id: str = "",
    project: str = "",
    timestamp: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    total_tokens: int = 0,
) -> UsageEvent:
    """Build a normalized UsageEvent; ``UsageEvent.normalized()`` fills total."""
    return UsageEvent(
        event_key=event_key,
        agent=agent,
        model=model,
        provider=provider,
        provider_source=provider_source,
        served_model=served_model,
        session_id=session_id,
        project=project,
        timestamp=timestamp,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        source_file=source_file,
    ).normalized()

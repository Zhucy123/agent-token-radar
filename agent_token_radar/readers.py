"""Strictly read-only primitives shared by adapters.

Robustness requirements from the spec, implemented once here:

* **SQLite**: third-party DBs are never opened in place. They are copied (DB +
  ``-wal`` + ``-shm``) into a temporary directory and opened with
  ``file:...?mode=ro`` so the owning application is never locked or blocked.
* **JSONL**: append-only, byte-offset incremental reads; malformed /
  incomplete lines are skipped with a warning and never abort the scan.
* **zstd JSONL**: DSH writes a concatenation of independent zstd frames.
  Python 3.14's stdlib ``compression.zstd.decompress`` decodes all frames at
  once; older runtimes fall back to manual frame splitting by magic number.
* **Everything is opened read-only** (``"rb"`` / ``"r"``); no writes, moves or
  deletes ever target an agent directory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .base import Checkpoint

logger = logging.getLogger("agent_token_radar.readers")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


# --------------------------------------------------------------------------
# JSONL
# --------------------------------------------------------------------------
def read_jsonl_incremental(
    path: Path | str,
    cp: Checkpoint,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], Checkpoint]:
    """Read a JSONL file starting at ``cp.byte_offset``.

    Returns ``(records, new_checkpoint)``. The new offset points at the last
    clean newline, so a half-written trailing line is retried next scan
    instead of being committed (or silently lost).

    A file that shrank (rotation / truncation) is re-read from byte 0.
    """
    p = Path(path)
    records: list[dict[str, Any]] = []
    warn = on_warning or (lambda m: logger.warning("%s", m))
    new_cp = Checkpoint(
        file_path=str(p),
        mtime=cp.mtime,
        byte_offset=cp.byte_offset,
        size=cp.size,
        updated_at=cp.updated_at,
    )

    try:
        st = p.stat()
    except OSError as e:
        warn(f"{p}: cannot stat ({e.__class__.__name__}); skipped")
        return records, new_cp

    new_cp.mtime = st.st_mtime
    new_cp.size = st.st_size

    if st.st_size < cp.byte_offset:
        warn(f"{p}: shrank ({cp.byte_offset} -> {st.st_size}); re-reading from 0")
        cp.byte_offset = 0
        new_cp.byte_offset = 0

    start = max(0, cp.byte_offset)
    try:
        with p.open("rb") as fh:
            fh.seek(start)
            raw = fh.read()
    except OSError as e:
        warn(f"{p}: cannot read ({e.__class__.__name__}); skipped")
        return records, new_cp

    consumed = 0
    for idx, line in enumerate(raw.splitlines(keepends=True)):
        if not line.endswith(b"\n"):
            # Incomplete final line: leave it for the next scan.
            warn(f"{p}: incomplete trailing JSON line at +{start + consumed}; deferred")
            break
        consumed += len(line)
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeError) as e:
            warn(f"{p}: malformed JSON line {idx} (+{start + consumed - len(line)}): {e}; skipped")
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            warn(f"{p}: JSON line {idx} is not an object; skipped")

    new_cp.byte_offset = start + consumed
    new_cp.touch(byte_offset=new_cp.byte_offset)
    # touch() re-stats, which may have advanced past what we read; that's fine,
    # but keep the exact consumed offset as the authoritative cursor.
    new_cp.byte_offset = start + consumed
    return records, new_cp


def read_jsonl_full(
    path: Path | str,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Read a whole JSONL file, skipping malformed lines with warnings."""
    p = Path(path)
    warn = on_warning or (lambda m: logger.warning("%s", m))
    out: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    warn(f"{p}: malformed JSON line {idx}: {e}; skipped")
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
                else:
                    warn(f"{p}: JSON line {idx} is not an object; skipped")
    except OSError as e:
        warn(f"{p}: cannot read ({e.__class__.__name__}); skipped")
    return out


def read_json_file(
    path: Path | str,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> Any | None:
    warn = on_warning or (lambda m: logger.warning("%s", m))
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        warn(f"{path}: cannot read JSON ({e.__class__.__name__}); skipped")
        return None


# --------------------------------------------------------------------------
# zstd-framed JSONL (DSH)
# --------------------------------------------------------------------------
def _zstd_decode_one(blob: bytes) -> bytes:
    """Decode one (possibly multi-frame) zstd blob with whichever backend is
    available. Returns plain bytes; raises on failure so callers can warn."""
    try:
        from compression import zstd as _std  # Python 3.14+ stdlib

        return _std.decompress(blob)
    except ImportError:
        pass
    try:
        import zstandard as _pkg  # optional third-party on 3.11-3.13

        return _pkg.ZstdDecompressor().decompress(blob, max_output_size=256 * 1024 * 1024)
    except ImportError:
        pass
    raise RuntimeError(
        "no zstd decoder available: use Python 3.14+ (stdlib compression.zstd) "
        "or `pip install zstandard`"
    )


def _decompress_frames_manual(data: bytes, warn: Callable[[str], None]) -> bytes:
    """Fallback: split concatenated frames by magic and decode each one.

    Used when a whole-buffer decode fails (e.g. a torn final frame): good
    frames are still recovered, bad ones are skipped with a warning.
    """
    offsets: list[int] = []
    pos = 0
    while True:
        i = data.find(ZSTD_MAGIC, pos)
        if i < 0:
            break
        offsets.append(i)
        pos = i + len(ZSTD_MAGIC)

    if not offsets:
        return b""

    parts: list[bytes] = []
    for j, start in enumerate(offsets):
        end = offsets[j + 1] if j + 1 < len(offsets) else len(data)
        try:
            chunk = _zstd_decode_one(data[start:end])
            if chunk and not chunk.endswith(b"\n"):
                chunk += b"\n"  # prevent adjacent frames from gluing lines
            parts.append(chunk)
        except Exception as e:  # noqa: BLE001 - one bad frame must not abort
            warn(f"bad zstd frame #{j} at byte {start}: {e.__class__.__name__}; skipped")
    return b"".join(parts)


def decompress_zstd_frames(
    data: bytes,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> bytes:
    """Decode a concatenation of zstd frames into plain bytes.

    DSH writes one independent frame per append batch. A whole-buffer decode
    can silently GLUE the last line of one frame to the first line of the next
    when a frame's payload lacks its trailing newline (measured on a real v2
    log: 526 correct lines vs 515 glued ones). We therefore decode frame by
    frame and re-insert a newline at every boundary. That also recovers all
    good frames when the tail frame is torn.
    """
    warn = on_warning or (lambda m: logger.warning("%s", m))
    if not data:
        return b""
    if not data.startswith(ZSTD_MAGIC):
        # Uncompressed ``.jsonl`` configuration: pass through untouched.
        return data
    # Per-frame decoding is the authoritative path; only fall back to a
    # whole-buffer decode if the magic scan finds nothing (single frame).
    out = _decompress_frames_manual(data, warn)
    if out:
        return out
    try:
        whole = _zstd_decode_one(data)
        if whole and not whole.endswith(b"\n"):
            whole += b"\n"
        return whole
    except Exception as e:  # noqa: BLE001
        warn(f"zstd decode failed ({e.__class__.__name__}); no data recovered")
        return b""


def read_zstd_jsonl_full(
    path: Path | str,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Decode a DSH-style multi-frame zstd session log into JSON objects.

    Whole-file re-read is intentional: zstd frame boundaries cannot be derived
    from a byte offset, and DSH may truncate/re-encode the tail in place after
    a crash. Idempotency comes from ``event_key`` + UPSERT, not from offsets.
    """
    p = Path(path)
    warn = on_warning or (lambda m: logger.warning("%s", m))
    try:
        data = p.read_bytes()
    except OSError as e:
        warn(f"{p}: cannot read ({e.__class__.__name__}); skipped")
        return []
    text = decompress_zstd_frames(data, on_warning=warn)
    out: list[dict[str, Any]] = []
    # Split on LF ONLY. ``str.splitlines()`` also breaks on U+0085 (NEL),
    # U+2028/2029 and friends -- and agent logs legitimately contain those
    # characters inside JSON strings (measured: a real DSH session had 66 NEL,
    # which splitlines turned into 68 unparsable "lines"). The record separator
    # of a JSONL file is a single \n, nothing else.
    for idx, line in enumerate(text.decode("utf-8", errors="replace").split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            warn(f"{p}: malformed JSON line {idx}: {e}; skipped")
            continue
        if isinstance(obj, dict):
            out.append(obj)
        else:
            warn(f"{p}: JSON line {idx} is not an object; skipped")
    return out


# --------------------------------------------------------------------------
# SQLite (read-only copy)
# --------------------------------------------------------------------------
def _temp_working_dir(prefix: str = "atr_ro_") -> Path:
    """A writable scratch directory for the read-only copy.

    Our own ``data/tmp`` comes first: it is the one directory this project owns,
    and the system temp dir is not reliably writable (sandboxed shells, locked
    down Windows profiles, read-only ``%TEMP%``).

    ``tempfile.mkdtemp`` is deliberately NOT used for the name: it creates the
    directory mode ``0o700``, which some sandboxes and restrictive ACL setups
    then refuse to write into. A plain ``mkdir`` of a unique name inherits the
    parent's permissions and works everywhere we have seen.
    """
    from .paths import PROJECT_ROOT  # local import: avoids a cycle at import time

    bases: list[Path] = []
    try:
        own = PROJECT_ROOT / "data" / "tmp"
        own.mkdir(parents=True, exist_ok=True)
        bases.append(own)
    except OSError:
        pass
    bases.append(Path(tempfile.gettempdir()))

    last: OSError | None = None
    for base in bases:
        for _ in range(8):
            candidate = base / f"{prefix}{uuid.uuid4().hex[:12]}"
            try:
                candidate.mkdir(mode=0o755)
            except FileExistsError:
                continue
            except OSError as e:
                last = e
                break
            return candidate
    raise last or OSError("no writable scratch directory")


@contextmanager
def readonly_sqlite_copy(
    path: Path | str,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> Iterator[sqlite3.Connection | None]:
    """Context manager yielding a connection to a READ-ONLY temp copy.

    Usage::

        with readonly_sqlite_copy(db_path) as con:
            rows = con.execute("SELECT ...").fetchall()

    The original file (and its ``-wal`` / ``-shm``) is never opened for
    writing, never locked, and the temp copy is deleted on exit. A corrupt
    copy yields nothing rather than raising into the scan loop.
    """
    p = Path(path)
    warn = on_warning or (lambda m: logger.warning("%s", m))
    try:
        tmpdir = _temp_working_dir()
    except OSError as e:
        warn(f"{p}: no writable scratch directory ({e.__class__.__name__}: {e}); skipped")
        yield None  # type: ignore[misc]
        return
    tmp = Path(tmpdir) / "copy.db"
    con: sqlite3.Connection | None = None
    try:
        shutil.copyfile(p, tmp)
        for suffix in ("-wal", "-shm"):
            src = Path(str(p) + suffix)
            if src.exists():
                try:
                    shutil.copyfile(src, Path(str(tmp) + suffix))
                except OSError:
                    pass  # WAL is best-effort; the main DB is authoritative
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        yield con
    except Exception as e:  # noqa: BLE001
        warn(f"{p}: read-only SQLite copy failed ({e.__class__.__name__}: {e}); skipped")
        yield None  # type: ignore[misc]
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)

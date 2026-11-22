"""SQLite-backed adapters: OpenCode, MiMoCode, ZCode, Devin, MiniMax, Qoder IDE.

These agents store usage in a local SQLite database. Per the project's hard
rule, every one of them is read through :func:`readonly_sqlite_copy` -- the
original file is copied to a temp dir and opened with ``mode=ro`` +
``query_only``, so the owning application is never locked or modified.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import agent_candidates, config_dir, home, local_data_dir
from ..readers import readonly_sqlite_copy
from ._common import glob_sources
from ._helpers import make_event, safe_int
from ._usagekit import TokenBuckets, epoch_to_ms, nested


# ---------------------------------------------------------------------------
# OpenCode / MiMoCode -- identical schema, identical parser
# ---------------------------------------------------------------------------
_OPENCODE_INPUT = ("input",)
_OPENCODE_OUTPUT = ("output",)


class _OpenCodeFamilyAdapter(BaseAdapter):
    """OpenCode's ``session_message`` / ``message`` tables (also used by MiMoCode).

    Usage lives in a JSON blob:
        tokens: { input, output, reasoning, cache: { read, write } }
    ``input`` here INCLUDES cache reads (OpenCode convention).
    """

    db_names: tuple[str, ...] = ("opencode.db",)

    def _db_roots(self) -> list[Path]:
        return []

    def detect(self) -> list[Source]:
        out: list[Source] = []
        seen: set[str] = set()
        for root in self._db_roots():
            root = Path(root)
            if not root.is_dir():
                continue
            for name in self.db_names:
                p = root / name
                if p.is_file():
                    k = str(p.resolve()).lower()
                    if k not in seen:
                        seen.add(k)
                        out.append(Source(path=p, kind="sqlite", agent=self.name))
            # Channel builds: opencode-<channel>.db
            for p in root.glob("*.db"):
                if p.name in self.db_names:
                    continue
                if not p.is_file():
                    continue
                k = str(p.resolve()).lower()
                if k not in seen and p.stem.startswith(self.db_names[0].split(".")[0]):
                    seen.add(k)
                    out.append(Source(path=p, kind="sqlite", agent=self.name))
        return out

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        warn = result.warnings.append

        for src in self.detect():
            key = str(src.path)
            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                rows: list[tuple[Any, ...]] = []
                # V2 schema first, then V1.
                try:
                    rows = con.execute(
                        "SELECT id, session_id, data, time_created FROM session_message "
                        "WHERE type='assistant'"
                    ).fetchall()
                    schema = 2
                except Exception:  # noqa: BLE001
                    try:
                        rows = con.execute(
                            "SELECT id, session_id, data, time_created FROM message"
                        ).fetchall()
                        schema = 1
                    except Exception as e:  # noqa: BLE001
                        warn(f"{key}: no known message table ({e.__class__.__name__}); skipped")
                        continue

                import json as _json

                for row in rows:
                    raw = row[2]
                    if not isinstance(raw, (str, bytes, bytearray)):
                        continue
                    try:
                        data = _json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("role") != "assistant":
                        continue
                    tokens = data.get("tokens")
                    if not isinstance(tokens, dict) or not tokens:
                        continue

                    created = nested(data, "time", "created") or row[3]
                    ts = epoch_to_ms(created)
                    if not ts:
                        continue

                    buckets = TokenBuckets(
                        input=safe_int(tokens.get("input")),
                        output=safe_int(tokens.get("output")),
                        cache_read=safe_int(nested(tokens, "cache", "read")),
                        cache_write=safe_int(nested(tokens, "cache", "write")),
                        reasoning=safe_int(tokens.get("reasoning")),
                    )
                    if buckets.is_empty():
                        continue

                    model = data.get("modelID") or nested(data, "model", "id") or ""
                    sid = str(row[1] or data.get("sessionID") or "")
                    mid = str(row[0] or data.get("id") or "")

                    result.events.append(make_event(
                        agent=self.name,
                        event_key=f"{self.name}|{mid}",
                        source_file=key,
                        model=str(model),
                        session_id=sid,
                        timestamp=ts,
                        **dict(zip(
                            ("input_tokens", "output_tokens", "cache_read_tokens",
                             "cache_write_tokens", "reasoning_tokens"),
                            buckets.inclusive(),
                        )),
                    ))

            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result


class OpenCodeAdapter(_OpenCodeFamilyAdapter):
    name = "opencode"
    display_name = "OpenCode"
    env_override = "TOKEI_OPENCODE_DATA_DIR"

    def _db_roots(self) -> list[Path]:
        import os
        roots: list[Path] = []
        for env in ("TOKEI_OPENCODE_DATA_DIR", "OPENCODE_DATA_DIR"):
            if os.environ.get(env):
                roots.append(Path(os.environ[env]).expanduser())
        roots += [
            local_data_dir() / "opencode",
            home() / ".local" / "share" / "opencode",
            config_dir() / "opencode",
        ]
        return roots


class MiMoCodeAdapter(_OpenCodeFamilyAdapter):
    name = "mimocode"
    display_name = "MiMoCode"
    env_override = "TOKEI_MIMOCODE_DB"
    db_names = ("mimocode.db",)

    def _db_roots(self) -> list[Path]:
        import os
        roots: list[Path] = []
        if os.environ.get("TOKEI_MIMOCODE_DB"):
            p = Path(os.environ["TOKEI_MIMOCODE_DB"]).expanduser()
            roots.append(p.parent)
        roots += [
            local_data_dir() / "mimocode",
            config_dir() / "mimocode",
            home() / ".local" / "share" / "mimocode",
        ]
        return roots


# ---------------------------------------------------------------------------
# ZCode -- model_usage table
# ---------------------------------------------------------------------------
class ZCodeAdapter(BaseAdapter):
    name = "zcode"
    display_name = "ZCode"
    env_override = "TOKEI_ZCODE_DB"

    def detect(self) -> list[Source]:
        import os
        if os.environ.get("TOKEI_ZCODE_DB"):
            p = Path(os.environ["TOKEI_ZCODE_DB"]).expanduser()
            return [Source(path=p, kind="sqlite", agent=self.name)] if p.is_file() else []
        return glob_sources([home() / ".zcode" / "cli" / "db"], "*.sqlite",
                            kind="sqlite", agent=self.name)

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        warn = result.warnings.append

        for src in self.detect():
            key = str(src.path)
            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                try:
                    rows = con.execute(
                        "SELECT id, session_id, model_id, input_tokens, output_tokens, "
                        "reasoning_tokens, cache_creation_input_tokens, "
                        "cache_read_input_tokens, started_at, completed_at "
                        "FROM model_usage ORDER BY started_at ASC"
                    ).fetchall()
                except Exception as e:  # noqa: BLE001
                    warn(f"{key}: model_usage unavailable ({e.__class__.__name__}); skipped")
                    continue

                for r in rows:
                    inp = safe_int(r["input_tokens"])
                    out = safe_int(r["output_tokens"])
                    reason = safe_int(r["reasoning_tokens"])
                    cw = safe_int(r["cache_creation_input_tokens"])
                    cr = safe_int(r["cache_read_input_tokens"])
                    if not (inp or out or reason or cw or cr):
                        continue

                    ts = epoch_to_ms(r["completed_at"]) or epoch_to_ms(r["started_at"])
                    if not ts:
                        continue

                    buckets = TokenBuckets(
                        input=inp, output=out, cache_read=cr,
                        cache_write=cw, reasoning=reason,
                    )
                    result.events.append(make_event(
                        agent=self.name,
                        event_key=f"{self.name}|{r['id']}",
                        source_file=key,
                        model=str(r["model_id"] or ""),
                        session_id=str(r["session_id"] or ""),
                        timestamp=ts,
                        **dict(zip(
                            ("input_tokens", "output_tokens", "cache_read_tokens",
                             "cache_write_tokens", "reasoning_tokens"),
                            buckets.inclusive(),
                        )),
                    ))

            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result


# ---------------------------------------------------------------------------
# Devin -- message_nodes table with a chat_message JSON payload
# ---------------------------------------------------------------------------
class DevinAdapter(BaseAdapter):
    name = "devin"
    display_name = "Devin"
    env_override = "TOKEI_DEVIN_CLI_DB"

    def detect(self) -> list[Source]:
        import os
        if os.environ.get("TOKEI_DEVIN_CLI_DB"):
            p = Path(os.environ["TOKEI_DEVIN_CLI_DB"]).expanduser()
            return [Source(path=p, kind="sqlite", agent=self.name)] if p.is_file() else []
        roots = [
            local_data_dir() / "devin" / "cli",
            home() / ".local" / "share" / "devin" / "cli",
        ]
        return glob_sources(roots, "sessions.db", kind="sqlite", agent=self.name)

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        warn = result.warnings.append

        for src in self.detect():
            key = str(src.path)
            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                try:
                    rows = con.execute(
                        "SELECT n.session_id, n.chat_message, n.created_at, s.model "
                        "FROM message_nodes n LEFT JOIN sessions s ON s.id = n.session_id"
                    ).fetchall()
                except Exception as e:  # noqa: BLE001
                    warn(f"{key}: message_nodes unavailable ({e.__class__.__name__}); skipped")
                    continue

                import json as _json
                seen: set[tuple] = set()

                for r in rows:
                    raw = r["chat_message"]
                    if not isinstance(raw, (str, bytes, bytearray)):
                        continue
                    try:
                        cm = _json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    metrics = nested(cm, "metadata", "metrics")
                    if not isinstance(metrics, dict):
                        continue

                    inp = safe_int(metrics.get("input_tokens"))
                    out = safe_int(metrics.get("output_tokens"))
                    cr = safe_int(metrics.get("cache_read_tokens"))
                    cw = safe_int(metrics.get("cache_creation_tokens"))
                    if not (inp or out or cr or cw):
                        continue

                    created = nested(cm, "metadata", "created_at") or r["created_at"]
                    ts = epoch_to_ms(created)
                    if not ts and isinstance(created, str):
                        from ._helpers import iso_to_ms
                        ts = iso_to_ms(created)
                    if not ts:
                        continue

                    model = nested(cm, "metadata", "generation_model") or r["model"] or ""
                    # Devin writes each reply twice into sibling nodes.
                    # ``sig`` elements MUST stay plain str/int: repr() of a
                    # tuple of those is exact and identical in every process
                    # (verified under PYTHONHASHSEED 0/1/12345/random), which
                    # is what makes the digest below stable. Putting a dict,
                    # set, float or arbitrary object in here would make repr()
                    # order- or format-dependent and silently reintroduce the
                    # unstable-key bug. Values are already str()/int()-coerced
                    # by the query layer, so this holds today.
                    sig = (r["session_id"], ts, model, inp, out, cr, cw)
                    if sig in seen:
                        continue
                    seen.add(sig)

                    result.events.append(make_event(
                        agent=self.name,
                        # Stable across processes: Python's hash() of a string
                        # is randomised per interpreter (PYTHONHASHSEED), so the
                        # old abs(hash(sig)) gave a DIFFERENT event_key on every
                        # run. The UPSERT then never matched the row written by
                        # the previous scan and re-inserted the whole file, so
                        # repeated scans multiplied this agent's usage.
                        # sha256 over the repr is stable and gives a fixed-width,
                        # collision-safe key (no abs(), no truncation of a
                        # negative number).
                        event_key=(
                            f"{self.name}|"
                            f"{hashlib.sha256(repr(sig).encode()).hexdigest()[:20]}"
                        ),
                        source_file=key,
                        model=str(model),
                        session_id=str(r["session_id"] or ""),
                        timestamp=ts,
                        input_tokens=inp, output_tokens=out,
                        cache_read_tokens=cr, cache_write_tokens=cw,
                    ))

            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result


# ---------------------------------------------------------------------------
# MiniMax Code -- local_runtime_token_usage
# ---------------------------------------------------------------------------
class MiniMaxAdapter(BaseAdapter):
    name = "minimax"
    display_name = "MiniMax Code"
    env_override = "TOKEI_MINIMAX_DB"

    def detect(self) -> list[Source]:
        import os
        if os.environ.get("TOKEI_MINIMAX_DB"):
            p = Path(os.environ["TOKEI_MINIMAX_DB"]).expanduser()
            return [Source(path=p, kind="sqlite", agent=self.name)] if p.is_file() else []
        return glob_sources(
            [home() / ".minimax" / "v2" / "sqlite"],
            "runtime-state.sqlite", kind="sqlite", agent=self.name,
        )

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        warn = result.warnings.append
        import json as _json

        for src in self.detect():
            key = str(src.path)
            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                try:
                    rows = con.execute(
                        "SELECT id, session_id, ts, input_tokens, output_tokens, "
                        "reasoning_tokens, cache_read_tokens, cache_write_tokens, raw "
                        "FROM local_runtime_token_usage"
                    ).fetchall()
                except Exception as e:  # noqa: BLE001
                    warn(f"{key}: local_runtime_token_usage unavailable "
                         f"({e.__class__.__name__}); skipped")
                    continue

                for r in rows:
                    inp = safe_int(r["input_tokens"])
                    out = safe_int(r["output_tokens"])
                    cr = safe_int(r["cache_read_tokens"])
                    cw = safe_int(r["cache_write_tokens"])
                    reason = safe_int(r["reasoning_tokens"])
                    if not (inp or out or cr or cw or reason):
                        continue

                    # Only trust reasoning when the provider's own total proves
                    # it is NOT already inside output.
                    raw = r["raw"]
                    if reason and isinstance(raw, (str, bytes, bytearray)):
                        try:
                            total = safe_int((_json.loads(raw) or {}).get("totalTokens"))
                        except (ValueError, TypeError):
                            total = 0
                        if total != inp + out + cr + cw + reason:
                            reason = 0
                    elif reason:
                        reason = 0

                    ts = epoch_to_ms(r["ts"])
                    if not ts:
                        continue

                    result.events.append(make_event(
                        agent=self.name,
                        event_key=f"{self.name}|{r['id']}",
                        source_file=key,
                        model=str(r["model"] or "") if "model" in r.keys() else "",
                        session_id=str(r["session_id"] or ""),
                        timestamp=ts,
                        input_tokens=inp, output_tokens=out,
                        cache_read_tokens=cr, cache_write_tokens=cw,
                        reasoning_tokens=reason,
                    ))

            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result


# ---------------------------------------------------------------------------
# Qoder IDE / Desktop -- chat_message.token_info JSON columns
# ---------------------------------------------------------------------------
class QoderIdeAdapter(BaseAdapter):
    name = "qoder-ide"
    display_name = "Qoder Desktop"
    env_override = "TOKEI_QODER_IDE_DB"

    def _db_candidates(self) -> list[Path]:
        import os
        if os.environ.get("TOKEI_QODER_IDE_DB"):
            return [Path(os.environ["TOKEI_QODER_IDE_DB"]).expanduser()]
        rel = Path("SharedClientCache") / "cache" / "db" / "local.db"
        return [
            config_dir() / "Qoder" / rel,
            local_data_dir() / "Qoder" / rel,
            home() / ".qoder" / rel,
        ]

    def detect(self) -> list[Source]:
        return [Source(path=p, kind="sqlite", agent=self.name)
                for p in self._db_candidates() if p.is_file()]

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        warn = result.warnings.append
        import json as _json

        for src in self.detect():
            key = str(src.path)
            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                try:
                    rows = con.execute(
                        "SELECT id, session_id, request_id, token_info, model_info, gmt_create "
                        "FROM chat_message WHERE token_info IS NOT NULL AND token_info != ''"
                    ).fetchall()
                except Exception as e:  # noqa: BLE001
                    warn(f"{key}: chat_message unavailable ({e.__class__.__name__}); skipped")
                    continue

                for r in rows:
                    try:
                        ti = _json.loads(r["token_info"])
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(ti, dict):
                        continue
                    inp = safe_int(ti.get("prompt_tokens"))
                    out = safe_int(ti.get("completion_tokens"))
                    cr = safe_int(ti.get("cached_tokens"))
                    if not (inp or out or cr):
                        continue

                    model = ""
                    if isinstance(r["model_info"], (str, bytes, bytearray)):
                        try:
                            model = str((_json.loads(r["model_info"]) or {}).get("model_key") or "")
                        except (ValueError, TypeError):
                            model = ""

                    ts = epoch_to_ms(r["gmt_create"])
                    if not ts:
                        continue

                    result.events.append(make_event(
                        agent=self.name,
                        event_key=f"{self.name}|{r['id']}",
                        source_file=key,
                        model=model,
                        session_id=str(r["session_id"] or ""),
                        timestamp=ts,
                        input_tokens=inp, output_tokens=out,
                        cache_read_tokens=cr,
                    ))

            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result

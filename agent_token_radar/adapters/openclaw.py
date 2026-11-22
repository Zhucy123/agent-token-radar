"""OpenClaw adapter.

Data lives in a registry SQLite database that points at per-agent databases::

    ~/.openclaw/state/openclaw.sqlite          registry (agent_databases)
    <agent db>                                 transcript_events table

Each ``transcript_events`` row holds a JSON event whose body may be stored either
as plain text (``event_json``) OR zstd-compressed bytes (``event_zstd``, a
concatenation of frames). Python 3.14's stdlib zstd handles the multi-frame
case, reusing :func:`agent_token_radar.readers.decompress_zstd_frames`.

The usage record is:: 

    event.message.role == "assistant" &&
    event.message.usage is a dict
      usage: { input, output, cacheRead, cacheWrite, reasoningTokens }
      model: message.responseModel -> message.model -> session_windows.model

Legacy installs keep ``agents/*/sessions/*.jsonl`` instead -- also supported.

event_key = ``openclaw|<session_id>|<event id or content hash>``
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import home
from ..readers import decompress_zstd_frames, read_jsonl_incremental
from ._helpers import make_event
from ._usagekit import epoch_flexible, nested

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class OpenClawAdapter(BaseAdapter):
    name = "openclaw"
    display_name = "OpenClaw"
    env_override = "TOKEI_OPENCLAW_DB"

    def _state_dir(self) -> Path:
        if os.environ.get("OPENCLAW_STATE_DIR"):
            return Path(os.environ["OPENCLAW_STATE_DIR"]).expanduser()
        if self.env_override and os.environ.get(self.env_override):
            return Path(os.environ[self.env_override]).expanduser().parent.parent
        return home() / ".openclaw"

    def _registry(self) -> Path:
        if self.env_override and os.environ.get(self.env_override):
            return Path(os.environ[self.env_override]).expanduser()
        return self._state_dir() / "state" / "openclaw.sqlite"

    def _agent_dbs(self) -> list[Path]:
        """Resolve per-agent DB paths from the registry (read-only copy)."""
        from ..readers import readonly_sqlite_copy

        reg = self._registry()
        root = self._state_dir()
        out: list[Path] = []
        if not reg.is_file():
            # Fall back to any sqlite under state/.
            state = root / "state"
            if state.is_dir():
                out.extend(p for p in state.glob("*.sqlite") if p.is_file())
            return out

        with readonly_sqlite_copy(reg, on_warning=lambda m: None) as con:
            if con is None:
                return out
            try:
                rows = con.execute("SELECT path FROM agent_databases").fetchall()
            except Exception:  # noqa: BLE001
                return out
            for r in rows:
                raw = r[0]
                if not isinstance(raw, str) or not raw:
                    continue
                p = Path(raw)
                cand = p if p.is_absolute() else (root / p)
                if cand.is_file():
                    out.append(cand)
        return out

    def _legacy_jsonl(self) -> list[Path]:
        root = self._state_dir() / "agents"
        if not root.is_dir():
            return []
        return [p for p in root.glob("*/sessions/*.jsonl")
                if p.is_file() and not p.name.endswith(".trajectory.jsonl")]

    def detect(self) -> list[Source]:
        out: list[Source] = []
        for db in self._agent_dbs():
            out.append(Source(path=db, kind="sqlite", agent=self.name))
        for jl in self._legacy_jsonl():
            out.append(Source(path=jl, kind="jsonl", agent=self.name))
        return out

    # -- event decoding ------------------------------------------------------

    @staticmethod
    def _decode_row(row: Any) -> dict[str, Any] | None:
        """Row -> event dict, handling both plain and zstd-framed bodies."""
        event_json = None
        event_zstd = None
        try:
            event_json = row["event_json"]
            event_zstd = row["event_zstd"]
        except (IndexError, KeyError):
            return None

        if isinstance(event_json, (str, bytes, bytearray)) and event_json:
            try:
                obj = json.loads(event_json)
                return obj if isinstance(obj, dict) else None
            except (ValueError, TypeError):
                return None
        if isinstance(event_zstd, (bytes, bytearray)) and event_zstd:
            blob = bytes(event_zstd)
            plain = (
                decompress_zstd_frames(blob, on_warning=lambda m: None)
                if blob.startswith(_ZSTD_MAGIC)
                else blob
            )
            try:
                obj = json.loads(plain)
                return obj if isinstance(obj, dict) else None
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _parse_body(obj: dict[str, Any]) -> tuple[str, int, int, int, int, int, str, str] | None:
        """Return (sid, ts, in, out, cr, cw, reason, model) or None."""
        event = obj.get("event") if isinstance(obj.get("event"), dict) else obj
        msg = event.get("message") if isinstance(event.get("message"), dict) else None
        if msg is None:
            return None
        if msg.get("role") != "assistant":
            return None
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None

        inp = int(usage.get("input") or 0)
        out = int(usage.get("output") or 0)
        cr = int(usage.get("cacheRead") or usage.get("cache_read") or 0)
        cw = int(usage.get("cacheWrite") or usage.get("cache_write") or 0)
        reason = int(usage.get("reasoningTokens") or usage.get("reasoning") or 0)
        if not (inp or out or cr or cw or reason):
            return None

        ts = (epoch_flexible(event.get("timestamp"))
              or epoch_flexible(msg.get("timestamp"))
              or epoch_flexible(obj.get("created_at")))
        if not ts:
            return None

        model = (msg.get("responseModel") or msg.get("model") or "")
        sid = str(obj.get("session_id") or event.get("session_id") or "")
        return sid, ts, inp, out, cr, cw, reason, str(model)

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        from ..readers import readonly_sqlite_copy

        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append

        for src in self.detect():
            key = str(src.path)

            if src.kind == "jsonl":
                cp = cps.get(key) or Checkpoint.for_path(src.path)
                records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
                result.checkpoints[key] = new_cp
                for rec in records:
                    parsed = self._parse_body(rec)
                    if not parsed:
                        continue
                    sid, ts, inp, out, cr, cw, reason, model = parsed
                    blob = json.dumps(rec, sort_keys=True, default=str)[:4000]
                    ekey = f"{self.name}|{sid}|h" + hashlib.sha256(blob.encode()).hexdigest()[:24]
                    result.events.append(make_event(
                        agent=self.name, event_key=ekey, source_file=key,
                        model=model, session_id=sid, timestamp=ts,
                        input_tokens=inp, output_tokens=out,
                        cache_read_tokens=cr, cache_write_tokens=cw,
                        reasoning_tokens=reason,
                    ))
                continue

            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                try:
                    rows = con.execute(
                        "SELECT * FROM transcript_events"
                    ).fetchall()
                except Exception as e:  # noqa: BLE001
                    warn(f"{key}: transcript_events unavailable "
                         f"({e.__class__.__name__}); skipped")
                    continue

                for row in rows:
                    obj = self._decode_row(row)
                    if obj is None:
                        continue
                    parsed = self._parse_body(obj)
                    if not parsed:
                        continue
                    sid, ts, inp, out, cr, cw, reason, model = parsed
                    eid = obj.get("id") or obj.get("event_id")
                    if not isinstance(eid, str) or not eid:
                        eid = "h" + hashlib.sha256(
                            json.dumps(obj, sort_keys=True, default=str)[:4000].encode()
                        ).hexdigest()[:24]
                    ekey = f"{self.name}|{sid}|{eid}"
                    result.events.append(make_event(
                        agent=self.name, event_key=ekey, source_file=key,
                        model=model, session_id=sid, timestamp=ts,
                        input_tokens=inp, output_tokens=out,
                        cache_read_tokens=cr, cache_write_tokens=cw,
                        reasoning_tokens=reason,
                    ))

            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result

"""Kimi Code adapter.

Session layout (two protocol generations):

    protocol 1.5:  sessions/<workdir-hash>/<sid>/agents/<agent>/wire.jsonl
    protocol 1.0:  sessions/<workdir-hash>/<sid>/wire.jsonl

Records:

    {"type":"usage.record","time":<epoch ms>,"model":"...",
     "usage":{"inputOther":..,"output":..,"inputCacheRead":..,"inputCacheCreation":..}}

    {"type":"StatusUpdate","timestamp":...,
     "payload":{"token_usage":{"input_other":..,"output":..,
                               "input_cache_read":..,"input_cache_creation":..},
                "message_id":"..."}}

``inputOther`` / ``input_other`` is the FRESH input (already excludes cache),
so buckets are additive. No reasoning bucket exists.

Quirk: the root ``wire.jsonl`` of a session mirrors its agent wires. To avoid
double counting we skip the root file whenever agent wires exist for the same
session directory, which is exactly what tokei's multiset subtraction achieves.

event_key = ``kimi|<session>|<time>|<model>|<tokens-hash>``
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ._jsonl_base import JsonlAdapter
from ._usagekit import epoch_to_ms
from ..base import Source


def _emit_key(sid: str, ts: int, model: str, b: tuple[int, int, int, int, int],
              mid: str | None = None) -> str:
    if mid:
        return f"kimi|{sid}|m|{mid}"
    blob = f"{ts}|{model}|{b[0]}|{b[1]}|{b[2]}|{b[3]}"
    return "kimi|" + hashlib.sha256(blob.encode()).hexdigest()[:28]


class KimiCodeAdapter(JsonlAdapter):
    name = "kimi"
    display_name = "Kimi Code"
    env_override = "KIMI_CODE_HOME"
    roots = ((".kimi-code", ("sessions",)), (".kimi", ("sessions",)))
    pattern = "wire.jsonl"

    def detect(self) -> list[Source]:
        import os

        roots: list[Path] = []
        for env in ("TOKEI_KIMI_DIR", "KIMI_CODE_HOME", "KIMI_SHARE_DIR"):
            if os.environ.get(env):
                roots.append(Path(os.environ[env]).expanduser() / "sessions")
        from ..paths import home

        roots += [home() / ".kimi-code" / "sessions", home() / ".kimi" / "sessions"]

        out: list[Source] = []
        seen: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for p in root.glob("**/wire.jsonl"):
                if not p.is_file():
                    continue
                # Skip the session-root mirror when the session has agent wires.
                session_dir = p.parent
                if p.parent.name.startswith("session-") or (
                    (session_dir / "agents").is_dir()
                ):
                    continue
                k = str(p.resolve()).lower()
                if k not in seen:
                    seen.add(k)
                    out.append(Source(path=p, kind="jsonl", agent=self.name))
        return out

    def parse_record(
        self, rec: dict[str, Any], *, source: Path, state: dict[str, Any]
    ) -> Any:
        rtype = rec.get("type")

        # --- protocol 1.5 ---------------------------------------------------
        if rtype == "usage.record":
            u = rec.get("usage")
            if not isinstance(u, dict):
                return None
            b = (
                int(u.get("inputOther") or 0),
                int(u.get("output") or 0),
                int(u.get("inputCacheRead") or 0),
                int(u.get("inputCacheCreation") or 0),
                0,
            )
            if not any(b):
                return None
            model = str(rec.get("model") or "")
            ts = epoch_to_ms(rec.get("time")) or epoch_to_ms(rec.get("timestamp"))
            sid = self._sid(source, state)
            return self.emit(
                agent=self.name, event_key=_emit_key(sid, ts, model, b),
                source=source, model=model, session_id=sid, timestamp=ts, buckets=b,
            )

        # --- protocol 1.0 ---------------------------------------------------
        if rtype == "StatusUpdate" or rec.get("message", {}).get("type") == "StatusUpdate":
            payload = rec.get("payload") or (
                rec.get("message", {}).get("payload")
                if isinstance(rec.get("message"), dict) else None
            )
            if not isinstance(payload, dict):
                return None
            tu = payload.get("token_usage")
            if not isinstance(tu, dict):
                return None
            b = (
                int(tu.get("input_other") or 0),
                int(tu.get("output") or 0),
                int(tu.get("input_cache_read") or 0),
                int(tu.get("input_cache_creation") or 0),
                0,
            )
            if not any(b):
                return None
            model = str(rec.get("model") or payload.get("model") or "")
            ts = epoch_to_ms(rec.get("timestamp")) or epoch_to_ms(rec.get("time"))
            sid = self._sid(source, state)
            mid = payload.get("message_id")
            return self.emit(
                agent=self.name,
                event_key=_emit_key(sid, ts, model, b,
                                    mid if isinstance(mid, str) and mid else None),
                source=source, model=model, session_id=sid, timestamp=ts, buckets=b,
            )

        return None

    @staticmethod
    def _sid(source: Path, state: dict[str, Any]) -> str:
        if "sid" in state:
            return state["sid"]
        # .../<sid>/agents/<agent>/wire.jsonl  -> parent.parent.parent
        # .../<sid>/wire.jsonl                -> parent
        parts = source.parts
        sid = source.parent.name
        if "agents" in parts:
            idx = parts.index("agents")
            if idx >= 1:
                sid = parts[idx - 1]
        state["sid"] = sid
        return sid

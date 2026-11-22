"""Command Code adapter.

Records are message-shaped with a sticky model and independent token buckets:

    {"type":"session","id":...,"cwd":...,"model":...}
    {"type":"model_change","model":...}
    {"role":"assistant","type":"message","usage":{
         "inputTokens":..,"outputTokens":..,"cacheReadTokens":..,
         "cacheWriteTokens":..,"costUsd":..}}

Buckets are ADDITIVE here (no subtraction). There is no reasoning field, so
``reasoning_tokens`` stays 0 rather than being guessed from output.

event_key = ``cmdcode|<sha256(id + timestamp)>`` (falls back to a usage hash)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ._helpers import iso_to_ms
from ._jsonl_base import JsonlAdapter
from ._usagekit import epoch_flexible, pick_usage

_INPUT = ("inputTokens", "input_tokens", "input")
_OUTPUT = ("outputTokens", "output_tokens", "output")
_CACHE_R = ("cacheReadTokens", "cache_read_tokens", "cacheRead", "cache_read")
_CACHE_W = ("cacheWriteTokens", "cache_write_tokens", "cacheWrite", "cache_write")


class CommandCodeAdapter(JsonlAdapter):
    name = "cmdcode"
    display_name = "Command Code"
    env_override = "TOKEI_CMDCODE_DIR"
    roots = ((".commandcode", ("projects",)),)
    # sessions live one level deeper: projects/<proj>/*.jsonl
    pattern = "*.jsonl"

    def parse_record(
        self, rec: dict[str, Any], *, source: Path, state: dict[str, Any]
    ) -> Any:
        rtype = rec.get("type")

        if rtype == "session":
            if rec.get("id"):
                state["session"] = str(rec["id"])
            if rec.get("sessionId"):
                state["session"] = str(rec["sessionId"])
            if rec.get("model"):
                state["model"] = str(rec["model"])
            return None

        if rtype == "model_change":
            if rec.get("model"):
                state["model"] = str(rec["model"])
            return None

        # Command Code puts ``role`` at the top level on some versions and
        # inside ``message`` on others.
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        role = msg.get("role") or rec.get("role")
        if role != "assistant":
            return None
        if rtype not in (None, "message"):
            return None

        usage = rec.get("usage")
        if not isinstance(usage, dict):
            usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None

        buckets = pick_usage(
            usage, input_keys=_INPUT, output_keys=_OUTPUT,
            cache_read_keys=_CACHE_R, cache_write_keys=_CACHE_W,
        )
        if buckets.is_empty():
            return None

        model = rec.get("model") or msg.get("model") or state.get("model") or ""
        ts = epoch_flexible(rec.get("timestamp")) or epoch_flexible(msg.get("timestamp"))
        sid = str(rec.get("sessionId") or rec.get("session_id")
                  or state.get("session") or source.stem)

        rid = rec.get("id") or msg.get("id")
        if isinstance(rid, str) and rid:
            ekey = f"{self.name}|h" + hashlib.sha256(
                f"{rid}\0{ts}".encode()
            ).hexdigest()[:24]
        else:
            ekey = f"{self.name}|h" + hashlib.sha256(
                f"{ts}|{buckets.input}|{buckets.output}|{buckets.cache_read}".encode()
            ).hexdigest()[:24]

        return self.emit(
            agent=self.name, event_key=ekey, source=source,
            model=str(model), session_id=sid, timestamp=ts,
            buckets=buckets.additive(),
        )

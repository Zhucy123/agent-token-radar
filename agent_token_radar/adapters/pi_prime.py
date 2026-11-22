"""Pi Coding Agent CLI + Prime Agent adapters.

Both write the **same** JSONL shape (Prime explicitly reuses Pi's usage
record), so one parser serves both:

    {"type":"session",  "id":..., "cwd":...}              -> sticky session/project
    {"type":"model_change","provider":...,"modelId":...}  -> sticky model
    {"type":"message","message":{
         "role":"assistant",
         "usage":{"input":..,"output":..,"cacheRead":..,"cacheWrite":..,
                  "reasoning":..,"cost":{...}},
         "responseModel": "...", "model": "..."
    }}

``input`` here is INCLUSIVE of cache reads (Pi convention), so cache buckets
are subtracted. ``reasoning`` is a subset of ``output`` and is never added.

event_key = ``<agent>|<session_id>|<record id or line index>``
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ._helpers import iso_to_ms
from ._jsonl_base import JsonlAdapter
from ._usagekit import TokenBuckets, epoch_flexible, nested, pick_usage

_INPUT = ("input",)
_OUTPUT = ("output",)
_CACHE_R = ("cacheRead", "cache_read")
_CACHE_W = ("cacheWrite", "cache_write")
_REASON = ("reasoning", "reason", "reasoningTokens")


def _record_id(rec: dict[str, Any], msg: dict[str, Any], line_no: int) -> str:
    for cand in (msg.get("id"), rec.get("id"), msg.get("responseId"), rec.get("responseId")):
        if isinstance(cand, str) and cand:
            return cand
    blob = repr(sorted(rec.items()))[:2000]
    return "h" + hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:20]


class _PiFamilyAdapter(JsonlAdapter):
    """Shared parser for Pi / Prime; subclasses set ``name`` and roots."""

    def parse_record(
        self, rec: dict[str, Any], *, source: Path, state: dict[str, Any]
    ) -> Any:
        rtype = rec.get("type")

        if rtype == "session":
            if rec.get("id"):
                state["session"] = str(rec["id"])
            if rec.get("cwd"):
                state["project"] = str(rec["cwd"])
            return None

        if rtype == "model_change":
            prov = rec.get("provider")
            mid = rec.get("modelId") or rec.get("model")
            if mid:
                state["model"] = f"{prov}/{mid}" if prov else str(mid)
            return None

        if rtype != "message":
            return None

        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            return None
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None

        # Sticky model, refreshed whenever a record carries one.
        model = msg.get("responseModel") or msg.get("model")
        if isinstance(model, str) and model:
            prov = msg.get("provider")
            state["model"] = f"{prov}/{model}" if prov and "/" not in model else model
        model = str(state.get("model") or "")

        buckets = pick_usage(
            usage,
            input_keys=_INPUT,
            output_keys=_OUTPUT,
            cache_read_keys=_CACHE_R,
            cache_write_keys=_CACHE_W,
            reasoning_keys=_REASON,
        )
        if buckets.is_empty():
            return None

        ts = epoch_flexible(rec.get("timestamp")) or epoch_flexible(msg.get("timestamp"))
        sid = str(state.get("session") or source.stem)
        line_no = state.get("_line", 0)
        state["_line"] = line_no + 1

        return self.emit(
            agent=self.name,
            event_key=f"{self.name}|{sid}|{_record_id(rec, msg, line_no)}",
            source=source,
            model=model,
            session_id=sid,
            timestamp=ts,
            buckets=buckets.inclusive(),  # Pi convention: input includes cache
        )


class PiAgentAdapter(_PiFamilyAdapter):
    name = "pi"
    display_name = "Pi Coding Agent"
    env_override = "PI_CODING_AGENT_SESSION_DIR"
    roots = ((".pi", ("agent/sessions",)), (".omp", ("agent/sessions",)))


class PrimeAgentAdapter(_PiFamilyAdapter):
    name = "prime"
    display_name = "Prime Agent"
    env_override = "PRIME_AGENT_SESSION_DIR"
    roots = ((".prime", ("agent/sessions", "agent/session-artifacts")),)

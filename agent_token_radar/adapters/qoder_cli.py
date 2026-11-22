"""Qoder CLI / Qoder CN adapters (Claude-Code-shaped session logs).

Records are Claude-style:

    {"type":"runtime-config","model":"..."}                       -> sticky model
    {"type":"assistant","timestamp":"...","message":{
         "usage":{"input_tokens":..,"output_tokens":..,
                  "cache_read_input_tokens":..,"cache_creation_input_tokens":..,
                  "request_id":"..."}}}

``input_tokens`` is INCLUSIVE of the cache buckets here (tokei subtracts both),
which differs from Claude Code's additive convention -- a detail that silently
inflates totals if copied from the Claude adapter.

camp counts as ``calls`` rather than sessions in tokei; we treat subagent files
as ordinary usage rows, deduped by ``request_id`` so a mirrored copy cannot
double count.

event_key = ``<agent>|<request_id or message.id>``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._helpers import iso_to_ms
from ._jsonl_base import JsonlAdapter
from ._usagekit import epoch_flexible, nested, pick_usage

_INPUT = ("input_tokens", "inputTokens", "input")
_OUTPUT = ("output_tokens", "outputTokens", "output")
_CACHE_R = ("cache_read_input_tokens", "cacheReadInputTokens", "cache_read")
_CACHE_W = ("cache_creation_input_tokens", "cacheCreationInputTokens", "cache_write")


class _QoderCliBase(JsonlAdapter):
    pattern = "*.jsonl"

    def parse_record(
        self, rec: dict[str, Any], *, source: Path, state: dict[str, Any]
    ) -> Any:
        rtype = rec.get("type")

        if rtype == "runtime-config":
            m = rec.get("model")
            if isinstance(m, str) and m:
                state["model"] = m
            return None

        if rtype != "assistant":
            return None

        msg = rec.get("message")
        if not isinstance(msg, dict):
            return None
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None

        buckets = pick_usage(
            usage, input_keys=_INPUT, output_keys=_OUTPUT,
            cache_read_keys=_CACHE_R, cache_write_keys=_CACHE_W,
        )
        # cache_creation may be split into 5m/1h ephemeral buckets.
        if not buckets.cache_write:
            eph = nested(usage, "cache_creation")
            if isinstance(eph, dict):
                buckets.cache_write = int(eph.get("ephemeral_5m_input_tokens") or 0) + \
                    int(eph.get("ephemeral_1h_input_tokens") or 0)
        if buckets.is_empty():
            return None

        model = msg.get("model") or state.get("model") or ""
        if isinstance(model, str) and model:
            state["model"] = model

        rid = usage.get("request_id") or msg.get("id") or rec.get("uuid") or rec.get("id")
        if not isinstance(rid, str) or not rid:
            rid = f"{source.stem}:{state.get('_line', 0)}"
        state["_line"] = state.get("_line", 0) + 1

        ts = epoch_flexible(rec.get("timestamp")) or epoch_flexible(msg.get("timestamp"))
        sid = str(rec.get("sessionId") or rec.get("session_id") or source.stem)

        return self.emit(
            agent=self.name,
            event_key=f"{self.name}|{rid}",
            source=source,
            model=str(model),
            session_id=sid,
            timestamp=ts,
            buckets=buckets.inclusive(),  # Qoder subtracts cache from input
        )


class QoderCliAdapter(_QoderCliBase):
    name = "qodercli"
    display_name = "Qoder CLI"
    env_override = "TOKEI_QODERCLI_DIR"
    roots = ((".qoder", ("projects",)),)


class QoderCliCnAdapter(_QoderCliBase):
    name = "qodercli-cn"
    display_name = "Qoder CN"
    env_override = "TOKEI_QODERCLI_CN_DIR"
    roots = ((".qoder-cn", ("projects",)),)

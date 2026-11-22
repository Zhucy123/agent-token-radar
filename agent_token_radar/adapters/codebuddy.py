"""CodeBuddy Code adapter.

CodeBuddy uses the same session-log engine as WorkBuddy, so records look like
WorkBuddy's but the usage object is duplicated under three keys, in priority
order:

    item.message.usage
    item.providerData.usage
    item.providerData.rawUsage

The key names inside are inconsistent across CodeBuddy versions, hence the wide
candidate lists. ``input_tokens`` may be INCLUSIVE of the cache buckets when a
``total_tokens`` field equals ``input+output`` -- in that case the cache
buckets are subtracted.

event_key = ``codebuddy|<sha256(messageId or timestamp+usage)>``
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ._helpers import iso_to_ms, safe_int
from ._jsonl_base import JsonlAdapter
from ._usagekit import TokenBuckets, epoch_flexible, nested, pick_usage

_INPUT = ("input_tokens", "inputTokens", "input", "prompt_tokens")
_OUTPUT = ("output_tokens", "outputTokens", "output", "completion_tokens")
_CACHE_R = (
    "cache_read_input_tokens", "cacheReadInputTokens", "cache_read", "cacheRead",
    "cached_tokens", "cachedTokens", "prompt_cache_hit_tokens",
)
_CACHE_W = (
    "cache_creation_input_tokens", "cacheCreationInputTokens",
    "cache_write_input_tokens", "cacheWriteInputTokens",
    "prompt_cache_write_tokens", "cache_write", "cacheWrite",
)
_REASON = ("reasoning_tokens", "reasoningTokens", "reasoning", "thinking_tokens",
           "completion_thinking_tokens")
_TOTAL = ("total_tokens", "totalTokens", "total")


def _usage_object(item: dict[str, Any]) -> dict[str, Any] | None:
    for path in (("message", "usage"), ("providerData", "usage"), ("providerData", "rawUsage")):
        obj = nested(item, *path)
        if isinstance(obj, dict) and obj:
            return obj
    return None


class CodeBuddyAdapter(JsonlAdapter):
    name = "codebuddy"
    display_name = "CodeBuddy Code"
    env_override = "TOKEI_CODEBUDDY_DIR"
    roots = ((".codebuddy", ("projects",)),)

    def parse_record(
        self, rec: dict[str, Any], *, source: Path, state: dict[str, Any]
    ) -> Any:
        usage = _usage_object(rec)
        if usage is None:
            return None

        buckets = pick_usage(
            usage,
            input_keys=_INPUT,
            output_keys=_OUTPUT,
            cache_read_keys=_CACHE_R,
            cache_write_keys=_CACHE_W,
            reasoning_keys=_REASON,
            total_keys=_TOTAL,
        )
        # Nested detail objects (prompt_tokens_details.cached_tokens etc.)
        if not buckets.cache_read:
            buckets.cache_read = safe_int(nested(usage, "prompt_tokens_details", "cached_tokens")) or \
                safe_int(nested(usage, "inputTokensDetails", "cached_tokens"))
        if not buckets.reasoning:
            buckets.reasoning = safe_int(
                nested(usage, "completion_tokens_details", "reasoning_tokens")
            )

        if buckets.is_empty():
            return None

        # Inclusive-input detection: an explicit total equal to input+output
        # means input already contains the cached part.
        inclusive = False
        if buckets.total and buckets.total == buckets.input + buckets.output:
            inclusive = True

        pd = rec.get("providerData") if isinstance(rec.get("providerData"), dict) else {}
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        model = (
            pd.get("requestModelId") or pd.get("requestModelName")
            or rec.get("model") or msg.get("model") or ""
        )
        sid = str(rec.get("sessionId") or rec.get("session_id") or state.get("session") or "")
        if sid:
            state["session"] = sid

        ts = epoch_flexible(rec.get("timestamp")) or epoch_flexible(msg.get("timestamp"))

        msg_id = pd.get("messageId") or rec.get("id") or msg.get("id")
        if isinstance(msg_id, str) and msg_id:
            ekey = f"{self.name}|{msg_id}"
        else:
            blob = f"{ts}|{buckets.input}|{buckets.output}|{buckets.cache_read}|{buckets.reasoning}"
            ekey = f"{self.name}|h" + hashlib.sha256(blob.encode()).hexdigest()[:24]

        conv = buckets.inclusive() if inclusive else buckets.additive()
        return self.emit(
            agent=self.name, event_key=ekey, source=source,
            model=str(model), session_id=sid, timestamp=ts, buckets=conv,
        )

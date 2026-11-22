"""Tolerant token-field extraction shared by the many JSONL-style adapters.

The 20+ agents supported here all write "some JSON per line", but their usage
keys disagree wildly:

    Claude / Qoder     usage.input_tokens / output_tokens / cache_read_input_tokens
    Pi / Prime / OpenClaw   usage.input / output / cacheRead / cacheWrite / reasoning
    Command Code       usage.inputTokens / outputTokens / cacheReadTokens
    CodeBuddy          input_tokens | inputTokens | input | prompt_tokens (all seen)

Rather than 20 near-duplicate parsers, this module owns one tolerant reader
that resolves the first matching key from an ordered candidate list, and
normalises the two "inclusive input" conventions described below.

INCLUSIVE-INPUT CONVENTION
--------------------------
Two shapes exist in the wild and they must not be mixed:

* **Additive** (Anthropic / OpenAI): ``input`` EXCLUDES the cache buckets, so
  total = input + output + cache_read + cache_write.
* **Inclusive** (Pi / Muse / ZCode / Grok Build / Qwen Code): ``input`` is the
  full prompt size INCLUDING cache hits, so cache_read must be subtracted or
  the prompt is counted twice.

Each adapter declares which convention its agent uses; ``finalize`` applies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "TokenBuckets",
    "first_int",
    "nested",
    "pick_usage",
    "epoch_to_ms",
    "epoch_flexible",
]


def first_int(container: Mapping[str, Any] | None, *keys: str) -> int:
    """First non-zero integer among ``keys``; 0 when nothing matches."""
    if not isinstance(container, Mapping):
        return 0
    for k in keys:
        if k in container:
            v = container[k]
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                n = int(v)
                if n:
                    return max(0, n)
            elif isinstance(v, str):
                try:
                    n = int(float(v))
                except (TypeError, ValueError):
                    continue
                if n:
                    return max(0, n)
    return 0


def nested(obj: Any, *path: str) -> Any:
    """Safe deep lookup: ``nested(o, "a", "b")`` == ``o["a"]["b"]``."""
    cur = obj
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def pick_usage(
    usage: Mapping[str, Any],
    *,
    input_keys: Iterable[str],
    output_keys: Iterable[str],
    cache_read_keys: Iterable[str] = (),
    cache_write_keys: Iterable[str] = (),
    reasoning_keys: Iterable[str] = (),
    total_keys: Iterable[str] = (),
) -> "TokenBuckets":
    """Resolve one usage object into normalised buckets (no convention applied)."""
    return TokenBuckets(
        input=first_int(usage, *input_keys),
        output=first_int(usage, *output_keys),
        cache_read=first_int(usage, *cache_read_keys),
        cache_write=first_int(usage, *cache_write_keys),
        reasoning=first_int(usage, *reasoning_keys),
        total=first_int(usage, *total_keys),
    )


@dataclass(slots=True)
class TokenBuckets:
    """Raw token buckets as read from one record."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    total: int = 0

    # -- constructors for the two conventions --------------------------------

    def additive(
        self, *, reasoning_in_output: bool = True
    ) -> tuple[int, int, int, int, int]:
        """Additive input: use buckets as-is.

        ``reasoning`` is reported separately but is a SUBSET of output, so it
        is never added to the total again. Returns
        ``(input, output, cache_read, cache_write, reasoning)``.
        """
        out = self.output
        if reasoning_in_output:
            # Output already contains reasoning; keep both, total counts once.
            pass
        else:
            out = self.output + self.reasoning
        return self.input, out, self.cache_read, self.cache_write, self.reasoning

    def inclusive(self) -> tuple[int, int, int, int, int]:
        """Inclusive input: ``input`` includes cache_read/cache_write."""
        fresh = max(self.input - self.cache_read - self.cache_write, 0)
        if self.input == 0 and self.cache_read > 0:
            # Some agents report only the cached part.
            fresh = 0
        return fresh, self.output, self.cache_read, self.cache_write, self.reasoning

    def is_empty(self) -> bool:
        return not (
            self.input or self.output or self.cache_read
            or self.cache_write or self.reasoning or self.total
        )


def epoch_to_ms(value: Any) -> int:
    """Coerce an epoch-ish number to UTC milliseconds.

    Handles seconds, milliseconds and microseconds by magnitude, which is how
    every agent in this project writes timestamps.
    """
    if isinstance(value, bool) or value is None:
        return 0
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    if n > 100_000_000_000_000:  # microseconds
        return n // 1000
    if n > 100_000_000_000:  # milliseconds
        return n
    if n > 100_000_000:  # seconds
        return n * 1000
    return 0


def epoch_flexible(value: Any) -> int:
    """Like :func:`epoch_to_ms` but also accepts ISO-8601 strings."""
    if isinstance(value, str) and value and not value.strip().isdigit():
        from ._helpers import iso_to_ms

        return iso_to_ms(value)
    return epoch_to_ms(value)

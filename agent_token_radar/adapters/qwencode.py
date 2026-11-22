"""Qwen Code adapter.

Two log shapes under ``$QWEN_HOME`` (default ``~/.qwen``):

  per-request: ``usage/token-usage-*.jsonl``
      {"schemaVersion":1, "id":..., "sessionId":..., "model":...,
       "inputTokens":.., "cachedTokens":.., "outputTokens":.., "thoughtsTokens":..}
  legacy summary: ``usage_record.jsonl``
      {"version":1, "sessionId":..., "models":{ "<model>": {..same fields..} }}

Quirks taken from tokei:
  * ``inputTokens`` INCLUDES ``cachedTokens`` -> subtract, or the prompt is
    counted twice.
  * When a session appears in BOTH the per-request files and the legacy
    summary, the per-request rows win; the summary would double count it.

This adapter therefore scans per-request files first, records the session ids
it saw, and skips summary entries for those sessions.

event_key = ``qwencode|<record id>`` / ``qwencode|sum|<session>|<model>``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import home
from ..readers import read_jsonl_incremental
from ._helpers import iso_to_ms, make_event


def _i(v: Any) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _buckets(rec: dict[str, Any]) -> tuple[int, int, int, int, int] | None:
    """(fresh_input, output, cache_read, cache_write, reasoning) or None."""
    inp = _i(rec.get("inputTokens"))
    cached = _i(rec.get("cachedTokens"))
    out = _i(rec.get("outputTokens"))
    think = _i(rec.get("thoughtsTokens"))
    if not (inp or cached or out or think):
        return None
    fresh = inp - cached if inp >= cached else (0 if cached else inp)
    return max(fresh, 0), out, cached, 0, think


def _ts_of(rec: dict[str, Any]) -> int:
    from ._usagekit import epoch_flexible

    ts = epoch_flexible(rec.get("timestamp")) or epoch_flexible(rec.get("startTime"))
    if not ts:
        d = rec.get("localDate")
        if isinstance(d, str) and len(d) >= 10:
            ts = iso_to_ms(d[:10] + "T00:00:00Z")
    return ts


class QwenCodeAdapter(BaseAdapter):
    name = "qwencode"
    display_name = "Qwen Code"
    env_override = "QWEN_HOME"

    def _roots(self) -> list[Path]:
        import os

        roots: list[Path] = []
        if self.env_override and os.environ.get(self.env_override):
            roots.append(Path(os.environ[self.env_override]).expanduser())
        roots.append(home() / ".qwen")
        return roots

    def detect(self) -> list[Source]:
        out: list[Source] = []
        seen: set[str] = set()
        for root in self._roots():
            # per-request files live in <root>/usage/
            usage_dir = root / "usage"
            if usage_dir.is_dir():
                for p in sorted(usage_dir.glob("token-usage-*.jsonl")):
                    k = str(p.resolve()).lower()
                    if k not in seen:
                        seen.add(k)
                        out.append(Source(path=p, kind="jsonl", agent=self.name))
            summary = root / "usage_record.jsonl"
            if summary.is_file():
                k = str(summary.resolve()).lower()
                if k not in seen:
                    seen.add(k)
                    out.append(Source(path=summary, kind="jsonl", agent=self.name))
        return out

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append

        sources = self.detect()
        per_request = [s for s in sources if s.path.name != "usage_record.jsonl"]
        summaries = [s for s in sources if s.path.name == "usage_record.jsonl"]

        covered_sessions: set[str] = set()

        for src in per_request:
            key = str(src.path)
            cp = cps.get(key) or Checkpoint.for_path(src.path)
            records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
            result.checkpoints[key] = new_cp

            for rec in records:
                if rec.get("schemaVersion") != 1:
                    continue
                rid = rec.get("id")
                if not rid:
                    continue
                buckets = _buckets(rec)
                if buckets is None:
                    continue
                sid = str(rec.get("sessionId") or "")
                if sid:
                    covered_sessions.add(sid)
                result.events.append(make_event(
                    agent=self.name,
                    event_key=f"{self.name}|{rid}",
                    source_file=key,
                    model=str(rec.get("model") or ""),
                    session_id=sid,
                    timestamp=_ts_of(rec),
                    **dict(zip(
                        ("input_tokens", "output_tokens", "cache_read_tokens",
                         "cache_write_tokens", "reasoning_tokens"),
                        buckets,
                    )),
                ))

        for src in summaries:
            key = str(src.path)
            cp = cps.get(key) or Checkpoint.for_path(src.path)
            records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
            result.checkpoints[key] = new_cp

            for rec in records:
                if rec.get("version") != 1:
                    continue
                sid = str(rec.get("sessionId") or "")
                if sid and sid in covered_sessions:
                    continue  # per-request rows already own this session
                models = rec.get("models")
                if not isinstance(models, dict):
                    continue
                for model, mv in models.items():
                    if not isinstance(mv, dict):
                        continue
                    buckets = _buckets(mv)
                    if buckets is None:
                        continue
                    result.events.append(make_event(
                        agent=self.name,
                        event_key=f"{self.name}|sum|{sid}|{model}",
                        source_file=key,
                        model=str(model),
                        session_id=sid,
                        timestamp=_ts_of(mv) or _ts_of(rec),
                        **dict(zip(
                            ("input_tokens", "output_tokens", "cache_read_tokens",
                             "cache_write_tokens", "reasoning_tokens"),
                            buckets,
                        )),
                    ))
        return result

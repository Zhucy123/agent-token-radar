"""Grok Build adapter.

Unlike most agents, Grok's token usage is NOT in the session files. It is in
the application log::

    ${GROK_HOME:-~/.grok}/logs/unified.jsonl   (JSONL, one JSON object per line)

The per-request usage record is a log line with a specific message id::

    {"msg":"shell.turn.inference_done","sid":"<session>","ts":"<iso8601>",
     "ctx":{"prompt_tokens":N,       # INCLUDES cached
            "cached_prompt_tokens":N,
            "completion_tokens":N,   # INCLUDES reasoning
            "reasoning_tokens":N,
            "loop_index":N,"attempts":N,"model_elapsed_ms":N}}

So: ``input = prompt - cached``, ``output = completion - reasoning``, and the
reasoning bucket is kept separate. The model name is NOT in the log line --
tokei reads it from ``sessions/*/*/summary.json:current_model_id``, matched by
the session group directory. We do the same as a best-effort lookup.

A log line that fails the ``msg`` filter is ignored, including every other
``unified.jsonl`` record (auth, retries, timers ...).

event_key = ``grok|<sid>:<ts>:<loop_index>:<attempts>`` (tuple of all token
values appended so two identical-timestamp loops cannot collide)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..readers import read_jsonl_incremental
from ._helpers import iso_to_ms, make_event


class GrokBuildAdapter(BaseAdapter):
    name = "grok"
    display_name = "Grok Build"
    env_override = "GROK_HOME"

    def _home(self) -> Path:
        if self.env_override and os.environ.get(self.env_override):
            return Path(os.environ[self.env_override]).expanduser()
        from ..paths import home

        return home() / ".grok"

    def detect(self) -> list[Source]:
        log = self._home() / "logs" / "unified.jsonl"
        return [Source(path=log, kind="jsonl", agent=self.name)] if log.is_file() else []

    def _session_models(self) -> dict[str, str]:
        """session group dir -> current_model_id (from sessions/*/*/summary.json)."""
        cache = getattr(self, "_sm", None)
        if cache is not None:
            return cache
        import json as _json

        mapping: dict[str, str] = {}
        base = self._home() / "sessions"
        if base.is_dir():
            for summary in base.glob("*/*/summary.json"):
                try:
                    with summary.open("r", encoding="utf-8", errors="replace") as fh:
                        obj = _json.load(fh)
                except (OSError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                model = obj.get("current_model_id")
                if not isinstance(model, str) or not model:
                    continue
                # The log's `sid` equals the *directory* name of the session.
                mapping[summary.parent.name] = model
                if isinstance(obj.get("request_id"), str):
                    mapping.setdefault(obj["request_id"], model)
        self._sm = mapping
        return mapping

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append
        models = self._session_models()

        for src in self.detect():
            key = str(src.path)
            cp = cps.get(key) or Checkpoint.for_path(src.path)
            records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
            result.checkpoints[key] = new_cp

            for rec in records:
                if rec.get("msg") != "shell.turn.inference_done":
                    continue
                ctx = rec.get("ctx")
                if not isinstance(ctx, dict):
                    continue
                prompt = int(ctx.get("prompt_tokens") or 0)
                cached = int(ctx.get("cached_prompt_tokens") or 0)
                completion = int(ctx.get("completion_tokens") or 0)
                reasoning = int(ctx.get("reasoning_tokens") or 0)
                if not (prompt or completion or reasoning):
                    continue

                sid = rec.get("sid")
                if not isinstance(sid, str) or not sid:
                    continue
                ts = iso_to_ms(rec.get("ts"))
                if not ts:
                    continue

                reasoning = min(max(reasoning, 0), completion)
                fresh_in = max(prompt - cached, 0)
                out = max(completion - reasoning, 0)

                loop_index = ctx.get("loop_index")
                attempts = ctx.get("attempts")
                ekey = (
                    f"{self.name}|{sid}:{ts}:{loop_index}:{attempts}"
                    f":{prompt}:{cached}:{completion}:{reasoning}"
                )

                result.events.append(make_event(
                    agent=self.name,
                    event_key=ekey,
                    source_file=key,
                    model=str(models.get(sid, "")),
                    session_id=sid,
                    timestamp=ts,
                    input_tokens=fresh_in,
                    output_tokens=out,
                    cache_read_tokens=cached,
                    reasoning_tokens=reasoning,
                ))
        return result

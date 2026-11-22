"""Muse Code adapter.

Session logs live at::

    ~/.local/share/muse/sessions/*/*/*/*/session.jsonl
    ~/.local/share/muse/sessions/*/session.jsonl

The per-call usage record is nested three levels deep:

    payload_type == "runtime.session"
    payload.kind == "run"
    payload.event.kind == "model_completed"
      payload.event.usage  { input_tokens, output_tokens, cached_tokens,
                             cache_write_tokens, reasoning_tokens }
      payload.event.model

``input_tokens`` INCLUDES ``cached_tokens`` (Muse convention) -> subtract.
``reasoning`` is a subset of output: reported, never re-added.

Timestamps are epoch in **microseconds OR milliseconds**; the magnitude
decides (µs > 1e14, ms > 1e11).

event_key = ``muse|<source_run_record_id>``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._jsonl_base import JsonlAdapter
from ._usagekit import epoch_to_ms, nested
from ..base import Source


class MuseCodeAdapter(JsonlAdapter):
    name = "muse"
    display_name = "Muse Code"
    env_override = "TOKEI_MUSE_DIR"
    roots = ((".local/share/muse", ("sessions",)),)
    pattern = "session.jsonl"

    def _extra_roots(self) -> list[Path]:
        import os
        from ..paths import local_data_dir

        out = [local_data_dir() / "muse" / "sessions"]
        if os.environ.get("MUSE_DIR"):
            out.insert(0, Path(os.environ["MUSE_DIR"]).expanduser() / "sessions")
        return out

    def _run_models(self, source: Path) -> dict[str, str]:
        """Sidecar map: run id -> model id, from run.model.configured events."""
        cache = getattr(self, "_model_cache", None)
        if cache is None:
            cache = self._model_cache = {}
        key = str(source)
        if key in cache:
            return cache[key]

        mapping: dict[str, str] = {}
        try:
            with source.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "run.model" not in line and "model_id" not in line:
                        continue
                    import json as _json

                    try:
                        obj = _json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if nested(obj, "payload", "kind") == "model.configured" or (
                        nested(obj, "payload", "type") == "run.model.configured"
                    ):
                        rid = nested(obj, "payload", "record", "run_stream", "id")
                        mid = nested(obj, "payload", "record", "model_id")
                        if isinstance(rid, str) and isinstance(mid, str) and mid != "same-as-main":
                            mapping[rid] = mid
        except OSError:
            pass
        cache[key] = mapping
        return mapping

    def parse_record(
        self, rec: dict[str, Any], *, source: Path, state: dict[str, Any]
    ) -> Any:
        if rec.get("payload_type") != "runtime.session":
            return None
        payload = rec.get("payload")
        if not isinstance(payload, dict) or payload.get("kind") != "run":
            return None

        event = payload.get("event")
        if not isinstance(event, dict) or event.get("kind") != "model_completed":
            return None

        usage = event.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None

        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cached = int(usage.get("cached_tokens") or usage.get("cache_read_tokens") or 0)
        cw = int(usage.get("cache_write_tokens") or 0)
        reason = int(usage.get("reasoning_tokens") or 0)
        if not (inp or out or cached or cw or reason):
            return None

        model = event.get("model") or ""
        if not model:
            rid = payload.get("run_id") or rec.get("run_id")
            if isinstance(rid, str):
                model = self._run_models(source).get(rid, "")

        record = rec.get("record") if isinstance(rec.get("record"), dict) else {}
        ts = epoch_to_ms(record.get("recorded_at") or rec.get("recorded_at")
                         or event.get("recorded_at"))
        if not ts:
            return None

        sid = str(nested(record, "stream", "id") or source.parent.name)
        rid_key = record.get("source_run_record_id") or rec.get("id")
        if not isinstance(rid_key, str) or not rid_key:
            rid_key = f"{source.stem}:{state.get('_line', 0)}"
        state["_line"] = state.get("_line", 0) + 1

        # input includes cached -> subtract
        fresh = max(inp - cached, 0) if inp >= cached else (0 if cached else inp)
        buckets = (fresh, out, cached, cw, reason)

        return self.emit(
            agent=self.name,
            event_key=f"{self.name}|{rid_key}",
            source=source,
            model=str(model),
            session_id=sid,
            timestamp=ts,
            buckets=buckets,
        )

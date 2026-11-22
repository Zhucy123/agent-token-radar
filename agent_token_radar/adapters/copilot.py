"""GitHub Copilot adapter.

Copilot is the one agent whose accounting is deliberately **output-only**: its
local records do not expose a trustworthy split between fresh input and cache
buckets (the upstream SDK's semantics are still unsettled -- see tokei's
README and github/copilot-sdk#2834). Recording a guessed input value would be
worse than recording none, so input / cache buckets are stored as 0 and
``reasoning_tokens`` is split out of ``outputTokens`` (it is a subset there).

Two local surfaces exist and are reconciled:

* JSONL:  ``~/.copilot/session-state/<sid>/events.jsonl``
          authoritative record = ``type=="session.shutdown"`` with
          ``data.modelMetrics.<model>.usage``
* SQLite: ``assistant_usage_events`` joined to ``sessions`` (for ``cwd``)

event_key = ``copilot|<sid>|<model>|<start ts>`` for shutdown aggregates,
            ``copilot|<sid>|<event ts>|<model>`` for per-event rows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import home
from ..readers import readonly_sqlite_copy
from ._helpers import make_event
from ._usagekit import epoch_flexible, first_int, nested


class CopilotAdapter(BaseAdapter):
    name = "copilot"
    display_name = "GitHub Copilot"
    env_override = "COPILOT_HOME"

    def _dir(self) -> Path:
        for env in ("TOKEI_COPILOT_DIR", "TOKEI_COPILOT_CLI_DIR",
                    "COPILOT_HOME", "GITHUB_COPILOT_HOME", "TOKEI_COPILOT_HOME"):
            if os.environ.get(env):
                return Path(os.environ[env]).expanduser()
        return home() / ".copilot"

    def detect(self) -> list[Source]:
        root = self._dir()
        out: list[Source] = []
        if not root.is_dir():
            return out
        for p in root.rglob("events.jsonl"):
            if p.is_file():
                out.append(Source(path=p, kind="jsonl", agent=self.name))
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in (".db", ".sqlite"):
                out.append(Source(path=p, kind="sqlite", agent=self.name))
        return out

    # -- shared extraction ---------------------------------------------------

    @staticmethod
    def _usage_from(obj: dict[str, Any]) -> tuple[int, int, int, str, str, int] | None:
        """Return (ts, output, reasoning, model, sid, premium) or None."""
        # usage may sit at record level or under "data".
        cand = obj.get("usage")
        if not isinstance(cand, dict):
            data = obj.get("data")
            cand = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(cand, dict):
            cand = obj  # last resort: flat fields on the record

        out_tok = first_int(cand, "outputTokens", "output_tokens",
                            "completion_tokens", "output")
        reason = first_int(cand, "reasoningTokens", "reasoning_tokens", "reasoning")
        if not out_tok and not reason:
            return None

        model = str(
            obj.get("model") or obj.get("modelId") or obj.get("model_id")
            or nested(obj, "data", "model") or ""
        )
        sid = str(
            obj.get("sessionId") or obj.get("session_id")
            or nested(obj, "data", "sessionId") or ""
        )
        ts = (epoch_flexible(obj.get("timestamp")) or epoch_flexible(obj.get("startTime"))
              or epoch_flexible(obj.get("createdAt")) or epoch_flexible(obj.get("created_at")))
        if not ts:
            return None

        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        premium = int(data.get("totalPremiumRequests")
                      or obj.get("totalPremiumRequests") or 0)
        # reasoning is a subset of output in Copilot's contract
        visible = max(out_tok - reason, 0)
        return ts, visible, reason, model, sid, premium

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append
        sources = self.detect()

        # Sessions already covered by a DB aggregate row, so mirrored JSONL
        # copies of the same session are not counted twice.
        db_sessions: set[str] = set()

        for src in [s for s in sources if s.kind == "sqlite"]:
            key = str(src.path)
            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                try:
                    rows = con.execute(
                        "SELECT * FROM assistant_usage_events"
                    ).fetchall()
                except Exception:  # noqa: BLE001 - not every db has this table
                    continue
                for row in rows:
                    rec = {k: row[k] for k in row.keys()}
                    parsed = self._usage_from(rec)
                    if not parsed:
                        continue
                    ts, out_tok, reason, model, sid, _ = parsed
                    if sid:
                        db_sessions.add(sid)
                    result.events.append(make_event(
                        agent=self.name,
                        event_key=f"{self.name}|{sid}|{ts}|{model}",
                        source_file=key,
                        model=model, session_id=sid, timestamp=ts,
                        output_tokens=out_tok, reasoning_tokens=reason,
                    ))
            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp

        from ..readers import read_jsonl_incremental

        for src in [s for s in sources if s.kind == "jsonl"]:
            key = str(src.path)
            cp = cps.get(key) or Checkpoint.for_path(src.path)
            records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
            result.checkpoints[key] = new_cp

            for rec in records:
                if rec.get("type") != "session.shutdown":
                    continue
                metrics = nested(rec, "data", "modelMetrics")
                if not isinstance(metrics, dict):
                    continue
                sid = str(rec.get("sessionId") or nested(rec, "data", "sessionId")
                          or src.parent.name)
                if sid in db_sessions:
                    continue  # DB row is authoritative for this session
                start = nested(rec, "data", "sessionStartTime")
                for model, mv in metrics.items():
                    usage = mv.get("usage") if isinstance(mv, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    out_tok = first_int(usage, "outputTokens", "output_tokens")
                    reason = first_int(usage, "reasoningTokens", "reasoning_tokens")
                    if not out_tok and not reason:
                        continue
                    ts = epoch_flexible(start) or epoch_flexible(rec.get("timestamp"))
                    if not ts:
                        continue
                    result.events.append(make_event(
                        agent=self.name,
                        event_key=f"{self.name}|{sid}|{model}|{ts}",
                        source_file=key,
                        model=str(model), session_id=sid, timestamp=ts,
                        output_tokens=max(out_tok - reason, 0),
                        reasoning_tokens=reason,
                    ))
        return result

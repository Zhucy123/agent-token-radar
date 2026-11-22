"""Codex adapter.

Authoritative source (DISCOVERY.md 2.1)
--------------------------------------
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` (append-only JSONL).

Only ``type == "token_usage_record"`` carries per-request usage in
``payload.usage``. ``payload.info.*_token_usage`` / ``thread_token_usage`` /
``turn_token_usage`` are CUMULATIVE rollups and are deliberately ignored --
reading them would double-count every request in the session.

event_key = ``codex|{session_id}|{response_id}|{ordinal}``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import agent_candidates
from ..readers import read_jsonl_incremental
from ._common import glob_sources
from ._helpers import iso_to_ms, make_event, project_label, safe_int


class CodexAdapter(BaseAdapter):
    name = "codex"
    display_name = "Codex"
    home_env = "CODEX_HOME"

    def detect(self) -> list[Source]:
        roots = agent_candidates(".codex", env=self.home_env, subpaths=("sessions", "archived_sessions"))
        return glob_sources(roots, "rollout-*.jsonl", kind="jsonl", agent=self.name)

    def _title_map(self) -> dict[str, str]:
        """Read ``session_index.jsonl``: ``{thread id: human title}``.

        Codex keeps the user-facing thread names in one index file next to the
        rollout directory, not inside the rollout files themselves. The file is
        small and has no usage data, so it is read whole rather than checkpointed.
        """
        roots = agent_candidates(".codex", env=self.home_env)
        out: dict[str, str] = {}
        seen: set[Path] = set()
        for root in roots:
            cand = Path(root) / "session_index.jsonl"
            if not cand.is_file() or cand in seen:
                continue
            seen.add(cand)
            try:
                with cand.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        sid = str(rec.get("id") or "")
                        name = rec.get("thread_name")
                        if sid and isinstance(name, str) and name.strip():
                            out[sid] = name.strip()
            except OSError:
                continue
        return out

    def _session_meta(self, sources: list[Source]) -> tuple[dict[str, str], dict[str, str]]:
        """``({path: model_provider}, {path: cwd})`` from each rollout's meta.

        Codex states its own backend and its working directory exactly once, in
        the first record of a session file. An incremental read skips that
        record on every later scan (the checkpoint is already past it), so both
        values have to be read separately -- the same trap that hid session
        titles and Claude's custom-title records.

        Only the opening slice of each file is read: ``session_meta`` is the
        first record by construction, and a rollout can be tens of MB.
        """
        providers: dict[str, str] = {}
        projects: dict[str, str] = {}
        for src in sources:
            try:
                fh = src.path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if '"session_meta"' not in stripped:
                        continue
                    try:
                        rec = json.loads(stripped)
                    except ValueError:
                        continue
                    if rec.get("type") != "session_meta":
                        continue
                    payload = rec.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    prov = payload.get("model_provider")
                    if isinstance(prov, str) and prov.strip():
                        providers[str(src.path)] = prov.strip()
                    cwd = project_label(payload.get("cwd"))
                    if cwd:
                        projects[str(src.path)] = cwd
                    break  # one session_meta per file
            finally:
                fh.close()
        return providers, projects

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append
        models: dict[str, str] = {}
        sources = self.detect()
        #: Codex names its own backend in ``session_meta.model_provider``
        #: (``openai`` on this machine). Read independently of the checkpoint,
        #: because that record sits behind it on every scan after the first.
        declared_providers, projects = self._session_meta(sources)
        result.titles = self._title_map()

        for src in sources:
            key = str(src.path)
            cp = cps.get(key) or Checkpoint.for_path(src.path)
            records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
            result.checkpoints[key] = new_cp

            for rec in records:
                rtype = rec.get("type")
                payload = rec.get("payload") or {}
                if not isinstance(payload, dict):
                    continue

                # Capture the model once from session_meta / turn_context.
                if rtype in ("session_meta", "turn_context"):
                    m = payload.get("model") or payload.get("model_slug")
                    if not m and isinstance(payload.get("turn_context"), dict):
                        m = payload["turn_context"].get("model")
                    if not m and isinstance(payload.get("info"), dict):
                        m = payload["info"].get("model")
                    if isinstance(m, str) and m:
                        models[key] = m

                if rtype != "token_usage_record":
                    continue
                usage = payload.get("usage")
                if not isinstance(usage, dict):
                    continue

                session_id = str(
                    payload.get("session_id") or payload.get("thread_id") or ""
                )
                response_id = str(payload.get("response_id") or "")
                ordinal = safe_int(rec.get("ordinal"))
                ekey = f"codex|{session_id}|{response_id}|{ordinal}"

                model = models.get(key, "")
                # ``model_provider`` from session_meta is what Codex itself
                # names (usually ``openai``). On this machine Codex talks to
                # magpie's local gateway, which records the real backend per
                # request, so the measured value still wins inside the resolver.
                provider, provider_source = self.resolve_provider(
                    self.name, model, response_id,
                    declared=declared_providers.get(key, ""),
                )
                served = self.resolve_served_model(response_id)

                result.events.append(
                    make_event(
                        agent=self.name,
                        event_key=ekey,
                        source_file=key,
                        model=model,
                        provider=provider,
                        provider_source=provider_source,
                        served_model=served,
                        session_id=session_id,
                        project=projects.get(key, ""),
                        timestamp=iso_to_ms(rec.get("timestamp")),
                        input_tokens=safe_int(usage.get("input_tokens")),
                        output_tokens=safe_int(usage.get("output_tokens")),
                        cache_read_tokens=safe_int(usage.get("cached_input_tokens")),
                        cache_write_tokens=safe_int(usage.get("cache_write_input_tokens")),
                        reasoning_tokens=safe_int(usage.get("reasoning_output_tokens")),
                        total_tokens=safe_int(usage.get("total_tokens")),
                    )
                )
        return result

"""Claude Code adapter.

Authoritative source (DISCOVERY.md 2.2)
--------------------------------------
``~/.claude/projects/<project-slug>/<uuid>.jsonl`` (append-only JSONL).

Only ``type == "assistant"`` -> ``message.usage`` is per-request.
``type == "cost-state"`` -> ``modelUsage`` is a CUMULATIVE snapshot and is
deliberately ignored.

event_key = ``claude|{message.id}`` (message.id is the provider request id and
is naturally idempotent).
"""

from __future__ import annotations

import json

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import agent_candidates
from ..readers import read_jsonl_incremental
from ._common import glob_sources
from ._helpers import iso_to_ms, make_event, project_label, safe_int

#: Claude Code's marker for a message it answered locally without calling the
#: API ("No response requested."). Every token bucket is zero; it is not a model.
_SYNTHETIC_MODEL = "<synthetic>"


class ClaudeAdapter(BaseAdapter):
    name = "claude"
    display_name = "Claude Code"
    home_env = "CLAUDE_CONFIG_DIR"

    def detect(self) -> list[Source]:
        roots = agent_candidates(".claude", env=self.home_env, subpaths=("projects",))
        return glob_sources(roots, "*.jsonl", kind="jsonl", agent=self.name)

    def collect_titles(self, sources: list[Source]) -> dict[str, str]:
        """Read ``custom-title`` records from every session file.

        These must be read independently of the incremental checkpoint. A title
        is written once, near the start of a session; by the time anyone looks
        at the dashboard the checkpoint is already past it, so an incremental
        read sees an empty tail and the title is never collected (measured:
        zero titles for all 12 local sessions before this change).

        The scan is bounded: only ``custom-title`` lines are parsed, and the
        whole file is streamed once per scan.
        """
        titles: dict[str, str] = {}
        for src in sources:
            try:
                fh = src.path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                for line in fh:
                    # Cheap pre-filter: skip the many large assistant records.
                    if '"custom-title"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "custom-title":
                        continue
                    title = rec.get("customTitle")
                    sid = str(rec.get("sessionId") or "")
                    if isinstance(title, str) and title.strip() and sid:
                        titles[sid] = title.strip()
            finally:
                fh.close()
        return titles

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append

        sources = self.detect()
        result.titles = self.collect_titles(sources)

        for src in sources:
            key = str(src.path)
            cp = cps.get(key) or Checkpoint.for_path(src.path)
            records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
            result.checkpoints[key] = new_cp

            for rec in records:
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                details = usage.get("output_tokens_details")
                reasoning = (
                    details.get("thinking_tokens")
                    if isinstance(details, dict)
                    else 0
                )
                msg_id = str(msg.get("id") or rec.get("uuid") or "")
                if not msg_id:
                    msg_id = f"{rec.get('sessionId','')}|{rec.get('timestamp','')}|{safe_int(usage.get('input_tokens'))}"

                model = str(msg.get("model") or "")
                # Claude Code writes ``model: "<synthetic>"`` for messages that
                # never reached the API (its text is "No response requested.")
                # with every token bucket at 0. It is a local placeholder, not a
                # model, so it must not become a dashboard row.
                if model == _SYNTHETIC_MODEL:
                    continue
                # Claude Code logs no provider of its own. ``msg.id`` is the
                # upstream request id and equals magpie's ``response_id``, so a
                # local gateway can state it directly (measured: 587/591 on this
                # machine); otherwise the model name is the only clue and the
                # value is flagged as inferred.
                declared = str(
                    msg.get("provider")
                    or rec.get("provider")
                    or (usage.get("provider") if isinstance(usage, dict) else "")
                    or ""
                )
                provider, provider_source = self.resolve_provider(
                    self.name, model, msg_id, declared=declared
                )
                # ``claude-sonnet-5`` is a routing alias, not a model: the
                # gateway recorded what it actually served. Keep both, so the
                # dashboard can show the real model without losing the alias the
                # agent logged.
                served = self.resolve_served_model(msg_id)
                # ``cwd`` is on every record, so the project is what the session
                # was actually working on -- not a guess from the directory slug
                # (subagent logs live one level deeper and would be mislabelled).
                project = project_label(rec.get("cwd"))

                result.events.append(
                    make_event(
                        agent=self.name,
                        event_key=f"claude|{msg_id}",
                        source_file=key,
                        model=model,
                        provider=provider,
                        provider_source=provider_source,
                        served_model=served,
                        session_id=str(rec.get("sessionId") or ""),
                        project=project,
                        timestamp=iso_to_ms(rec.get("timestamp")),
                        input_tokens=safe_int(usage.get("input_tokens")),
                        output_tokens=safe_int(usage.get("output_tokens")),
                        cache_read_tokens=safe_int(usage.get("cache_read_input_tokens")),
                        cache_write_tokens=safe_int(usage.get("cache_creation_input_tokens")),
                        reasoning_tokens=safe_int(reasoning),
                    )
                )
        return result

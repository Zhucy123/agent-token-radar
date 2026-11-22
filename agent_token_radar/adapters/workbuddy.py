"""WorkBuddy adapter.

Authoritative source (DISCOVERY.md 2.4)
--------------------------------------
``~/.workbuddy/projects/<project>/<uuid>.jsonl`` (append-only JSONL).

Usage lives on records carrying ``providerData.rawUsage`` (OpenAI-style
prompt/completion tokens). ``message.usage`` is a compatible fallback with the
same numbers. ``providerData.usage`` is yet another mirror of the same values.

event_key = ``workbuddy|{id}`` (record id is a UUID).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import agent_candidates
from ..readers import read_jsonl_incremental, readonly_sqlite_copy
from ._common import glob_sources
from ._helpers import make_event, project_label, safe_int


def _nested(d: Any, *path: str) -> Any:
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _read_session_titles(db_path: Path) -> dict[str, str]:
    """``{session id: title}`` from WorkBuddy's own ``workbuddy.db``.

    WorkBuddy keeps one row per conversation in the ``sessions`` table with both
    ``title`` (auto-generated from the first prompt) and ``custom_title`` (what
    the user renamed it to). The custom title wins when present.

    Read through a throwaway copy: the app may hold a write lock at any moment.
    """
    out: dict[str, str] = {}
    if not db_path.is_file():
        return out
    try:
        with readonly_sqlite_copy(db_path) as con:
            if con is None:
                return out
            for r in con.execute("SELECT id, title, custom_title FROM sessions"):
                sid = str(r["id"] or "")
                if not sid:
                    continue
                title = (r["custom_title"] or r["title"] or "").strip()
                if title:
                    out[sid] = title
    except (sqlite3.Error, OSError):
        return out
    return out


class _WorkBuddyBase(BaseAdapter):
    """Shared JSONL ingestion for WorkBuddy and WorkBuddy AI

    Both products write byte-identical records (verified on this machine):
    ``providerData.rawUsage`` with OpenAI-style prompt/completion tokens.
    Only the data directory differs, so subclasses just declare ``dot_dirs``.
    """

    dot_dirs: tuple[str, ...] = (".workbuddy",)

    def detect(self) -> list[Source]:
        seen: set[str] = set()
        out: list[Source] = []
        for dot in self.dot_dirs:
            for src in glob_sources(
                agent_candidates(dot, env=self.home_env, subpaths=("projects",)),
                "*.jsonl", kind="jsonl", agent=self.name,
            ):
                k = str(src.path.resolve()).lower()
                if k not in seen:
                    seen.add(k)
                    out.append(src)
        return out

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append

        # Session titles live in the product's own SQLite index, not in the
        # per-session JSONL logs.
        for dot in self.dot_dirs:
            for root in agent_candidates(dot, env=self.home_env):
                result.titles.update(_read_session_titles(root / "workbuddy.db"))

        for src in self.detect():
            key = str(src.path)
            cp = cps.get(key) or Checkpoint.for_path(src.path)
            records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
            result.checkpoints[key] = new_cp

            for rec in records:
                pd = rec.get("providerData")
                if not isinstance(pd, dict):
                    continue
                raw = pd.get("rawUsage")
                if not isinstance(raw, dict):
                    continue
                # Only records with an actual usage payload qualify.
                if not any(
                    k in raw
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                ):
                    continue

                msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                musage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}

                def pick(*names: str) -> int:
                    for n in names:
                        v = raw.get(n)
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            return safe_int(v)
                    return 0

                input_t = pick("prompt_tokens") or safe_int(musage.get("input_tokens"))
                output_t = pick("completion_tokens") or safe_int(musage.get("output_tokens"))
                cache_read = (
                    safe_int(_nested(raw, "prompt_tokens_details", "cached_tokens"))
                    or pick("prompt_cache_hit_tokens", "cache_read_input_tokens")
                    or safe_int(musage.get("cache_read_input_tokens"))
                )
                cache_write = pick(
                    "prompt_cache_write_tokens", "cache_creation_input_tokens"
                )
                reasoning = (
                    safe_int(_nested(raw, "completion_tokens_details", "reasoning_tokens"))
                    or pick("completion_thinking_tokens")
                )
                total = pick("total_tokens")

                rid = str(rec.get("id") or "")
                if not rid:
                    rid = f"{rec.get('sessionId','')}|{rec.get('timestamp','')}|{input_t}"

                # Timestamp is epoch milliseconds in WorkBuddy logs.
                ts = rec.get("timestamp")
                ts_ms = safe_int(ts) if isinstance(ts, (int, float)) else 0

                usage_meta = pd.get("usage") if isinstance(pd.get("usage"), dict) else {}
                model = (
                    pd.get("model")
                    or rec.get("model")
                    or usage_meta.get("model")
                    or ""
                )
                # WorkBuddy records the backing service when it proxies one
                # (e.g. "workbuddy"), so the same model name stays attributable.
                # When it does not, the product itself IS the provider: its own
                # catalogue serves these models, which the resolver knows.
                declared = str(
                    pd.get("provider")
                    or rec.get("provider")
                    or usage_meta.get("provider")
                    or ""
                )
                provider, provider_source = self.resolve_provider(
                    self.name, str(model), str(pd.get("messageId") or rid),
                    declared=declared,
                )

                result.events.append(
                    make_event(
                        agent=self.name,
                        event_key=f"{self.name}|{rid}",
                        source_file=key,
                        model=str(model),
                        provider=provider,
                        provider_source=provider_source,
                        served_model="",
                        session_id=str(rec.get("sessionId") or ""),
                        project=project_label(rec.get("cwd")),
                        timestamp=ts_ms,
                        input_tokens=input_t,
                        output_tokens=output_t,
                        cache_read_tokens=cache_read,
                        cache_write_tokens=cache_write,
                        reasoning_tokens=reasoning,
                        total_tokens=total,
                    )
                )
        return result


class WorkBuddyAdapter(_WorkBuddyBase):
    """WorkBuddy (domestic build): ``~/.workbuddy/projects/**/*.jsonl``."""

    name = "workbuddy"
    display_name = "WorkBuddy"
    home_env = "WORKBUDDY_HOME"
    dot_dirs = (".workbuddy",)

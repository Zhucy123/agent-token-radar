"""Reusable base for the family of "one JSON object per line" agent logs.

Many supported agents (Pi, Prime, CodeBuddy, Qoder, Kimi, Muse, Command Code,
Copilot, Qwen ...) differ only in:

    * where the files live,
    * how to recognise a per-request usage record,
    * which keys hold the token counts,
    * whether ``input`` is inclusive of the cache buckets.

Subclasses implement :meth:`parse_record` and declare ``roots``; everything
else (incremental JSONL reading, dedup by ``event_key``, warnings, checkpoints)
is handled here.
"""

from __future__ import annotations

import os
from abc import abstractmethod
from pathlib import Path
from typing import Any, Iterable

from ..base import BaseAdapter, Checkpoint, ScanResult, Source, UsageEvent
from ..readers import read_jsonl_incremental
from ._common import glob_sources
from ._helpers import make_event


class JsonlAdapter(BaseAdapter):
    """Incremental JSONL scanner with per-file byte offsets."""

    #: (dot-directory under $HOME, [sub-paths...]) pairs to search.
    roots: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: glob applied under each root, e.g. ``"*.jsonl"``.
    pattern: str = "*.jsonl"
    #: extra roots resolved by the subclass (e.g. platform-specific dirs).
    env_override: str | None = None

    # -- discovery -----------------------------------------------------------

    def _extra_roots(self) -> Iterable[Path]:
        return ()

    def detect(self) -> list[Source]:
        from ..paths import home

        base_roots: list[Path] = []
        if self.env_override and os.environ.get(self.env_override):
            base_roots.append(Path(os.environ[self.env_override]).expanduser())
        for dot, subs in self.roots:
            for sub in subs:
                base_roots.append(home() / dot / sub)
        base_roots.extend(self._extra_roots())

        seen: set[str] = set()
        out: list[Source] = []
        for src in glob_sources(base_roots, self.pattern, kind="jsonl", agent=self.name):
            key = str(src.path.resolve()).lower()
            if key not in seen:
                seen.add(key)
                out.append(src)
        return out

    # -- parsing -------------------------------------------------------------

    @abstractmethod
    def parse_record(
        self, rec: dict[str, Any], *, source: Path, state: dict[str, Any]
    ) -> UsageEvent | None:
        """Return a normalized event, or ``None`` if this line carries none.

        ``state`` is a per-file scratch dict for sticky values (current model,
        session id, cwd) that agents emit on separate records.
        """

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append

        for src in self.detect():
            key = str(src.path)
            cp = cps.get(key) or Checkpoint.for_path(src.path)
            records, new_cp = read_jsonl_incremental(src.path, cp, on_warning=warn)
            result.checkpoints[key] = new_cp

            # Sticky per-file state is rebuilt from records seen in THIS batch.
            # A fresh (full) scan therefore re-derives it; an incremental scan
            # may miss a model_change that happened earlier, which is why
            # adapters persist the derived model into the event itself.
            state: dict[str, Any] = {}
            for rec in records:
                try:
                    ev = self.parse_record(rec, source=src.path, state=state)
                except Exception as e:  # noqa: BLE001 - one bad record must not abort
                    warn(f"{key}: record parse failed ({e.__class__.__name__}: {e}); skipped")
                    continue
                if ev is not None:
                    result.events.append(ev)
        return result

    # -- helpers for subclasses ---------------------------------------------

    @staticmethod
    def emit(
        *,
        agent: str,
        event_key: str,
        source: Path | str,
        model: str = "",
        session_id: str = "",
        timestamp: int = 0,
        buckets: tuple[int, int, int, int, int],
    ) -> UsageEvent:
        inp, out, cr, cw, reason = buckets
        return make_event(
            agent=agent,
            event_key=event_key,
            source_file=str(source),
            model=model,
            session_id=session_id,
            timestamp=timestamp,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cr,
            cache_write_tokens=cw,
            reasoning_tokens=reason,
        )

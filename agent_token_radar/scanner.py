"""Scan orchestration (Phase 1 skeleton, Phase 2 adapters plug in here).

Flow per adapter::

    sources = adapter.detect()                 # strictly read-only discovery
    cps     = store.load_checkpoints()         # previous cursors
    result  = adapter.scan(cps)                # -> events + new cursors + warnings
    counts  = store.upsert_events(result.events)   # UPSERT by event_key
    store.save_checkpoints(result.checkpoints)

One adapter failing (bad file, permission, unexpected schema) is contained:
its error is reported and the remaining adapters still run.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .base import AdapterReport, BaseAdapter, ScanReport
from .registry import all_adapters
from .store import Store

logger = logging.getLogger("agent_token_radar.scanner")


class Scanner:
    def __init__(self, store: Store | None = None, *, adapters: Iterable[BaseAdapter] | None = None):
        self.owns_store = store is None
        self.store = store or Store.default()
        self.adapters = list(adapters) if adapters is not None else all_adapters()
        #: Result of the last provider backfill (see :meth:`Store.backfill_providers`).
        self.last_backfill: dict[str, int] = {}
        #: Result of the last project backfill (see :meth:`Store.backfill_projects`).
        self.last_project_backfill: dict[str, int] = {}

    def close(self) -> None:
        if self.owns_store:
            self.store.close()

    def __enter__(self) -> "Scanner":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def run(self, *, only: Iterable[str] | None = None) -> ScanReport:
        """Run one incremental scan across all (or selected) adapters."""
        wanted = set(only) if only else None
        report = ScanReport()
        checkpoints = self.store.load_checkpoints()

        for adapter in self.adapters:
            if wanted is not None and adapter.name not in wanted:
                continue
            ar = AdapterReport(name=adapter.name)
            report.adapters.append(ar)
            try:
                sources = adapter.detect()
                ar.sources = len(sources)
                if not sources:
                    logger.debug("adapter %s: no sources found", adapter.name)
                    continue

                result = adapter.scan(checkpoints)
                ar.events_seen = len(result.events)
                ar.warnings.extend(result.warnings)
                for w in result.warnings:
                    logger.warning("[%s] %s", adapter.name, w)

                counts = self.store.upsert_events(result.events)
                ar.inserted = counts["inserted"]
                ar.updated = counts["updated"]
                ar.unchanged = counts["unchanged"]

                # Titles are per session, not per event, so they persist even
                # when a rescan produces no new usage rows.
                self.store.upsert_titles(adapter.name, result.titles)

                self.store.save_checkpoints(result.checkpoints)
            except Exception as e:  # noqa: BLE001 - contain adapter failures
                ar.error = f"{type(e).__name__}: {e}"
                logger.exception("adapter %s failed", adapter.name)

        # Rows written before provider resolution existed cannot be reached by
        # an incremental rescan (the checkpoint already passed them), so fill
        # them from their event_key once per run.
        try:
            self.last_backfill = self.store.backfill_providers(self)
        except Exception:  # noqa: BLE001 - a backfill must never fail a scan
            logger.exception("provider backfill failed")
            self.last_backfill = {}

        # The project dimension has no such shortcut: the working directory only
        # exists in the agent's own log, so this re-reads the heads of the
        # session files once and caches nothing.
        try:
            self.last_project_backfill = self.store.backfill_projects(self)
        except Exception:  # noqa: BLE001
            logger.exception("project backfill failed")
            self.last_project_backfill = {}
        return report

    def scan(self, *, only: Iterable[str] | None = None) -> ScanReport:
        """Alias so callers can write ``Scanner().scan()``."""
        return self.run(only=only)

"""agent-token-radar: local, read-only, multi-agent TOKEN usage statistics.

Phase 1 delivers the architecture only:

* :mod:`base`      -- ``BaseAdapter`` ABC + normalized ``UsageEvent`` +
                      ``Checkpoint`` / ``ScanResult`` / reports
* :mod:`paths`     -- cross-platform (Windows/Linux) read-only path resolution
* :mod:`readers`   -- JSONL incremental reads, multi-frame zstd decode,
                      strictly read-only SQLite temp copies
* :mod:`store`     -- SQLite ``usage_events`` (UNIQUE event_key + UPSERT) and
                      ``scan_checkpoints`` (file_path / mtime / byte_offset)
* :mod:`registry`  -- automatic adapter registration from ``adapters/*.py``
* :mod:`scanner`   -- orchestration with per-adapter failure containment
* :mod:`adapters`  -- 5 confirmed agents (Codex, Claude, Hermes, WorkBuddy, DSH)

Token counts only. No price / cost calculation. Actual per-agent extraction
is implemented in Phase 2.
"""

from .base import (
    AdapterReport,
    BaseAdapter,
    Checkpoint,
    ScanReport,
    ScanResult,
    Source,
    UsageEvent,
)
from .paths import default_db_path
from .scanner import Scanner
from .store import Store

__version__ = "0.0.1"

__all__ = [
    "BaseAdapter",
    "UsageEvent",
    "Source",
    "Checkpoint",
    "ScanResult",
    "ScanReport",
    "AdapterReport",
    "Scanner",
    "Store",
    "default_db_path",
]

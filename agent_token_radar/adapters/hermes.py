"""Hermes adapter.

Authoritative source (DISCOVERY.md 2.3)
--------------------------------------
``%LOCALAPPDATA%/hermes/state.db`` (Linux: ``$XDG_DATA_HOME/hermes/state.db``).

``session_model_usage`` holds per-(session, model, task) AGGREGATES with a
complete disjoint set of token columns. The ``sessions`` table holds another
whole-session rollup -- used for cross-checking ONLY, never inserted alongside
``session_model_usage`` (that would double-count). ``estimated_cost_usd`` /
``actual_cost_usd`` are deliberately IGNORED: Hermes computes those with its own
price table, and this tool bills every agent from one shared pricing.json so the
figures stay comparable across agents. Mixing the two would make Hermes look
like it was priced differently from everyone else for no stated reason.

event_key = ``hermes|{session_id}|{model}|{task}``

The key deliberately carries NO timestamp. ``session_model_usage`` is an
AGGREGATE table: its row for one (session, model, task) keeps growing while the
session is active, and ``last_seen`` changes with it. Including ``last_seen``
(as an earlier version did) minted a NEW key on every scan of an active
session, so the UPSERT never matched, the older rows were never replaced, and
the cumulative counters were added again on top of themselves -- the same
session counted twice, three times, once per scan. A business-identity key lets
the UPSERT overwrite the row with the current cumulative totals instead.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import config_dir, home, local_data_dir
from ..readers import readonly_sqlite_copy
from ._common import glob_sources
from ._helpers import make_event, safe_int, seconds_to_ms

_SELECT = """
SELECT session_id, model, task,
       input_tokens, output_tokens, cache_read_tokens,
       cache_write_tokens, reasoning_tokens,
       first_seen, last_seen, api_call_count
FROM session_model_usage
"""


class HermesAdapter(BaseAdapter):
    name = "hermes"
    display_name = "Hermes"
    home_env = "HERMES_HOME"

    def detect(self) -> list[Source]:
        roots: list[Path] = [
            home() / ".hermes",
            local_data_dir() / "hermes",
            config_dir() / "hermes",
            home() / ".local" / "share" / "hermes",
        ]
        if self.home_env and os.environ.get(self.home_env):
            roots.insert(0, Path(os.environ[self.home_env]).expanduser())
        return glob_sources(roots, "state.db", kind="sqlite", agent=self.name)

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        warn = result.warnings.append

        for src in self.detect():
            key = str(src.path)
            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                try:
                    rows = con.execute(_SELECT).fetchall()
                except Exception as e:  # noqa: BLE001 - schema drift must not abort
                    warn(f"{key}: cannot read session_model_usage ({e.__class__.__name__}: {e}); skipped")
                    continue

                for r in rows:
                    sid = str(r["session_id"] or "")
                    model = str(r["model"] or "")
                    task = str(r["task"] or "")
                    last_ms = seconds_to_ms(r["last_seen"])
                    ts = last_ms or seconds_to_ms(r["first_seen"])
                    # No timestamp in the key: this table is cumulative, so a
                    # changing last_seen must update the SAME row rather than
                    # insert a new one on every scan. See the module docstring.
                    ekey = f"hermes|{sid}|{model}|{task}"

                    # Hermes logs no provider; its model strings carry a
                    # ``provider/model`` prefix when it proxies one.
                    provider, provider_source = self.resolve_provider(
                        self.name, model
                    )

                    result.events.append(
                        make_event(
                            agent=self.name,
                            event_key=ekey,
                            source_file=key,
                            model=model,
                            provider=provider,
                            provider_source=provider_source,
                            session_id=sid,
                            timestamp=ts,
                            input_tokens=safe_int(r["input_tokens"]),
                            output_tokens=safe_int(r["output_tokens"]),
                            cache_read_tokens=safe_int(r["cache_read_tokens"]),
                            cache_write_tokens=safe_int(r["cache_write_tokens"]),
                            reasoning_tokens=safe_int(r["reasoning_tokens"]),
                        )
                    )

            # Aggregates are keyed by business identity; size+mtime gating is
            # all a checkpoint can honestly record for a SQLite source.
            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result

"""SQLite storage for agent-token-radar (Phase 1).

Two tables, exactly as specified:

``usage_events``
    One row per de-duplicated usage record. ``event_key`` is UNIQUE and is
    the UPSERT target: re-scanning the same source updates the row in place,
    so streaming output or a repeated scan can never double-count tokens.

``scan_checkpoints``
    ``file_path`` (PK), ``mtime``, ``byte_offset`` -- plus ``size`` /
    ``updated_at`` extensions. JSONL sources use ``byte_offset`` for
    incremental reads; zstd-framed / SQLite sources use size+mtime gating.

Tokens are measured and stored; cost is derived, never stored. ``usage_events``
carries no cost column on purpose -- a price is joined onto it at query time
from ``model_prices`` (see ``pricing.py``), so a repriced or withdrawn model
takes effect without rewriting a single historical row. Cost is also
time-of-day aware (DeepSeek peak/off-peak), and that decision depends on each
row's own timestamp rather than on when the report is generated.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .base import (
    SCHEMA_VERSION,
    USAGE_COLUMNS,
    Checkpoint,
    CheckpointMap,
    UsageEvent,
)
from .paths import default_db_path, ensure_own_dirs
from .pricing import PriceTable, cost_sql, ensure_price_table, money
from .pricing import UNCACHED_INPUT_SQL
from .provider_sources import SOURCE_STRENGTH

logger = logging.getLogger("agent_token_radar.store")

# ``UNCACHED_INPUT_SQL`` is DEFINED in :mod:`pricing` (and re-imported above) so
# that the display aggregates here and ``COST_ROW_SQL`` there share one
# definition: agents disagree about whether ``input_tokens`` already contains
# the cache buckets, and billing the raw input column double-charges the cache
# hits of the inclusive agents (Codex, WorkBuddy). Two copies of that expression
# is exactly how the display and the money drifted apart before.

#: SQL for the cache hit rate in [0, 1]: cached reads over all prompt tokens.
#:
#: Written as ``SUM(cached) / (SUM(uncached) + SUM(cached))`` -- aggregate the
#: numerator and denominator separately, then divide. The obvious per-row form
#: (``AVG(cache_read / (uncached + cache_read))``) is wrong: it averages ratios,
#: so a row with an empty prompt contributes a 0.0 that drags the total down
#: (measured: 0% overall against a true 96.8%). Zero when there is no prompt at
#: all, so an empty dataset cannot divide by zero.
#:
#: The result is NULL when the underlying data is self-contradictory. A hit
#: rate is a ratio and cannot exceed 100%, but the denominator is built from
#: :data:`UNCACHED_INPUT_SQL`, which goes negative on corrupt input (a row
#: claiming more cache reads than its own total). One such row -- total=10,
#: cache_read=50 -- makes the denominator 10 while the numerator stays 50 and
#: would yield 500%; ``app.js`` only guards against <= 0 and non-finite values,
#: so that would have been rendered verbatim.
#:
#: The obvious fix -- clamp to 1.0 -- is WORSE than the bug: it would show a
#: spotless "100%" for data that is broken, disguising the defect as a perfect
#: cache. NULL is the honest answer: no rate can be computed from a
#: contradiction. The UI already renders NULL as a dash (``moneyCell``'s
#: convention, and ``pct()`` guards non-finite), so the reader sees "unknown"
#: rather than a number nobody can defend. ``uncached_input_tokens`` itself is
#: still reported negative, so the bad row remains visible for diagnosis.
#:
#: On sane data this is a no-op: the real database's 77k rows all have
#: uncached >= 3, so every figure is unchanged.
CACHE_HIT_SQL = (
    # A hit rate can never exceed 100% -- cached reads are a subset of the
    # prompt. Wherever the numbers make that impossible, no rate is knowable
    # and the answer is NULL, which the UI renders as a dash.
    #
    # Three ways the data can be impossible:
    #   * numerator exceeds the denominator (cached > prompt total)
    #   * the denominator is negative (the row claims more cache than it has
    #     tokens at all, so the "prompt" is not a quantity)
    #
    # NULL rather than a clamp: clamping printed a spotless "100%" for data
    # known to be broken, which is a quieter lie than the 500% it replaces.
    #
    # A denominator of exactly 0 is the EMPTY case, not a corrupt one (a row
    # with no prompt at all), and that is a true 0%.
    #
    # The numerator/denominator test must be on the ratio, not on the
    # denominator alone: for total=10, cache_read=50 the denominator is still
    # positive (-40 + 50 = 10) while the numerator is 50.
    f"CASE WHEN (SUM({UNCACHED_INPUT_SQL}) + SUM(cache_read_tokens)) = 0 "
    f"THEN 0.0 "
    f"WHEN (SUM({UNCACHED_INPUT_SQL}) + SUM(cache_read_tokens)) > 0 "
    f"AND CAST(SUM(cache_read_tokens) AS REAL) "
    f"<= (SUM({UNCACHED_INPUT_SQL}) + SUM(cache_read_tokens)) "
    f"THEN MIN(1.0, CAST(SUM(cache_read_tokens) AS REAL) "
    f"/ (SUM({UNCACHED_INPUT_SQL}) + SUM(cache_read_tokens))) "
    f"ELSE NULL END"
)

#: SQL for one row's reference cost in USD, joined against ``model_prices``.
#:
#: NULL when the model has no official price. That NULL is load-bearing: the
#: aggregate queries use ``SUM(cost_usd)`` and a NULL is skipped, so an unpriced
#: model is reported as *missing* rather than silently counted as $0.00. The
#: coverage figure in every response tells the reader how much of the tokens
#: that SUM actually covers.
#:
#: Peak vs off-peak is decided per row from the row's own timestamp, so this is
#: a callable rather than a constant: ``pricing.cost_sql()`` renders the peak
#: predicate in. A vendor with no schedule bills the peak column always.
COST_ROW_SQL = cost_sql()

#: LEFT JOIN so unpriced models still produce a row (with a NULL cost).
PRICE_JOIN_SQL = "LEFT JOIN model_prices mp ON mp.model = usage_events.model"

#: Tokens that a cost figure actually covers, for the coverage ratio.
PRICED_TOKENS_SQL = (
    "SUM(CASE WHEN mp.price_key IS NOT NULL AND mp.price_key <> '' "
    "THEN usage_events.total_tokens ELSE 0 END)"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_events (
    event_key          TEXT PRIMARY KEY,
    agent              TEXT NOT NULL,
    model              TEXT NOT NULL DEFAULT '',
    provider           TEXT NOT NULL DEFAULT '',
    provider_source    TEXT NOT NULL DEFAULT '',
    served_model       TEXT NOT NULL DEFAULT '',
    session_id         TEXT NOT NULL DEFAULT '',
    project            TEXT NOT NULL DEFAULT '',
    timestamp          INTEGER NOT NULL DEFAULT 0,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens       INTEGER NOT NULL DEFAULT 0,
    source_file        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scan_checkpoints (
    file_path    TEXT PRIMARY KEY,
    mtime        REAL NOT NULL DEFAULT 0,
    byte_offset  INTEGER NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One title per session. Kept apart from usage_events so a session with
-- thousands of usage rows still stores its title exactly once.
CREATE TABLE IF NOT EXISTS session_titles (
    agent      TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, session_id)
);

CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_agent    ON usage_events(agent, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_model    ON usage_events(model, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_session  ON usage_events(session_id);
-- by_project groups on (project, agent) with a "project <> ''" filter. Without
-- this the whole table is scanned for a grouping that only needs those two
-- columns, which was the single largest cost of a whole-history report.
CREATE INDEX IF NOT EXISTS idx_usage_project  ON usage_events(project, agent);
"""

_UPSERT_COLUMNS = ", ".join(USAGE_COLUMNS)
_UPSERT_PLACEHOLDERS = ", ".join("?" for _ in USAGE_COLUMNS)
# On conflict, update everything except the key. This is what makes a second
# scan idempotent AND lets a corrected/late-arriving sample replace an earlier
# partial one (e.g. streamed output) without ever adding a duplicate row.
_UPSERT_UPDATES = ", ".join(
    f"{c}=excluded.{c}" for c in USAGE_COLUMNS if c != "event_key"
)
UPSERT_SQL = (
    f"INSERT INTO usage_events ({_UPSERT_COLUMNS}) "
    f"VALUES ({_UPSERT_PLACEHOLDERS}) "
    f"ON CONFLICT(event_key) DO UPDATE SET {_UPSERT_UPDATES}"
)


def _strip_session_prefix(session_id: str) -> str:
    """``session-abc`` -> ``abc``; ids are written both ways by the agents."""
    return session_id[len("session-"):] if session_id.startswith("session-") else session_id


class Store:
    """SQLite wrapper with one connection PER THREAD.

    ``sqlite3`` connections are bound to the thread that created them. The
    web dashboard runs synchronous endpoints inside Starlette's threadpool
    while a daemon thread performs background scans, so a single shared
    connection raises ``sqlite3.ProgrammingError``. ``threading.local()``
    gives every thread its own WAL connection to the same database; WAL mode
    lets readers proceed while a scan writes.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._closed = False
        self.prices = PriceTable.load()
        # Whole-table statistics that do not depend on the time range. They are
        # served from here until events are written; see provider_stats().
        self._stats_cache: dict[str, Any] = {}
        # Initialize schema once on this thread; other threads do it lazily.
        self._init_connection(self.con)

    def _init_connection(self, con: sqlite3.Connection) -> None:
        con.row_factory = sqlite3.Row
        with con:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA busy_timeout = 5000")
            # SQLite's default page cache is 2 MB, while this database is ~41 MB
            # of mostly-cold pages: a whole-history report re-read them from disk
            # five times over. 64 MB keeps the working set resident, which is
            # what turned `report over everything` from seconds into sub-second.
            # Negative means kibibytes, so -65536 == 64 MiB.
            con.execute("PRAGMA cache_size = -65536")
            # Give the sorter the same headroom: every aggregate does a GROUP BY
            # over the whole table and the default is small enough to spill.
            con.execute("PRAGMA temp_store = MEMORY")
            con.executescript(SCHEMA_SQL)
            self._migrate(con)
            con.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            # The official list prices live in the same database as the events
            # so one query can join them and every existing aggregate can sum a
            # cost column. pricing.json stays the source of truth and is
            # re-applied on every open, so a repriced or withdrawn model cannot
            # linger from an earlier run.
            try:
                ensure_price_table(con, self.prices)
            except Exception:
                logger.exception("could not load the model price table")

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a
        database from an earlier build would silently keep the old shape and
        every INSERT naming the new column would fail. Each step is idempotent:
        ``PRAGMA table_info`` is the source of truth, not the version marker.
        """
        existing = {r["name"] for r in con.execute("PRAGMA table_info(usage_events)")}
        addable = {
            # provider: which backend served the model. Historic rows keep ''
            # (unknown) rather than a guess; a rescan fills real values in via
            # the event_key UPSERT.
            "provider": "TEXT NOT NULL DEFAULT ''",
            # provider_source: 'measured' | 'inferred' | ''. Historic rows keep
            # '' and are filled by backfill_providers() from their event_key.
            "provider_source": "TEXT NOT NULL DEFAULT ''",
            # served_model: what the backend actually served, when a local
            # gateway measured it (Claude Code logs routing aliases).
            "served_model": "TEXT NOT NULL DEFAULT ''",
            # project: the working directory, i.e. which project the tokens
            # were spent on. Empty for rows written before this column existed.
            "project": "TEXT NOT NULL DEFAULT ''",
        }
        for column, decl in addable.items():
            if column not in existing:
                con.execute(f"ALTER TABLE usage_events ADD COLUMN {column} {decl}")
                logger.info("migrated usage_events: added column %s", column)

        # ---- one-shot purge of placeholder rows -------------------------
        # Earlier builds stored requests that never moved a single token: Claude
        # Code's local ``<synthetic>`` messages ("No response requested.") and
        # Antigravity's failed generations (``FAILED_PRECONDITION``, no model).
        # They contribute nothing to any total -- every bucket is 0, so the cache
        # denominator is 0 too -- but they surfaced as an ``unknown`` model row.
        # Removing them changes no aggregate; guarded by a meta marker so the
        # table is scanned at most once per database.
        # ---- sweep placeholder rows -------------------------------------
        # Earlier builds stored requests that never moved a single token: Claude
        # Code's local ``<synthetic>`` messages ("No response requested.") and
        # Antigravity's failed generations (``FAILED_PRECONDITION``, no model).
        # They contribute to no aggregate -- every bucket is 0, so the cache
        # denominator is 0 -- yet they surfaced as an ``unknown`` model row and
        # inflated the request count. The sweep is unconditional rather than
        # one-shot: a row written by an older build (or an older running
        # process) reappears after the marker is set, and only an every-open
        # sweep is self-healing. It is a plain full scan on an local table.
        cur = con.execute(
            """DELETE FROM usage_events
                WHERE total_tokens = 0
                  AND input_tokens = 0
                  AND output_tokens = 0
                  AND cache_read_tokens = 0
                  AND cache_write_tokens = 0
                  AND reasoning_tokens = 0"""
        )
        if cur.rowcount and cur.rowcount > 0:
            logger.info("swept %d zero-token placeholder rows", cur.rowcount)

    @property
    def con(self) -> sqlite3.Connection:
        """Thread-local connection (created on first use in each thread)."""
        c = getattr(self._local, "con", None)
        if c is None:
            if self._closed:
                raise RuntimeError("Store is closed")
            c = sqlite3.connect(str(self.db_path))
            self._init_connection(c)
            self._local.con = c
        return c

    @classmethod
    def default(cls) -> "Store":
        return cls(ensure_own_dirs())

    def close(self) -> None:
        """Close this thread's connection. Other threads keep theirs."""
        self._closed = True
        c = getattr(self._local, "con", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._local.con = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- writes
    def upsert_events(self, events: Iterable[UsageEvent]) -> dict[str, int]:
        """UPSERT a batch. Returns ``{inserted, updated, unchanged}``.

        ``unchanged`` counts rows whose normalized content was already
        identical (useful for the CLI's "new records" figure and for the
        idempotency tests in Phase 2).
        """
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        rows: list[tuple[Any, ...]] = []
        keys: list[str] = []
        for ev in events:
            n = ev.normalized()
            if not n.event_key:
                logger.warning("dropping UsageEvent with empty event_key (agent=%s)", n.agent)
                continue
            rows.append(n.to_row())
            keys.append(n.event_key)

        if not rows:
            return counts

        # Read the pre-existing rows once so we can classify insert vs update
        # vs no-op without a trigger or a second connection.
        before: dict[str, tuple[Any, ...]] = {}
        CHUNK = 400
        for i in range(0, len(keys), CHUNK):
            part = keys[i : i + CHUNK]
            q = f"SELECT * FROM usage_events WHERE event_key IN ({','.join('?' for _ in part)})"
            for r in self.con.execute(q, part):
                before[r["event_key"]] = tuple(r[c] for c in USAGE_COLUMNS)

        with self.con:
            self.con.executemany(UPSERT_SQL, rows)

        # Any write changes the whole-table statistics the footer reads.
        self.invalidate_stats_cache()

        for row in rows:
            key = row[0]
            old = before.get(key)
            if old is None:
                counts["inserted"] += 1
            elif old == row:
                counts["unchanged"] += 1
            else:
                counts["updated"] += 1
        return counts

    def upsert_titles(self, agent: str, titles: Mapping[str, str]) -> int:
        """Store/refresh human-readable session titles. Returns rows written.

        A title is only overwritten by a non-empty one, so an incremental scan
        that sees an older record without a title cannot blank a good value.
        """
        if not titles:
            return 0
        now = int(time.time() * 1000)
        payload = [
            (agent, str(sid), str(title), now)
            for sid, title in titles.items()
            if sid and title
        ]
        if not payload:
            return 0
        with self.con:
            self.con.executemany(
                """INSERT INTO session_titles (agent, session_id, title, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(agent, session_id) DO UPDATE SET
                     title      = CASE WHEN excluded.title <> ''
                                       THEN excluded.title ELSE session_titles.title END,
                     updated_at = excluded.updated_at""",
                payload,
            )
        return len(payload)

    def backfill_projects(self, scanner=None, *, limit: int = 40000) -> dict[str, int]:
        """Fill ``project`` for rows written before the column existed.

        Unlike provider, a project cannot be derived from ``event_key``: the
        working directory only exists in the agent's own log. So this re-reads
        the *sources* and maps ``(agent, session_id) -> project``.

        Re-reading is cheap in the common case because the session id is on the
        same record as the cwd, and a session almost always uses one directory.
        Rows whose session cannot be found stay empty rather than guessed.
        """
        from .adapters._helpers import decode_dsh_project_dir, project_label

        counts = {"sessions": 0, "rows": 0}
        session_project: dict[tuple[str, str], str] = {}

        if scanner is not None:
            for adapter in scanner.adapters:
                name = adapter.name
                try:
                    sources = adapter.detect()
                except Exception:  # noqa: BLE001 - a broken agent must not stop this
                    continue
                for src in sources:
                    try:
                        self._read_projects_from_source(
                            name, src, session_project,
                            project_label=project_label,
                            decode_dir=decode_dsh_project_dir,
                        )
                    except Exception:  # noqa: BLE001
                        continue

        if not session_project:
            return counts
        counts["sessions"] = len(session_project)

        rows = self.con.execute(
            """SELECT DISTINCT agent, session_id FROM usage_events
               WHERE project = '' AND session_id <> ''
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
        updates: list[tuple[str, str, str]] = []
        for r in rows:
            agent = str(r["agent"] or "")
            sid = str(r["session_id"] or "")
            project = (
                session_project.get((agent, sid))
                # DSH stores ``session-<uuid>`` in usage rows while its
                # directory is named with the bare uuid.
                or session_project.get((agent, _strip_session_prefix(sid)))
            )
            if project:
                updates.append((project, agent, sid))
        if updates:
            with self.con:
                self.con.executemany(
                    """UPDATE usage_events SET project = ?
                       WHERE agent = ? AND session_id = ? AND project = ''""",
                    updates,
                )
            self.invalidate_stats_cache()
            counts["rows"] = len(updates)
        return counts

    @staticmethod
    def _read_projects_from_source(
        agent: str,
        src,
        out: dict[tuple[str, str], str],
        *,
        project_label,
        decode_dir,
    ) -> None:
        """Extract ``(agent, session_id) -> project`` from one source file.

        Deliberately cheap. Reading every agent log in full to recover one
        string would cost minutes (DSH alone has ~780 logs / 2-3 GB), so:

        * DSH's project IS the encoded directory name -- no file is opened;
        * JSONL logs are read only until the cwd is found, which is the first
          record for Codex (``session_meta``) and an early record for Claude.
        """
        import json as _json

        path = src.path

        # DSH: the project lives in the directory name, not in any record.
        if src.kind == "zstd-jsonl":
            try:
                out.setdefault(
                    (agent, path.parent.name),
                    decode_dir(path.parent.parent.name),
                )
            except (IndexError, AttributeError):
                pass
            return

        if src.kind != "jsonl":
            return

        # A session uses one working directory; the first record that names it
        # is enough, so this stops as soon as both ids are known.
        MAX_LINES = 400
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return
        try:
            for i, line in enumerate(fh):
                if i > MAX_LINES:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    rec = _json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                rec_cwd = rec.get("cwd")
                payload = rec.get("payload")
                # Codex nests the cwd inside ``payload``; Claude keeps it at the
                # top level. Both shapes appear in one scan, so both are read.
                if not isinstance(rec_cwd, str) and isinstance(payload, dict):
                    rec_cwd = payload.get("cwd")
                cwd = project_label(rec_cwd)
                if not cwd:
                    continue
                for key in ("sessionId", "session_id"):
                    sid = rec.get(key)
                    if isinstance(sid, str) and sid:
                        out.setdefault((agent, sid), cwd)
                if isinstance(payload, dict):
                    for key in ("session_id", "thread_id", "id"):
                        sid = payload.get(key)
                        if isinstance(sid, str) and sid:
                            out.setdefault((agent, sid), cwd)
        finally:
            fh.close()

    def backfill_providers(self, scanner=None, *, limit: int = 20000) -> dict[str, int]:
        """Fill ``provider`` / ``provider_source`` for rows written before this.

        Historic rows cannot be re-derived by rescanning: the incremental
        checkpoint is already past those records, so an incremental scan sees an
        empty tail and never re-emits them. The request id does survive, inside
        ``event_key``, so the resolution is repeated here from that key alone.

        Three kinds of gap are repaired:

        * ``provider = ''`` -- nothing was ever resolved; fill both columns;
        * ``provider <> '' AND provider_source = ''`` -- e.g. DSH rows written
          before provenance was tracked. The provider is already correct and
          written by the agent itself, so only the label is filled;
        * ``provider_source`` in ``inferred`` / ``product`` -- a fallback whose
          rules may have improved since. These are recomputed.

        ``scanner`` is accepted for API compatibility; the resolution is
        deliberately derived from ``event_key`` + the agent's own product
        identity so a backfill can never *upgrade* a row's evidence.

        Note on Codex: its ``session_meta.model_provider`` says ``openai`` (the
        API vendor), while magpie measures the same backend as ``codex``. Reading
        the declared value would split one model into two providers for the same
        upstream, so the product identity is used instead and labelled
        ``product`` -- honest about being a decision rather than a measurement,
        without inventing a distinction that does not exist.
        """
        del scanner  # documented above: unused on purpose

        from .provider_sources import (
            SOURCE_INFERRED,
            SOURCE_MEASURED,
            SOURCE_NONE,
            SOURCE_PRODUCT,
            request_id_from_event_key,
            resolver,
        )

        counts = {
            SOURCE_NONE: 0,
            SOURCE_MEASURED: 0,
            SOURCE_PRODUCT: 0,
            SOURCE_INFERRED: 0,
            "labelled": 0,
            "served": 0,
            "rows": 0,
        }

        # ---- pass 0: undo a wrong "measured" label written by an earlier build.
        # One release read Codex's ``session_meta.model_provider`` (``openai``,
        # the API vendor) and marked those rows measured. That value is not used
        # any more -- magpie measures the same upstream as ``codex``, so keeping
        # both would split one model across two provider names -- but a measured
        # row is never recomputed, so this has to be reset explicitly.
        #
        # Scoped to Codex + ``openai`` so it can never touch a real measurement
        # from another agent.
        with self.con:
            self.con.execute(
                """UPDATE usage_events
                      SET provider = '', provider_source = ''
                    WHERE agent = 'codex' AND provider = 'openai'"""
            )

        # ---- pass 1: a provider exists but its provenance was never recorded.
        # Rows written before this column existed all have an empty label, and
        # those providers came from the agents' own records -> measured.
        row = self.con.execute(
            """SELECT COUNT(*) FROM usage_events
               WHERE provider <> '' AND provider_source = ''"""
        ).fetchone()
        pending = int(row[0] if row else 0)
        if pending:
            with self.con:
                self.con.execute(
                    "UPDATE usage_events SET provider_source = ? "
                    "WHERE provider <> '' AND provider_source = ''",
                    (SOURCE_MEASURED,),
                )
            counts["labelled"] = pending

        # ---- pass 2: rows with no provider, plus rows whose provider is only a
        # guess. An inferred value is a function of the rules in
        # ``provider_sources``; when those rules improve, the old guess is simply
        # wrong, so it is recomputed. A *measured* row is never touched -- it
        # came from evidence and re-deriving it could only lose information.
        rows = self.con.execute(
            """SELECT event_key, agent, model, session_id, provider, provider_source
                 FROM usage_events
                WHERE (provider = '' AND model <> '')
                   OR provider_source IN ('inferred', 'product', '')
                LIMIT ?""",
            (int(limit),),
        ).fetchall()
        if rows:
            updates: list[tuple[str, str, str, str]] = []
            for r in rows:
                counts["rows"] += 1
                agent = str(r["agent"] or "")
                model = str(r["model"] or "")
                was_fallback = str(r["provider_source"] or "") in (
                    SOURCE_INFERRED, SOURCE_PRODUCT,
                )
                rid = request_id_from_event_key(agent, str(r["event_key"] or ""))
                provider, source = resolver.resolve(agent, model, rid)
                if not provider:
                    counts[SOURCE_NONE] += 1
                    if was_fallback:
                        # No longer explainable: clear the stale guess rather
                        # than keep a value the rules no longer support.
                        updates.append(("", "", "", str(r["event_key"])))
                    continue
                counts[source] = counts.get(source, 0) + 1
                updates.append(
                    (provider, source, resolver.served_model(rid),
                     str(r["event_key"]))
                )

            if updates:
                with self.con:
                    self.con.executemany(
                        """UPDATE usage_events
                              SET provider = ?, provider_source = ?, served_model = ?
                            WHERE event_key = ?
                              AND (provider = ''
                                   OR provider_source IN ('inferred', 'product', ''))""",
                        updates,
                    )
                self.invalidate_stats_cache()

        # ---- pass 3: rows that were resolved before served models were tracked.
        # Claude Code's rows are the important case here: their alias looked
        # like a real model until the gateway's own record was consulted.
        rows = self.con.execute(
            """SELECT event_key, agent FROM usage_events
               WHERE served_model = '' AND agent IN ('claude', 'codex')
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
        if rows:
            served_updates: list[tuple[str, str]] = []
            for r in rows:
                agent = str(r["agent"] or "")
                rid = request_id_from_event_key(agent, str(r["event_key"] or ""))
                real = resolver.served_model(rid)
                if real:
                    served_updates.append((real, str(r["event_key"])))
            counts["served"] = len(served_updates)
            if served_updates:
                with self.con:
                    self.con.executemany(
                        "UPDATE usage_events SET served_model = ? WHERE event_key = ?",
                        served_updates,
                    )
        else:
            counts["served"] = 0
        return counts

    def provider_stats(self) -> dict[str, Any]:
        """How the stored providers were obtained, per agent.

        Used by the dashboard footer so the user can see how much of the data is
        measured versus guessed instead of trusting a single global number.

        Whole-table and range-independent, so it is cached: measured at ~65 ms
        per call on 76k rows, and it was being paid on every range switch even
        though the answer never changes between scans. Cleared by
        ``invalidate_stats_cache`` whenever events are written.
        """
        cached = self._stats_cache.get("provider")
        if cached is not None:
            return cached
        rows = self.con.execute(
            """SELECT agent, provider_source, COUNT(*) AS n
               FROM usage_events GROUP BY agent, provider_source"""
        ).fetchall()
        per_agent: dict[str, dict[str, int]] = {}
        totals = {"measured": 0, "product": 0, "inferred": 0, "": 0}
        for r in rows:
            agent = str(r["agent"] or "")
            src = str(r["provider_source"] or "")
            n = int(r["n"] or 0)
            slot = per_agent.setdefault(
                agent, {"measured": 0, "product": 0, "inferred": 0, "": 0}
            )
            slot[src] = slot.get(src, 0) + n
            totals[src] = totals.get(src, 0) + n
        out = {"per_agent": per_agent, "totals": totals}
        self._stats_cache["provider"] = out
        return out

    def by_project(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Usage per project, merged across agents, biggest first.

        A project is the agent's working directory. The same folder is often
        opened by several agents (measured here: ``D:/UEProjects/Untitled Game``
        under both Codex and DSH), and listing them as separate top-level rows
        hides how much that project really costs. So the headline row is the
        PROJECT, with a nested ``agents`` list carrying each agent's own
        numbers -- the dashboard shows one row and expands it on demand.

        ``limit`` therefore counts projects, not (project, agent) pairs.

        Rows written before ``project`` existed simply do not appear until a
        rescan fills them in (an incremental scan cannot reach them; a full
        rescan can, and so can the backfill below).
        """
        where, args = self._where(since_ms, until_ms)
        clause = "project <> ''"
        where = f"{where} AND {clause}" if where else f" WHERE {clause}"
        rows = self.con.execute(
            f"""SELECT usage_events.project AS project, usage_events.agent AS agent,
                       COUNT(*) AS events,
                       SUM(usage_events.total_tokens) AS total_tokens,
                       SUM(usage_events.input_tokens) AS input_tokens,
                       SUM(usage_events.output_tokens) AS output_tokens,
                       SUM(usage_events.cache_read_tokens) AS cache_read_tokens,
                       SUM(usage_events.cache_write_tokens) AS cache_write_tokens,
                       SUM({UNCACHED_INPUT_SQL}) AS uncached_input_tokens,
                       SUM(usage_events.reasoning_tokens) AS reasoning_tokens,
                       {CACHE_HIT_SQL} AS cache_hit,
                       SUM({COST_ROW_SQL}) AS cost_usd,
                       {PRICED_TOKENS_SQL} AS priced_tokens
                FROM usage_events {PRICE_JOIN_SQL} {where}
                GROUP BY usage_events.project, usage_events.agent""",
            args,
        ).fetchall()

        # Merge in Python rather than SQL so the per-agent detail survives.
        # Every number is a SUM, so adding two groups is exact; the hit rate is
        # then recomputed from the merged sums (never averaged from the parts --
        # that would weight a one-request agent like a thousand-request one).
        merged: dict[str, dict[str, Any]] = {}
        for r in rows:
            project = str(r["project"] or "")
            bucket = merged.get(project)
            if bucket is None:
                bucket = merged[project] = {
                    "project": project,
                    "events": 0,
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "uncached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost_usd": 0.0,
                    # Whether ANY row in this group carried a price. Needed
                    # because 0.0 is a real cost ("priced, and it rounded to
                    # nothing") while "no priced row at all" must stay NULL and
                    # render as a dash -- never as a free-looking $0.00. The
                    # per-agent rows below already distinguish the two; the
                    # project total used to collapse them into 0.0.
                    "has_cost": False,
                    "priced_tokens": 0,
                    "agents": [],
                }
            for col in ("events", "total_tokens", "input_tokens", "output_tokens",
                        "cache_read_tokens", "cache_write_tokens",
                        "uncached_input_tokens", "reasoning_tokens", "priced_tokens"):
                bucket[col] += int(r[col] or 0)
            # A NULL cost means "no official price", so it adds nothing.
            row_cost = r["cost_usd"]
            if row_cost is not None:
                bucket["cost_usd"] += float(row_cost)
                bucket["has_cost"] = True
            bucket["agents"].append({
                "agent": str(r["agent"] or ""),
                "events": int(r["events"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                "cache_write_tokens": int(r["cache_write_tokens"] or 0),
                "uncached_input_tokens": int(r["uncached_input_tokens"] or 0),
                # None (not 0.0) when the data is self-contradictory: the UI
                # must show "unknown", not a confident 0%.
                "cache_hit": (None if r["cache_hit"] is None
                              else float(r["cache_hit"])),
                "cost_usd": (None if row_cost is None else float(row_cost)),
                "priced_tokens": int(r["priced_tokens"] or 0),
            })

        out: list[dict[str, Any]] = []
        for bucket in merged.values():
            # Reuse the shared mirror instead of dividing here: this site had
            # its own copy of the formula, which is how it kept an unclamped
            # 500% after the SQL and the mirror were both fixed.
            bucket["cache_hit"] = self._cache_hit(bucket)
            # No priced row in this project at all -> NULL, so the UI shows a
            # dash. Hiding that as $0.00 would claim the project was free.
            if not bucket.pop("has_cost"):
                bucket["cost_usd"] = None
            # Agents inside a project are ordered by their own spend.
            bucket["agents"].sort(key=lambda a: -a["total_tokens"])
            out.append(bucket)
        out.sort(key=lambda b: -b["total_tokens"])
        return out[: int(limit)]

    def project_stats(self) -> dict[str, Any]:
        """How many rows carry a project, so the UI can say so honestly.

        Cached for the same reason as ``provider_stats``: whole-table, and the
        answer only changes when events are written.
        """
        cached = self._stats_cache.get("project")
        if cached is not None:
            return cached
        row = self.con.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN project <> '' THEN 1 ELSE 0 END) AS with_project
               FROM usage_events"""
        ).fetchone()
        total = int(row["total"] or 0) if row else 0
        with_project = int(row["with_project"] or 0) if row else 0
        out = {"total": total, "with_project": with_project,
               "coverage": (with_project / total) if total else 0.0}
        self._stats_cache["project"] = out
        return out

    def invalidate_stats_cache(self) -> None:
        """Drop the cached whole-table statistics after events are written."""
        self._stats_cache.clear()

    def titles_for(self, agent: str | None = None) -> dict[str, str]:
        """``session_id -> title`` for one agent, or for all agents."""
        if agent is None:
            cur = self.con.execute("SELECT session_id, title FROM session_titles")
        else:
            cur = self.con.execute(
                "SELECT session_id, title FROM session_titles WHERE agent = ?", (agent,)
            )
        return {r["session_id"]: r["title"] for r in cur if r["title"]}

    def save_checkpoints(self, checkpoints: CheckpointMap | Sequence[Checkpoint]) -> None:
        """Persist cursors. ``byte_offset`` is authoritative per file path."""
        items = checkpoints.values() if isinstance(checkpoints, dict) else checkpoints
        payload = [
            (
                c.file_path,
                float(c.mtime or 0),
                int(c.byte_offset or 0),
                int(c.size or 0),
                int(c.updated_at or time.time() * 1000),
            )
            for c in items
        ]
        if not payload:
            return
        with self.con:
            self.con.executemany(
                """INSERT INTO scan_checkpoints
                       (file_path, mtime, byte_offset, size, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                       mtime=excluded.mtime,
                       byte_offset=excluded.byte_offset,
                       size=excluded.size,
                       updated_at=excluded.updated_at""",
                payload,
            )

    # ---------------------------------------------------------------- reads
    def load_checkpoints(self, agent: str | None = None) -> CheckpointMap:
        """Load cursors; optionally restrict to one agent's source files."""
        sql = "SELECT * FROM scan_checkpoints"
        args: tuple[Any, ...] = ()
        if agent:
            # source_file carries the path; also match by agent column.
            sql += (
                " WHERE file_path IN "
                "(SELECT DISTINCT source_file FROM usage_events WHERE agent = ?)"
            )
            args = (agent,)
        out: CheckpointMap = {}
        for r in self.con.execute(sql, args):
            out[r["file_path"]] = Checkpoint(
                file_path=r["file_path"],
                mtime=r["mtime"],
                byte_offset=r["byte_offset"],
                size=r["size"],
                updated_at=r["updated_at"],
            )
        return out

    def count(self) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0])

    def span_days(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> int:
        """How many days the selected rows actually cover.

        Drives the chart's bucket width. Returns 0 when there is no data, so the
        caller can fall back to daily buckets without special-casing.
        """
        where, args = self._where(since_ms, until_ms)
        row = self.con.execute(
            f"SELECT MIN(timestamp), MAX(timestamp) FROM usage_events {where}", args
        ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return 0
        lo, hi = int(row[0]), int(row[1])
        return max(1, (hi - lo) // 86_400_000 + 1)

    def schema_version(self) -> int:
        r = self.con.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(r[0]) if r else 0

    def totals(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        agent: str | None = None,
        model: str | None = None,
    ) -> dict[str, int]:
        where, args = self._where(since_ms, until_ms, agent, model)
        cols = (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
        sel = ", ".join(f"COALESCE(SUM({c}),0)" for c in cols)
        row = self.con.execute(
            # CACHE_HIT_SQL is NOT wrapped in COALESCE here: its NULL is
            # meaningful ("the data contradicts itself, so there is no honest
            # rate") and COALESCE would turn that back into a confident 0.0.
            f"""SELECT COUNT(*), {sel},
                       COALESCE(SUM({UNCACHED_INPUT_SQL}),0),
                       {CACHE_HIT_SQL},
                       COALESCE(SUM({COST_ROW_SQL}),0),
                       COALESCE({PRICED_TOKENS_SQL},0)
                FROM usage_events {PRICE_JOIN_SQL} {where}""",
            args,
        ).fetchone()
        out = {"events": int(row[0])}
        for c, v in zip(cols, row[1:]):
            out[c] = int(v or 0)
        # `uncached_input_tokens` is what the user actually pays attention to:
        # the input that missed the cache. It is derived, not stored, because
        # the raw input column means different things per agent (see
        # UNCACHED_INPUT_SQL).
        out["uncached_input_tokens"] = int(row[7] or 0)
        # NULL means "the data contradicts itself, so there is no honest rate".
        # Coercing that to 0.0 would report a confident "0% cached" for data we
        # know is broken -- the same lie as clamping it to 100%.
        out["cache_hit"] = (None if row[8] is None else float(row[8]))
        out["cost_usd"] = float(row[9] or 0.0)
        # `cost_usd` is a SUM over priced rows only. `priced_tokens` says how
        # much of the usage that sum actually represents, so the UI can state
        # the gap instead of presenting a partial total as the whole bill.
        out["priced_tokens"] = int(row[10] or 0)
        total = out["total_tokens"] or 0
        out["priced_ratio"] = (out["priced_tokens"] / total) if total else 0.0
        return out

    def by_agent(
        self, *, since_ms: int | None = None, until_ms: int | None = None
    ) -> list[dict[str, Any]]:
        where, args = self._where(since_ms, until_ms)
        rows = self.con.execute(
            f"""SELECT usage_events.agent AS agent,
                       COUNT(*) AS events,
                       SUM(usage_events.total_tokens) AS total_tokens,
                       SUM(usage_events.input_tokens) AS input_tokens,
                       SUM(usage_events.output_tokens) AS output_tokens,
                       SUM(usage_events.cache_read_tokens) AS cache_read_tokens,
                       SUM(usage_events.cache_write_tokens) AS cache_write_tokens,
                       SUM({UNCACHED_INPUT_SQL}) AS uncached_input_tokens,
                       {CACHE_HIT_SQL} AS cache_hit,
                       SUM({COST_ROW_SQL}) AS cost_usd,
                       {PRICED_TOKENS_SQL} AS priced_tokens
                FROM usage_events {PRICE_JOIN_SQL} {where}
                GROUP BY usage_events.agent
                ORDER BY total_tokens DESC""",
            args,
        ).fetchall()
        return [dict(r) for r in rows]

    def by_model(
        self, *, since_ms: int | None = None, until_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """Usage per (agent, model, provider).

        ``provider`` is part of the grouping key on purpose: the same model
        served by ``workbuddy`` and by ``tokenrhythm`` is different usage, and
        merging the two would hide where the tokens actually went.

        ``served_model`` carries what a local gateway measured. Claude Code logs
        a routing alias (``claude-sonnet-5``) for a request the gateway actually
        answered with ``deepseek-v4.1-flash``; grouping on the alias would split
        one real model across several rows and make the provider look wrong.
        When a served name exists it is the grouping key, and the alias is
        reported alongside it.

        A ``"provider/model"`` prefix baked into the model name is normalised
        away BEFORE grouping, so ``workbuddy/x`` and ``x`` land in the same row
        instead of appearing as two spellings of one model.
        """
        where, args = self._where(since_ms, until_ms)
        rows = self.con.execute(
            f"""SELECT usage_events.model AS model, usage_events.provider AS provider,
                       usage_events.provider_source AS provider_source,
                       usage_events.served_model AS served_model,
                       usage_events.agent AS agent,
                       COUNT(*) AS events,
                       SUM(usage_events.total_tokens) AS total_tokens,
                       SUM(usage_events.input_tokens) AS input_tokens,
                       SUM(usage_events.output_tokens) AS output_tokens,
                       SUM(usage_events.cache_read_tokens) AS cache_read_tokens,
                       SUM(usage_events.cache_write_tokens) AS cache_write_tokens,
                       SUM(usage_events.reasoning_tokens) AS reasoning_tokens,
                       SUM({UNCACHED_INPUT_SQL}) AS uncached_input_tokens,
                       {CACHE_HIT_SQL} AS cache_hit,
                       SUM({COST_ROW_SQL}) AS cost_usd,
                       {PRICED_TOKENS_SQL} AS priced_tokens,
                       MAX(mp.input_usd) AS price_input_usd,
                       MAX(mp.output_usd) AS price_output_usd,
                       -- The unit column shows the PEAK rate, and says so in the
                       -- UI. A time-of-day vendor's actual billed rate can be
                       -- half of it, which is why the row's cost is summed per
                       -- request instead of being derived from this column.
                       MAX(mp.input_offpeak_usd) AS price_input_offpeak_usd,
                       MAX(mp.output_offpeak_usd) AS price_output_offpeak_usd,
                       -- Whether this model bills by time of day. Kept as a
                       -- correlated EXISTS: a LEFT JOIN onto price_peak was
                       -- measured SLOWER (446 ms vs 229 ms on a whole-table
                       -- report) because a model with two windows multiplies the
                       -- group's input rows. The EXISTS runs once per group, and
                       -- there are only tens of groups.
                       MAX(CASE WHEN EXISTS (SELECT 1 FROM price_peak pp
                                              WHERE pp.model = usage_events.model)
                                THEN 1 ELSE 0 END) AS has_peak_pricing
                FROM usage_events {PRICE_JOIN_SQL} {where}
                GROUP BY usage_events.model, usage_events.provider,
                         usage_events.provider_source, usage_events.served_model,
                         usage_events.agent
                ORDER BY total_tokens DESC""",
            args,
        ).fetchall()

        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        aliases: dict[tuple[str, str, str], set[str]] = {}
        for r in rows:
            d = dict(r)
            model, provider = self.split_embedded_provider(
                d.get("model") or "", d.get("provider") or ""
            )
            served = str(d.get("served_model") or "").strip()
            # Display/group on the real model when one was measured.
            shown = served or model
            key = (d["agent"], shown, provider)
            src = str(d.get("provider_source") or "")
            cur = merged.get(key)
            if cur is None:
                d["model"] = shown
                d["provider"] = provider
                d["provider_source"] = src
                d["alias"] = model if served and model and model != served else ""
                merged[key] = d
            else:
                for col in ("events", "total_tokens", "input_tokens", "output_tokens",
                            "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                            "priced_tokens"):
                    cur[col] = int(cur.get(col) or 0) + int(d.get(col) or 0)
                # A cost is a SUM of row costs, so it merges by addition -- but
                # only across rows that HAVE a cost. A NULL must not become 0.
                if d.get("cost_usd") is not None:
                    cur["cost_usd"] = float(cur.get("cost_usd") or 0.0) + float(d["cost_usd"])
                # Derived values must be RECOMPUTED from the merged buckets; a
                # hit rate is a ratio, so summing two ratios is meaningless.
                cur["uncached_input_tokens"] = (
                    self._uncached_input(cur)
                )
                cur["cache_hit"] = self._cache_hit(cur)
                # A group must never look better-evidenced than its weakest row,
                # so the source collapses to the minimum strength present.
                cur_rank = SOURCE_STRENGTH.get(str(cur.get("provider_source") or ""), 0)
                new_rank = SOURCE_STRENGTH.get(src, 0)
                if new_rank < cur_rank:
                    cur["provider_source"] = src
                if d.get("alias") and not cur.get("alias"):
                    cur["alias"] = d["alias"]
                # Unit prices are per-model facts, but a merged group can span
                # several spellings; keep the highest so the column reads as an
                # upper bound rather than silently dropping to zero.
                for col in ("price_input_usd", "price_output_usd",
                            "price_input_offpeak_usd", "price_output_offpeak_usd"):
                    if d.get(col) is not None:
                        prev = cur.get(col)
                        cur[col] = float(d[col]) if prev is None else max(float(prev), float(d[col]))
                if d.get("has_peak_pricing"):
                    cur["has_peak_pricing"] = 1
            if model and served and model != served:
                aliases.setdefault(key, set()).add(model)

        result: list[dict[str, Any]] = []
        for k, d in merged.items():
            seen_aliases = aliases.get(k)
            if seen_aliases:
                # Several aliases can collapse into one served model.
                d["alias"] = " / ".join(sorted(seen_aliases))
            d.pop("served_model", None)
            result.append(d)
        return sorted(result, key=lambda d: -(d.get("total_tokens") or 0))
    def overview_snapshot(
        self, *, since_ms: int | None = None, until_ms: int | None = None
    ) -> dict[str, Any]:
        """Totals, per-agent and per-model rows from ONE table scan.

        ``overview`` used to call ``totals()``, ``by_agent()`` and ``by_model()``
        separately, and each of those re-scanned ``usage_events`` and re-evaluated
        the cost expression per row. On a whole-history report (76k rows) that
        was three scans of the same data for three views of the same numbers.

        The grouping done by ``by_model`` is strictly finer than the other two --
        one row per (agent, model, provider, provider_source, served_model) -- so
        the per-agent and overall figures are exact SUMs of it. Nothing is
        approximated here; ``test_cost.py`` asserts the three views agree with
        the standalone methods to the cent.
        """
        by_model = self.by_model(since_ms=since_ms, until_ms=until_ms)

        # Roll the model rows up per agent. Every column is a SUM, so adding is
        # exact; the cache-hit ratio is then recomputed from the merged sums
        # rather than averaged (an agent with one request must not weigh the same
        # as one with a million).
        agents: dict[str, dict[str, Any]] = {}
        for r in by_model:
            a = str(r.get("agent") or "")
            bucket = agents.get(a)
            if bucket is None:
                bucket = agents[a] = {
                    "agent": a, "events": 0, "total_tokens": 0,
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "reasoning_tokens": None, "priced_tokens": 0,
                    "cost_usd": 0.0, "has_cost": False,
                }
            for col in ("events", "total_tokens", "input_tokens", "output_tokens",
                        "cache_read_tokens", "cache_write_tokens",
                        "priced_tokens"):
                bucket[col] += int(r.get(col) or 0)
            # SQL's SUM() over an all-NULL column is NULL, not 0, and the
            # standalone by_agent() reports it that way. Preserve that: start
            # NULL and only start counting once a non-NULL value arrives.
            rv = r.get("reasoning_tokens")
            if rv is not None:
                bucket["reasoning_tokens"] = int(bucket["reasoning_tokens"] or 0) + int(rv)
            if r.get("cost_usd") is not None:
                bucket["cost_usd"] += float(r["cost_usd"])
                bucket["has_cost"] = True

        by_agent: list[dict[str, Any]] = []
        for bucket in sorted(agents.values(), key=lambda d: -d["total_tokens"]):
            bucket["uncached_input_tokens"] = self._uncached_input(bucket)
            bucket["cache_hit"] = self._cache_hit(bucket)
            # A NULL total means "no priced rows in this group", which the UI
            # renders as a dash; 0.0 would read as "costs nothing".
            if not bucket.pop("has_cost"):
                bucket["cost_usd"] = None
            by_agent.append(bucket)

        totals = {
            "events": sum(b["events"] for b in by_agent),
            "total_tokens": sum(b["total_tokens"] for b in by_agent),
            "input_tokens": sum(b["input_tokens"] for b in by_agent),
            "output_tokens": sum(b["output_tokens"] for b in by_agent),
            "cache_read_tokens": sum(b["cache_read_tokens"] for b in by_agent),
            "cache_write_tokens": sum(b["cache_write_tokens"] for b in by_agent),
            "reasoning_tokens": sum(b["reasoning_tokens"] for b in by_agent),
            "priced_tokens": sum(b["priced_tokens"] for b in by_agent),
        }
        priced_rows = [b for b in by_agent if b.get("cost_usd") is not None]
        totals["cost_usd"] = (
            sum(float(b["cost_usd"]) for b in priced_rows) if priced_rows else 0.0
        )
        totals["uncached_input_tokens"] = self._uncached_input(totals)
        totals["cache_hit"] = self._cache_hit(totals)
        total = totals["total_tokens"] or 0
        totals["priced_ratio"] = (totals["priced_tokens"] / total) if total else 0.0
        return {"totals": totals, "by_agent": by_agent, "by_model": by_model}

    def models_by_agent(
        self, *, since_ms: int | None = None, until_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """Same rows as :meth:`by_model`, ready to be nested under each agent."""
        rows = self.by_model(since_ms=since_ms, until_ms=until_ms)
        for r in rows:
            r["model"] = r.get("model") or ""
            r["provider"] = r.get("provider") or ""
            r["provider_source"] = r.get("provider_source") or ""
            r["alias"] = r.get("alias") or ""
        return rows

    @staticmethod
    def split_embedded_provider(model: str, provider: str) -> tuple[str, str]:
        """Separate a ``"provider/model"`` prefix baked into the model name.

        Some agents record the routing inside the model string itself
        (``workbuddy/deepseek-v4.1-flash``, ``workbuddy/kimi-k3.1-pro``) instead
        of a dedicated field. Without this, the UI would show the provider twice
        and the same model would appear under two different names.

        The prefix is stripped on its own evidence: when the row has no explicit
        provider, the prefix BECOMES the provider (that is the only information
        available). When a provider is present, the prefix is stripped only if
        it matches, so an unrelated slash in a model name is left alone.
        """
        if not model or "/" not in model:
            return model, provider
        head, _, tail = model.partition("/")
        if not head or not tail:
            return model, provider
        if provider:
            if head.lower() == provider.lower():
                return tail, provider
            return model, provider
        return tail, head

    @staticmethod
    def _uncached_input(row: Mapping[str, Any]) -> int:
        """Python mirror of :data:`UNCACHED_INPUT_SQL`, for merged rows."""
        return int(
            int(row.get("total_tokens") or 0)
            - int(row.get("output_tokens") or 0)
            - int(row.get("cache_read_tokens") or 0)
            - int(row.get("cache_write_tokens") or 0)
        )

    @staticmethod
    def _cache_hit(row: Mapping[str, Any]) -> float | None:
        """Python mirror of :data:`CACHE_HIT_SQL`, for merged rows.

        Returns None when cached reads exceed the prompt total, which proves
        the row contradicts itself. A clamp would dress broken data up as a
        perfect cache hit; a dash is the honest answer.

        This mirror must stay in step with the SQL above, or merged rows (which
        are recomputed in Python) would disagree with rows aggregated straight
        from the database.
        """
        cached = int(row.get("cache_read_tokens") or 0)
        denom = int(row.get("uncached_input_tokens") or 0) + cached
        if denom == 0:
            return 0.0
        if denom < 0 or cached > denom:
            return None
        return min(1.0, max(0.0, cached / denom))

    def top_sessions(
        self,
        *,
        limit: int = 10,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        # Built here rather than via _where(): the title join makes bare column
        # names ambiguous (`agent` exists in both tables).
        clauses: list[str] = []
        args: list[Any] = []
        if since_ms is not None:
            clauses.append("timestamp >= ?")
            args.append(int(since_ms))
        if until_ms is not None:
            clauses.append("timestamp < ?")
            args.append(int(until_ms))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        # Aggregate FIRST, then attach titles to just those rows. Joining the
        # title table inside the GROUP BY made SQLite scan every usage row
        # against three OR-ed id forms (19.7s over 73k rows, measured).
        rows = [dict(r) for r in self.con.execute(
            f"""SELECT usage_events.session_id AS session_id,
                       usage_events.agent AS agent,
                       MAX(usage_events.timestamp) AS last_seen,
                       COUNT(*) AS events,
                       SUM(usage_events.total_tokens) AS total_tokens,
                       SUM(usage_events.input_tokens) AS input_tokens,
                       SUM(usage_events.output_tokens) AS output_tokens,
                       SUM(usage_events.cache_read_tokens) AS cache_read_tokens,
                       SUM(usage_events.cache_write_tokens) AS cache_write_tokens,
                       SUM({UNCACHED_INPUT_SQL}) AS uncached_input_tokens,
                       {CACHE_HIT_SQL} AS cache_hit,
                       SUM({COST_ROW_SQL}) AS cost_usd,
                       {PRICED_TOKENS_SQL} AS priced_tokens
                FROM usage_events {PRICE_JOIN_SQL} {where}
                GROUP BY usage_events.session_id, usage_events.agent
                ORDER BY total_tokens DESC LIMIT ?""",
            (*args, int(limit)),
        ).fetchall()]
        if not rows:
            return rows

        wanted: list[tuple[str, str, str]] = []
        for r in rows:
            sid = str(r["session_id"] or "")
            wanted.append((r["agent"], sid, _strip_session_prefix(sid)))

        titles: dict[tuple[str, str], str] = {}
        for agent, sid, bare in wanted:
            for key in {sid, bare, f"session-{bare}"}:
                if not key:
                    continue
                found = self.con.execute(
                    "SELECT title FROM session_titles WHERE agent = ? AND session_id = ?",
                    (agent, key),
                ).fetchone()
                if found and found["title"]:
                    titles[(agent, sid)] = found["title"]
                    break

        for r in rows:
            r["title"] = titles.get((r["agent"], str(r["session_id"] or "")), "")
        return rows

    def by_day(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        tz_offset_ms: int = 0,
        agent: str | None = None,
        bucket_days: int = 1,
    ) -> list[dict[str, Any]]:
        """Bucketed time series, optionally split by agent for stacked charts.

        ``bucket_days`` widens each bucket past one day. Grouping one row per
        day is fine for a week, but "all time" is unbounded: someone with two
        years of history would get 730 bars and an unreadable axis. The caller
        picks the granularity from the span; the SQL does the grouping so a
        long range never returns thousands of rows.
        """
        where, args = self._where(since_ms, until_ms, agent=agent)
        offset = int(tz_offset_ms)
        if bucket_days <= 1:
            expr = f"strftime('%Y-%m-%d', (timestamp + {offset}) / 1000, 'unixepoch')"
        else:
            # Anchor the bucket on a fixed epoch so every bar is exactly
            # ``bucket_days`` long and two loads of the same data agree.
            span = int(bucket_days) * 86400
            expr = (
                "strftime('%Y-%m-%d', "
                f"((((timestamp + {offset}) / 1000) / {span}) * {span}) - 0, "
                "'unixepoch')"
            )
        rows = self.con.execute(
            f"""SELECT {expr} AS day, usage_events.agent AS agent,
                       SUM(usage_events.total_tokens) AS total_tokens,
                       SUM(usage_events.input_tokens) AS input_tokens,
                       SUM(usage_events.output_tokens) AS output_tokens,
                       SUM(usage_events.cache_read_tokens) AS cache_read_tokens,
                       SUM(usage_events.cache_write_tokens) AS cache_write_tokens,
                       SUM({UNCACHED_INPUT_SQL}) AS uncached_input_tokens,
                       {CACHE_HIT_SQL} AS cache_hit,
                       SUM({COST_ROW_SQL}) AS cost_usd
                FROM usage_events {PRICE_JOIN_SQL} {where}
                GROUP BY day, usage_events.agent ORDER BY day""",
            args,
        ).fetchall()
        return [dict(r) for r in rows]

    def daily_totals(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        tz_offset_ms: int = 0,
    ) -> list[dict[str, Any]]:
        """One row per local calendar day, for the calendar heatmap.

        Separate from ``by_day`` on purpose: that one splits by agent so the
        stacked chart can draw bands, and summing its rows back together here
        would still leave the *cost coverage* wrong -- an unpriced model has a
        NULL cost that a client-side sum cannot distinguish from a day with no
        usage at all. Carrying ``priced_tokens`` lets the tooltip say how much of
        the day's tokens the estimate actually covers, which is the project's
        rule for every money figure.

        Days with no usage produce no row; the calendar fills the gaps itself so
        the grid stays a real calendar instead of a compressed list.
        """
        where, args = self._where(since_ms, until_ms)
        offset = int(tz_offset_ms)
        expr = f"strftime('%Y-%m-%d', (timestamp + {offset}) / 1000, 'unixepoch')"
        rows = self.con.execute(
            f"""SELECT {expr} AS day,
                       COUNT(*) AS events,
                       SUM(usage_events.total_tokens) AS total_tokens,
                       SUM({COST_ROW_SQL}) AS cost_usd,
                       {PRICED_TOKENS_SQL} AS priced_tokens
                FROM usage_events {PRICE_JOIN_SQL} {where}
                GROUP BY day ORDER BY day""",
            args,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            total = int(r["total_tokens"] or 0)
            priced = int(r["priced_tokens"] or 0)
            out.append({
                "day": r["day"],
                "events": int(r["events"] or 0),
                "total_tokens": total,
                # None, not 0: a day whose models are all unpriced has no
                # estimate, and "0" would claim it was free.
                "cost_usd": r["cost_usd"],
                "priced_ratio": (priced / total) if total else 0.0,
            })
        return out

    @staticmethod
    def _where(
        since_ms: int | None,
        until_ms: int | None,
        agent: str | None = None,
        model: str | None = None,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        args: list[Any] = []
        if since_ms is not None:
            clauses.append("timestamp >= ?")
            args.append(int(since_ms))
        if until_ms is not None:
            clauses.append("timestamp < ?")
            args.append(int(until_ms))
        if agent:
            clauses.append("agent = ?")
            args.append(agent)
        if model:
            clauses.append("model = ?")
            args.append(model)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, tuple(args)

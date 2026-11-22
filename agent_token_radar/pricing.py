"""Reference cost estimation from official list prices.

Every token figure this tool reports is measured. Money is not: it is derived by
multiplying tokens by a list price a vendor published, and list prices are only
comparable to a real bill when the vendor publishes all of them and the workload
was actually billed at list.

Four rules hold the design together:

1. **A model with no price contributes no cost, and is counted as unpriced.**
   Never interpolate a price from a similarly named model; a made-up number is
   worse than a stated gap.
2. **The price table is hand-maintained data, not code.** Vendors reprice, so
   ``pricing.json`` carries the source URL and the date each price was read.
   Repricing is a data edit, not a code change.
3. **Cost per row is computed in SQL.** Rows live in ``usage_events``; the
   price is joined onto that table so *every* existing aggregate (by agent, by
   model, by project, by session, by day) can sum a cost column without each
   query re-deriving the price.
4. **Time-of-day pricing is applied per request, from that request's own
   timestamp.** A vendor whose rate changes during the day cannot be billed with
   one flat number, and an average over the day would be wrong for every row in
   it. DeepSeek is the case that forced this.

No exchange rate is involved anywhere. Vendors that publish a CNY sheet
(Zhipu, Qwen) also publish a USD sheet, and the USD sheet is what this table
records -- so every figure in here is already the currency the vendor itself
quotes for international billing.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

PRICING_PATH = Path(__file__).with_name("pricing.json")

#: Buckets a row's tokens are billed in. Cache read and write are separate
#: prices at most vendors, so they must not be folded into input.
BUCKETS = ("input", "output", "cache_read", "cache_write")

#: SQL for "how many tokens were actually sent uncached".
#:
#: Agents disagree about whether ``input_tokens`` already contains the cache
#: buckets, and neither display nor billing may depend on which parser wrote
#: the row:
#:
#: * **additive** (DSH, Claude, Gemini, Hermes): input EXCLUDES cache, and the
#:   agent's own total is ``input + output + cache_read + cache_write``.
#: * **inclusive** (Codex, WorkBuddy): input is the full prompt size INCLUDING
#:   cache hits, and the agent's total is just ``input + output``. Codex stores
#:   ``input_tokens=572,797,230`` against ``cached_input_tokens=558,823,552`` --
#:   reading that input column as "uncached" overstates it 41x.
#:
#: Both conventions satisfy ``total = uncached + output + cache_read +
#: cache_write``, so this one expression is exact for every agent (verified:
#: no negative results across 73k rows) and no migration is needed.
#:
#: This lives here rather than in ``store.py`` because BOTH the display
#: aggregates and :data:`COST_ROW_SQL` must use it: ``store.py`` imports this
#: module, so the constant cannot be defined there without a cycle, and two
#: copies of the expression would drift apart -- which is exactly how the
#: inclusive-input double-billing bug got in (the dashboard showed uncached
#: input while the cost SQL billed the full prompt AND the cache read again).
#:
#: The columns are QUALIFIED with the ``usage_events`` table name even though
#: every current call site reads a single unaliased ``FROM usage_events``. That
#: is not redundant: these queries LEFT JOIN ``model_prices``, and a bare
#: ``output_tokens`` becomes ``ambiguous column name`` the moment that table
#: grows a column of the same name. Verified by adding an ``output_tokens``
#: column to ``model_prices`` on a database copy: the unqualified form errors
#: immediately, the qualified form is unaffected.
UNCACHED_INPUT_SQL = (
    "(usage_events.total_tokens - usage_events.output_tokens "
    "- usage_events.cache_read_tokens - usage_events.cache_write_tokens)"
)


def _dec(value: Any) -> Decimal | None:
    """Parse a price cell, tolerating the nulls a JSON table carries."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _minutes(hhmm: str) -> int:
    """'06:30' -> 390. Minutes since midnight, so windows compare as integers."""
    hh, _, mm = str(hhmm).partition(":")
    return int(hh) * 60 + int(mm or 0)


def _epoch_day(date_str: str) -> int:
    """'2026-10-01' -> days since the Unix epoch, in UTC.

    The holiday table stores this integer rather than the date string so the
    per-row predicate compares integers. Computed with ``calendar.timegm`` (not
    ``mktime``) so a machine in UTC+8 does not shift the holiday by a day.
    """
    return calendar.timegm(time.strptime(str(date_str).strip(), "%Y-%m-%d")) // 86400


#: Epoch milliseconds per day. The peak test needs "which day" and "what time
#: of day", and both fall out of integer division on the UTC epoch -- no
#: calendar function, no string parsing.
_MS_PER_DAY = 86_400_000

#: Day-of-week offset. 1970-01-01 (epoch day 0) was a Thursday, so epoch_day %
#: 7 gives 0=Thursday. Shifting by 4 puts Sunday at 0, matching
#: ``strftime('%w')``, which keeps the rule readable against the vendor's
#: "Monday through Friday" wording.
_DOW_SHIFT = 4


def _peak_predicate_sql(model_col: str = "usage_events.model",
                        ts_col: str = "usage_events.timestamp") -> str:
    """SQL predicate: is this row inside a peak window?

    Written with integer arithmetic on the UTC epoch instead of ``strftime``.
    That is not micro-optimisation: the predicate is inlined once per billable
    bucket (four times per row), and each ``strftime`` call costs a string
    round-trip, so the calendar-function version measured ~730 ms per aggregate
    over 76k rows versus ~45 ms for plain arithmetic. Same answer, ~16x cheaper.

    ``ts_col`` is epoch MILLISECONDS. Minutes-since-midnight is
    ``(ts / 60000) % 1440``; day-of-week is ``(ts / 86400000 + 4) % 7`` with
    0=Sunday (epoch day 0 was a Thursday, hence the +4). The holiday list is
    still a lookup, but keyed on the integer epoch day, so it is a plain
    equality against a small table rather than a date-string format-and-compare.
    """
    return f"""
    EXISTS (
        SELECT 1 FROM price_peak p
         WHERE p.model = {model_col}
           AND p.vendor <> ''
           AND ({ts_col} / {_MS_PER_DAY} + {_DOW_SHIFT}) % 7 >= p.peak_start_dow
           AND ({ts_col} / {_MS_PER_DAY} + {_DOW_SHIFT}) % 7 <  p.peak_end_dow
           AND NOT EXISTS (
                 SELECT 1 FROM price_peak_skip s
                  WHERE s.vendor = p.vendor
                    AND s.epoch_day = {ts_col} / {_MS_PER_DAY})
           AND (({ts_col} / 60000) % 1440) >= p.start_minute
           AND (({ts_col} / 60000) % 1440) <  p.end_minute
    )
"""


class PeakSchedule:
    """A vendor's time-of-day billing windows.

    Everything is expressed as data -- UTC windows, a weekday flag, holiday
    dates -- so a vendor changing its schedule is a JSON edit. The SQL that
    applies this reads the same numbers back out of the ``price_peak`` table
    (see ``ensure_price_table``); this class is the Python side of the same
    facts, used for validation and for the API metadata.
    """

    def __init__(self, vendor: str, spec: Mapping[str, Any]) -> None:
        self.vendor = vendor
        self.windows: list[tuple[int, int]] = [
            (_minutes(a), _minutes(b)) for a, b in (spec.get("utc_windows") or [])
        ]
        self.weekdays_only = bool(spec.get("weekdays_only"))
        self.skip_dates: frozenset[str] = frozenset(spec.get("skip_dates") or [])
        self.multiplier = Decimal(str(spec.get("offpeak_multiplier") or 1))
        self.skip_dates_source = str(spec.get("skip_dates_source") or "")
        self.skip_dates_note = str(spec.get("skip_dates_note") or "")

    @property
    def dow_start(self) -> int:
        """First day-of-week that can be peak (0=Sunday, matching strftime %w).

        ``weekdays_only`` means Monday..Friday, i.e. 1..5 with Saturday and
        Sunday outside. A schedule with no weekday restriction covers all seven.
        """
        return 1 if self.weekdays_only else 0

    @property
    def dow_end(self) -> int:
        """Exclusive end of the peak weekday range (Saturday = 6)."""
        return 6 if self.weekdays_only else 7

    def __bool__(self) -> bool:
        return bool(self.windows)

    def is_peak(self, when_utc) -> bool:
        """Whether a UTC datetime falls in a peak window.

        Kept for tests and for anyone reading the rules in Python; the cost
        itself is computed in SQL so it stays per-row and per-query.
        """
        date = when_utc.strftime("%Y-%m-%d")
        if date in self.skip_dates:
            return False
        if self.weekdays_only and when_utc.weekday() >= 5:
            return False
        minute = when_utc.hour * 60 + when_utc.minute
        return any(lo <= minute < hi for lo, hi in self.windows)

    def as_meta(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "utc_windows": [[f"{lo // 60:02d}:{lo % 60:02d}", f"{hi // 60:02d}:{hi % 60:02d}"]
                            for lo, hi in self.windows],
            "weekdays_only": self.weekdays_only,
            "skip_dates": sorted(self.skip_dates),
            "offpeak_multiplier": float(self.multiplier),
            "skip_dates_source": self.skip_dates_source,
            "skip_dates_note": self.skip_dates_note,
        }


class PriceTable:
    """The official list prices, keyed by the model string as logged."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.raw = data
        self.models: dict[str, dict[str, Any]] = dict(data.get("models") or {})
        self.sources: dict[str, dict[str, Any]] = dict(data.get("sources") or {})
        self.updated: str = str(data.get("_updated") or "")
        self.peak: dict[str, PeakSchedule] = {
            vendor: PeakSchedule(vendor, spec)
            for vendor, spec in (data.get("peak_hours") or {}).items()
            if isinstance(spec, Mapping)
        }

    @classmethod
    def load(cls, path: Path | None = None) -> "PriceTable":
        p = path or PRICING_PATH
        try:
            with open(p, encoding="utf-8") as fh:
                return cls(json.load(fh))
        except FileNotFoundError:
            logger.warning("pricing table missing at %s; costs will be unknown", p)
            return cls({})
        except Exception:
            logger.exception("pricing table at %s is unreadable", p)
            return cls({})

    # ------------------------------------------------------------------ query

    def price_for(self, model: str) -> dict[str, Any] | None:
        """The list price for a model, or None when the vendor has none here.

        Lookup is exact on purpose. A fuzzy match would silently bill
        ``gpt-6.1-terra`` at ``gpt-6.1-sol`` rates, which is exactly the kind of
        wrong number this module refuses to produce.
        """
        return self.models.get((model or "").strip())

    def usd_per_million(self, model: str) -> dict[str, dict[str, Decimal]] | None:
        """Per-1M-token USD prices, split into peak and off-peak.

        Returns ``{"peak": {...}, "offpeak": {...}}``. For a flat-rate model the
        two are identical, so callers never need to branch. No conversion
        happens here: every entry is already USD, as the vendor publishes it.
        """
        p = self.price_for(model)
        if not p:
            return None
        unit = _dec(p.get("input"))
        if unit is None:
            return None
        out = _dec(p.get("output")) or unit
        hit = _dec(p.get("cache_read"))
        # No published cache-hit price means the vendor bills cached input at
        # the full input rate; anything else would invent a discount.
        write = _dec(p.get("cache_write")) or unit

        peak = {
            "input": unit,
            "output": out,
            "cache_read": hit if hit is not None else unit,
            "cache_write": write,
        }
        off = p.get("offpeak")
        if isinstance(off, Mapping):
            offpeak = {}
            for bucket in BUCKETS:
                raw = _dec(off.get(bucket))
                if raw is None:
                    # Off-peak block incomplete: fall back to the vendor's
                    # published multiplier rather than the peak price, which
                    # would silently overbill every off-peak request.
                    sched = self.peak.get(str(p.get("vendor") or ""))
                    mult = sched.multiplier if sched else Decimal(1)
                    offpeak[bucket] = peak[bucket] * mult
                else:
                    offpeak[bucket] = raw
        else:
            offpeak = dict(peak)
        return {"peak": peak, "offpeak": offpeak}

    # ------------------------------------------------------------------ resolve

    #: Strips the spellings that differ between a vendor's model id and what an
    #: agent writes into its log, without collapsing genuinely different models.
    #: Each pattern is anchored so `gpt-6.1-terra` can never fall through to
    #: `gpt-6.1-sol`.
    _ALIAS_PATTERNS = (
        # TokenRhythm / gateways append the model's release date or an expiry:
        # `deepseek-v4-flash-0731`, `...-expires-on-0910`.
        (r"^(.*?)-(?:expires-on-)?\d{4}(?:-\d{2})?$", r"\1"),
        # Regional / tier suffixes on the same underlying model.
        (r"^(.*?)-(?:sg|sgp|hk|us|eu|jp|cn)$", r"\1"),
        # WorkBuddy records `hy4std` / `hy4flash` / `hy4free` for the HY4 line;
        # `hy` is its own naming, handled by the alias table below.
    )

    def resolve(self, model: str) -> tuple[str, dict[str, Any] | None]:
        """Map a logged model string to a price entry.

        Returns ``(matched_key, price)``. ``matched_key`` is empty when nothing
        matched, and the caller must then treat the cost as unknown rather than
        zero. Resolution is deliberately conservative: an exact hit or a
        spelling-normalised hit only, never a similarity guess. Tokei falls back
        to "same tier, previous version" for unknown models and labels the
        result as an estimate; this tool has no tier map, so it reports the gap
        instead of inventing an anchor.
        """
        name = (model or "").strip()
        if not name:
            return "", None
        if name in self.models:
            return name, self.models[name]

        lowered = name.lower()
        for key in self.models:
            if key.lower() == lowered:
                return key, self.models[key]

        for pattern, repl in self._ALIAS_PATTERNS:
            m = re.match(pattern, name)
            if not m:
                continue
            candidate = re.sub(pattern, repl, name)
            for key in self.models:
                if key.lower() == candidate.lower():
                    return key, self.models[key]
        return "", None


def money(value: Any) -> Decimal:
    """Quantise a cost to cents for stable JSON and display."""
    d = value if isinstance(value, Decimal) else Decimal(str(value or 0))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


#: SQL expression for the cost of one row, in USD, given the aliases used by
#: the usage queries. Returns NULL when the model has no price, so SUM() skips
#: it instead of turning an unknown into a zero that looks like "free".
#:
#: The input bucket is :data:`UNCACHED_INPUT_SQL`, NOT ``input_tokens``.
#: ``input_tokens`` means different things per agent (additive: excludes cache;
#: inclusive: the full prompt already containing the cache hits), so billing it
#: directly charged the inclusive agents (Codex, WorkBuddy) for their cached
#: tokens twice -- once at the input rate and again at the cache-read rate. On
#: this machine's real database that overstated total cost 5x
#: ($1875.84 vs the correct $374.47, codex alone $1532.53 vs $120.38).
#:
#: That derived bucket is wrapped in ``MAX(0, ...)`` because it CAN go negative
#: on corrupt input (a row claiming more cache reads than its own total). The
#: old direct ``input_tokens`` could not: ``UsageEvent.normalized`` clamps it at
#: >= 0. Without the clamp a corrupt row would subtract from the bill and a
#: total could come out negative -- money the vendor never refunds. The display
#: side deliberately does NOT clamp, so the bad data stays visible:
#: ``test_bad_total_shows_negative_input_without_crashing`` asserts the negative
#: uncached input is still reported. This clamp is billing-only: it stops the
#: corruption at the money without hiding it from the person diagnosing the
#: parser.
#:
#: Peak hours are resolved per row from ``usage_events.timestamp``, never from
#: "now" and never as a daily average: a request logged at 02:00 UTC on a
#: Tuesday is billed at the peak rate even if the report is generated at the
#: weekend.
#:
#: The peak test wraps the WHOLE sum rather than sitting inside each of the four
#: buckets. That is deliberate: an earlier version wrote
#: ``CASE WHEN peak THEN input_usd ELSE input_offpeak_usd END`` per bucket, so
#: SQLite evaluated the predicate four times per row and every aggregate cost
#: ~730 ms over 76k rows. One evaluation with two full-rate branches is the same
#: arithmetic and about four times cheaper.
COST_ROW_SQL = (
    """
    CASE
      WHEN mp.price_key IS NULL OR mp.price_key = '' THEN NULL
      WHEN {peak} THEN
          (COALESCE(MAX(0, """
    + UNCACHED_INPUT_SQL
    + """), 0) / 1000000.0) * mp.input_usd
        + (COALESCE(usage_events.output_tokens, 0)      / 1000000.0) * mp.output_usd
        + (COALESCE(usage_events.cache_read_tokens, 0)  / 1000000.0) * mp.cache_read_usd
        + (COALESCE(usage_events.cache_write_tokens, 0) / 1000000.0) * mp.cache_write_usd
      ELSE
          (COALESCE(MAX(0, """
    + UNCACHED_INPUT_SQL
    + """), 0) / 1000000.0) * mp.input_offpeak_usd
        + (COALESCE(usage_events.output_tokens, 0)      / 1000000.0) * mp.output_offpeak_usd
        + (COALESCE(usage_events.cache_read_tokens, 0)  / 1000000.0) * mp.cache_read_offpeak_usd
        + (COALESCE(usage_events.cache_write_tokens, 0) / 1000000.0) * mp.cache_write_offpeak_usd
    END
"""
)

#: Whether the row's own timestamp is inside a peak window.
#:
#: The window and holiday list are read from the ``price_peak`` /
#: ``price_peak_skip`` tables, which are rebuilt from pricing.json on every
#: connection, so changing the schedule is a data edit. Everything is UTC:
#: ``timestamp`` is stored in UTC milliseconds, and a local wall clock would
#: make the answer depend on the machine's timezone rather than the vendor's
#: billing clock.
#:
#: Built from integer arithmetic on the epoch rather than ``strftime`` -- see
#: ``_peak_predicate_sql`` for why that matters.
PEAK_HOURS_SQL = _peak_predicate_sql()


def cost_sql() -> str:
    """``COST_ROW_SQL`` with the peak-hours predicate filled in."""
    return COST_ROW_SQL.replace("{peak}", PEAK_HOURS_SQL)


def ensure_price_table(con, table: PriceTable) -> int:
    """Create and populate the price tables inside the usage DB.

    The prices live in the same database as the events so a single SQL query
    can join them. The table is keyed by the RESOLVED price key, and every
    model string actually present in ``usage_events`` is materialised against
    it -- including the ones that resolve to nothing, which get a NULL price.

    A NULL price is the whole point: ``SUM()`` skips it, so an unpriced model
    can never be silently billed at zero. Storing the mapping explicitly (one
    row per logged spelling) also keeps the join a plain equality, so the
    aggregate queries stay index-friendly and their SQL stays readable.

    Rows are replaced on every open: pricing.json stays the source of truth, so
    a repriced or withdrawn model cannot linger from an earlier run. That is
    also what makes a schedule edit take effect without a migration.
    """
    con.execute(
        """CREATE TABLE IF NOT EXISTS model_prices (
               model         TEXT PRIMARY KEY,
               price_key     TEXT NOT NULL DEFAULT '',
               vendor        TEXT NOT NULL DEFAULT '',
               currency      TEXT NOT NULL DEFAULT 'USD',
               input_usd     REAL NOT NULL DEFAULT 0,
               output_usd    REAL NOT NULL DEFAULT 0,
               cache_read_usd  REAL NOT NULL DEFAULT 0,
               cache_write_usd REAL NOT NULL DEFAULT 0,
               input_offpeak_usd   REAL NOT NULL DEFAULT 0,
               output_offpeak_usd  REAL NOT NULL DEFAULT 0,
               cache_read_offpeak_usd  REAL NOT NULL DEFAULT 0,
               cache_write_offpeak_usd REAL NOT NULL DEFAULT 0
           )"""
    )
    # One row per (model, peak window) so the peak predicate stays a plain
    # EXISTS over small tables instead of a hard-coded time test.
    #
    # The weekday range is stored as integers (0=Sunday) rather than a
    # boolean: the predicate then compares two numbers against the epoch-derived
    # day-of-week and needs no special case for weekends.
    con.execute(
        """CREATE TABLE IF NOT EXISTS price_peak (
               model         TEXT NOT NULL,
               vendor        TEXT NOT NULL DEFAULT '',
               start_minute  INTEGER NOT NULL DEFAULT 0,
               end_minute    INTEGER NOT NULL DEFAULT 0,
               peak_start_dow INTEGER NOT NULL DEFAULT 0,
               peak_end_dow   INTEGER NOT NULL DEFAULT 7,
               PRIMARY KEY (model, start_minute, end_minute)
           )"""
    )
    # Holidays keyed by epoch DAY (an integer), so the predicate is one equality
    # against an integer instead of formatting a date string per row.
    con.execute(
        """CREATE TABLE IF NOT EXISTS price_peak_skip (
               vendor    TEXT NOT NULL,
               epoch_day INTEGER NOT NULL,
               PRIMARY KEY (vendor, epoch_day)
           )"""
    )
    # A database built by an earlier version has these tables with the old
    # shape (a date TEXT and a weekdays_only flag), and CREATE TABLE IF NOT
    # EXISTS will not reshape them. Both are derived data rebuilt from
    # pricing.json just below, so drop and recreate rather than migrate.
    for table_name, wanted in (
        ("price_peak", ("peak_start_dow",)),
        ("price_peak_skip", ("epoch_day",)),
    ):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table_name})")}
        if have and not set(wanted) <= have:
            con.execute(f"DROP TABLE {table_name}")
    # Recreate any table the drop above removed.
    con.execute(
        """CREATE TABLE IF NOT EXISTS price_peak (
               model         TEXT NOT NULL,
               vendor        TEXT NOT NULL DEFAULT '',
               start_minute  INTEGER NOT NULL DEFAULT 0,
               end_minute    INTEGER NOT NULL DEFAULT 0,
               peak_start_dow INTEGER NOT NULL DEFAULT 0,
               peak_end_dow   INTEGER NOT NULL DEFAULT 7,
               PRIMARY KEY (model, start_minute, end_minute)
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS price_peak_skip (
               vendor    TEXT NOT NULL,
               epoch_day INTEGER NOT NULL,
               PRIMARY KEY (vendor, epoch_day)
           )"""
    )
    # A database built by an earlier version has this table without the
    # off-peak columns, and CREATE TABLE IF NOT EXISTS will not add them. The
    # table is fully rebuilt right below, so adding the columns and then
    # clearing it is enough -- but the columns must exist before the INSERT
    # names them.
    existing = {r[1] for r in con.execute("PRAGMA table_info(model_prices)")}
    for col in ("input_offpeak_usd", "output_offpeak_usd",
                "cache_read_offpeak_usd", "cache_write_offpeak_usd"):
        if col not in existing:
            con.execute(
                f"ALTER TABLE model_prices ADD COLUMN {col} REAL NOT NULL DEFAULT 0"
            )
    con.execute("DELETE FROM model_prices")
    con.execute("DELETE FROM price_peak")
    con.execute("DELETE FROM price_peak_skip")

    # Every spelling that appears in the data, so the join always hits a row.
    try:
        seen = {r[0] for r in con.execute("SELECT DISTINCT model FROM usage_events")}
    except Exception:
        seen = set()

    rows = []
    peak_rows = []
    for model in sorted(seen | set(table.models)):
        key, price = table.resolve(model)
        unit = table.usd_per_million(key) if key else None
        meta = (table.models.get(key) or {}) if key else {}
        if unit is None:
            rows.append((model, "", "", "", 0.0, 0.0, 0.0, 0.0,
                         0.0, 0.0, 0.0, 0.0))
            continue
        pk, off = unit["peak"], unit["offpeak"]
        vendor = str(meta.get("vendor") or "")
        rows.append((
            model, key, vendor, str(meta.get("currency") or "USD"),
            float(pk["input"]), float(pk["output"]),
            float(pk["cache_read"]), float(pk["cache_write"]),
            float(off["input"]), float(off["output"]),
            float(off["cache_read"]), float(off["cache_write"]),
        ))
        # A model whose vendor has no schedule needs no window row: the peak
        # predicate is then false and every request bills at the off-peak
        # column, which for a flat-rate model is identical to peak.
        sched = table.peak.get(vendor)
        if sched:
            peak_rows += [
                (model, vendor, lo, hi, sched.dow_start, sched.dow_end)
                for lo, hi in sched.windows
            ]

    if rows:
        con.executemany(
            """INSERT OR REPLACE INTO model_prices
               (model, price_key, vendor, currency,
                input_usd, output_usd, cache_read_usd, cache_write_usd,
                input_offpeak_usd, output_offpeak_usd,
                cache_read_offpeak_usd, cache_write_offpeak_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    if peak_rows:
        con.executemany(
            """INSERT OR REPLACE INTO price_peak
               (model, vendor, start_minute, end_minute, peak_start_dow, peak_end_dow)
               VALUES (?,?,?,?,?,?)""",
            peak_rows,
        )
    skips = [(v, _epoch_day(d))
             for v, s in table.peak.items() for d in s.skip_dates]
    if skips:
        con.executemany(
            "INSERT OR REPLACE INTO price_peak_skip (vendor, epoch_day) VALUES (?,?)",
            skips,
        )
    con.commit()
    return sum(1 for r in rows if r[1])

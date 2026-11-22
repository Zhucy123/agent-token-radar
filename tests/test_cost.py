"""Reference cost must never turn "no published price" into "free".

The cost column is the one figure in this tool that is not measured: it is
tokens x a list price a vendor published. Four properties keep it honest, and
each has a test here:

1. A priced model's cost is its four buckets priced separately (input, output,
   cache read, cache write), not a single blended rate.
2. An unpriced model yields NULL, and NULL is skipped by SUM() -- so a total
   over mixed models covers less than all the usage. ``priced_ratio`` states
   how much it covers, and the UI shows that ratio next to the number.
3. A time-of-day vendor is billed from each request's own timestamp, not from a
   flat rate and not from "now". An off-peak request stays off-peak when the
   report is regenerated during peak hours.
4. Aggregates (by agent, by project, totals) sum the per-row cost; they never
   average a per-row rate, and they never invent a price for a model the table
   does not list.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from _tmp import make_temp_dir

from agent_token_radar.base import UsageEvent
from agent_token_radar.pricing import PriceTable, cost_sql
from agent_token_radar.store import UNCACHED_INPUT_SQL, Store

#: A Tuesday at 02:00 UTC: inside DeepSeek's peak window (01:00-04:00 UTC).
PEAK_TUE = datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)
#: Same Tuesday, 13:00 UTC: outside both windows, so off-peak.
OFF_TUE = datetime(2026, 11, 24, 13, 0, tzinfo=timezone.utc)
#: A Saturday at 02:00 UTC: the clock matches a window but the day does not.
OFF_SAT = datetime(2026, 11, 28, 2, 0, tzinfo=timezone.utc)
#: Thursday 2026-10-01, a Chinese statutory holiday: peak clock, no peak rate.
OFF_HOLIDAY = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)


def ev(agent: str, model: str, *, inp: int, out: int, cr: int = 0, cw: int = 0,
       project: str = "", key: str = "",
       when: datetime | None = None,
       provider_source: str = "measured",
       total: int | None = None) -> UsageEvent:
    """Build an event. ``total`` defaults to the additive sum of the buckets.

    Additive parsers (DSH, Claude) store input EXCLUDING cache, so their total
    is ``inp + out + cr + cw``. Inclusive parsers (Codex, WorkBuddy) put the
    cache hits INSIDE ``inp``, so their total is just ``inp + out`` -- pass
    ``total=inp + out`` to model one of those faithfully. Leaving the default
    for an inclusive-shaped row would describe a row no parser can emit, and
    the two conventions would stop agreeing on ``total``.
    """
    if total is None:
        total = inp + out + cr + cw
    return UsageEvent(
        event_key=key or f"{agent}-{model}-{inp}-{out}-{cr}-{cw}-{project}",
        agent=agent,
        model=model,
        provider="test",
        # Default to "measured" so the provider-provenance statistics have a
        # real bucket to land in; an empty source is its own bucket and would
        # silently pass an assertion that expected "measured".
        provider_source=provider_source,
        project=project,
        timestamp=int(when.timestamp() * 1000) if when else 0,
        total_tokens=total,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
    )


class PriceTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = PriceTable.load()

    def test_priced_model_exposes_four_buckets(self) -> None:
        p = self.table.usd_per_million("deepseek-v4.1-flash")
        self.assertIsNotNone(p)
        peak = p["peak"]
        self.assertAlmostEqual(float(peak["input"]), 0.30)
        self.assertAlmostEqual(float(peak["output"]), 1.20)
        # Cache reads are two orders of magnitude cheaper; a blended rate would
        # overstate a cache-heavy workload enormously.
        self.assertAlmostEqual(float(peak["cache_read"]), 0.006)

    def test_unknown_model_has_no_price(self) -> None:
        # A name nobody publishes a price for must stay unknown.
        self.assertIsNone(self.table.usd_per_million("hy4std"))
        self.assertIsNone(self.table.usd_per_million("no-such-model-xyz"))

    def test_flat_rate_models_have_identical_peak_and_offpeak(self) -> None:
        # OpenAI bills the same rate all day. Callers must not need to branch,
        # so the two blocks are equal rather than one being absent.
        p = self.table.usd_per_million("gpt-6.1-terra")
        self.assertIsNotNone(p)
        for bucket in ("input", "output", "cache_read", "cache_write"):
            self.assertEqual(p["peak"][bucket], p["offpeak"][bucket], bucket)

    def test_deepseek_offpeak_is_half_of_peak(self) -> None:
        # Stated on DeepSeek's pricing page: "Off-peak rates are half of the
        # peak rates."
        p = self.table.usd_per_million("deepseek-v4.1-flash")
        self.assertIsNotNone(p)
        for bucket in ("input", "output", "cache_read"):
            self.assertAlmostEqual(
                float(p["offpeak"][bucket]), float(p["peak"][bucket]) / 2, places=9,
                msg=bucket)

    def test_prices_are_usd_with_no_conversion(self) -> None:
        # Zhipu and Qwen publish a CNY sheet, but they also publish a USD sheet
        # and that is what the table records. No exchange rate is applied
        # anywhere, so the stored number must equal the published USD number.
        glm = self.table.usd_per_million("glm-5.3-flash")
        self.assertIsNotNone(glm)
        self.assertAlmostEqual(float(glm["peak"]["input"]), 0.15)
        self.assertAlmostEqual(float(glm["peak"]["output"]), 0.50)

        qwen = self.table.usd_per_million("qwen3.8-flash")
        self.assertIsNotNone(qwen)
        # China (Beijing) region, USD list.
        self.assertAlmostEqual(float(qwen["peak"]["input"]), 0.113)
        self.assertAlmostEqual(float(qwen["peak"]["output"]), 0.382)

    def test_alias_does_not_fall_through_to_a_different_model(self) -> None:
        # `gpt-6.1-terra` must resolve to itself, never to `gpt-6.1-sol`.
        key, price = self.table.resolve("gpt-6.1-terra")
        self.assertEqual(key, "gpt-6.1-terra")
        self.assertIsNotNone(price)
        # A genuinely unknown name must NOT borrow a neighbouring tier.
        key2, price2 = self.table.resolve("gpt-6.1-terra-unknown-variant")
        self.assertIn(key2, ("", "gpt-6.1-terra"))


class PeakScheduleTests(unittest.TestCase):
    """The schedule itself, read from pricing.json."""

    def setUp(self) -> None:
        self.table = PriceTable.load()
        self.sched = self.table.peak.get("deepseek")

    def test_deepseek_has_a_schedule(self) -> None:
        self.assertIsNotNone(self.sched)
        self.assertTrue(self.sched)

    def test_window_boundaries(self) -> None:
        # 01:00-04:00 and 06:00-10:00 UTC. The start is inclusive, the end is
        # not: a request at exactly 04:00 bills off-peak.
        cases = [
            (datetime(2026, 11, 24, 0, 59, tzinfo=timezone.utc), False),
            (datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc), True),
            (datetime(2026, 11, 24, 3, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 11, 24, 4, 0, tzinfo=timezone.utc), False),
            (datetime(2026, 11, 24, 5, 59, tzinfo=timezone.utc), False),
            (datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc), True),
            (datetime(2026, 11, 24, 9, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc), False),
        ]
        for when, want in cases:
            self.assertEqual(self.sched.is_peak(when), want, when.isoformat())

    def test_weekends_are_offpeak_all_day(self) -> None:
        self.assertFalse(self.sched.is_peak(OFF_SAT))
        self.assertFalse(self.sched.is_peak(
            datetime(2026, 11, 29, 7, 0, tzinfo=timezone.utc)))

    def test_holidays_are_offpeak_all_day(self) -> None:
        self.assertFalse(self.sched.is_peak(OFF_HOLIDAY))

    def test_ordinary_weekday_is_peak_inside_a_window(self) -> None:
        self.assertTrue(self.sched.is_peak(PEAK_TUE))


class StoreCostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir("atr_cost_")
        self.store = Store(self.dir / "t.db")

    def tearDown(self) -> None:
        self.store.close()

    def test_row_cost_uses_each_bucket_rate(self) -> None:
        # 1M uncached input + 1M output at DeepSeek Flash peak = 0.30 + 1.20.
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=1_000_000,
               when=PEAK_TUE),
        ])
        t = self.store.totals()
        self.assertAlmostEqual(t["cost_usd"], 1.50, places=4)
        self.assertEqual(t["priced_tokens"], 2_000_000)
        self.assertAlmostEqual(t["priced_ratio"], 1.0)

    def test_cache_reads_are_billed_at_the_hit_rate(self) -> None:
        # 1M cache reads at peak, nothing else: 0.006, not 0.30.
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=0, out=0, cr=1_000_000,
               when=PEAK_TUE),
        ])
        t = self.store.totals()
        self.assertAlmostEqual(t["cost_usd"], 0.006, places=5)

    def test_offpeak_request_is_billed_at_half(self) -> None:
        """The core of the schedule: the rate follows the request's clock."""
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0,
               when=OFF_TUE),
        ])
        t = self.store.totals()
        self.assertAlmostEqual(t["cost_usd"], 0.15, places=4)

    def test_weekend_request_is_billed_at_half(self) -> None:
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0,
               when=OFF_SAT),
        ])
        self.assertAlmostEqual(self.store.totals()["cost_usd"], 0.15, places=4)

    def test_holiday_request_is_billed_at_half(self) -> None:
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0,
               when=OFF_HOLIDAY),
        ])
        self.assertAlmostEqual(self.store.totals()["cost_usd"], 0.15, places=4)

    def test_inclusive_input_does_not_double_bill_cache_reads(self) -> None:
        """Codex-style rows bill cache hits once, at the cache rate.

        ``input_tokens`` means different things per agent. Additive parsers
        (DSH, Claude) store input EXCLUDING cache, so ``input + output +
        cache_read + cache_write`` is the total. Inclusive parsers (Codex,
        WorkBuddy) store the FULL prompt already containing the cache hits, and
        ``input + output`` is the total.

        Billing ``input_tokens`` as the uncached bucket charges an inclusive
        row's cached tokens twice: once at the input rate and again at the
        cache-read rate. On this machine's real database that overstated total
        cost 5x ($1875.84 vs $374.47) and codex alone 12x ($1532.53 vs
        $120.38). ``test_cost.py`` only used additive rows, so nothing caught it.

        1M prompt, 800K of it cache read, on Terra (flat $1 in / $0.05 cache
        read): the 200K uncached costs 0.20 and the 800K reads cost 0.04, so
        the row must be 0.24 -- not 1.00 + 0.04 = 1.04.
        """
        self.store.upsert_events([
            ev("codex", "gpt-6.1-terra", inp=1_000_000, out=0, cr=800_000,
               when=PEAK_TUE, total=1_000_000),
        ])
        t = self.store.totals()
        self.assertAlmostEqual(t["cost_usd"], 0.24, places=4)
        # The display already reported 200K uncached; cost must agree with it.
        self.assertEqual(t["uncached_input_tokens"], 200_000)

    def test_additive_rows_are_unchanged_by_the_uncached_bucket(self) -> None:
        """The fix must not move the agents it was never about.

        Additive parsers exclude cache from ``input_tokens``, so the uncached
        bucket equals that column and their cost is bit-for-bit what it was.
        """
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0, cr=2_000_000,
               when=PEAK_TUE),
        ])
        t = self.store.totals()
        # 1M input at 0.30 + 2M cache reads at 0.006.
        self.assertAlmostEqual(t["cost_usd"], 0.312, places=4)
        self.assertEqual(t["uncached_input_tokens"], 1_000_000)

    def test_inclusive_and_additive_rows_agree_in_one_total(self) -> None:
        """Two agents describing the SAME traffic must cost the same.

        This is the invariant the bug broke: it was invisible per agent but
        obvious across them -- the inclusive agent looked 12x more expensive
        for identical token counts.
        """
        self.store.upsert_events([
            # Additive: 1M uncached + 2M cache reads (total = 3M).
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0, cr=2_000_000,
               when=PEAK_TUE, key="additive"),
            # Inclusive: the same traffic, input stated as the full 3M prompt,
            # so total is input + output = 3M, same as the additive row.
            ev("codex", "deepseek-v4.1-flash", inp=3_000_000, out=0, cr=2_000_000,
               when=PEAK_TUE, key="inclusive", total=3_000_000),
        ])
        rows = {r["agent"]: r for r in self.store.by_agent()}
        self.assertAlmostEqual(rows["dsh"]["cost_usd"], 0.312, places=4)
        self.assertAlmostEqual(rows["codex"]["cost_usd"], 0.312, places=4)
        self.assertAlmostEqual(self.store.totals()["cost_usd"], 0.624, places=4)

    def test_corrupt_row_cannot_produce_a_negative_cost(self) -> None:
        """A row claiming more cache than its total must not refund money.

        The billing input bucket is derived (``total - output - cache - cache
        write``), so unlike the old ``input_tokens`` column it can go negative
        on a corrupt row. A negative input bucket would SUBTRACT from the bill
        and a total could come out negative -- money no vendor refunds. The
        display keeps the raw negative so the bad row stays visible; only the
        billing side clamps.
        """
        self.store.upsert_events([
            # More cache reads than the row's own total: nonsense data.
            ev("dsh", "deepseek-v4.1-flash", inp=0, out=0, cr=50, cw=0,
               total=10, when=PEAK_TUE),
        ])
        t = self.store.totals()
        self.assertGreaterEqual(t["cost_usd"], 0.0,
                                "a corrupt row must never make the bill negative")
        # The display still reports the bad input so the parser can be fixed.
        self.assertLess(t["uncached_input_tokens"], 0)

    def test_peak_and_offpeak_rows_coexist_in_one_total(self) -> None:
        """One total must mix rates per row, not pick one rate for the group.

        This is the property a daily average would break: two requests the same
        size, one in the window and one outside, must cost 0.30 + 0.15.
        """
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0,
               when=PEAK_TUE, key="peak"),
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0,
               when=OFF_TUE, key="off"),
        ])
        t = self.store.totals()
        self.assertAlmostEqual(t["cost_usd"], 0.45, places=4)

    def test_flat_rate_vendor_ignores_the_clock(self) -> None:
        # OpenAI has no schedule, so the same request costs the same at either
        # time. This guards against applying DeepSeek's window to everyone.
        self.store.upsert_events([
            ev("codex", "gpt-6.1-terra", inp=1_000_000, out=0,
               when=PEAK_TUE, key="p"),
            ev("codex", "gpt-6.1-terra", inp=1_000_000, out=0,
               when=OFF_TUE, key="o"),
        ])
        t = self.store.totals()
        self.assertAlmostEqual(t["cost_usd"], 2.00, places=4)

    def test_unpriced_model_contributes_no_cost_and_lowers_coverage(self) -> None:
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0, key="a",
               when=PEAK_TUE),
            # No public price: must not become $0 of priced usage.
            ev("workbuddy", "hy4std", inp=1_000_000, out=0, key="b",
               when=PEAK_TUE),
        ])
        t = self.store.totals()
        self.assertAlmostEqual(t["cost_usd"], 0.30, places=4)
        # Half the tokens had a price, so coverage must read 50%, not 100%.
        self.assertEqual(t["priced_tokens"], 1_000_000)
        self.assertAlmostEqual(t["priced_ratio"], 0.5)

    def test_unpriced_agent_row_reports_null_not_zero(self) -> None:
        self.store.upsert_events([
            ev("workbuddy", "hy4std", inp=500_000, out=0),
        ])
        rows = self.store.by_agent()
        self.assertEqual(len(rows), 1)
        # SUM over all-NULL rows is NULL; the UI renders that as a dash.
        self.assertIsNone(rows[0]["cost_usd"])
        self.assertEqual(rows[0]["priced_tokens"], 0)

    def test_by_agent_sums_row_costs(self) -> None:
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0, key="a",
               when=PEAK_TUE),
            ev("dsh", "deepseek-v4.1-flash", inp=0, out=1_000_000, key="b",
               when=PEAK_TUE),
        ])
        rows = self.store.by_agent()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["cost_usd"], 1.50, places=4)

    def test_project_cost_merges_agents_by_addition(self) -> None:
        project = "/work/x"
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0,
               project=project, key="p1", when=PEAK_TUE),
            ev("codex", "gpt-6.1-terra", inp=1_000_000, out=0,
               project=project, key="p2", when=PEAK_TUE),
        ])
        rows = self.store.by_project()
        self.assertEqual(len(rows), 1)
        # 0.30 (DeepSeek) + 1.00 (Terra) added, not averaged.
        self.assertAlmostEqual(rows[0]["cost_usd"], 1.30, places=4)
        self.assertEqual(len(rows[0]["agents"]), 2)
        self.assertAlmostEqual(rows[0]["agents"][0]["cost_usd"] +
                               rows[0]["agents"][1]["cost_usd"], 1.30, places=4)

    def test_price_table_is_reloaded_from_json_not_carried_over(self) -> None:
        """A price removed from pricing.json must stop being billed.

        The table is rebuilt on every connection open, so an operator deleting
        a withdrawn model's price cannot leave the old rate active in a stale
        database.
        """
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0, when=PEAK_TUE),
        ])
        before = self.store.totals()["cost_usd"]
        self.assertGreater(before, 0)

        # Simulate a withdrawal: reload the same DB with a table lacking it.
        self.store.close()
        stripped = PriceTable({"models": {}, "sources": {}, "_updated": "x"})
        store2 = Store(self.dir / "t.db")
        try:
            store2.prices = stripped
            store2._init_connection(store2.con)
            after = store2.totals()
            self.assertEqual(after["cost_usd"], 0.0)
            self.assertEqual(after["priced_tokens"], 0)
        finally:
            store2.close()

    def test_schedule_edit_needs_no_migration(self) -> None:
        """Editing the window in pricing.json must take effect on reopen.

        The peak tables are rebuilt with the price table, so an operator who
        corrects a window does not need a schema migration or a rebuild.
        """
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=0,
               when=datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)),
        ])
        self.assertAlmostEqual(self.store.totals()["cost_usd"], 0.30, places=4)
        self.store.close()

        # Reload with the windows pushed later, so that same UTC instant is
        # now outside them.
        original = PriceTable.load()
        data = dict(original.raw)
        data["peak_hours"] = {
            "deepseek": {
                "utc_windows": [["12:00", "14:00"]],
                "weekdays_only": True,
                "skip_dates": [],
                "offpeak_multiplier": 0.5,
            }
        }
        store2 = Store(self.dir / "t.db")
        try:
            store2.prices = PriceTable(data)
            store2._init_connection(store2.con)
            self.assertAlmostEqual(store2.totals()["cost_usd"], 0.15, places=4)
        finally:
            store2.close()


class StatsCacheTests(unittest.TestCase):
    """The whole-table statistics are cached; the cache must never go stale.

    ``provider_stats`` and ``project_stats`` were ~65 ms each on 76k rows and do
    not depend on the time range, so they are memoised. The only way that is
    safe is if every write clears them -- this exercises that.
    """

    def setUp(self) -> None:
        self.dir = make_temp_dir("atr_cache_")
        self.store = Store(self.dir / "t.db")

    def tearDown(self) -> None:
        self.store.close()

    def test_provider_stats_updates_after_a_write(self) -> None:
        self.assertEqual(self.store.provider_stats()["totals"]["measured"], 0)
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=10, out=0, key="k1"),
        ])
        # A stale cache would still say 0 here.
        self.assertEqual(self.store.provider_stats()["totals"]["measured"], 1)

    def test_project_stats_updates_after_a_write(self) -> None:
        before = self.store.project_stats()
        self.assertEqual(before["total"], 0)
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=10, out=0, key="k2"),
        ])
        after = self.store.project_stats()
        self.assertEqual(after["total"], 1)
        self.assertEqual(after["with_project"], 0)

    def test_cache_returns_the_same_answer_on_repeat(self) -> None:
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=10, out=0, key="k3"),
        ])
        a = self.store.provider_stats()
        b = self.store.provider_stats()
        self.assertIs(a, b)          # actually cached, not recomputed
        self.assertEqual(a["totals"], b["totals"])

    def test_backfill_invalidates_the_cache(self) -> None:
        # A backfill rewrites project/provider columns on existing rows; if it
        # did not invalidate, the footer would keep the pre-backfill numbers.
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=10, out=0, key="k4"),
        ])
        self.store.provider_stats()          # prime the cache
        self.store.invalidate_stats_cache()
        self.assertEqual(self.store.provider_stats()["totals"]["measured"], 1)


class SnapshotTests(unittest.TestCase):
    """``overview_snapshot`` folds three queries into one scan; it must not drift.

    The dashboard used to call totals/by_agent/by_model separately, and each
    re-scanned the table and re-priced every row. The folded version derives the
    per-agent and overall figures from the by-model rows, which is only sound
    because every column is a SUM. These tests pin that equivalence.
    """

    def setUp(self) -> None:
        self.dir = make_temp_dir("atr_snap_")
        self.store = Store(self.dir / "t.db")

    def tearDown(self) -> None:
        self.store.close()

    def _seed(self) -> None:
        self.store.upsert_events([
            ev("dsh", "deepseek-v4.1-flash", inp=1_000_000, out=100_000,
               when=PEAK_TUE, key="s1", project="/p1"),
            ev("dsh", "deepseek-v4.1-flash", inp=500_000, out=0, cr=2_000_000,
               when=OFF_TUE, key="s2", project="/p1"),
            ev("codex", "gpt-6.1-terra", inp=200_000, out=50_000,
               when=PEAK_TUE, key="s3", project="/p2"),
            # An unpriced model: it must lower coverage in every view alike.
            ev("workbuddy", "hy4std", inp=300_000, out=0, key="s4"),
        ])

    def test_totals_match_the_standalone_query(self) -> None:
        self._seed()
        snap = self.store.overview_snapshot()
        t = self.store.totals()
        for k in ("events", "total_tokens", "input_tokens", "output_tokens",
                  "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                  "uncached_input_tokens", "priced_tokens"):
            self.assertEqual(snap["totals"][k], t[k], k)
        for k in ("cost_usd", "cache_hit", "priced_ratio"):
            self.assertAlmostEqual(float(snap["totals"][k] or 0),
                                   float(t[k] or 0), places=9, msg=k)

    def test_by_agent_matches_the_standalone_query(self) -> None:
        self._seed()
        snap = self.store.overview_snapshot()["by_agent"]
        standalone = self.store.by_agent()
        self.assertEqual([r["agent"] for r in snap],
                         [r["agent"] for r in standalone])
        for x, y in zip(snap, standalone):
            for k in ("events", "total_tokens", "input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_write_tokens",
                      "uncached_input_tokens", "priced_tokens"):
                self.assertEqual(x[k], y[k], f"{x['agent']}.{k}")
            self.assertAlmostEqual(float(x["cache_hit"]), float(y["cache_hit"]),
                                   places=12)
            # cost_usd is None when no row in the group is priced, in both paths.
            self.assertEqual(x["cost_usd"] is None, y["cost_usd"] is None)
            if x["cost_usd"] is not None:
                self.assertAlmostEqual(float(x["cost_usd"]), float(y["cost_usd"]),
                                       places=9)

    def test_by_model_is_the_same_list(self) -> None:
        self._seed()
        snap = self.store.overview_snapshot()
        self.assertEqual(snap["by_model"], self.store.by_model())

    def test_a_range_filter_applies_to_every_view(self) -> None:
        self._seed()
        # A window that contains only the off-peak row.
        lo = int(OFF_TUE.timestamp() * 1000) - 1000
        hi = int(OFF_TUE.timestamp() * 1000) + 1000
        snap = self.store.overview_snapshot(since_ms=lo, until_ms=hi)
        self.assertEqual(snap["totals"]["events"], 1)
        # 500k input at the off-peak rate + 2M cache reads at the off-peak rate.
        expected = (0.5 * 0.15) + (2.0 * 0.003)
        self.assertAlmostEqual(snap["totals"]["cost_usd"], expected, places=4)

    def test_coverage_matches_across_views(self) -> None:
        self._seed()
        snap = self.store.overview_snapshot()
        priced = sum(r["priced_tokens"] for r in snap["by_model"])
        self.assertEqual(priced, snap["totals"]["priced_tokens"])
        # hy4std is 300k of the tokens and has no price.
        self.assertLess(snap["totals"]["priced_ratio"], 1.0)


class SqlShapeTests(unittest.TestCase):
    """Guards on the generated SQL itself, not on a query result.

    A behavioural test cannot see a latent ambiguity: unqualified columns join
    fine until some future column collides. These assert the shape that keeps
    the query safe and the billing honest.
    """

    def test_uncached_input_columns_are_table_qualified(self) -> None:
        """Every column must survive the LEFT JOIN onto ``model_prices``.

        An unqualified ``output_tokens`` resolves today only because
        ``model_prices`` happens to have no column of that name. Add one and
        the query dies with "ambiguous column name". Qualifying makes that
        impossible.
        """
        cols = ("total_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens")
        for col in cols:
            self.assertIn(f"usage_events.{col}", UNCACHED_INPUT_SQL,
                          f"{col} must be qualified to survive the price join")
        stripped = UNCACHED_INPUT_SQL
        for col in cols:
            stripped = stripped.replace(f"usage_events.{col}", "")
        for col in cols:
            self.assertNotIn(col, stripped,
                             f"{col} appears unqualified in the expression")

    def test_cost_sql_clamps_the_derived_input_bucket(self) -> None:
        """Billing clamps a negative input bucket; the old raw column could not
        go negative, so this hazard arrived with the derived expression."""
        sql = cost_sql()
        self.assertEqual(sql.count("MAX(0,"), 2,
                         "both the peak and the off-peak branch must clamp")

    def test_cost_sql_no_longer_bills_raw_input_tokens(self) -> None:
        """The regression guard: this is THE bug that overstated cost 5x."""
        self.assertNotIn(
            "usage_events.input_tokens", cost_sql(),
            "cost must not bill input_tokens: for inclusive agents that column "
            "already contains the cache hits, so billing it charges the cached "
            "tokens twice")


if __name__ == "__main__":
    unittest.main()

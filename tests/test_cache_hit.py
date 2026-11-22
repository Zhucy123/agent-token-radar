"""Cache-hit rate and "uncached input" must not depend on an agent's convention.

Agents disagree about whether ``input_tokens`` includes the cache buckets:

* additive  (DSH, Claude, Gemini, Hermes): total = input + output + cr + cw
* inclusive (Codex, WorkBuddy):            total = input + output

Codex really stores ``input_tokens=572,797,230`` with
``cached_input_tokens=558,823,552``.  Reading that input column as "uncached"
overstates it 41x, so the dashboard derives the uncached part from the total
instead.  These tests pin that derivation and the ratio arithmetic.
"""

from __future__ import annotations

import unittest

from _tmp import drop_temp_dir, make_temp_dir

from agent_token_radar.base import UsageEvent
from agent_token_radar.store import CACHE_HIT_SQL, UNCACHED_INPUT_SQL, Store


def ev(agent: str, total: int, output: int, cache_read: int,
       cache_write: int = 0, key: str = "") -> UsageEvent:
    """One row with the given buckets; input is irrelevant to the derivation."""
    return UsageEvent(
        event_key=key or f"{agent}-{total}-{output}-{cache_read}-{cache_write}",
        agent=agent,
        total_tokens=total,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


class UncachedDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir("atr_hit_")
        self.store = Store(self.dir / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        drop_temp_dir(self.dir)

    def test_additive_agent_uncached_equals_its_stored_input(self) -> None:
        """An additive agent already stores uncached input; derivation must agree."""
        # DSH shape: input 100 excludes cache, so total = 100 + 20 + 900.
        self.store.upsert_events([ev("dsh", total=1020, output=20, cache_read=900)])
        t = self.store.totals()
        self.assertEqual(t["uncached_input_tokens"], 100)
        self.assertAlmostEqual(t["cache_hit"], 900 / 1000, places=6)

    def test_inclusive_agent_input_is_not_reported_as_uncached(self) -> None:
        """The Codex case: input includes cache and must be reduced."""
        # Codex shape: the agent's own total is input + output only.
        # input 1000 (includes 900 cached) + output 20 -> total 1020.
        self.store.upsert_events([ev("codex", total=1020, output=20, cache_read=900)])
        t = self.store.totals()
        self.assertEqual(t["uncached_input_tokens"], 100)
        self.assertAlmostEqual(t["cache_hit"], 900 / 1000, places=6)

    def test_cache_write_is_excluded_from_the_prompt_denominator(self) -> None:
        """Cache write is paid once, not part of the re-read denominator."""
        self.store.upsert_events([ev("x", total=1000, output=100, cache_read=700,
                              cache_write=50)])
        t = self.store.totals()
        # 1000 - 100 - 700 - 50 = 150 uncached.
        self.assertEqual(t["uncached_input_tokens"], 150)
        # Hit rate ignores writes: 700 / (150 + 700).
        self.assertAlmostEqual(t["cache_hit"], 700 / 850, places=6)

    def test_empty_prompt_does_not_divide_by_zero(self) -> None:
        self.store.upsert_events([ev("x", total=0, output=0, cache_read=0)])
        t = self.store.totals()
        self.assertEqual(t["cache_hit"], 0.0)

    def test_hit_rate_is_a_ratio_of_sums_not_an_average_of_ratios(self) -> None:
        """The bug this guards: averaging per-row ratios gave 0% overall.

        One busy row (99% cached) plus many empty rows. Averaging ratios counts
        every empty row as 0.0 and collapses the result; the correct aggregate
        is computed from summed numerator and denominator.
        """
        rows = [ev("a", total=1_000_000, output=10_000, cache_read=980_000,
                   key="busy")]
        for i in range(50):
            rows.append(ev("a", total=0, output=0, cache_read=0, key=f"empty{i}"))
        self.store.upsert_events(rows)
        t = self.store.totals()
        # uncached = 1,000,000 - 10,000 - 980,000 = 10,000; hit = 980k/990k.
        self.assertEqual(t["uncached_input_tokens"], 10_000)
        self.assertAlmostEqual(t["cache_hit"], 980_000 / 990_000, places=6)
        self.assertGreater(t["cache_hit"], 0.98)

    def test_sql_expressions_are_used_consistently(self) -> None:
        """Every aggregation must report the same global figures."""
        self.store.upsert_events([
            ev("a", total=1020, output=20, cache_read=900, key="a1"),
            ev("b", total=2000, output=100, cache_read=1500, cache_write=100,
               key="b1"),
        ])
        t = self.store.totals()
        for rows in (self.store.by_agent(), self.store.by_project(),
                     self.store.by_model(), self.store.top_sessions()):
            pass  # each must at least run without an SQL error
        by_agent = self.store.by_agent()
        self.assertEqual(sum(r["uncached_input_tokens"] for r in by_agent),
                         t["uncached_input_tokens"])
        self.assertEqual(sum(r["total_tokens"] for r in by_agent),
                         t["total_tokens"])

    def test_bad_total_shows_negative_input_without_crashing(self) -> None:
        """A corrupt row keeps its negative input visible, and must not blow up.

        This asserts the DISPLAY side deliberately does NOT clamp: an uncached
        value derived from a bad total (more cache reads than the row's own
        total) is shown as the negative number it is, so whoever is diagnosing
        the parser can see that the data is wrong. Clamping it to 0 here would
        turn a visible defect into a plausible-looking zero.

        Billing is the one place that DOES clamp -- see ``COST_ROW_SQL``'s
        ``MAX(0, ...)``: a negative bucket would subtract from the bill and
        could make a total negative, and no vendor refunds that. The split is
        intentional: show the defect, never bill it.
        """
        self.store.upsert_events([ev("x", total=10, output=100, cache_read=50)])
        t = self.store.totals()
        # Unclamped on purpose: the bad total surfaces as a negative input.
        self.assertLess(t["uncached_input_tokens"], 0)
        # The rate cannot be computed from a contradiction, so it is None.
        self.assertIsNone(t["cache_hit"])

    def test_corrupt_row_reports_no_rate_at_all(self) -> None:
        """Neither 500% nor 100% nor 0% -- no rate, because none is knowable.

        The denominator is built from the derived uncached value, which goes
        negative on a row claiming more cache reads than its own total. For
        total=10, cache_read=50 the denominator collapses to 10 while the
        numerator stays 50, so the plain division reads 5.0 (500%), which
        ``app.js`` would have rendered verbatim.

        Clamping to 1.0 fixes the absurd number but replaces it with a subtler
        lie: a spotless "100%" for data that is broken. NULL is the honest
        answer, and the UI renders it as a dash.
        """
        self.store.upsert_events([
            ev("x", total=10, output=0, cache_read=50, cache_write=0),
        ])
        t = self.store.totals()
        self.assertIsNone(t["cache_hit"],
                          "a contradictory row has no defensible hit rate")
        # The same rule must hold on the per-agent rows.
        for row in self.store.by_agent():
            self.assertIsNone(row["cache_hit"])

    def test_merged_rows_report_no_rate_like_the_sql(self) -> None:
        """The Python mirror must not disagree with the SQL.

        ``by_project`` merges rows in Python and recomputes the rate from the
        merged sums, so a mirror that merely clamped would report 100% there
        while the SQL-aggregated views reported None.
        """
        self.store.upsert_events([UsageEvent(
            event_key="p1", agent="x", project="/p",
            total_tokens=10, output_tokens=0, cache_read_tokens=50,
        )])
        merged = self.store.by_project()
        self.assertEqual(len(merged), 1)
        self.assertIsNone(merged[0]["cache_hit"])

    def test_every_view_reports_no_rate(self) -> None:
        """No view may swallow the NULL and show a confident number.

        This is the failure mode that bit twice: the rate itself was fixed, but
        a caller kept hiding it -- ``totals()`` wrapped the expression in
        COALESCE(..., 0), and the dashboard card did ``|| 0``. A fix that stops
        at the expression is not a fix. Every public view is checked here so a
        new aggregation cannot quietly reintroduce the 100% claim.
        """
        self.store.upsert_events([UsageEvent(
            event_key="bad", agent="x", model="m", project="/p",
            session_id="s1", timestamp=1789982075201,
            total_tokens=10, output_tokens=0, cache_read_tokens=50,
        )])
        snap = self.store.overview_snapshot()
        views = {
            "totals": self.store.totals()["cache_hit"],
            "by_agent": self.store.by_agent()[0]["cache_hit"],
            "by_model": self.store.by_model()[0]["cache_hit"],
            "by_project": self.store.by_project()[0]["cache_hit"],
            "by_day": self.store.by_day()[0]["cache_hit"],
            "top_sessions": self.store.top_sessions()[0]["cache_hit"],
            "snapshot.totals": snap["totals"]["cache_hit"],
            "snapshot.by_agent": snap["by_agent"][0]["cache_hit"],
            "snapshot.by_model": snap["by_model"][0]["cache_hit"],
        }
        wrong = {k: v for k, v in views.items() if v is not None}
        self.assertEqual(wrong, {}, "these views hid the unknown rate")
        # The raw negative input stays visible on every one of them, so the
        # bad data is still diagnosable.
        self.assertLess(self.store.totals()["uncached_input_tokens"], 0)


class SqlConstantTests(unittest.TestCase):
    def test_expressions_are_plain_sql_fragments(self) -> None:
        self.assertIn("total_tokens", UNCACHED_INPUT_SQL)
        self.assertIn("cache_read_tokens", CACHE_HIT_SQL)
        self.assertNotIn(";", UNCACHED_INPUT_SQL + CACHE_HIT_SQL)


class PlaceholderPurgeTests(unittest.TestCase):
    """Zero-token placeholder rows must not survive in a database.

    Earlier builds stored Claude Code's local ``<synthetic>`` messages and
    Antigravity's failed generations. Every bucket is 0, so they move no
    aggregate, but they surfaced as an ``unknown`` model row in the dashboard
    and inflated the request count.
    """

    def setUp(self) -> None:
        self.dir = make_temp_dir("atr_purge_")
        self.db = self.dir / "t.db"

    def tearDown(self) -> None:
        drop_temp_dir(self.dir)

    def test_placeholder_rows_are_swept_on_open(self) -> None:
        store = Store(self.db)
        # A real row must survive; the placeholders must not.
        store.upsert_events([ev("a", total=1000, output=100, cache_read=800,
                                key="real")])
        store.close()

        import sqlite3

        con = sqlite3.connect(self.db)
        con.execute(
            """INSERT INTO usage_events
                   (event_key, agent, model, total_tokens)
               VALUES ('ph1', 'claude', '<synthetic>', 0),
                      ('ph2', 'gemini', 'unknown', 0)"""
        )
        con.commit()
        con.close()

        store = Store(self.db)  # open runs the sweep
        keys = [r["event_key"] for r in store.con.execute(
            "SELECT event_key FROM usage_events ORDER BY event_key"
        )]
        self.assertEqual(keys, ["real"], "placeholder rows must be swept")
        self.assertEqual(store.totals()["total_tokens"], 1000)
        store.close()

    def test_a_row_written_after_a_sweep_is_swept_on_the_next_open(self) -> None:
        """The sweep must self-heal, not run once.

        An older running process can re-insert a placeholder after the sweep
        has already run. A one-shot marker would leave that row in place
        forever, which is exactly what happened on the user's machine.
        """
        import sqlite3

        store = Store(self.db)
        store.upsert_events([ev("a", total=500, output=50, cache_read=400,
                                key="real")])
        store.close()

        con = sqlite3.connect(self.db)
        con.execute(
            """INSERT INTO usage_events
                   (event_key, agent, model, total_tokens)
               VALUES ('late', 'gemini', 'unknown', 0)"""
        )
        con.commit()
        con.close()

        store = Store(self.db)
        keys = [r["event_key"] for r in store.con.execute(
            "SELECT event_key FROM usage_events"
        )]
        self.assertEqual(keys, ["real"], "a late placeholder must not stick")
        store.close()


if __name__ == "__main__":
    unittest.main()

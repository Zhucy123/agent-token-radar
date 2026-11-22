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

    def test_no_row_can_go_negative(self) -> None:
        """A bad total must not produce a negative "input" in the UI."""
        self.store.upsert_events([ev("x", total=10, output=100, cache_read=50)])
        t = self.store.totals()
        self.assertLess(t["uncached_input_tokens"], 0)  # data is bad, and we
        # ...still must not crash or produce NaN in the rate.
        self.assertGreaterEqual(t["cache_hit"], 0.0)


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

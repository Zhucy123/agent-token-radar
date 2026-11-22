"""The trend chart must stay readable however long the history is.

"All time" is unbounded. The first implementation returned one row per day, so
a user with two years of history got 731 bars and an axis of unreadable,
overlapping labels. The store now buckets by a caller-chosen width and the web
layer picks that width from the data's real span.

Two properties matter and are pinned here:

* widening the bucket never changes the grand total -- it only regroups;
* the chosen width keeps the bucket count bounded for a long history.
"""

from __future__ import annotations

import datetime as dt
import unittest

from _tmp import drop_temp_dir, make_temp_dir

from agent_token_radar.base import UsageEvent
from agent_token_radar.store import Store


def daily_rows(start: dt.date, days: int, per_day: int = 1000) -> list[UsageEvent]:
    """One event per day from ``start``, ``per_day`` total tokens each."""
    out = []
    for i in range(days):
        d = start + dt.timedelta(days=i)
        ms = int(dt.datetime(d.year, d.month, d.day, 12, 0).timestamp() * 1000)
        out.append(UsageEvent(
            event_key=f"k-{d.isoformat()}",
            agent="dsh",
            model="m",
            timestamp=ms,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=850,
            cache_write_tokens=0,
            total_tokens=per_day,
        ))
    return out


class BucketWidthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir("atr_bucket_")
        self.store = Store(self.dir / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        drop_temp_dir(self.dir)

    def test_widening_buckets_preserves_the_total(self) -> None:
        """Grouping is a view concern: no tokens may appear or vanish."""
        self.store.upsert_events(daily_rows(dt.date(2024, 1, 1), 731))
        base = sum(int(r["total_tokens"]) for r in self.store.by_day(bucket_days=1))
        for width in (7, 30, 90):
            got = sum(int(r["total_tokens"])
                      for r in self.store.by_day(bucket_days=width))
            self.assertEqual(got, base, f"bucket_days={width} changed the total")

    def test_bucket_count_shrinks_with_width(self) -> None:
        self.store.upsert_events(daily_rows(dt.date(2024, 1, 1), 731))
        n1 = len(self.store.by_day(bucket_days=1))
        n7 = len(self.store.by_day(bucket_days=7))
        n30 = len(self.store.by_day(bucket_days=30))
        self.assertEqual(n1, 731)
        # 731 days / 7 = 105 weeks (rounded up because of the epoch anchor).
        self.assertLessEqual(n7, 106)
        self.assertLessEqual(n30, 26)
        self.assertLess(n30, n7)
        self.assertLess(n7, n1)

    def test_span_days_counts_real_extent(self) -> None:
        self.store.upsert_events(daily_rows(dt.date(2024, 1, 1), 731))
        self.assertEqual(self.store.span_days(), 731)

    def test_span_days_is_zero_without_data(self) -> None:
        """The caller must be able to fall back without special-casing."""
        self.assertEqual(self.store.span_days(), 0)

    def test_two_year_history_stays_a_readable_number_of_bars(self) -> None:
        """The regression this file exists for: 731 bars was unreadable."""
        self.store.upsert_events(daily_rows(dt.date(2024, 1, 1), 731))
        span = self.store.span_days()
        width = 1 if span <= 130 else 7 if span <= 900 else 30
        bars = len(self.store.by_day(bucket_days=width))
        self.assertLessEqual(bars, 110, "a two-year span must not produce 731 bars")
        self.assertGreater(bars, 1)

    def test_bucket_keys_are_sorted_dates(self) -> None:
        """The renderer indexes series by position, so order must be stable."""
        self.store.upsert_events(daily_rows(dt.date(2024, 3, 1), 40))
        keys = [r["day"] for r in self.store.by_day(bucket_days=7)]
        self.assertEqual(keys, sorted(keys))
        for k in keys:
            dt.date.fromisoformat(k)  # raises if the bucket key is malformed


if __name__ == "__main__":
    unittest.main()

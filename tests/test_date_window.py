"""Explicit date windows: an inclusive local calendar range.

The dashboard can be filtered to any two calendar days, and a click on the trend
chart selects exactly one. Both send ``since``/``until`` as local ``YYYY-MM-DD``
strings, so the parsing and boundary maths live here with tests rather than
being trusted by eye.

Three things are easy to get wrong and are pinned below:

* **Inclusivity.** ``until=2026-11-22`` must mean "through the end of the 22nd".
  Treating it as exclusive midnight silently drops the last day -- a whole day
  of usage, with nothing on screen to suggest data is missing.
* **Local days.** Every other figure is bucketed by the user's local day, so a
  UTC interpretation would make "yesterday" mean two different days depending
  on the hour.
* **Reversed windows.** ``since`` after ``until`` must not quietly behave like
  "no filter" or "empty filter" without anyone agreeing on it.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from agent_token_radar.web import _bounds, _parse_day  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - fastapi is optional
    if exc.name != "fastapi":
        raise
    _bounds = None  # type: ignore[assignment]
    _parse_day = None  # type: ignore[assignment]


def setUpModule() -> None:
    if _bounds is None or _parse_day is None:
        raise unittest.SkipTest("fastapi not installed")

DAY = 86_400_000


def _local_midnight_ms(iso: str) -> int:
    day = dt.date.fromisoformat(iso)
    return int(dt.datetime.combine(day, dt.time.min).astimezone().timestamp() * 1000)


class ParseDayTests(unittest.TestCase):
    def test_iso_date_is_local_midnight(self) -> None:
        self.assertEqual(_parse_day("2026-11-22"), _local_midnight_ms("2026-11-22"))

    def test_empty_and_missing_are_none(self) -> None:
        for value in (None, "", "   "):
            self.assertIsNone(_parse_day(value))

    def test_garbage_is_none_not_an_exception(self) -> None:
        """A bad query parameter must not 500 the dashboard."""
        for value in ("zzz", "2026-13-40", "22/11/2026", "2026-11"):
            self.assertIsNone(_parse_day(value))

    def test_surrounding_space_is_tolerated(self) -> None:
        self.assertEqual(_parse_day(" 2026-11-22 "),
                         _local_midnight_ms("2026-11-22"))


class BoundsTests(unittest.TestCase):
    def test_single_day_covers_that_whole_day(self) -> None:
        since, until = _bounds("custom", "2026-11-22", "2026-11-22")
        self.assertEqual(since, _local_midnight_ms("2026-11-22"))
        self.assertEqual(until, since + DAY)

    def test_until_is_inclusive(self) -> None:
        """Three days means three days, not two."""
        since, until = _bounds("custom", "2026-11-20", "2026-11-22")
        self.assertEqual(until - since, 3 * DAY)

    def test_only_since_leaves_it_open_ended(self) -> None:
        since, until = _bounds("custom", "2026-11-20", None)
        self.assertEqual(since, _local_midnight_ms("2026-11-20"))
        self.assertIsNone(until)

    def test_only_until_starts_at_the_beginning_of_time(self) -> None:
        since, until = _bounds("custom", None, "2026-11-20")
        self.assertIsNone(since)
        self.assertEqual(until, _local_midnight_ms("2026-11-20") + DAY)

    def test_reversed_window_does_not_report_usage(self) -> None:
        """since > until cannot describe any period.

        Returning a normal open range would show every record under a heading
        that claims two specific days; returning nothing is the only honest
        answer, and the UI explains the mistake separately.
        """
        since, until = _bounds("custom", "2026-11-25", "2026-11-20")
        self.assertEqual(until, since, "a reversed window must be empty")

    def test_presets_still_resolve(self) -> None:
        """Adding explicit dates must not change what the old labels meant."""
        for rng in ("today", "7d", "30d"):
            since, until = _bounds(rng)
            self.assertIsNotNone(since, f"{rng} lost its lower bound")

    def test_all_stays_unbounded(self) -> None:
        self.assertEqual(_bounds("all"), (None, None))

    def test_explicit_dates_win_over_the_label(self) -> None:
        """The picker sends range=custom, but a stale label must not override."""
        since, until = _bounds("7d", "2026-11-22", "2026-11-22")
        self.assertEqual(since, _local_midnight_ms("2026-11-22"))
        self.assertEqual(until, since + DAY)

    def test_unknown_label_raises_instead_of_meaning_all(self) -> None:
        """An unrecognised range must NOT quietly become "all time".

        This shipped: the frontend offered a 「昨天」 button that the backend's
        ``_range_bounds`` never learned, the ValueError was swallowed into
        ``(None, None)``, and the dashboard drew all 92 days under a heading
        that said "yesterday". Nothing looked broken -- which is why the
        fallback had to go. A loud error is the only safe answer here.
        """
        with self.assertRaises(ValueError):
            _bounds("nonsense")


class YesterdayTests(unittest.TestCase):
    """「昨天」 is one specific day, bounded on both sides.

    The regression this guards: the button and the date label both said
    "yesterday" while the chart drew the entire history, because the label was
    unknown to the backend and fell through to "all". Checking that the button
    exists -- or that it is pressed -- could never have caught it; only a
    statement about the returned WINDOW can.
    """

    def test_yesterday_is_recognised(self) -> None:
        since, until = _bounds("yesterday")
        self.assertIsNotNone(since, "yesterday must have a lower bound")
        self.assertIsNotNone(
            until, "yesterday must have an UPPER bound too, or it runs into today")

    def test_yesterday_covers_exactly_one_day(self) -> None:
        since, until = _bounds("yesterday")
        self.assertEqual(until - since, DAY,
                         "yesterday must span exactly 24 hours")

    def test_yesterday_ends_where_today_begins(self) -> None:
        y_since, y_until = _bounds("yesterday")
        t_since, _ = _bounds("today")
        self.assertEqual(y_until, t_since,
                         "yesterday must stop exactly where today starts")

    def test_yesterday_is_before_today(self) -> None:
        y_since, _ = _bounds("yesterday")
        t_since, _ = _bounds("today")
        self.assertEqual(t_since - y_since, DAY)

    def test_every_offered_preset_resolves_without_falling_back(self) -> None:
        """The set the UI renders and the set the backend knows must agree.

        Reading the buttons out of index.html is the point: adding a button
        without a backend branch is exactly the mistake being guarded.
        """
        html = (ROOT / "agent_token_radar" / "static" / "index.html").read_text(
            encoding="utf-8")
        offered = re.findall(r'data-range="([a-z0-9]+)"', html)
        self.assertIn("yesterday", offered, "the button should still exist")
        for label in offered:
            with self.subTest(label=label):
                self.assertTrue(
                    _bounds(label) is not None,
                    f"the toolbar offers range={label} but the backend "
                    f"cannot resolve it")

    def test_cli_offers_every_range_the_backend_and_ui_do(self) -> None:
        """CLI and Web are separate entry points; both must know the same labels.

        They drifted once already: the Web toolbar grew 「昨天」 while the CLI's
        ``--range`` choices still stopped at four values, so the same word meant
        "a day" in one place and "not a valid option" in the other. Reading the
        argparse definition keeps the two honest without duplicating a list.
        """
        cli_src = (ROOT / "agent_token_radar" / "cli.py").read_text(encoding="utf-8")
        match = re.search(r'"--range",\s*\n?\s*choices=\[([^\]]+)\]', cli_src)
        self.assertIsNotNone(match, "--range choices not found in cli.py")
        choices = set(re.findall(r'"([a-z0-9]+)"', match.group(1)))

        html = (ROOT / "agent_token_radar" / "static" / "index.html").read_text(
            encoding="utf-8")
        offered = set(re.findall(r'data-range="([a-z0-9]+)"', html))

        self.assertEqual(
            offered - choices, set(),
            f"the Web offers {offered - choices} but the CLI does not")
        for label in choices:
            with self.subTest(label=label):
                self.assertIsNotNone(_bounds(label))


if __name__ == "__main__":
    unittest.main()

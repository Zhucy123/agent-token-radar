"""The calendar heatmap's contract.

Two failure modes matter here and neither shows up as an exception:

* **A tooltip that lies about money.** A day whose models are all unpriced has
  no cost. Rendering that as "$0.00" states the usage was free, which is the one
  thing the project's pricing rules forbid -- so an unpriced day must say so, and
  a partially-priced day must carry its coverage ratio.
* **A calendar that is not a calendar.** The grid has to hold EVERY day of the
  year, including quiet ones, or the shape of the year is unrecoverable. Leap
  years are the obvious trap.

The API is exercised through the real app (minus the scanner), and the frontend
contract is read out of the source.
"""

from __future__ import annotations

import datetime as dt
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_token_radar.web import create_app

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "agent_token_radar" / "static" / "app.js"
INDEX_HTML = ROOT / "agent_token_radar" / "static" / "index.html"
APP_CSS = ROOT / "agent_token_radar" / "static" / "app.css"


class CalendarApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def test_every_day_of_the_year_is_present(self) -> None:
        """A heatmap that only plots days with data is not a calendar."""
        r = self.client.get("/api/calendar", params={"year": "2025"})
        self.assertEqual(r.status_code, 200)
        days = r.json()["days"]
        self.assertEqual(len(days), 365)
        self.assertEqual(days[0]["day"], "2025-01-01")
        self.assertEqual(days[-1]["day"], "2025-12-31")

    def test_a_leap_year_gets_366_days(self) -> None:
        r = self.client.get("/api/calendar", params={"year": "2024"})
        days = r.json()["days"]
        self.assertEqual(len(days), 366)
        self.assertIn("2024-02-29", {d["day"] for d in days})

    def test_days_are_contiguous(self) -> None:
        """No gap and no duplicate: the grid indexes straight into this list."""
        days = self.client.get("/api/calendar", params={"year": "2025"}).json()["days"]
        seen = [d["day"] for d in days]
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))
        first = dt.date.fromisoformat(seen[0])
        for i, iso in enumerate(seen):
            self.assertEqual(first + dt.timedelta(days=i), dt.date.fromisoformat(iso))

    def test_weekday_is_monday_first(self) -> None:
        """The renderer lays columns out Monday-first, so 2026-01-01 is a Thursday."""
        days = self.client.get("/api/calendar", params={"year": "2026"}).json()["days"]
        first = days[0]
        self.assertEqual(first["day"], "2026-01-01")
        self.assertEqual(first["weekday"], 3)   # Mon=0 .. Thu=3

    def test_a_quiet_day_reports_no_usage_not_zero_cost(self) -> None:
        """``cost_usd`` NULL means "no price", which is not "$0.00".

        Every day here is dense, so a day with no usage must come back with
        ``has_data`` false and a NULL cost rather than a confident 0.
        """
        days = self.client.get("/api/calendar", params={"year": "2025"}).json()["days"]
        quiet = [d for d in days if not d["has_data"]]
        self.assertTrue(quiet, "2025 should hold no usage in this database")
        for d in quiet:
            self.assertEqual(d["total_tokens"], 0)
            self.assertIsNone(d["cost_usd"],
                              "a day with no usage has no cost; 0.0 would read as free")

    def test_a_used_day_carries_cost_and_coverage(self) -> None:
        cal = self.client.get("/api/calendar").json()
        used = [d for d in cal["days"] if d["has_data"]]
        if not used:
            self.skipTest("no usage recorded in the current year")
        for d in used:
            self.assertGreater(d["total_tokens"], 0)
            self.assertGreaterEqual(d["priced_ratio"], 0.0)
            self.assertLessEqual(d["priced_ratio"], 1.0)
            # Either an estimate, or explicitly unpriced -- never a bare 0.
            self.assertTrue(d["cost_usd"] is None or d["cost_usd"] > 0,
                            f"{d['day']}: {d['cost_usd']!r}")

    def test_peak_is_the_busiest_day(self) -> None:
        cal = self.client.get("/api/calendar").json()
        peak = max((d["total_tokens"] for d in cal["days"]), default=0)
        self.assertEqual(cal["peak_tokens"], peak)

    def test_an_unknown_year_falls_back_instead_of_failing(self) -> None:
        """A bad query string must not take the dashboard down."""
        for bogus in ("abc", "", "0", "99999", "-1"):
            r = self.client.get("/api/calendar", params={"year": bogus})
            self.assertEqual(r.status_code, 200, bogus)
            self.assertEqual(r.json()["year"], dt.date.today().year, bogus)

    def test_a_past_year_is_honoured(self) -> None:
        r = self.client.get("/api/calendar", params={"year": "2024"})
        self.assertEqual(r.json()["year"], 2024)
        self.assertEqual(len(r.json()["days"]), 366)


class CalendarFrontendTests(unittest.TestCase):
    def _js(self) -> str:
        return APP_JS.read_text(encoding="utf-8")

    def _css(self) -> str:
        return APP_CSS.read_text(encoding="utf-8")

    def test_the_section_exists_and_is_labelled(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('id="calGrid"', html)
        self.assertIn('id="calendar-title"', html)
        self.assertIn('role="grid"', html)

    def test_it_shares_one_column_template_with_the_month_strip(self) -> None:
        """Two independent auto-column grids drifted apart (17.45px vs 15px), so
        a month label pointed at the wrong week."""
        css = self._css()
        m = re.search(r"\.cal-months,\s*\n\s*\.cal-grid\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m, "the two grids must share one rule")
        body = m.group(1)
        self.assertIn("--cal-cols", body)
        self.assertIn("minmax(", body)

    def test_the_cells_are_square_and_fill_their_column(self) -> None:
        css = self._css()
        m = re.search(r"\n  \.cal-cell\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("aspect-ratio: 1", body)
        self.assertNotIn("inline-size: .8125rem", body,
                         "a fixed 13px side left the year covering only half the panel")

    def test_the_cells_carry_no_hairline_ring(self) -> None:
        """The 1px inset ring is what made the whole year look out of focus.

        Measured: with the ring, 421 anti-aliased pixels per scanline at 100%
        scaling and 839 at 150%; without it, 5 and 215 (and 0 with an integer
        pitch). A 1px line on a ~22px square is a fraction of a device pixel
        once the display scales, so Chromium smears it into a grey halo.
        """
        css = self._css()
        m = re.search(r"\n  \.cal-cell\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("box-shadow", m.group(1),
                         "no ring on the cells; the palest band is a fill instead")

    def test_the_palest_band_is_a_fill_not_the_panel_colour(self) -> None:
        """A near-invisible square is not a square. Level 0 must have its own mix."""
        css = self._css()
        m = re.search(r"\n  \.cal-cell\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m)
        self.assertIn("var(--cal-l0)", m.group(1))
        # And the five bands are declared once, on the panel, so the legend and
        # the squares cannot drift apart.
        for name in ("--cal-l0", "--cal-l1", "--cal-l2", "--cal-l3"):
            self.assertIn(name, css, name)
        swatch = re.search(r"\n  \.cal-swatch\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(swatch)
        self.assertIn("var(--cal-l0", swatch.group(1),
                      "the legend swatch must use the same band colours")

    def test_the_weekday_gutter_shares_the_row_grid(self) -> None:
        """The Mon/Wed/Fri labels sat ~94px above their rows.

        Cause: the gutter was a sibling of a wrapper holding BOTH the month strip
        and the day field, so the strip's height pushed the field down while the
        labels stayed at the top. They must share one grid so the second column
        (and hence the row positions) is common to both.
        """
        html = INDEX_HTML.read_text(encoding="utf-8")
        body = re.search(r'<div class="cal-body">(.*?)\n        </div>', html, re.S)
        self.assertIsNotNone(body)
        block = body.group(1)
        # Month strip and weekday gutter are BOTH direct children of cal-body,
        # and the year area holds only the day field.
        self.assertIn('id="calMonthRow"', block)
        self.assertIn('id="calWeekdays"', block)
        year_area = re.search(r'<div class="cal-year-area">(.*?)\n          </div>',
                              block, re.S)
        self.assertIsNotNone(year_area)
        self.assertNotIn('id="calMonthRow"', year_area.group(1),
                         "the month strip must not sit inside the year area")
        css = self._css()
        m = re.search(r"\n  \.cal-body\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m)
        self.assertIn("grid-template-areas", m.group(1))

    def test_the_weekday_rows_have_the_same_pitch_as_the_grid(self) -> None:
        """Content-sized rows put Wed next to Tuesday. `1fr` rows + the same gap
        as the day field make each label row exactly one square row tall."""
        css = self._css()
        # There are several `.cal-weekdays` rules (grid-area, then the layout);
        # take the one that actually sets the row template.
        bodies = [m.group(1) for m in
                  re.finditer(r"\n  \.cal-weekdays\s*\{(.*?)\n  \}", css, re.S)]
        self.assertTrue(bodies, "no .cal-weekdays rule found")
        body = next((b for b in bodies if "grid-template-rows" in b), None)
        self.assertIsNotNone(body, "no .cal-weekdays rule sets grid-template-rows")
        self.assertIn("repeat(7, minmax(0, 1fr))", body)
        self.assertIn("gap: 2px", body)

    def test_no_usage_renders_as_words_not_zero(self) -> None:
        """The one rule the whole project holds: an unknown price is not $0."""
        js = self._js()
        self.assertIn('t("cal.tipEmpty")', js)
        self.assertIn('t("cal.tipNoPrice")', js)
        # The tooltip must branch on null before touching money().
        m = re.search(r"function calendarDayLabel\(d\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("d.cost_usd === null", body)

    def test_partial_coverage_is_stated(self) -> None:
        js = self._js()
        # Called with a template variable, so match the key rather than a call
        # shape: asserting on `t("cal.tipPartial")` verbatim fails on the real
        # `t("cal.tipPartial", { pct })`.
        self.assertIn('"cal.tipPartial"', js)
        self.assertIn("priced_ratio < 0.999", js)

    def test_clicking_a_day_focuses_it_on_the_whole_page(self) -> None:
        js = self._js()
        m = re.search(r"function wireCalendar\(grid\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(m)
        self.assertIn("pickDay(cell.dataset.day)", m.group(1))

    def test_it_is_one_tab_stop_with_arrow_navigation(self) -> None:
        """365 tab stops would bury the rest of the page."""
        js = self._js()
        self.assertIn("cell.tabIndex = -1", js)
        self.assertRegex(js, r"/\*\* Arrow-key navigation over the calendar")
        self.assertIn("onCalendarKey", js)
        # At the boundary it must NOT preventDefault, or arrow keys get trapped.
        m = re.search(r"function onCalendarKey\(ev, grid\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(m)
        self.assertIn("return;   // let the page scroll", m.group(1))

    def test_a_language_switch_rebuilds_the_calendar(self) -> None:
        js = self._js()
        m = re.search(r"async function setLang\(lang\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(m)
        self.assertIn("renderCalendar(window.__lastCalendar)", m.group(1))

    def test_the_number_format_switch_reaches_the_calendar(self) -> None:
        js = self._js()
        m = re.search(r"function setNumFmt\(mode\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(m)
        self.assertIn("renderCalendar(window.__lastCalendar)", m.group(1))

    def test_the_year_arrows_meet_the_touch_target(self) -> None:
        css = self._css()
        blocks = re.findall(r"@media \(pointer: coarse\)\s*\{(.*?)\n  \}", css, re.S)
        self.assertTrue(blocks, "no coarse-pointer block found")
        self.assertIn(".cal-step", "\n".join(blocks))


if __name__ == "__main__":
    unittest.main()

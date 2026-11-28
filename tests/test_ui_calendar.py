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

from agent_token_radar.i18n import STRINGS

try:
    from fastapi.testclient import TestClient
    from agent_token_radar.web import create_app
except ModuleNotFoundError as exc:  # pragma: no cover - fastapi is optional
    if exc.name not in ("fastapi", "httpx"):
        raise
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "agent_token_radar" / "static" / "app.js"
INDEX_HTML = ROOT / "agent_token_radar" / "static" / "index.html"
APP_CSS = ROOT / "agent_token_radar" / "static" / "app.css"


class CalendarApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if TestClient is None or create_app is None:
            raise unittest.SkipTest("fastapi not installed")
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

    def test_the_tooltip_sits_outside_the_sideways_scroller(self) -> None:
        """A single-axis scroll container coerces its other axis to `auto`.

        `.cal-wrap` scrolls sideways (`overflow-x: auto`), so `overflow-y`
        computes to `auto` too and anything taller than it is clipped. The
        tooltip started out as a child of that wrapper, which cut it off at the
        bottom and put a vertical scrollbar across the calendar. It has to be a
        child of the panel, which does not clip.
        """
        html = INDEX_HTML.read_text(encoding="utf-8")
        wrap_open = html.index('<div class="cal-wrap">')
        wrap_close = html.index("</div>\n      </div>", wrap_open)
        scroller = html[wrap_open:wrap_close]
        self.assertIn('id="calGrid"', scroller, "the grid does belong in the scroller")
        self.assertNotIn('id="calTip"', scroller,
                         "the tooltip must not live inside the clipping scroller")
        self.assertIn('id="calTip"', html)

        # The panel is the tooltip's containing block, so it must not clip and
        # must establish positioning.
        css = self._css()
        m = re.search(r"\n  \.cal-panel\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("position: relative", body)
        self.assertIn("overflow: visible", body)

    def test_the_tooltip_is_placed_inside_the_panel_on_both_axes(self) -> None:
        """It has to stay inside the panel horizontally AND vertically.

        Measured on a 1200px window: the box ran to 762px against a scroller
        ending at 814px, so the second line of every tall tooltip was cut. The
        placement must clamp against the panel's client box, flipping below the
        square when there is no room above it, and fall back to pinning itself
        inside the box for a very short panel.
        """
        js = self._js()
        body = self._fn("wireCalendar")
        # The offset parent is the panel, and its padding/client box is used.
        self.assertIn('const panel = $("calPanel") || tip.parentElement;', body)
        self.assertIn("const pcs = getComputedStyle(panel);", body)
        self.assertIn("panel.clientWidth", body)
        self.assertIn("panel.clientHeight", body)
        # Clamped on both axes, with the below-the-square flip.
        self.assertIn("if (cr.top - th - 8 < boxTop) top = cr.bottom + 8 - boxTop;", body)
        self.assertIn("Math.max(0, Math.min(top, boxBottom - boxTop - th))", body)
        self.assertIn("left = Math.max(4, Math.min(left, innerW - tw + 4));", body)
        # The old bug: positioning against the scroller.
        self.assertNotIn("const wrap = tip.parentElement;", body)
        self.assertNotIn("wr.width - tw - 4", body)

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

    def test_tooltip_separator_follows_the_ui_language(self) -> None:
        """A hardcoded ideographic comma leaked into English tooltips.

        The pieces are already translated, but they were joined with "，", so an
        English page read "15 Nov (Saturday)，no usage". The join has to come
        from the string table too.
        """
        js = self._js()
        m = re.search(r"function calendarDayLabel\(d\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn('t("cal.tipSep")', body)
        self.assertIn("parts.join(sep)", body)
        self.assertNotIn("，", body)
        self.assertEqual(STRINGS["zh"]["cal.tipSep"], "，")
        self.assertEqual(STRINGS["en"]["cal.tipSep"], ", ")
        self.assertNotRegex(STRINGS["en"]["cal.tipSep"], r"[\u4e00-\u9fff]")

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

    def _fn(self, name: str) -> str:
        js = self._js()
        m = re.search(rf"function {name}\(([^)]*)\)\s*\{{(.*?)\n  \}}", js, re.S)
        self.assertIsNotNone(m, f"{name} is missing")
        return m.group(2)

    def test_a_narrow_window_reveals_the_busy_side(self) -> None:
        """A scroll container starts at its left edge -- January -- and January of
        a fresh year usually holds nothing, so a laptop user was shown a screen of
        empty squares and had to hunt sideways. The placement has to choose the
        window of week columns holding the MOST active days, not today's column:
        today is one day, and a reader opening the panel wants the year's work."""
        js = self._js()
        body = self._fn("placeCalendarReveal")
        # One bucket per week column, incremented for every day with usage.
        self.assertIn("const active = new Array(cols).fill(0);", body)
        self.assertIn("if (d.total_tokens > 0)", body)
        self.assertIn("active[Math.floor((i + firstWeekday) / 7)] += 1;", body)
        # ...and a sliding window picks the widest sum of them.
        self.assertIn("running += active[c];", body)
        self.assertIn("running -= active[c - visible];", body)
        self.assertIn("if (running >= bestScore)", body)
        # Ties go to the LATER window, so the recent side wins a draw.
        self.assertIn("bestStart = c - visible + 1;", body)

    def test_the_number_of_visible_columns_comes_from_the_measured_pitch(self) -> None:
        """How many columns fit is a measurement, never a guess.

        Guessing it from the viewport width (or from the root font size) is what
        parked the panel at 84px of a possible 303px: the guess said 5 columns
        could fit while the laid-out pitch allowed 17. The floor must divide the
        usable width by the pitch read off real boxes, and the sticky weekday
        gutter has to be subtracted because it covers the first columns.
        """
        js = self._js()
        lay = self._fn("calendarLayout")
        self.assertIn("getBoundingClientRect().left", lay)
        self.assertIn("const pitch = at(1) - at(0);", lay)

        body = self._fn("placeCalendarReveal")
        self.assertIn("const gutterW = gutter ? gutter.getBoundingClientRect().width + 8 : 0;",
                      body)
        self.assertIn("const usable = wrap.clientWidth - gutterW;", body)
        self.assertIn("Math.floor((usable + 2) / pitch)", body)
        # No deriving the pitch from font size or client width.
        for banned in ("fontSize", "getComputedStyle", "offsetWidth"):
            self.assertNotIn(banned, lay + body, f"{banned} is a guess, not a measurement")

    def test_reveal_gives_up_gracefully(self) -> None:
        """No overflow, no days, or no day with usage must leave the year at
        January instead of throwing or scrolling somewhere arbitrary -- and every
        scroll must be clamped, so a long delta can never fling the panel past
        the end."""
        js = self._js()
        body = self._fn("placeCalendarReveal")
        self.assertIn("const overflow = wrap.scrollWidth - wrap.clientWidth;", body)
        self.assertIn("if (overflow <= 1) { settle(0); return; }", body)
        self.assertIn("if (!days.length) return;", body)
        self.assertIn("if (!active.some((n) => n > 0)) { settle(0); return; }", body)
        self.assertIn("if (visible >= cols) { settle(0); return; }", body)
        # The only assignment goes through the clamp.
        self.assertIn("wrap.scrollLeft = Math.max(0, Math.min(target, maxScroll));",
                      body)
        self.assertEqual(body.count("scrollLeft ="), 1,
                         "every scroll must go through settle()")

    def test_reveal_only_re_places_when_the_measured_geometry_changes(self) -> None:
        """A text-size change moves the grid a moment after the event, so the
        trigger cannot be "a resize happened" -- it has to be the measured
        geometry, compared against what was placed last.

        Two real failures came out of getting this wrong: a placement computed
        from a stale 14px pitch parked the panel at 84px of a possible 303px
        (showing 50 of the year's 94 active days), and a version keyed on the
        wrapper width skipped every later correction. Comparing against reality
        makes a stale read a no-op instead of a wrong answer.
        """
        js = self._js()
        body = self._fn("placeIfChanged")
        self.assertIn("const lay = calendarLayout();", body)
        self.assertIn("const width = lay.wrap.clientWidth;", body)
        self.assertIn("Math.abs(lay.pitch - lastPlaced.pitch) <= 0.01", body)
        self.assertIn("Math.abs(width - lastPlaced.width) <= 0.5", body)
        # It remembers what it placed, so the same geometry is a no-op...
        self.assertIn("lastPlaced = { pitch: lay.pitch, width, year: cal.year, days };",
                      body)
        # ...and it re-places through the armed path, not by writing scrollLeft.
        self.assertIn("placeCalendarReveal(cal, lay);", body)
        self.assertNotIn("scrollLeft =", body)

    def test_switching_years_re_places_even_though_the_pitch_is_the_same(self) -> None:
        """A 365-day year and a 366-day one lay out at the same column pitch, so
        "the geometry changed" is not enough to detect a year switch.

        Leaving the year out of the comparison meant "previous year" kept the
        old year's scroll offset; with an empty year on screen the panel sat at
        its far right, showing nothing, while the year it should have been
        showing started at the left (measured: 303px into an empty 2025).
        """
        js = self._js()
        body = self._fn("placeIfChanged")
        self.assertIn("const cal = window.__lastCalendar;", body)
        self.assertIn("const days = (cal.days || []).length;", body)
        self.assertIn("cal.year === lastPlaced.year", body)
        self.assertIn("days === lastPlaced.days", body)
        self.assertIn("lastPlaced = { pitch: lay.pitch, width, year: cal.year, days };",
                      body)
        # The re-place must use the year that was just measured, not a stale one.
        self.assertIn("placeCalendarReveal(cal, lay);", body)

    def test_the_reveal_runs_from_the_render_and_from_a_font_change(self) -> None:
        """Something has to CALL the placement, and the two callers are fixed.

        The ResizeObserver is the trigger for a window resize, but it is not
        reliable for a text-size change -- measured in the browser check, after
        two font-size changes it stopped calling back at all, and the panel was
        left showing the previous size's columns. So the settings path calls in
        too, at the one moment the change is known to have happened.
        """
        js = self._js()
        render = re.search(r"function renderCalendar\(cal\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(render)
        self.assertIn("revealCalendarData(cal)", render.group(1))

        apply = re.search(r"function applySetting\(setting, value\)\s*\{(.*?)\n  \}",
                          js, re.S)
        self.assertIsNotNone(apply)
        self.assertIn('setting.key === "fs"', apply.group(1))
        self.assertIn("revealCalendarData(state.calendar)", apply.group(1))

        # And the load path has to keep what applySetting reads.
        self.assertIn("state.calendar = cal;", js)

    def test_a_width_change_re_anchors_but_a_scroll_does_not(self) -> None:
        """A narrower window fits fewer columns, so which columns are worth
        showing changes with it, and a text-size change resizes the panel too --
        both must re-run the placement. A reader's own horizontal scroll changes
        no width, so it must not be undone underneath them: nothing may listen
        for scroll."""
        js = self._js()
        m = re.search(r"state\._calObserver = new ResizeObserver\(\(\) => "
                      r"placeIfChanged\(\)\);", js, re.S)
        self.assertIsNotNone(m, "the calendar needs a width-driven ResizeObserver")
        self.assertIn("state._calObserver.observe(calWrap);", js)
        # The grid too: the squares are sized in rem while the scroller can keep
        # the same box, so watching only the wrapper misses a font-size change.
        self.assertIn("if (calGrid) state._calObserver.observe(calGrid);", js)
        self.assertIn('window.addEventListener("resize", () => placeIfChanged()',
                      js)
        # Nothing in the calendar watches the scroll position, so a reader who
        # scrolls the year themselves is left where they put it.
        for fn in ("calendarLayout", "placeIfChanged",
                   "placeCalendarReveal", "revealCalendarData"):
            self.assertNotIn("addEventListener", self._fn(fn),
                             f"{fn} must not listen for events")

    def test_an_empty_placement_clears_the_stale_handle(self) -> None:
        """The verify script reads window.__calReveal. If an early return left the
        previous run's handle in place, the script would read a window that
        describes a different width and pass on numbers nothing produced."""
        js = self._js()
        body = self._fn("placeCalendarReveal")
        head = body.split("const days", 1)[0]
        self.assertIn("window.__calReveal = null;", head,
                      "the handle must be cleared before any early return")

    def test_the_weekday_gutter_stays_put_while_the_year_scrolls(self) -> None:
        """Once the panel scrolls sideways the labels are only useful next to the
        squares they name, so they are pinned to the left edge."""
        css = self._css()
        m = re.search(r"\n  \.cal-weekdays\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m)
        bodies = "\n".join(x.group(1) for x in
                           re.finditer(r"\n  \.cal-weekdays\s*\{(.*?)\n  \}", css, re.S))
        self.assertIn("position: sticky", bodies)
        self.assertIn("inset-inline-start: 0", bodies)


if __name__ == "__main__":
    unittest.main()

"""The toolbar's full/short number switch.

Three things are easy to get wrong here, and only one of them is visible at a
glance:

* **The suffix is a word, not a proper noun.** 万/亿 and K/M/B are ordinary
  magnitude words with equivalents in the other language, so a Chinese screen
  must never print "124.9M" -- the same rule the chart axis already follows.
  A hard-coded "K/M/B" table would pass a screenshot review of the English page
  and fail the Chinese one.
* **A switch that repaints the cards only.** Tables, the chart legend, the axis
  and the status line all bake the formatted string at render time. Anything
  skipped keeps the old format and the page contradicts itself.
* **A language switch that does not repaint the cards.** The suffix lives in the
  formatted string, so switching zh -> en changes every card. This was a real
  bug: setLang re-rendered tables/footer/chart but not the cards, so an English
  page showed "29.7亿" on top.

The first two are checked by running the real ``fmtShort``/``fmt`` through Node;
the rest read the wiring in app.js/index.html and the shared CSS rule.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "agent_token_radar" / "static" / "app.js"
APP_CSS = ROOT / "agent_token_radar" / "static" / "app.css"
INDEX_HTML = ROOT / "agent_token_radar" / "static" / "index.html"
NODE = shutil.which("node")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


class ToolbarWiringTests(unittest.TestCase):
    def test_both_modes_are_offered_as_segments(self) -> None:
        html = _html()
        self.assertIn('id="numSwitch"', html)
        self.assertIn('data-numfmt="full"', html)
        self.assertIn('data-numfmt="short"', html)

    def test_the_group_has_an_accessible_name(self) -> None:
        html = _html()
        m = re.search(r'<div[^>]*id="numSwitch"[^>]*>', html)
        self.assertIsNotNone(m)
        tag = m.group(0)
        self.assertIn('role="group"', tag)
        self.assertIn("data-i18n-aria=\"toolbar.num\"", tag)

    def test_every_segment_is_translated(self) -> None:
        html = _html()
        for mode in ("full", "short"):
            m = re.search(rf'data-numfmt="{mode}"[^>]*>', html)
            self.assertIsNotNone(m, mode)
            self.assertIn(f'data-i18n="num.{mode}"', m.group(0))

    def test_the_control_is_wired(self) -> None:
        src = _js()
        self.assertIn('$("numSwitch")?.addEventListener("click"', src)
        self.assertIn("setNumFmt(btn.dataset.numfmt)", src)
        self.assertIn("syncNumButtons();", src)

    def test_the_choice_persists(self) -> None:
        src = _js()
        self.assertIn("localStorage.getItem(\"atr.numfmt\")", src)
        self.assertIn('localStorage.setItem("atr.numfmt"', src)

    def test_the_switch_repaints_every_number_surface(self) -> None:
        """Cards alone is not enough: tables, chart and status carry numbers."""
        src = _js()
        m = re.search(r"function setNumFmt\(mode\)\s*\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m, "setNumFmt not found")
        body = m.group(1)
        for call in ("renderCards(", "renderTables(", "renderChart(",
                     '"status.loaded"'):
            self.assertIn(call, body,
                          f"setNumFmt must repaint via {call}")

    def test_a_language_switch_repaints_the_cards(self) -> None:
        """The regression: the suffix changes language with the UI.

        In short mode every card string carries 万/亿 or K/M/B, so a language
        switch that skips renderCards leaves the old suffix on screen.
        """
        src = _js()
        m = re.search(r"async function setLang\(lang\)\s*\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m, "setLang not found")
        body = m.group(1)
        self.assertIn("renderCards(", body,
                      "setLang must re-render the cards: the magnitude suffix "
                      "is language dependent, so skipping them shows a Chinese "
                      "unit on an English page")


class SharedSegmentStyleTests(unittest.TestCase):
    def test_the_number_switch_joins_the_segmented_track(self) -> None:
        """A separate look would read as an unrelated control."""
        css = _css()
        self.assertIsNotNone(
            re.search(r"\.ranges,\s*\.lang-switch,\s*\.num-switch\s*\{", css),
            "num-switch must share the sunken track with the other segments")

    def test_it_meets_the_touch_target(self) -> None:
        """The coarse-pointer block lists controls by name; a new one that is
        not added there stays at its 34px desktop height on a phone.

        Every `@media (pointer: coarse)` block is collected, not just the first:
        a second block further down the file is equally in effect, and matching
        only the first one made this assertion fail for a real but harmless
        reason the moment another component added its own touch tweaks.
        """
        css = _css()
        blocks = re.findall(r"@media \(pointer: coarse\)\s*\{(.*?)\n  \}", css, re.S)
        self.assertTrue(blocks, "no coarse-pointer block found")
        combined = "\n".join(blocks)
        self.assertIn(".num-switch button", combined,
                      "the number switch must be in a coarse-pointer block "
                      "or it stays 34px on touch")


@unittest.skipIf(NODE is None, "node not available")
class FormattingTests(unittest.TestCase):
    """Run the real fmtShort/fmt and inspect the text that would be displayed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="atr_num_")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, values: list[int]) -> dict:
        # NUM_UNITS is lifted from the real source rather than retyped here: a
        # duplicated unit table would keep passing while the shipped one drifted.
        script = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let i = src.indexOf("{", start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
function extractUnits() {
  const start = src.indexOf("const NUM_UNITS =");
  if (start < 0) throw new Error("NUM_UNITS not found");
  let i = src.indexOf("{", start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error("unbalanced NUM_UNITS");
}
const cfg = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const units = extractUnits();
const shortSrc = extract("fmtShort");
const out = {};
for (const lang of ["zh", "en"]) {
  for (const mode of ["full", "short"]) {
    const state = { lang, numfmt: mode };
    const fn = new Function("state", "cfg", `
      ${units}
      ${shortSrc}
      const fmt = (n) => (state.numfmt === "short"
        ? fmtShort(n)
        : new Intl.NumberFormat(state.lang === "zh" ? "zh-CN" : "en-US").format(Math.trunc(Number(n) || 0)));
      return cfg.values.map((v) => fmt(v));
    `);
    out[`${lang}.${mode}`] = fn(state, cfg);
  }
}
console.log(JSON.stringify(out));
"""
        tmp = Path(self.tmp.name)
        js = tmp / "probe.js"
        cfg_path = tmp / "cfg.json"
        js.write_text(script, encoding="utf-8")
        cfg_path.write_text(json.dumps({"values": values}), encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(js), str(APP_JS), str(cfg_path)],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        if proc.returncode != 0:
            self.fail(f"node failed: {proc.stderr}")
        return json.loads(proc.stdout)

    def test_full_mode_is_the_exact_grouped_number(self) -> None:
        out = self._run([124_855_543])
        self.assertEqual(out["zh.full"], ["124,855,543"])
        self.assertEqual(out["en.full"], ["124,855,543"])

    def test_short_mode_uses_chinese_units_on_a_chinese_screen(self) -> None:
        out = self._run([124_855_543, 8_856_542, 296_660_1662])
        for text in out["zh.short"]:
            self.assertTrue("亿" in text or "万" in text, text)
            self.assertNotIn("K", text)
            self.assertNotIn("M", text)
            self.assertNotIn("B", text)

    def test_short_mode_uses_latin_units_on_an_english_screen(self) -> None:
        out = self._run([124_855_543, 8_856_542, 296_660_1662])
        for text in out["en.short"]:
            self.assertTrue(any(u in text for u in "KMB"), text)
            self.assertNotIn("亿", text)
            self.assertNotIn("万", text)

    def test_the_two_modes_actually_differ(self) -> None:
        out = self._run([124_855_543])
        self.assertNotEqual(out["zh.full"][0], out["zh.short"][0])

    def test_small_numbers_stay_readable_in_short_mode(self) -> None:
        """Below the smallest step an abbreviation has nothing to remove, so it
        must fall back to the grouped figure rather than a bare suffix."""
        out = self._run([42, 999])
        self.assertEqual(out["zh.short"], ["42", "999"])
        self.assertEqual(out["en.short"], ["42", "999"])

    def test_a_trailing_point_zero_is_dropped(self) -> None:
        """"1.0亿" reads like a rounding bug; "1亿" does not."""
        out = self._run([100_000_000])
        self.assertEqual(out["zh.short"], ["1亿"])


if __name__ == "__main__":
    unittest.main()

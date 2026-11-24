"""The summary cards must never crop the number they exist to show.

Two layouts were tried and both failed, in opposite ways, and neither failure
was visible in the source:

* **1fr grid + a vw-based font size.** The type grew with the VIEWPORT while
  the card stayed ~200px wide, so a 13-digit figure was clipped at 1280/1400/
  1600px (measured: 162px of digits in a 159px card).
* **Content-sized flex cards.** The number stopped being cropped, but the row
  went ragged (widths 216/230/248/261/214/228/288) and `flex-grow` stretched
  the last card of a short row to the full line (one card at 1422px of 1422px).

What ships: equal-width grid tracks (tidy) + a container-query type size + a
measured fit that shrinks only the figures that still overflow. These tests pin
the three parts that can silently regress: the grid, the fit call on every
render path, and the fit itself (run for real through Node).
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
NODE = shutil.which("node")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


class CardLayoutCssTests(unittest.TestCase):
    def test_the_cards_use_equal_width_grid_tracks(self) -> None:
        """A content-sized track makes the row ragged and stretches the tail."""
        css = _css()
        m = re.search(r"\.cards\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m, ".cards rule not found")
        body = m.group(1)
        self.assertIn("display: grid", body)
        self.assertIn("minmax(", body,
                      "auto-fit/minmax keeps every track the same width")
        self.assertNotIn("flex-wrap: wrap", body,
                         "content-sized flex cards were measured as ragged")

    def test_the_card_is_a_container(self) -> None:
        """The number's size must resolve from the CARD, not the viewport."""
        css = _css()
        m = re.search(r"\n  \.card\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m, ".card rule not found")
        self.assertIn("container-type: inline-size", m.group(1))

    def test_the_number_size_uses_card_units(self) -> None:
        css = _css()
        m = re.search(r"\.card \.value\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m, ".card .value rule not found")
        body = m.group(1)
        self.assertIn("cqi", body, "the size must come from the card width")
        self.assertNotIn("vw", body,
                         "a viewport-relative size grows when there is no room")

    def test_the_number_keeps_its_headline_size(self) -> None:
        """A pure-cqi size was measured at 13.6px -- too small to read as the
        card's headline. The preferred value must reach the ceiling."""
        css = _css()
        m = re.search(r"\.card \.value\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        cm = re.search(r"clamp\(([\d.]+)rem,\s*([\d.]+)cqi,\s*([\d.]+)rem\)", body)
        self.assertIsNotNone(cm, "expected a rem/cqi/rem clamp")
        floor, preferred, ceiling = (float(x) for x in cm.groups())
        # At a ~180px card, the preferred value must already exceed or meet the
        # ceiling, so normal data renders at full design size.
        self.assertGreaterEqual(preferred * 1.8, ceiling,
                                f"{preferred}cqi of a 180px card is "
                                f"{preferred * 1.8:.2f}rem, below the "
                                f"{ceiling}rem ceiling: the headline would shrink")
        self.assertLess(floor, ceiling)

    def test_the_floor_is_not_the_old_design_size(self) -> None:
        """A floor at the design size would leave nothing for the fit to do."""
        css = _css()
        m = re.search(r"\.card \.value\s*\{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(m)
        cm = re.search(r"clamp\(([\d.]+)rem", m.group(1))
        self.assertIsNotNone(cm)
        self.assertLess(float(cm.group(1)), 1.2)


class CardFitWiringTests(unittest.TestCase):
    def test_the_fit_is_called_after_rendering_the_cards(self) -> None:
        src = _js()
        m = re.search(r"function renderCards\(totals\)\s*\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m, "renderCards not found")
        self.assertIn("fitCardNumbers();", m.group(1),
                      "a render that skips the fit shows the CSS size, which "
                      "can still overflow")

    def test_the_fit_runs_again_on_resize(self) -> None:
        """A resize changes how many cards share a row, so the space for each
        number changes with it."""
        src = _js()
        self.assertIn("fitCardNumbers", src)
        self.assertIn("_cardObserver", src)
        self.assertIn('window.addEventListener("resize", refit', src)

    def test_the_resize_handler_is_throttled(self) -> None:
        src = _js()
        self.assertIn("requestAnimationFrame", src)

    def test_a_short_value_does_not_inherit_the_smaller_type(self) -> None:
        """The fit RESETS the size before measuring; without that, a number
        rendered after a long one keeps the shrunken size forever."""
        src = _js()
        m = re.search(r"function fitCardNumbers\(\)\s*\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        reset = body.find('el.style.fontSize = ""')
        measure = body.find("el.clientWidth")
        self.assertGreater(reset, -1, "the size must be cleared before measuring")
        self.assertGreater(measure, reset,
                           "the reset must happen BEFORE the measurement")

    def test_it_has_a_legibility_floor(self) -> None:
        src = _js()
        self.assertIn("MIN_CARD_FONT_PX", src)
        m = re.search(r"MIN_CARD_FONT_PX\s*=\s*(\d+)", src)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 8,
                                "an unbounded shrink makes the number vanish")
        self.assertLessEqual(int(m.group(1)), 12,
                             "a high floor would let the number overflow again")


@unittest.skipIf(NODE is None, "node not available")
class CardFitBehaviourTests(unittest.TestCase):
    """Run the real fitCardNumbers against stub elements."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="atr_cards_")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, cases: list[dict]) -> list[float]:
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
const cfg = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
// Each stub reports the geometry the fit needs: clientWidth (the space) and
// scrollWidth (the demand). getComputedStyle returns the CSS size.
const els = cfg.cases.map((c) => ({
  clientWidth: c.have, scrollWidth: c.need, style: { fontSize: "" }, _css: c.cssPx,
}));
global.document = { querySelectorAll: () => els };
global.getComputedStyle = (el) => ({ fontSize: el._css + "px" });
global.window = {};
const minMatch = src.match(/MIN_CARD_FONT_PX\s*=\s*(\d+)/);
const fn = new Function(`
  const MIN_CARD_FONT_PX = ${minMatch ? minMatch[1] : 9};
  ${extract("fitCardNumbers")}
  return fitCardNumbers;
`)();
fn();
console.log(JSON.stringify(els.map((e) => e.style.fontSize)));
"""
        tmp = Path(self.tmp.name)
        js = tmp / "probe.js"
        cfg_path = tmp / "cfg.json"
        js.write_text(script, encoding="utf-8")
        cfg_path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(js), str(APP_JS), str(cfg_path)],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        if proc.returncode != 0:
            self.fail(f"node failed: {proc.stderr}")
        return json.loads(proc.stdout)

    def test_a_fitting_number_is_left_alone(self) -> None:
        out = self._run([{"have": 200, "need": 180, "cssPx": 24}])
        self.assertEqual(out, [""], "nothing to do, so no inline size is set")

    def test_an_overflowing_number_is_shrunk(self) -> None:
        out = self._run([{"have": 124, "need": 200, "cssPx": 24}])
        self.assertEqual(len(out), 1)
        self.assertNotEqual(out[0], "")
        px = float(out[0].replace("px", ""))
        self.assertLess(px, 24, "an overflowing number must get smaller")

    def test_the_shrink_lands_close_to_the_available_width(self) -> None:
        """Under-shrinking leaves it still cropped; over-shrinking makes it
        needlessly tiny. The target is the available width with a hair of
        slack for rounding."""
        have, need, css = 124, 200, 24
        out = self._run([{"have": have, "need": need, "cssPx": css}])
        px = float(out[0].replace("px", ""))
        expected = css * (have / need)
        self.assertAlmostEqual(px, expected, delta=expected * 0.06)

    def test_the_floor_is_enforced(self) -> None:
        """An extreme value must not shrink the type out of existence."""
        out = self._run([{"have": 40, "need": 4000, "cssPx": 24}])
        px = float(out[0].replace("px", ""))
        self.assertGreaterEqual(px, 9)

    def test_a_hidden_card_is_skipped(self) -> None:
        """clientWidth 0 means it is not laid out; measuring it is meaningless
        and would divide by zero."""
        out = self._run([{"have": 0, "need": 100, "cssPx": 24}])
        self.assertEqual(out, [""])


if __name__ == "__main__":
    unittest.main()

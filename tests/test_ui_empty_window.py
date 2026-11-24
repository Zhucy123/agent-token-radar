"""An empty window must leave nothing of the previous one on screen.

The bug this pins: picking a day with no usage kept the PREVIOUS selection's
numbers on screen -- the legend still listed Agents with their totals, the y-axis
still had tick labels, and the canvas was still painted with the old bars. The
data was correct (the API returned ``days: []``); the page simply never cleared
the parts that only the chart renderer used to rebuild.

Two distinct causes, both pinned here:

* ``.chart-panel canvas { display: block }`` beat the UA sheet's
  ``[hidden] { display: none }``, so ``canvas.hidden = true`` hid nothing.
  Measured in a real browser: ``hidden === true`` while ``display === "block"``
  and the element was still visible.
* The empty branch returned early without touching the legend, the y-axis strip
  or the one-day share list.

The last check is the one that would have caught it end to end: after an empty
window, the legend and axis must be EMPTY, not merely present.
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


def _hidden_rule() -> re.Match:
    """The bare `[hidden]` declaration block, in full.

    The `[^}]*` here stops at the FIRST closing brace, so it used to return
    `'  [hidden] { display: none'` -- truncated before the ` !important; }`.
    A test then asserted `"!important" not in` that slice and passed forever,
    while the real rule DID carry `!important`. Match the whole block instead:
    that is the difference between checking a value and checking a guess.
    """
    css = _css()
    m = re.search(r"^[ \t]*\[hidden\]\s*\{([^}]*)\}", css, re.M)
    if m is None:
        raise AssertionError("no bare [hidden] rule in the stylesheet")
    return m


class HiddenAttributeTests(unittest.TestCase):
    """`hidden` must actually hide, whatever a component sets display to."""

    def test_a_bare_hidden_rule_exists(self) -> None:
        """A global rule, not one scoped to a component (`.update-panel[hidden]`).

        Those per-component rules already existed and did nothing for the chart,
        because the chart's own `display: block` came later in the cascade.
        """
        m = _hidden_rule()
        self.assertIn("display: none", m.group(1))

    def test_the_rule_lives_in_the_last_layer(self) -> None:
        """`@layer` order beats any component rule that is also in a layer."""
        css = _css()
        order = re.search(r"@layer\s+([^;{]+);", css)
        self.assertIsNotNone(order, "no @layer order statement")
        layers = [x.strip() for x in order.group(1).split(",")]
        self.assertEqual(layers[-1], "utilities",
                         "utilities must be the last layer declared")
        # And the rule must actually be inside it, not in components.
        idx_util = css.index("@layer utilities")
        self.assertGreater(_hidden_rule().start(), idx_util,
                           "[hidden] rule must sit in the utilities layer")

    def test_the_rule_is_also_forced(self) -> None:
        """Layer order alone is not enough: it only beats LAYERED rules.

        Measured in a real browser: with the layer but no `!important`, an
        unlayered `.chart-panel canvas { display: block }` (i.e. any component
        rule a future edit adds outside a layer) wins and the stale chart comes
        back. `!important` is the guard against that, so both are deliberate.
        """
        m = _hidden_rule()
        self.assertIn("!important", m.group(1),
                      "[hidden] must be !important: layer order does nothing "
                      "against an unlayered display rule on the same element")

    def test_component_still_sets_display_on_canvas(self) -> None:
        """The triggering rule is legitimate; it just must not beat `hidden`."""
        self.assertIn(".chart-panel canvas { display: block; }", _css())

    def test_empty_panel_collapses_instead_of_keeping_chart_height(self) -> None:
        """Otherwise a one-word note floats in a blank 21rem box, off to the right.

        The panel is a flex row, so the note was laid out beside the (empty)
        axis strip and scroller rather than at the left edge, and the panel kept
        its fixed chart height.
        """
        css = _css()
        self.assertIn(':has(#chartFallback:not([hidden]))', css,
                      "the empty state needs its own panel rule")
        m = re.search(
            r"\.chart-panel:has\(#chartFallback:not\(\[hidden\]\)\)\s*\{(.*?)\}",
            css, re.S)
        self.assertIsNotNone(m, "empty-state panel rule not found")
        body = m.group(1)
        self.assertIn("display: block", body,
                      "must stop being a flex row, or the note sits to the right")
        self.assertIn("block-size: auto", body,
                      "must not keep the chart's fixed height")


class EmptyChartClearingTests(unittest.TestCase):
    """The empty branch has to clear every part the renderer used to fill."""

    def test_empty_branch_does_not_return_before_clearing(self) -> None:
        src = _js()
        self.assertIn("function renderChartEmpty(", src,
                      "renderChartEmpty helper is missing")
        # renderChart must delegate instead of hiding the canvas and bailing.
        m = re.search(r"function renderChart\(trend\)\s*\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m, "renderChart not found")
        body = m.group(1)
        self.assertIn("renderChartEmpty(fallback)", body,
                      "renderChart must call renderChartEmpty for an empty window")
        self.assertNotIn('fallback.textContent = t("trend.noData")', body,
                         "renderChart must not inline the no-data branch again")

    def test_helper_clears_legend_axis_and_shares(self) -> None:
        src = _js()
        m = re.search(r"function renderChartEmpty\(fallback\)\s*\{(.*?)\n  \}",
                      src, re.S)
        self.assertIsNotNone(m, "renderChartEmpty not found")
        body = m.group(1)
        for needle, why in (
            ("legend.replaceChildren()", "stale legend kept Agent totals"),
            ("yaxis.replaceChildren()", "stale y-axis kept tick labels"),
            ("shares.replaceChildren()", "stale share rows kept old numbers"),
            ("canvas.onclick = null", "stale canvas still answered clicks"),
            ("_chartObserver", "ResizeObserver would repaint the old bars"),
            ('tip.style.left = ""', "stale tooltip coordinates left behind"),
        ):
            self.assertIn(needle, body, why)

    def test_single_bucket_empty_path_uses_the_same_helper(self) -> None:
        src = _js()
        m = re.search(r"function renderSingleBucket\(trend\)\s*\{(.*?)\n  \}",
                      src, re.S)
        self.assertIsNotNone(m, "renderSingleBucket not found")
        self.assertIn("renderChartEmpty(fallback)", m.group(1),
                      "a one-day window with no usage must clear the same way")


class EmptyTableWordingTests(unittest.TestCase):
    """An already-scanned install must not be told to scan again."""

    def test_wording_depends_on_whether_anything_was_ever_scanned(self) -> None:
        src = _js()
        m = re.search(r"function emptyTableText\(\)\s*\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m, "emptyTableText not found")
        body = m.group(1)
        self.assertIn("db_rows", body,
                      "the wording has to consult db_rows, not just row count")
        self.assertIn('t("empty.window")', body)
        self.assertIn('t("empty.table")', body)

    def test_every_empty_cell_uses_it(self) -> None:
        src = _js()
        self.assertNotIn('t("empty.table")', src.replace(
            'everScanned ? t("empty.window") : t("empty.table")', ""),
            "a table still hardcodes empty.table instead of emptyTableText()")


@unittest.skipIf(NODE is None, "node not available")
class EmptyChartBehaviourTests(unittest.TestCase):
    """Run renderChartEmpty against a DOM stub and inspect what it left."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="atr_empty_")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self) -> dict:
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
function makeEl(id) {
  const el = {
    id, hidden: false, textContent: "", children: [], style: {}, attrs: {},
    onclick: () => {}, onmousemove: () => {}, onmouseleave: () => {},
    replaceChildren() { this.children = []; },
    appendChild(c) { this.children.push(c); },
    removeAttribute(k) { delete this.attrs[k]; },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
  };
  return el;
}
const els = {
  trendChart: makeEl("trendChart"),
  chartFallback: makeEl("chartFallback"),
  agentShares: makeEl("agentShares"),
  chartLegend: makeEl("chartLegend"),
  chartTip: makeEl("chartTip"),
  chartYAxis: makeEl("chartYAxis"),
  "trend-title": makeEl("trend-title"),
};
// Pre-populate the way a real previous render would have.
els.chartLegend.children = [{}, {}, {}, {}];
els.chartYAxis.children = [{}, {}, {}, {}, {}];
els.agentShares.children = [{}, {}];
els.agentShares.hidden = false;
els.trendChart.hidden = false;
els.chartFallback.hidden = true;
// A tooltip left where the last hover put it.
els.chartTip.hidden = false;
els.chartTip.style.left = "759px";
els.chartTip.style.top = "52.8px";
const state = { _chartObserver: { disconnected: false, disconnect() { this.disconnected = true; } } };
const fn = new Function("$", "t", "state", "renderChartEmptyCall", `
  ${extract("renderChartEmpty")}
  return renderChartEmpty;
`);
const renderChartEmpty = fn(
  (id) => els[id] || null,
  (k) => k,
  state,
  null);
renderChartEmpty(els.chartFallback);
console.log(JSON.stringify({
  canvas_hidden: els.trendChart.hidden,
  canvas_onclick: els.trendChart.onclick,
  legend_children: els.chartLegend.children.length,
  yaxis_children: els.chartYAxis.children.length,
  shares_children: els.agentShares.children.length,
  shares_hidden: els.agentShares.hidden,
  fallback_hidden: els.chartFallback.hidden,
  fallback_text: els.chartFallback.textContent,
  tip_hidden: els.chartTip.hidden,
  tip_left: els.chartTip.style.left,
  tip_top: els.chartTip.style.top,
  title: els["trend-title"].textContent,
  observer_disconnected: state._chartObserver === null,
}));
"""
        tmp = Path(self.tmp.name)
        js = tmp / "probe.js"
        js.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(js), str(APP_JS)],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        if proc.returncode != 0:
            self.fail(f"node failed: {proc.stderr[:800]}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_leftovers_are_cleared(self) -> None:
        got = self._run()
        self.assertTrue(got["canvas_hidden"], "canvas must be hidden")
        self.assertIsNone(got["canvas_onclick"], "canvas click handler must go")
        self.assertEqual(got["legend_children"], 0, "legend must be emptied")
        self.assertEqual(got["yaxis_children"], 0, "y-axis must be emptied")
        self.assertEqual(got["shares_children"], 0, "share rows must be emptied")
        self.assertTrue(got["shares_hidden"], "share list must be hidden")
        self.assertFalse(got["fallback_hidden"], "the no-data note must show")
        self.assertEqual(got["fallback_text"], "trend.noData")
        self.assertTrue(got["tip_hidden"], "a lingering tooltip must be hidden")
        self.assertEqual(got["tip_left"], "",
                         "stale tooltip coordinates must be cleared")
        self.assertEqual(got["tip_top"], "",
                         "stale tooltip coordinates must be cleared")
        self.assertEqual(got["title"], "sec.trend",
                         "the section heading must return to the series title")
        self.assertTrue(got["observer_disconnected"],
                        "the old ResizeObserver must be dropped")


if __name__ == "__main__":
    unittest.main()

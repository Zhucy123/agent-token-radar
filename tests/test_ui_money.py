"""The dashboard's Agent table must format money like every other table.

``moneyCell`` exists so a USD amount renders as "$8.17" and an unpriced model
renders a dash. The Agent table passed the RAW ``cost_usd`` into ``fillTable``,
which only reaches ``moneyCell`` when the value is null -- so a priced cell was
copied through as text and printed ``8.171517142`` while the Model and project
tables printed ``$8.17`` for the same semantic column. Floating-point noise in
a money column ("0.17028839999999998") undermines the credibility of every
number on the page.

These tests read ``app.js`` as text and check the wiring, then run the real
``money``/``moneyCell``/``fillTable`` functions through Node against a tiny DOM
stub. That is the closest check available without a browser: the previous bug
was pure wiring, so asserting on the function bodies alone would not have
caught it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "agent_token_radar" / "static" / "app.js"
NODE = shutil.which("node")


def _src() -> str:
    return APP_JS.read_text(encoding="utf-8")


class AgentTableMoneyWiringTests(unittest.TestCase):
    """Static checks on how renderTables calls fillTable."""

    def test_agent_table_passes_a_money_column(self) -> None:
        src = _src()
        m = re.search(
            r'fillTable\("agentTable",.*?\),\s*([\d-]+),\s*(\w+),\s*([-\d]+),\s*(\d+)\);',
            src, re.S)
        self.assertIsNotNone(m, "agentTable call not found or shape changed")
        numeric_from, first_is_agent, agent_col, money_col = m.groups()
        self.assertEqual(first_is_agent, "true")
        self.assertEqual(agent_col, "-1", "the agent column is the row header here")
        self.assertNotEqual(money_col, "-1",
                            "the Agent table has a cost column and must format it")

    def test_the_money_column_index_matches_the_cost_header(self) -> None:
        """A wrong index is silent: the raw float just keeps printing."""
        src = _src()
        m = re.search(r'fillTable\("agentTable",\s*\[(.*?)\]', src, re.S)
        self.assertIsNotNone(m)
        headers = [h.strip() for h in m.group(1).split(",") if h.strip()]
        # Last header is col.cost; find its 0-based position.
        cost_positions = [i for i, h in enumerate(headers) if "col.cost" in h]
        self.assertEqual(len(cost_positions), 1,
                         f"expected exactly one cost header, got {headers}")
        expected = cost_positions[0]

        call = re.search(
            r'fillTable\("agentTable",.*?\),\s*([\d-]+),\s*(\w+),\s*([-\d]+),\s*(\d+)\);',
            src, re.S)
        money_col = int(call.group(4))
        self.assertEqual(money_col, expected,
                         f"moneyCol={money_col} but the cost header is at index "
                         f"{expected}: the cost column would print a raw float")

    def test_fill_table_has_a_money_branch(self) -> None:
        src = _src()
        self.assertIn("moneyCol", src)
        self.assertIn("i === moneyCol", src,
                      "fillTable must render the money column through moneyCell")


@unittest.skipIf(NODE is None, "node not available")
class MoneyRenderTests(unittest.TestCase):
    """Run the real functions and inspect the text that would be displayed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="atr_ui_")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, payload: dict) -> dict:
        # Written to a temp file rather than passed via `node -e`: with -e,
        # process.argv has no script slot, so argv[2] is the first extra arg
        # and the indices below would shift by one.
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
function makeEl(tag) {
  const el = {
    tagName: tag, children: [], className: "", textContent: "", dataset: {},
    title: "", colSpan: 0, scope: "",
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren() { this.children = []; },
    querySelector() { return { children: [], appendChild() {}, replaceChildren() {} }; },
  };
  el.classList = { add(c) { el.className = (el.className + " " + c).trim(); } };
  return el;
}
const fn = new Function("t", "agentCell", "$", "document", `
  ${extract("money")}
  ${extract("moneyCell")}
  ${extract("fillTable")}
  return { money, moneyCell, fillTable };
`);
const tbody = makeEl("tbody");
const tableEl = { querySelector: () => tbody };
const api = fn((k) => k, (v) => ({ value: v }), () => tableEl,
               { createElement: (tag) => makeEl(tag) });
const cfg = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
api.fillTable("agentTable", cfg.headers, cfg.rows, 1, true, -1, cfg.moneyCol);
const out = tbody.children.map((tr) => {
  const last = tr.children[tr.children.length - 1];
  const inner = last.children.length ? last.children[0] : last;
  return { text: inner.textContent, cls: inner.className || "" };
});
console.log(JSON.stringify(out));
"""
        tmp = Path(self.tmp.name)
        js = tmp / "probe.js"
        cfg_path = tmp / "cfg.json"
        js.write_text(script, encoding="utf-8")
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(js), str(APP_JS), str(cfg_path)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), f"node produced no output: {proc.stderr}")
        return json.loads(proc.stdout)

    def _payload(self, costs):
        headers = ["Agent", "Events", "Total", "Hit", "Input", "CacheRead",
                   "Output", "Cost"]
        rows = [[f"a{i}", "1", "2", "3%", "4", "5", "6", c]
                for i, c in enumerate(costs)]
        return {"headers": headers, "rows": rows, "moneyCol": len(headers) - 1}

    def test_priced_costs_render_as_currency(self) -> None:
        out = self._run(self._payload([0.17028839999999998, 232.89920820000003]))
        self.assertEqual(out[0]["text"], "$0.17")
        self.assertEqual(out[1]["text"], "$232.90")
        # No long float tails may survive into the cell.
        for cell in out:
            self.assertNotIn("99999", cell["text"])
            self.assertNotRegex(cell["text"], r"\.\d{3,}")

    def test_unpriced_cost_keeps_the_dash(self) -> None:
        out = self._run(self._payload([None]))
        self.assertEqual(out[0]["text"], "\u2014",
                         "an unpriced model must not print $0.00")
        self.assertIn("cost-unpriced", out[0]["cls"])


if __name__ == "__main__":
    unittest.main()

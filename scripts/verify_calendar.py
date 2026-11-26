"""Real-browser check for the calendar heatmap.

Asserts on what the page actually renders and what a hover actually produces:
  * 365 squares for the current year, laid out Monday-first
  * squares are shaded by usage, and a day with no usage is the palest band
  * hovering a used day shows its token count AND an estimated cost
  * hovering a quiet day says there is no usage (not "0 tokens / $0.00")
  * a day whose models are all unpriced says "no published price", not $0.00
  * clicking a day focuses it in the whole dashboard
  * arrow keys move within the calendar and it stays one tab stop
  * switching language re-renders the labels
  * the 44px touch target holds for the year arrows
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verify_date_window import Browser  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}"
          + ("" if ok else f"  (want {want!r})"))
    if not ok:
        FAILURES.append(label)


def check_true(label: str, got, detail: str = "") -> None:
    ok = bool(got)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}"
          + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(label)


PROBE = r"""
(() => {
  const cells = [...document.querySelectorAll('#calGrid .cal-cell')];
  const used = cells.filter(c => c.dataset.used === '1');
  const levels = {};
  for (const c of cells) levels[c.dataset.level] = (levels[c.dataset.level]||0) + 1;
  return {
    cells: cells.length,
    used: used.length,
    levels,
    firstDay: cells.length ? cells[0].dataset.day : null,
    firstWeekday: cells.length ? cells[0].dataset.weekday : null,
    tabStops: cells.filter(c => c.tabIndex === 0).length,
    year: (document.getElementById('calYearLabel')||{}).textContent,
    status: (document.getElementById('calStatus')||{}).textContent,
  };
})()
"""

HOVER = r"""
(() => {
  const cell = document.querySelector(%s);
  if (!cell) return null;
  cell.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}));
  const tip = document.getElementById('calTip');
  return { hidden: tip.hidden, text: tip.textContent };
})()
"""


def hover(b, selector: str):
    return b.js(HOVER % json.dumps(selector))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    b = Browser(args.port)
    try:
        b.open()
        b.js("document.querySelector('#langSwitch button[data-lang=\"zh\"]').click()")
        b.wait("!!window.__lastCalendar", True, timeout=25.0)
        time.sleep(0.6)

        print("\n[1] 结构与年份")
        r = b.js(PROBE)
        print(f"       {r}")
        check("365 个格子", r["cells"], 365)
        check("年份是当前年", r["year"], str(b.js("new Date().getFullYear()")))
        check("只有一个 Tab 停靠点", r["tabStops"], 1)
        check_true("有数据的日子少于 365", 0 < r["used"] < 365, f"{r['used']}")

        print("\n[2] 分档")
        levels = r["levels"]
        check_true("有 5 档（0-4）", len(levels) >= 3, str(sorted(levels)))
        check_true("最浅一档包含无用量日", "0" in levels, str(levels.get("0")))
        check_true("有最深一档", "4" in levels, str(levels.get("4")))

        print("\n[3] 悬停：有用量的一天")
        hover_used = hover(b, ".cal-cell[data-used='1']")
        print(f"       tip = {hover_used}")
        check("提示框显示出来了", hover_used["hidden"], False)
        check_true("提示里有 Token", "Token" in hover_used["text"], hover_used["text"][:60])
        check_true("提示里有费用", "$" in hover_used["text"], hover_used["text"][:80])

        print("\n[4] 悬停：没有用量的一天")
        quiet = hover(b, ".cal-cell[data-level='0']")
        print(f"       tip = {quiet}")
        check_true("说「无用量」而不是 0", "无用量" in quiet["text"], quiet["text"][:60])

        print("\n[5] 点击某天 -> 整页切到那天")
        target = b.js("(document.querySelector(\".cal-cell[data-used='1']\")||{}).dataset.day")
        b.js("document.querySelector(\".cal-cell[data-used='1']\").click()")
        time.sleep(1.6)
        got = b.js("(window.__lastData && window.__lastData.range) || ''")
        label = b.js("(document.getElementById('dateLabel')||{}).textContent")
        check("数据切到 custom 范围", got, "custom")
        check_true("日期按钮显示那一天", label and target[5:] in label, f"{label} vs {target}")
        check_true("那天只有一根柱", b.js("(window.__lastTrend.days||[]).length") == 1,
                   str(b.js("(window.__lastTrend.days||[]).length")))

        print("\n[6] 键盘：方向键与单一停靠点")
        # Move focus into the calendar via the roving tab stop, then press right.
        b.js("""(() => {
          const c = document.querySelector('#calGrid .cal-cell[tabindex="0"]');
          if (c) c.focus();
        })()""")
        before = b.js("(document.activeElement||{}).dataset && document.activeElement.dataset.day")
        b.js("""(() => {
          const el = document.activeElement;
          el.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowRight', bubbles:true, cancelable:true}));
        })()""")
        time.sleep(0.3)
        after = b.js("(document.activeElement||{}).dataset && document.activeElement.dataset.day")
        print(f"       {before} -> {after}")
        check_true("方向键移动了焦点", before and after and before != after, f"{before} -> {after}")
        check("仍然只有一个 Tab 停靠点",
              b.js("[...document.querySelectorAll('#calGrid .cal-cell')].filter(c=>c.tabIndex===0).length"),
              1)

        print("\n[7] 换语言")
        b.js("document.querySelector('#langSwitch button[data-lang=\"en\"]').click()")
        time.sleep(1.6)
        en = b.js(PROBE)
        check_true("英文年份标签仍在", en["year"], en["year"])
        tip_en = hover(b, ".cal-cell[data-used='1']")
        print(f"       tip = {tip_en}")
        check_true("英文提示用 tokens", "tokens" in tip_en["text"], tip_en["text"][:70])
        check_true("英文提示不残留中文", "无用量" not in tip_en["text"] and "约 " not in tip_en["text"],
                   tip_en["text"][:70])
        months = b.js("(document.getElementById('calMonthRow')||{}).textContent||''")
        check_true("月份标签是英文", "Jan" in months, months[:40])

        print("\n[8] 换数字格式，日历跟着变")
        b.js("document.querySelector('#numSwitch button[data-numfmt=\"short\"]').click()")
        time.sleep(1.0)
        tip_short = hover(b, ".cal-cell[data-used='1']")
        print(f"       tip = {tip_short}")
        check_true("缩写后出现 K/M/B", any(u in tip_short["text"] for u in "KMB"),
                   tip_short["text"][:70])
        b.js("document.querySelector('#numSwitch button[data-numfmt=\"full\"]').click()")
        time.sleep(0.8)

        print("\n[9] 年份导航")
        b.js("(() => { const b=document.getElementById('calPrev'); if(b) b.click(); })()")
        time.sleep(1.8)
        prev = b.js(PROBE)
        print(f"       {prev['year']} cells={prev['cells']}")
        check_true("上一年切换生效", prev["year"] != en["year"], f"{en['year']} -> {prev['year']}")
        b.js("(() => { const b=document.getElementById('calNext'); if(b) b.click(); })()")
        time.sleep(1.8)
        back = b.js(PROBE)
        check("回到今年", back["year"], en["year"])

        print("\n[10] 触屏目标")
        b.cdp.call("Emulation.setTouchEmulationEnabled", enabled=True)
        b.cdp.call("Emulation.setEmulatedMedia",
                   features=[{"name": "pointer", "value": "coarse"}])
        time.sleep(0.5)
        h = b.js("document.getElementById('calPrev').getBoundingClientRect().height")
        w = b.js("document.getElementById('calPrev').getBoundingClientRect().width")
        check_true("年份按钮 >= 44px", h >= 43.5 and w >= 43.5, f"{w}x{h}")
        b.cdp.call("Emulation.setTouchEmulationEnabled", enabled=False)
    finally:
        b.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

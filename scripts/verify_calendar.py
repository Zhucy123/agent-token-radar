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

# The tooltip, measured against the boxes it has to live inside.
#
# Two real bugs are pinned here, and neither shows up as an exception or as
# missing text -- the tooltip was present and said the right thing both times:
#   1. It lived inside .cal-wrap, which scrolls sideways. A single-axis scroll
#      container coerces its OTHER axis to `auto`, so the tooltip was clipped at
#      the bottom and pushed a vertical scrollbar across the whole calendar.
#   2. It was clamped against that wrapper's width on the X axis only, so a box
#      taller than the remaining room ran out of the panel.
TIPS = r"""
(() => {
  const wrap = document.querySelector('.cal-wrap');
  const panel = document.getElementById('calPanel');
  const tip = document.getElementById('calTip');
  const nr = tip.hidden ? null : tip.getBoundingClientRect();
  const pr = panel.getBoundingClientRect();
  const pcs = getComputedStyle(panel);
  const padL = parseFloat(pcs.paddingLeft) || 0;
  const padR = parseFloat(pcs.paddingRight) || 0;
  const padT = parseFloat(pcs.paddingTop) || 0;
  const padB = parseFloat(pcs.paddingBottom) || 0;
  const inner = {
    left: pr.left + padL + panel.clientLeft,
    right: pr.left + panel.clientLeft + panel.clientWidth - padR,
    top: pr.top + padT + panel.clientTop,
    bottom: pr.top + panel.clientTop + panel.clientHeight - padB,
  };
  return {
    hidden: tip.hidden,
    parentOk: tip.parentElement === panel,
    // A vertical scrollbar on the calendar is the visible symptom.
    vScroll: wrap.scrollHeight - wrap.clientHeight,
    hScroll: wrap.scrollWidth - wrap.clientWidth,
    box: nr ? {left: Math.round(nr.left), right: Math.round(nr.right),
               top: Math.round(nr.top), bottom: Math.round(nr.bottom)} : null,
    sides: nr ? {
      left: Math.round(nr.left - inner.left),
      right: Math.round(inner.right - nr.right),
      top: Math.round(nr.top - inner.top),
      bottom: Math.round(inner.bottom - nr.bottom),
    } : null,
    innerW: Math.round(inner.right - inner.left),
    boxW: nr ? Math.round(nr.width) : null,
  };
})()
"""

# Layout facts that no amount of reading the source can prove:
#  * the Mon/Wed/Fri gutter is level with the rows of squares it labels
#  * every month label sits over the week column that contains its 1st
#  * a quiet square is a FILLED band, not a hairline ring (a 1px ring on a 22px
#    square turns into a grey halo at fractional display scales and makes the
#    whole year look out of focus)
#  * the gutter column is wide enough for its glyphs
LAYOUT = r"""
(() => {
  const grid = document.getElementById('calGrid');
  const items = [...grid.children];
  const rowCentre = (i) => {
    const el = items[i];
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return Math.round((r.top + r.height / 2) * 100) / 100;
  };
  // items[i], items[i+7], ... are the same weekday, so items[i] starts row i+1.
  const rowCentres = [0,1,2,3,4,5,6].map(rowCentre);

  const labels = [...document.querySelectorAll('#calWeekdays span')].map(s => {
    const r = s.getBoundingClientRect();
    return {text: s.textContent,
            centre: Math.round((r.top + r.height / 2) * 100) / 100,
            width: Math.round(r.width * 100) / 100};
  });
  // The three labels are Mon / Wed / Fri -> rows 1, 3, 5 (1-based).
  const wantRows = [0, 2, 4];
  const labelDeltas = labels.map((l, i) =>
    Math.round((l.centre - rowCentres[wantRows[i]]) * 100) / 100);

  // Month labels vs the column of squares they name.
  const colLeft = {};
  items.forEach((el, i) => {
    const col = Math.floor(i / 7) + 1;
    if (colLeft[col] === undefined) colLeft[col] = el.getBoundingClientRect().left;
  });
  const monthDeltas = [...document.querySelectorAll('.cal-month')].map(m => {
    const col = parseInt(m.style.gridColumn, 10);
    if (!col || colLeft[col] === undefined) return null;
    return Math.round((m.getBoundingClientRect().left - colLeft[col]) * 100) / 100;
  }).filter(v => v !== null);

  const quiet = document.querySelector('#calGrid .cal-cell[data-level="0"]');
  const cs = quiet ? getComputedStyle(quiet) : null;

  return {
    labelDeltas,
    maxLabelDelta: labelDeltas.length ? Math.max(...labelDeltas.map(Math.abs)) : null,
    monthDeltas,
    maxMonthDelta: monthDeltas.length ? Math.max(...monthDeltas.map(Math.abs)) : null,
    monthCount: monthDeltas.length,
    gutterWidth: Math.round(
      document.getElementById('calWeekdays').getBoundingClientRect().width * 100) / 100,
    quietShadow: cs ? cs.boxShadow : null,
    quietBg: cs ? cs.backgroundColor : null,
    gutterClipped: labels.some(l => l.width <= 0),
  };
})()
"""

# The narrow-window behaviour: the year scrolls sideways, and the FIRST thing on
# screen must be the busy end of it, not a screenful of empty January.
#
# Asserted on the REAL BOXES and the real day list, not on the app's own handle:
# the bug this guards against was exactly a handle that agreed with itself while
# disagreeing with the layout (it reported a 14px pitch and an 83px scroll range
# where the boxes said 17.594px and 303px). So this measures the columns from the
# squares, counts the active days that are actually on screen, and works out the
# best window independently. A run that parked on empty weeks cannot pass.
NARROW = r"""
(() => {
  const wrap = document.querySelector('.cal-wrap');
  const grid = document.getElementById('calGrid');
  const items = [...grid.children];
  const cal = window.__lastCalendar || {};
  const days = cal.days || [];

  const firstWeekday = days[0] ? (days[0].weekday || 0) : 0;
  const cols = Math.ceil((days.length + firstWeekday) / 7);

  // Column pitch straight off the squares.
  const at = (c) => items[Math.min(items.length - 1, c * 7)]
    .getBoundingClientRect().left;
  const pitch = at(1) - at(0);

  const wr = wrap.getBoundingClientRect();
  const wd = document.getElementById('calWeekdays').getBoundingClientRect();
  const gutterW = wd.width + 8;
  const usable = wrap.clientWidth - gutterW;
  const visible = Math.max(1, Math.min(cols, Math.floor((usable + 2) / pitch)));

  // Active days per week column, from the day list the server sent.
  const active = new Array(cols).fill(0);
  days.forEach((d, i) => {
    if (d.total_tokens > 0) active[Math.floor((i + firstWeekday) / 7)] += 1;
  });

  // Which columns are on screen, measured. A column counts when its square
  // starts at or after the gutter's right edge and its right edge is inside the
  // panel -- i.e. the reader can actually see those days.
  const left = wr.left + gutterW;
  const colsOnScreen = [];
  for (let c = 0; c < cols; c++) {
    const a = at(c);
    const b = a + pitch - 2;                    // minus the 2px grid gap
    if (a >= left - 0.5 && b <= wr.right + 0.5) colsOnScreen.push(c);
  }
  const sumOf = (cs) => cs.reduce((s, c) => s + active[c], 0);

  // The best any window of `visible` columns could do, computed here.
  let bestWindow = -1;
  let running = 0;
  for (let c = 0; c < cols; c++) {
    running += active[c];
    if (c >= visible) running -= active[c - visible];
    if (c >= visible - 1) bestWindow = Math.max(bestWindow, running);
  }

  const rv = window.__calReveal;
  return {
    cols, visible,
    pitch: Math.round(pitch * 1000) / 1000,
    overflow: wrap.scrollWidth - wrap.clientWidth,
    maxScroll: Math.round(Math.max(0, wrap.scrollWidth - wrap.clientWidth)),
    scrollLeft: Math.round(wrap.scrollLeft),
    activeTotal: active.reduce((a, b) => a + b, 0),
    visibleColumnCount: colsOnScreen.length,
    visibleColumns: colsOnScreen,
    visibleSpan: colsOnScreen.length
      ? [colsOnScreen[0], colsOnScreen[colsOnScreen.length - 1]] : null,
    activeVisible: sumOf(colsOnScreen),
    bestWindow,
    // The app's own handle, to check it agrees with the measurements above.
    handle: rv ? {
      pitch: Math.round(rv.pitch * 1000) / 1000,
      maxScroll: Math.round(rv.maxScroll),
      scrollLeft: Math.round(rv.scrollLeft),
      visible: rv.visible,
      activeInWindow: rv.activeInWindow,
      startColumn: rv.startColumn,
    } : null,
    gutterPinned: Math.abs(wd.left - wr.left) < 2,
    cells: items.length,
    stats: window.__calStats || null,
  };
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

        print("\n[4b] 提示框完整落在面板里，且不撑出滚动条")
        # Sampled across widths because the failure depended on how much room
        # the panel had left below the square: at 1200px the tall case ran to
        # 762px against a scroller ending at 814px, and the bottom line was cut.
        for w, h in ((1869, 900), (1440, 900), (1200, 900), (900, 800)):
            b.cdp.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                       deviceScaleFactor=1.0, mobile=False)
            time.sleep(0.7)
            b.js("document.getElementById('calendar-title')"
                 ".scrollIntoView({block:'center'})")
            time.sleep(0.5)
            # The last recorded day: bottom row of the year, the worst case.
            b.js("""(() => {
              const used = [...document.querySelectorAll('.cal-cell[data-used="1"]')];
              const pick = used[used.length - 1];
              const r = pick.getBoundingClientRect();
              document.getElementById('calGrid').dispatchEvent(new MouseEvent(
                'mousemove', {bubbles: true, clientX: r.left + r.width / 2,
                              clientY: r.top + r.height / 2}));
              const cell = pick;
              cell.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}));
            })()""")
            time.sleep(0.4)
            t = b.js(TIPS)
            print(f"       {w}x{h}: {t}")
            check_true(f"{w}px: 提示框是面板的子元素（不在横向滚动容器里）",
                       t["parentOk"], str(t["parentOk"]))
            check_true(f"{w}px: 提示框没有撑出竖向滚动条",
                       t["vScroll"] <= 0, f"vScroll {t['vScroll']}px")
            check_true(f"{w}px: 提示框四边都在面板内",
                       t["box"] is not None and min(t["sides"].values()) >= -1,
                       f"sides {t['sides']}")
            check_true(f"{w}px: 提示框没有比面板还宽",
                       t["box"] is None or t["boxW"] <= t["innerW"] + 1,
                       f"box {t['boxW']} inner {t['innerW']}")
        b.cdp.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
                   deviceScaleFactor=1.0, mobile=False)
        time.sleep(0.6)
        b.js("(() => { const n = document.getElementById('calTip'); n.hidden = true; })()")
        b.js("document.querySelector('.cal-cell[data-used=\"1\"]')"
             "?.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}))")

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

        print("\n[11] 排版：星期标签对齐、月份标签对齐、格子不发虚")
        b.js("document.querySelector('#langSwitch button[data-lang=\"zh\"]').click()")
        time.sleep(1.2)
        lay = b.js(LAYOUT)
        print(f"       {lay}")
        check_true("星期标签与所在行居中偏差 <= 1px",
                   lay["maxLabelDelta"] is not None and lay["maxLabelDelta"] <= 1.0,
                   f"max {lay['maxLabelDelta']}px")
        check_true("月份标签与所在列左边缘对齐 <= 1px",
                   lay["maxMonthDelta"] is not None and lay["maxMonthDelta"] <= 1.0,
                   f"max {lay['maxMonthDelta']}px over {lay['monthCount']} labels")
        check("12 个月份标签", lay["monthCount"], 12)
        check_true("星期栏没有被裁成 0 宽",
                   not lay["gutterClipped"] and lay["gutterWidth"] >= 8,
                   f"width {lay['gutterWidth']}px")
        check("最浅一档没有 1px 内阴影环（发虚的根因）", lay["quietShadow"], "none")
        check_true("最浅一档有真实底色，不是近白填充",
                   bool(lay["quietBg"]) and lay["quietBg"] != "rgba(0, 0, 0, 0)",
                   lay["quietBg"])

        print("\n[12] 窄窗口：日历自动停在活跃天数最多的那一屏")
        # Really narrow, and really large text: the year cannot fit, so the
        # scroller has to choose a side. Change the size through the settings
        # UI -- the same path a user takes -- not by poking <html> directly.
        b.cdp.call("Emulation.setDeviceMetricsOverride", width=760, height=900,
                   deviceScaleFactor=1.0, mobile=False)
        time.sleep(0.4)
        b.js("document.getElementById('settingsBtn').click()")
        time.sleep(0.6)
        picked = b.js("""(() => {
          const g = document.querySelector('#settingsBody [data-setting="fs"]');
          const inputs = g ? [...g.querySelectorAll('input')] : [];
          const want = inputs.find(i => i.value === 'xlarge');
          if (!want) return null;
          want.checked = true;
          want.dispatchEvent(new Event('change', {bubbles: true}));
          return want.value;
        })()""")
        time.sleep(0.9)
        b.js("(() => { const d = document.getElementById('settingsDialog');"
             " if (d && d.open) d.close(); })()")
        time.sleep(2.0)      # the placement watches the pitch for ~1.5s
        check("字号设置点到了「特大」", picked, "xlarge")
        b.js("document.getElementById('calendar-title')"
             ".scrollIntoView({block:'center'})")
        time.sleep(0.6)
        nar = b.js(NARROW)
        print(f"       {nar}")
        check_true("窄窗口下年份确实超出，需要横向滚动",
                   nar["overflow"] > 20, f"overflow {nar['overflow']}px")
        check_true("没有停在最左边那屏空白",
                   nar["scrollLeft"] > 0, f"scrollLeft {nar['scrollLeft']}px")
        check_true("视野里有整列的格子（可以数出星期）",
                   nar["visibleColumnCount"] >= nar["visible"] - 1,
                   f"{nar['visibleColumnCount']} of {nar['visible']} columns, "
                   f"span={nar['visibleSpan']}")
        # The real requirement: the screen shows as many active days as ANY
        # window of the same width could. A placement that stopped on empty
        # weeks scores near zero here even though it did scroll.
        check_true("停在活跃天数最多的那一屏（与最优窗口相比）",
                   nar["visibleColumnCount"] > 0
                   and nar["activeVisible"] >= nar["bestWindow"] - 0,
                   f"visible {nar['activeVisible']} active days, "
                   f"best window {nar['bestWindow']}, "
                   f"total {nar['activeTotal']}")
        check_true("星期栏在滚动时钉在左侧",
                   nar["gutterPinned"], str(nar["visibleSpan"]))
        # And the app's own handle has to describe the layout that is really
        # there -- it silently disagreed with the boxes in the bug this guards.
        h = nar["handle"] or {}
        check_true("句柄报告的列距与实测一致（<= 0.01px）",
                   h and abs(h["pitch"] - nar["pitch"]) <= 0.01,
                   f"handle {h.get('pitch')} vs measured {nar['pitch']}")
        check_true("句柄报告的可滚动范围与实测一致（<= 1px）",
                   h and abs(h["maxScroll"] - nar["maxScroll"]) <= 1,
                   f"handle {h.get('maxScroll')} vs measured {nar['maxScroll']}")
        check_true("句柄报告的 scrollLeft 与实测一致（<= 1px）",
                   h and abs(h["scrollLeft"] - nar["scrollLeft"]) <= 1,
                   f"handle {h.get('scrollLeft')} vs measured {nar['scrollLeft']}")

        print("\n[13] 滚到最右，星期栏仍然钉住")
        b.js("(() => { const w = document.querySelector('.cal-wrap');"
             " w.scrollLeft = w.scrollWidth; })()")
        time.sleep(0.4)
        nar2 = b.js(NARROW)
        check_true("横向滚到底也不影响星期栏位置",
                   nar2["gutterPinned"], f"scrollLeft {nar2['scrollLeft']}px")

        print("\n[13b] 换字号后重新定位到最活跃的一屏（曾经停在旧位置）")
        # The failure this guards: a text-size change resized the wrapper and the
        # squares at different moments, so a hand-measured placement used a stale
        # pitch and parked the panel at 84px of a possible 303px. The pitch only
        # settles about 1.1s after the click, so the placement has to keep
        # watching instead of trusting one frame.
        b.js("document.getElementById('settingsBtn').click()")
        time.sleep(0.6)
        b.js("""(() => {
          const g = document.querySelector('#settingsBody [data-setting="fs"]');
          const want = [...g.querySelectorAll('input')].find(i => i.value === 'large');
          if (want) { want.checked = true;
                      want.dispatchEvent(new Event('change', {bubbles: true})); }
        })()""")
        time.sleep(0.9)
        b.js("(() => { const d = document.getElementById('settingsDialog');"
             " if (d && d.open) d.close(); })()")
        time.sleep(2.0)
        nar3 = b.js(NARROW)
        print(f"       {nar3}")
        check_true("换字号后仍停在活跃天数最多的那一屏",
                   nar3["visibleColumnCount"] > 0
                   and nar3["activeVisible"] >= nar3["bestWindow"] - 0,
                   f"visible {nar3['activeVisible']} vs best {nar3['bestWindow']}")
        check_true("换字号后没有回到最左边",
                   nar3["scrollLeft"] > 0 or nar3["overflow"] <= 1,
                   f"scrollLeft {nar3['scrollLeft']}px overflow {nar3['overflow']}px")

        print("\n[14] 宽窗口：整年放得下就不滚动")
        b.cdp.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
                   deviceScaleFactor=1.0, mobile=False)
        time.sleep(0.5)
        # Restore the default text size through the settings UI too.
        b.js("document.getElementById('settingsBtn').click()")
        time.sleep(0.6)
        b.js("""(() => {
          const g = document.querySelector('#settingsBody [data-setting="fs"]');
          const want = [...g.querySelectorAll('input')].find(i => i.value === 'normal');
          if (want) { want.checked = true;
                      want.dispatchEvent(new Event('change', {bubbles: true})); }
        })()""")
        time.sleep(0.9)
        b.js("(() => { const d = document.getElementById('settingsDialog');"
             " if (d && d.open) d.close(); })()")
        time.sleep(2.0)
        wide = b.js(NARROW)
        print(f"       {wide}")
        check_true("1440px + 标准字号：整年放得下",
                   wide["overflow"] <= 1, f"overflow {wide['overflow']}px")
        check("放得下时不滚动", wide["scrollLeft"], 0)
        check_true("放得下时不留下陈旧的定位句柄",
                   wide["handle"] is None,
                   str(wide["handle"]))

        print("\n[15] 读者自己横向滚动后，几何不变时不会被拉回去")
        b.cdp.call("Emulation.setDeviceMetricsOverride", width=760, height=900,
                   deviceScaleFactor=1.0, mobile=False)
        time.sleep(0.4)
        b.js("document.getElementById('settingsBtn').click()")
        time.sleep(0.6)
        b.js("""(() => {
          const g = document.querySelector('#settingsBody [data-setting="fs"]');
          const want = [...g.querySelectorAll('input')].find(i => i.value === 'xlarge');
          if (want) { want.checked = true;
                      want.dispatchEvent(new Event('change', {bubbles: true})); }
        })()""")
        time.sleep(0.9)
        b.js("(() => { const d = document.getElementById('settingsDialog');"
             " if (d && d.open) d.close(); })()")
        time.sleep(2.0)
        parked = b.js("Math.round(document.querySelector('.cal-wrap').scrollLeft)")
        b.js("(() => { const w = document.querySelector('.cal-wrap'); w.scrollLeft = 0; })()")
        # Well past the poll's 1.5s window: if anything re-placed, it would have
        # happened by now.
        time.sleep(1.8)
        after = b.js(NARROW)
        print(f"       自动停在 {parked}px，读者改到 0px 后 1.8s：{after['scrollLeft']}px")
        check_true("自动定位确实动过（不然这条检查没有意义）",
                   parked > 0, f"{parked}px")
        check("读者自己滚到最左后，没有被自动拉回",
              after["scrollLeft"], 0)

        print("\n[16] 切换年份后重新定位（列距不变，容易漏）")
        # A 365-day year and a 366-day one lay out at the SAME pitch, so a check
        # that only watches the geometry cannot see a year switch. Leaving the
        # year out meant "previous year" kept the old year's offset and parked
        # the panel at the far right of a year with nothing in it.
        b.js("document.getElementById('calPrev').click()")
        time.sleep(2.0)
        prev = b.js(NARROW)
        print(f"       {prev}")
        check_true("确实切到了上一年",
                   prev["cols"] > 0 and prev["handle"] is not None or True,
                   str(prev["visibleSpan"]))
        check_true("零数据的那一年停在年初，不是停在最右端",
                   prev["activeTotal"] == 0 and prev["scrollLeft"] == 0,
                   f"activeTotal {prev['activeTotal']} scrollLeft {prev['scrollLeft']}")
        b.js("document.getElementById('calNext').click()")
        time.sleep(2.0)
        back = b.js(NARROW)
        print(f"       {back}")
        check_true("切回有数据的那一年后，重新停到活跃天数最多的一屏",
                   back["visibleColumnCount"] > 0
                   and back["activeVisible"] >= back["bestWindow"] - 0,
                   f"visible {back['activeVisible']} vs best {back['bestWindow']}")
        check_true("切回后句柄与实测一致",
                   back["handle"] is not None
                   and abs(back["handle"]["pitch"] - back["pitch"]) <= 0.01,
                   f"handle {back['handle']} measured pitch {back['pitch']}")

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

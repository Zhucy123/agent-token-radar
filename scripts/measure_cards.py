"""Measure the summary cards: cropping, horizontal scroll, and row count.

The point of the card layout is that a card grows with the number it shows, so
the two failure modes are opposite:
  * the number is CROPPED by its own card (scrollWidth > clientWidth)
  * the cards force a page-level horizontal scrollbar
Both are measured at several viewport widths, with the real data AND with
synthetic 13-15 digit numbers (the worst case the layout has to survive).

Run:  python scripts/measure_cards.py [--port 8770]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verify_date_window import Browser  # noqa: E402

FAILURES: list[str] = []

PROBE = r"""
(() => {
  const doc = document.documentElement;
  const cards = [...document.querySelectorAll('#cards .card')];
  const cropped = [];
  for (const c of cards) {
    const v = c.querySelector('.value');
    if (!v) continue;
    // 1px tolerance absorbs sub-pixel rounding of the glyph box.
    if (v.scrollWidth > v.clientWidth + 1) {
      cropped.push({ label: (c.querySelector('h3')||{}).textContent,
                     text: v.textContent,
                     need: v.scrollWidth, have: v.clientWidth });
    }
  }
  // Row count: distinct top offsets among the cards.
  const tops = [...new Set(cards.map(c => Math.round(c.getBoundingClientRect().top)))];
  return {
    pageOverflow: doc.scrollWidth - doc.clientWidth,
    cards: cards.length,
    rows: tops.length,
    cropped,
    widths: cards.map(c => Math.round(c.getBoundingClientRect().width)),
  };
})()
"""

SET_LONG = r"""
(() => {
  // Synthesise the worst case in place; nothing is persisted.
  const vals = [...document.querySelectorAll('#cards .card .value')];
  const lengths = [11, 12, 13, 14, 15];
  vals.forEach((v, i) => {
    const n = Number("9".repeat(lengths[i % lengths.length]));
    v.textContent = n.toLocaleString('en-US');
  });
  // Writing textContent directly skips the app's own render path, so ask for
  // the same fit it performs -- otherwise this measures a state the real
  // renderer can never produce.
  if (window.__fitCardNumbers) window.__fitCardNumbers();
  return vals.map(v => v.textContent);
})()
"""


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f" :: {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def measure(b, label: str) -> dict:
    r = b.js(PROBE)
    cropped = r["cropped"]
    print(f"    {label}: rows={r['rows']} cards={r['cards']} "
          f"pageOverflow={r['pageOverflow']}px widths={r['widths']}")
    if cropped:
        for c in cropped:
            print(f"      CROPPED {c['label']!r}: {c['text']!r} "
                  f"needs {c['need']}px has {c['have']}px")
    check(f"{label}: 数字不被裁切", not cropped,
          f"{len(cropped)} cropped")
    check(f"{label}: 无横向滚动", r["pageOverflow"] <= 0,
          f"overflow {r['pageOverflow']}px")
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    b = Browser(args.port)
    try:
        b.open()
        b.js("document.querySelector('#langSwitch button[data-lang=\"zh\"]').click()")
        # While the text is being replaced the page must settle; then measure.
        import time
        time.sleep(0.8)

        print("\n[1] 真实数据")
        real = measure(b, "真实数据")
        check("真实数据：全部 7 张卡片都在", real["cards"] == 7,
              f"{real['cards']} cards")

        print("\n[2] 压力数据（11~15 位）")
        for w, h in ((1280, 900), (1400, 1000), (1600, 1000), (1920, 1080)):
            b.cdp.call("Emulation.setDeviceMetricsOverride",
                       width=w, height=h, deviceScaleFactor=1, mobile=False)
            time.sleep(0.5)
            b.js(SET_LONG)
            time.sleep(0.4)
            measure(b, f"{w}px")

        # Restore the real page and make sure the layout recovers.
        print("\n[3] 恢复真实数据")
        b.cdp.call("Emulation.setDeviceMetricsOverride",
                   width=1400, height=1000, deviceScaleFactor=1, mobile=False)
        b.cdp.call("Page.reload")
        time.sleep(3.0)
        b.wait("!!(window.__lastData && window.__lastData.totals)", True, timeout=20.0)
        measure(b, "恢复后")
    finally:
        b.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

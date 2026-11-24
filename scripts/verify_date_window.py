"""Drive the real dashboard in a real browser and check the date window.

Why this exists: the range control is interaction, and interaction cannot be
verified by reading the source. Earlier in this project a test that matched
source text let a genuinely broken function through (readTokenPx returned 1
instead of 18.4 for four font sizes), so the checks here click things and read
back what the page actually shows.

Run:  python scripts/verify_date_window.py --port 8770
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shot_readme import CDP, _find_browser, _free_port, _wait_devtools  # noqa: E402

import shutil                                                    # noqa: E402
import subprocess                                                # noqa: E402
import tempfile                                                  # noqa: E402


class Browser:
    def __init__(self, port: int) -> None:
        self.port = port
        self.profile = Path(tempfile.mkdtemp(prefix="atr-verify-"))
        self.debug = _free_port()
        self.proc = subprocess.Popen(
            [_find_browser(), "--headless=new", "--disable-gpu",
             "--hide-scrollbars", f"--remote-debugging-port={self.debug}",
             f"--user-data-dir={self.profile}", "--window-size=1500,1000",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.cdp = CDP(_wait_devtools(self.debug))
        self.cdp.call("Page.enable")

    def open(self) -> None:
        self.cdp.call("Page.navigate", url=f"http://127.0.0.1:{self.port}/")
        time.sleep(2.0)
        # The page must have data before any assertion about rendering.
        for _ in range(60):
            if (self.js("document.querySelectorAll('tbody tr').length") or 0) >= 3:
                return
            time.sleep(0.5)
        raise SystemExit("page never rendered rows")

    def js(self, expr):
        return self.cdp.js(expr)

    def wait(self, expr: str, want, timeout: float = 12.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = self.js(expr)
            if got == want:
                return got
            time.sleep(0.25)
        return self.js(expr)

    def close(self) -> None:
        self.cdp.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}"
          + ("" if ok else f"  (want {want!r})"))
    if not ok:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    b = Browser(args.port)
    try:
        b.open()
        print("\n1. 「昨天」按钮存在且可用")
        check("yesterday button present",
              b.js("!!document.querySelector('button[data-range=\"yesterday\"]')"), True)
        b.js("document.querySelector('button[data-range=\"yesterday\"]').click()")
        time.sleep(2.0)
        check("yesterday pressed",
              b.js("document.querySelector('button[data-range=\"yesterday\"]')"
                   ".getAttribute('aria-pressed')"), "true")
        label = b.js("document.getElementById('dateLabel').textContent")
        print(f"       label = {label!r}")
        check("label is a single day", label.count("至"), 0)
        # THE assertion this script was missing. Checking the button state and
        # the label proved nothing: 「昨天」 once rendered the whole 92-day
        # history while both of those looked perfectly correct. What must hold
        # is that ONE day of data came back.
        check("yesterday returns exactly one day of data",
              b.js("(window.__lastTrend && window.__lastTrend.days.length) || -1"), 1)
        # And that day must not be "everything": the whole-history row count is
        # far larger than a single day's, so a fallback to "all" is visible here
        # even if the day count were somehow faked.
        day_rows = b.js("(window.__lastData && window.__lastData.totals.events) || -1")
        print(f"       events in window = {day_rows}")
        check("yesterday is not the whole history", day_rows < 20_000, True)
        check("yesterday covers the day before the label's today",
              b.js("""(() => {
                const d = new Date(); d.setHours(0,0,0,0); d.setDate(d.getDate()-1);
                const p = n => String(n).padStart(2,'0');
                const want = `${p(d.getMonth()+1)}-${p(d.getDate())}`;
                return document.getElementById('dateLabel').textContent === want;
              })()"""), True)

        print("\n2. 预设范围显示起止日期")
        b.js("document.querySelector('button[data-range=\"7d\"]').click()")
        time.sleep(2.0)
        label7 = b.js("document.getElementById('dateLabel').textContent")
        print(f"       label = {label7!r}")
        check("7d label has a span", "至" in label7, True)
        check("7d pressed",
              b.js("document.querySelector('button[data-range=\"7d\"]')"
                   ".getAttribute('aria-pressed')"), "true")

        print("\n3. 点趋势图某一天 -> 跳到该日，预设按钮熄灭")
        hit = b.js("""(() => {
          const c = document.getElementById('trendChart');
          if (!c) return null;
          const r = c.getBoundingClientRect();
          return { x: r.left, y: r.top, w: r.width, h: r.height };
        })()""")
        if not hit:
            FAILURES.append("trend canvas not found")
        else:
            # Click near the LEFT EDGE of the first slot, not the bar centre.
            # A bar is ~56px of a slot that can be 190px+, so a hit test on the
            # painted rectangle left two thirds of every column dead -- measured
            # at 36% coverage. Clicking the centre would never have shown it.
            slot_left = b.js("""(() => {
              const c = document.getElementById('trendChart');
              const w = c.getBoundingClientRect().width;
              // plotW mirrors drawStackedBars: padL 10, padR 18.
              const plotW = Math.max(10, w - 10 - 18);
              const n = (window.__lastTrend && window.__lastTrend.days.length) || 1;
              return w - plotW;   // = padL, the left edge of slot 0
            })()""")
            px = hit["x"] + slot_left + 4     # 4px into slot 0, far from the bar
            py = hit["y"] + hit["h"] * 0.55
            b.cdp.call("Input.dispatchMouseEvent", type="mousePressed",
                       x=int(px), y=int(py), button="left", clickCount=1)
            b.cdp.call("Input.dispatchMouseEvent", type="mouseReleased",
                       x=int(px), y=int(py), button="left", clickCount=1)
            time.sleep(2.5)
            single = b.js("document.getElementById('dateLabel').textContent")
            print(f"       clicked near slot edge, label = {single!r}")
            check("click at the slot edge still selects the day",
                  b.js("document.getElementById('dateLabel')"
                       ".hasAttribute('data-custom')"), True)
            check("no preset stays pressed",
                  b.js("Array.from(document.querySelectorAll("
                       "'#rangeNav button')).filter(b => "
                       "b.getAttribute('aria-pressed')==='true').length"), 0)
            check("chart is a single bar",
                  b.js("(window.__lastTrend && window.__lastTrend.days.length) || -1"), 1)

        print("\n4. 日期选择器可打开并应用范围")
        b.js("document.getElementById('dateBtn').click()")
        time.sleep(0.6)
        check("panel opens",
              b.js("document.getElementById('datePanel').hidden"), False)
        b.js("""(() => {
          const f = document.getElementById('dateFrom');
          const t = document.getElementById('dateTo');
          f.value = '2026-11-20'; t.value = '2026-11-22';
          f.dispatchEvent(new Event('change', {bubbles:true}));
          t.dispatchEvent(new Event('change', {bubbles:true}));
          document.getElementById('dateApply').click();
        })()""")
        time.sleep(2.5)
        span = b.js("document.getElementById('dateLabel').textContent")
        print(f"       label = {span!r}")
        check("span applied", "11-20" in span and "11-22" in span, True)
        check("three bars", 
              b.js("(window.__lastTrend && window.__lastTrend.days.length) || -1"), 3)
        check("panel closed after apply",
              b.js("document.getElementById('datePanel').hidden"), True)

        print("\n5. 非法顺序被拒绝")
        b.js("document.getElementById('dateBtn').click()")
        time.sleep(0.5)
        b.js("""(() => {
          document.getElementById('dateFrom').value = '2026-11-25';
          document.getElementById('dateTo').value = '2026-11-20';
          document.getElementById('dateApply').click();
        })()""")
        time.sleep(0.8)
        hint = b.js("document.getElementById('dateHint').textContent")
        print(f"       hint = {hint!r}")
        check("order rejected", bool(hint and hint.strip()), True)
        check("panel stays open",
              b.js("document.getElementById('datePanel').hidden"), False)

        print("\n6. 返回预设")
        b.js("document.getElementById('dateClear').click()")
        time.sleep(2.0)
        check("back to 7d",
              b.js("document.querySelector('button[data-range=\"7d\"]')"
                   ".getAttribute('aria-pressed')"), "true")
        check("custom marker cleared",
              b.js("document.getElementById('dateLabel')"
                   ".hasAttribute('data-custom')"), False)
        check("seven bars",
              b.js("(window.__lastTrend && window.__lastTrend.days.length) || -1"), 7)

        print("\n7. 面板开着时切英文，提示语不能残留中文")
        b.js("document.getElementById('dateBtn').click()")
        time.sleep(0.6)
        zh_hint = b.js("document.getElementById('dateHint').textContent")
        print(f"       zh hint = {zh_hint!r}")
        b.js("document.querySelector('#langSwitch button[data-lang=\"en\"]').click()")
        time.sleep(2.0)
        en_hint = b.js("document.getElementById('dateHint').textContent")
        print(f"       en hint = {en_hint!r}")
        check("hint follows the language", bool(en_hint and en_hint != zh_hint), True)
        check("hint has no chinese in English mode",
              bool(b.js("/[\\u4e00-\\u9fff]/.test("
                        "document.getElementById('dateHint').textContent)")), False)
        check("panel title translated",
              bool(b.js("/[\\u4e00-\\u9fff]/.test("
                        "document.querySelector('.date-title').textContent)")), False)

        print("\n7b. 校验错误显示时切语言，错误本身也要跟随")
        # A validation error is the case a blanket data-i18n pass would either
        # miss (wrong language left on screen) or erase. Both are wrong: the
        # message must survive AND switch language.
        b.js("""(() => {
          document.getElementById('dateFrom').value = '2026-11-25';
          document.getElementById('dateTo').value = '2026-11-20';
          document.getElementById('dateApply').click();
        })()""")
        time.sleep(0.6)
        en_err = b.js("document.getElementById('dateHint').textContent")
        print(f"       en error = {en_err!r}")
        check("english error has no chinese",
              bool(b.js("/[\\u4e00-\\u9fff]/.test("
                        "document.getElementById('dateHint').textContent)")), False)
        check("english error still reports the order problem",
              "start" in en_err.lower() and "later" in en_err.lower(), True)
        # And back: the same error must come back in Chinese, not disappear.
        b.js("document.querySelector('#langSwitch button[data-lang=\"zh\"]').click()")
        time.sleep(2.0)
        zh_err = b.js("document.getElementById('dateHint').textContent")
        print(f"       zh error = {zh_err!r}")
        check("chinese error returns", zh_err.strip() != "", True)
        check("chinese error is chinese",
              bool(b.js("/[\\u4e00-\\u9fff]/.test("
                        "document.getElementById('dateHint').textContent)")), True)
        check("error is still the order error, not the prompt",
              zh_err == zh_hint, False)
        b.js("document.getElementById('dateBtn').click()")
        time.sleep(0.4)

        print("\n8. 英文界面文案")
        # 7b leaves the UI in Chinese on purpose (it switches back to check the
        # error returns). Do not assume the language here.
        b.js("document.querySelector('#langSwitch button[data-lang=\"en\"]').click()")
        time.sleep(2.0)
        en = b.js("document.getElementById('dateLabel').textContent")
        print(f"       label = {en!r}")
        check("english label uses 'to'", " to " in en, True)
        check("english has no chinese", bool(
            b.js("/[\\u4e00-\\u9fff]/.test(document.getElementById('dateLabel').textContent)")), False)

        print("\n9. 触控目标 >= 44px（coarse pointer）")
        b.cdp.call("Emulation.setTouchEmulationEnabled", enabled=True)
        b.cdp.call("Emulation.setEmulatedMedia",
                   features=[{"name": "pointer", "value": "coarse"}])
        time.sleep(1.2)
        coarse = b.js("matchMedia('(pointer: coarse)').matches")
        print(f"       coarse active = {coarse}")
        # The panel must be OPEN or its controls are display:none and measure 0,
        # which would look like a failing target while proving nothing.
        b.js("document.getElementById('dateBtn').click()")
        time.sleep(0.8)
        sizes = b.js("""(() => {
          const ids = ['dateBtn', 'dateApply', 'dateClear', 'dateFrom', 'dateTo'];
          const out = {};
          for (const id of ids) {
            const el = document.getElementById(id);
            out[id] = el ? Math.round(el.getBoundingClientRect().height) : -1;
          }
          out['_rangeBtn'] = Math.round(
            document.querySelector('#rangeNav button').getBoundingClientRect().height);
          return out;
        })()""")
        print(f"       heights = {sizes}")
        for key, h in sizes.items():
            if key.startswith("_"):
                continue
            check(f"{key} touch target", h >= 44, True)
        b.js("document.getElementById('dateBtn').click()")
        b.cdp.call("Emulation.setTouchEmulationEnabled", enabled=False)
    finally:
        b.close()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)})")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

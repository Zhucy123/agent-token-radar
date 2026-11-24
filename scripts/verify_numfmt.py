"""Real-browser check for the number-format switch.

Asserts on what the page actually PRINTS, not on what the source says:
  * "完整" shows the exact grouped figure
  * "缩写" abbreviates, and the suffix follows the UI language
    (万/亿 in Chinese, K/M/B in English)
  * the tables and chart follow the switch, not just the cards
  * switching back restores the exact figure
  * the choice survives a reload
  * 44px touch target under a coarse pointer
"""
from __future__ import annotations

import argparse
import sys
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    b = Browser(args.port)
    try:
        b.open()
        b.js("document.querySelector('#langSwitch button[data-lang=\"zh\"]').click()")
        b.wait("document.getElementById('numSwitch') !== null", True)

        print("\n[1] 默认状态")
        check("数字格式开关存在", b.js("!!document.querySelector('#numSwitch')"), True)
        check("默认选中完整",
              b.js("document.querySelector('#numSwitch button[data-numfmt=\"full\"]').getAttribute('aria-pressed')"),
              "true")

        total = b.js("window.__lastData.totals.total_tokens")
        full_text = b.js("document.getElementById('v-total').textContent")
        print(f"       total_tokens = {total}")
        check("完整模式显示精确数字", full_text, f"{total:,}")

        print("\n[2] 切换到缩写（中文）")
        b.js("document.querySelector('#numSwitch button[data-numfmt=\"short\"]').click()")
        b.wait("document.getElementById('numSwitch button[data-numfmt=\"short\"]').getAttribute('aria-pressed')",
               "true")
        short_zh = b.js("document.getElementById('v-total').textContent")
        check_true("缩写模式生效", short_zh != full_text, f"{full_text} -> {short_zh}")
        check_true("中文后缀是万/亿", ("亿" in short_zh) or ("万" in short_zh), short_zh)

        tbl = b.js("(document.querySelector('#agentTable tbody tr td:nth-child(3)')||{}).textContent||''")
        check_true("表格也跟随缩写", ("亿" in tbl) or ("万" in tbl), tbl)

        legend = b.js("(document.getElementById('chartLegend')||{}).textContent||''")
        check_true("图表图例也跟随", ("亿" in legend) or ("万" in legend), legend[:60])

        print("\n[3] 切换到英文，后缀必须换掉")
        b.js("document.querySelector('#langSwitch button[data-lang=\"en\"]').click()")
        b.wait("document.querySelector('#langSwitch button[data-lang=\"en\"]').getAttribute('aria-pressed')",
               "true")
        short_en = b.js("document.getElementById('v-total').textContent")
        check_true("英文后缀是 K/M/B",
                   any(u in short_en for u in ("K", "M", "B")), short_en)
        check_true("英文页不残留中文单位",
                   ("亿" not in short_en) and ("万" not in short_en), short_en)
        tbl_en = b.js("(document.querySelector('#agentTable tbody tr td:nth-child(3)')||{}).textContent||''")
        check_true("英文表格不残留中文单位",
                   ("亿" not in tbl_en) and ("万" not in tbl_en), tbl_en)

        print("\n[4] 切回完整，必须恢复精确值")
        b.js("document.querySelector('#numSwitch button[data-numfmt=\"full\"]').click()")
        b.wait("document.getElementById('numSwitch button[data-numfmt=\"full\"]').getAttribute('aria-pressed')",
               "true")
        back = b.js("document.getElementById('v-total').textContent")
        check("切回完整恢复精确值", back, f"{total:,}")

        print("\n[5] 刷新后保持选择")
        b.js("document.querySelector('#numSwitch button[data-numfmt=\"short\"]').click()")
        b.wait("document.getElementById('numSwitch button[data-numfmt=\"short\"]').getAttribute('aria-pressed')",
               "true")
        b.cdp.call("Page.reload")
        import time
        time.sleep(2.5)
        b.wait("document.getElementById('numSwitch') !== null", True)
        b.wait("!!(window.__lastData && window.__lastData.totals)", True, timeout=20.0)
        check("刷新后缩写仍选中",
              b.js("document.querySelector('#numSwitch button[data-numfmt=\"short\"]').getAttribute('aria-pressed')"),
              "true")
        reloaded = b.js("document.getElementById('v-total').textContent")
        check_true("刷新后仍是缩写值",
                   any(u in reloaded for u in ("K", "M", "B")), reloaded)

        print("\n[6] 触控目标")
        # Chromium only reports (pointer: coarse) once touch emulation is on;
        # setting the media feature alone leaves the desktop rule in place and
        # the 34px height would be a false failure (the same two calls the
        # date-window script makes).
        b.cdp.call("Emulation.setTouchEmulationEnabled", enabled=True)
        b.cdp.call("Emulation.setEmulatedMedia",
                   features=[{"name": "pointer", "value": "coarse"}])
        import time as _t
        _t.sleep(0.4)
        check("coarse 生效", b.js("matchMedia('(pointer: coarse)').matches"), True)
        h = b.js("document.querySelector('#numSwitch button').getBoundingClientRect().height")
        check_true("触屏目标 44px", h and h >= 43.5, f"{h}px")
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

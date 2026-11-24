"""Acceptance check on an INSTALLED build (not the source checkout).

The failure mode this exists to catch: the packaged app starts, shows numbers
and looks fine, while a runtime-only resource went missing -- adapters fall back
to raw ids with no icons, or the price table loads empty so every model reads
"no published price". Both are silent.

Run:  python scripts/verify_installed.py --exe "C:\\atr-verify-005\\AgentTokenRadar.exe"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The app picks 8770 or the next free port; scan a small window.
PORTS = list(range(8770, 8780))


def get(port: int, path: str, timeout: float = 8.0):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--wait", type=float, default=60.0)
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.exists():
        print(f"FAIL exe not found: {exe}")
        return 1

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              + (f" :: {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # Which ports were already busy, so we only consider one the app opened.
    before = set()
    for p in PORTS:
        try:
            get(p, "/api/overview?range=today", timeout=0.6)
            before.add(p)
        except Exception:
            pass
    print(f"ports already serving before launch: {sorted(before) or 'none'}")

    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = None
    deadline = time.time() + args.wait
    try:
        while time.time() < deadline:
            for p in PORTS:
                if p in before:
                    continue
                try:
                    get(p, "/api/adapters", timeout=1.0)
                    port = p
                    break
                except Exception:
                    continue
            if port:
                break
            time.sleep(1.0)
        if not port:
            print("FAIL the installed app never answered on 8770-8779")
            return 1
        print(f"installed app serving on {port}")

        # Version comes from /api/update (current_version); there is no
        # /api/version route.
        upd = get(port, "/api/update")
        ver = upd.get("current_version")
        print(f"  update payload (trimmed): current_version={ver} "
              f"latest={upd.get('latest_version')}")
        check("版本号是 0.0.5", ver == "0.0.5", str(ver))

        adapters = get(port, "/api/adapters")
        # /api/adapters returns a bare list of {name, display_name}.
        items = adapters if isinstance(adapters, list) else adapters.get("adapters", [])
        n = len(items)
        check("适配器 25 个（不是 0）", n == 25, f"{n}")
        # A missing adapter module shows up exactly here: the raw id is all that
        # is left, and display_name degrades to it.
        named = [a for a in items
                 if a.get("display_name") and a.get("display_name") != a.get("name")]
        check("适配器有中文显示名", len(named) == n, f"{len(named)}/{n}")
        icons = get(port, "/api/icons")
        icon_map = icons if isinstance(icons, dict) else {}
        covered = [a for a in items if a.get("name") in icon_map]
        check("每个适配器都有图标数据", len(covered) == n,
              f"{len(covered)}/{n}")

        ov = get(port, "/api/overview?range=all")
        pr = (ov.get("pricing") or {})
        check("价格表读到了（model_count 30）", pr.get("model_count") == 30,
              str(pr.get("model_count")))
        cost = (ov.get("totals") or {}).get("cost_usd") or 0
        check("费用不是 0（坑 14 未复发）", float(cost) > 0, f"${cost}")
        check("数据非空", (ov.get("db_rows") or 0) > 0, str(ov.get("db_rows")))

        # The installed build must write to %LOCALAPPDATA%, never beside the exe.
        local = []
        for p in exe.parent.rglob("*.db"):
            local.append(str(p))
        check("数据库不在安装目录", not local, "; ".join(local[:2]))
        appdata = Path.home() / "AppData" / "Local" / "agent-token-radar"
        db = appdata / "token_radar.db"
        check("数据库在 %LOCALAPPDATA%", db.exists(), str(db))

        # The new UI is inside the bundle, not just in the checkout.
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=8).read().decode("utf-8")
        check("安装包里带数字格式开关", 'id="numSwitch"' in html)
        check("安装包里带「昨天」按钮", 'data-range="yesterday"' in html)
        css = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/static/app.css", timeout=8).read().decode("utf-8")
        check("安装包里 CSS 有 num-switch", ".num-switch" in css)
        check("安装包里 CSS 保持等宽栅格", "display: grid" in css)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

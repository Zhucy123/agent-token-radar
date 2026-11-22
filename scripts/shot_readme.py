"""Capture the README screenshots from a running Agent Token Radar.

Why a script instead of hand-taken screenshots: the README promises these come
from the real app, so they must be re-taken whenever the interface changes.
Doing that by hand drifts (different window size, different scroll offset,
forgotten language switch). This drives one headless browser over CDP, scrolls
to an exact heading, and writes a fixed-size PNG -- so a re-shoot is one command
and every image matches the others.

Usage:
    python scripts/shot_readme.py --port 8771

Requires Edge or Chrome. Standard library only apart from the browser itself.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

#: 1400x938 matches the images both READMEs already reference.
WIDTH, HEIGHT = 1400, 938

#: (filename pattern, element id to align to the top, description)
SHOTS = [
    ("app-{lang}-1.png", "cards-title", "summary + trend"),
    ("app-{lang}-2.png", "model-title", "by model"),
    ("app-{lang}-3.png", "sessions-title", "top sessions"),
]

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_browser() -> str:
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    raise SystemExit("no Edge or Chrome found; install one to re-shoot")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_devtools(port: int, timeout: float = 30.0) -> str:
    """Return the webSocketDebuggerUrl of the first page target."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                targets = json.load(r)
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
            last = "no page target yet"
        except Exception as exc:                     # noqa: BLE001 - retried
            last = str(exc)
        time.sleep(0.4)
    raise SystemExit(f"devtools never became ready: {last}")


class CDP:
    """Minimal CDP client over a raw socket.

    No third-party websocket library: this project keeps to the standard
    library, and the framing needed here is a few dozen lines.
    """

    def __init__(self, ws_url: str) -> None:
        rest = ws_url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=60)
        key = base64.b64encode(b"agent-token-radar").decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("websocket handshake failed")
            buf += chunk
        if b"101" not in buf.split(b"\r\n", 1)[0]:
            raise SystemExit(f"websocket refused: {buf[:200]!r}")
        self._id = 0

    def _send_frame(self, payload: bytes) -> None:
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        mask = b"\x01\x02\x03\x04"
        header += mask
        self.sock.sendall(bytes(header)
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise SystemExit("websocket closed")
            out += chunk
        return out

    def _recv_frame(self) -> bytes:
        b0, b1 = self._read_exact(2)
        length = b1 & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8), "big")
        payload = self._read_exact(length)
        opcode = b0 & 0x0F
        if opcode == 0x8:
            raise SystemExit("websocket closed by browser")
        if opcode == 0x9:
            self._send_frame(payload)
            return self._recv_frame()
        return payload

    def call(self, method: str, **params) -> dict:
        self._id += 1
        msg = json.dumps({"id": self._id, "method": method,
                          "params": params}).encode()
        self._send_frame(msg)
        while True:
            data = json.loads(self._recv_frame())
            if data.get("id") == self._id:
                if "error" in data:
                    raise SystemExit(f"{method} failed: {data['error']}")
                return data.get("result", {})

    def js(self, expression: str):
        res = self.call("Runtime.evaluate", expression=expression,
                        returnByValue=True, awaitPromise=True)
        return res.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _shoot(browser: str, port: int, lang: str, anchor: str, out: Path) -> None:
    profile = Path(tempfile.mkdtemp(prefix="atr-shot-"))
    devtools = _free_port()
    proc = subprocess.Popen(
        [browser,
         "--headless=new",
         "--disable-gpu",
         "--hide-scrollbars",
         f"--remote-debugging-port={devtools}",
         f"--user-data-dir={profile}",
         f"--window-size={WIDTH},{HEIGHT}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        cdp = CDP(_wait_devtools(devtools))
        try:
            cdp.call("Page.enable")
            cdp.call("Emulation.setDeviceMetricsOverride",
                     width=WIDTH, height=HEIGHT,
                     deviceScaleFactor=1, mobile=False)
            # The language is persisted in localStorage, so it has to be set
            # before the app reads it -- hence navigate, set, reload.
            cdp.call("Page.navigate", url=f"http://127.0.0.1:{port}/")
            time.sleep(2.5)
            cdp.js(f"localStorage.setItem('atr.lang', {json.dumps(lang)})")
            cdp.call("Page.reload")
            time.sleep(3.5)

            # Wait until the tables are actually populated: the app fetches
            # before it renders, and a screenshot of empty tbody is useless.
            for _ in range(60):
                rows = cdp.js("document.querySelectorAll('tbody tr').length") or 0
                if rows >= 3:
                    break
                time.sleep(0.5)

            # Align the heading just under the sticky topbar + jump bar. The
            # heading has to clear BOTH bars: leaving it 12px below the jump
            # bar cut the section title off behind it in the first attempt.
            cdp.js(f"""
                (() => {{
                  const el = document.getElementById({json.dumps(anchor)});
                  if (!el) return 'missing';
                  const bars = document.querySelectorAll(
                    '.topbar, .section-nav, nav');
                  let offset = 0;
                  for (const b of bars) {{
                    const r = b.getBoundingClientRect();
                    if (r.height > 0 && r.top < 160) offset = Math.max(offset, r.bottom);
                  }}
                  const y = el.getBoundingClientRect().top + window.scrollY
                            - offset - 34;
                  window.scrollTo(0, Math.max(0, y));
                  return 'ok';
                }})()
            """)
            time.sleep(1.2)

            shot = cdp.call("Page.captureScreenshot", format="png")
            out.write_bytes(base64.b64decode(shot["data"]))
        finally:
            cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--lang", default="both", choices=["zh", "en", "both"])
    args = ap.parse_args()

    browser = _find_browser()
    langs = ["zh", "en"] if args.lang == "both" else [args.lang]
    DOCS.mkdir(exist_ok=True)
    for lang in langs:
        for pattern, anchor, label in SHOTS:
            out = DOCS / pattern.format(lang=lang)
            _shoot(browser, args.port, lang, anchor, out)
            print(f"  {out.name}  {out.stat().st_size // 1024}KB  ({label}, {lang})")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

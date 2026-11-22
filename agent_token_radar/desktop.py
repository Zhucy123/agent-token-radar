"""Desktop window (pywebview + embedded web server).

This is what the Windows ``.exe`` runs. It starts the same FastAPI app the
``serve`` command uses, in a background thread on a free localhost port, then
opens ONE native window pointed at it.

Why a separate module instead of teaching ``serve`` a ``--gui`` flag:

* ``serve`` must keep working headless (CI, a server box, ``nohup``). Importing
  pywebview there would make the Web dependency mandatory for everyone.
* A packaged single-file exe resolves its data directory differently from a
  checkout, and that difference belongs in one obvious place.

Design decisions that are easy to get wrong:

* **Bound to 127.0.0.1 only.** The dashboard shows every agent's usage on the
  machine; it must never be reachable from the network.
* **Port 0, not 8770.** A desktop app should not fight the CLI for a port, and
  the user never reads the URL, so a random free port is strictly better.
* **Single instance.** Two copies would scan concurrently and write the same
  SQLite file. A second launch focuses the first window instead.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("agent_token_radar.desktop")

APP_TITLE = "Agent Token Radar"
# Below this the tables stop being readable; above it there is nothing to gain
# (the layout already caps its own width).
WINDOW_MIN = (880, 600)
WINDOW_DEFAULT = (1280, 860)

# Held for the process lifetime so the named mutex stays owned.
_MUTEX: list[object] = []


def _pick_port(preferred: int | None = None) -> int:
    """Choose the localhost port, with the same fall-through as ``serve``.

    Preferred port first (8770 by default), and if it is taken move to the next
    one -- 8771, 8772, … up to 40 tries. Reusing cli's probe rather than a bare
    ``bind(:0)`` matters: the probe is the thing that knows Windows'
    ``SO_REUSEADDR`` has the opposite meaning to POSIX, and a desktop launch
    should not behave differently from a CLI one when 8770 is already busy.

    ``port=0`` is the last resort: let the OS assign something, which cannot
    collide.
    """
    from .cli import DEFAULT_PORT, _free_port

    start = preferred if preferred is not None else DEFAULT_PORT
    got = _free_port("127.0.0.1", start)
    if got is not None:
        return got
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until_up(url: str, timeout: float = 25.0) -> bool:
    """Block until the server answers, so the window never shows a blank page."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - connection refused while starting
            time.sleep(0.08)
    return False


def _start_server(port: int, scan_interval: int) -> threading.Thread:
    """Run uvicorn in a daemon thread."""
    import uvicorn

    from .web import create_app

    app = create_app(scan_interval=scan_interval)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        # The window and the server share one process; a reloader would spawn a
        # second copy and break single-instance behaviour.
        reload=False,
    )
    server = uvicorn.Server(config)

    def run() -> None:
        try:
            server.run()
        except Exception:  # noqa: BLE001 - log it; the window will report
            logger.exception("embedded server stopped")

    t = threading.Thread(target=run, name="atr-uvicorn", daemon=True)
    t.start()
    return t


def _claim_single_instance() -> bool:
    """True when this process is the first instance.

    A named kernel mutex is the cheapest reliable check on Windows. Elsewhere
    there is no desktop build, so it always answers True.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Global\\AgentTokenRadarWindow")
        already = kernel32.GetLastError() == 183      # ERROR_ALREADY_EXISTS
        if not already:
            _MUTEX.append(handle)                    # keep it owned
        return not already
    except Exception:  # noqa: BLE001 - never block startup on this
        return True


def _focus_existing_window() -> bool:
    """Raise a previously opened window; False when none was found."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)               # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _window_geometry() -> tuple[int, int]:
    """Size for the window: remembered if valid, else fitted to the screen.

    A 1280x860 default is wrong on a 1366x768 laptop (the window would hang off
    the taskbar) and needlessly small on a 4K monitor. Ask the OS for the usable
    desktop area and size to a fraction of it, then clamp to our own minimum.
    """
    stored = _load_geometry()
    if stored:
        return _clamp_to_screen(*stored)
    return _clamp_to_screen(WINDOW_DEFAULT[0], WINDOW_DEFAULT[1])


def _screen_work_area() -> tuple[int, int] | None:
    """Usable desktop size in LOGICAL pixels, excluding the taskbar.

    The unit matters. pywebview's Windows backend sizes the window in logical
    pixels (it is per-monitor DPI aware and converts internally), while
    ``SystemParametersInfoW`` reports the work area in *physical* pixels. Using
    the raw number on a 150%-scaled 3840x2160 display yields ``3532x1920``
    logical -- which becomes 5298x2880 physical, i.e. a window nearly three
    times the screen. Divide by the DPI scale first.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # SPI_GETWORKAREA = 0x0030
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(rect), 0
        ):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            scale = _dpi_scale()
            if scale > 1.0:
                width = int(width / scale)
                height = int(height / scale)
            return (width, height)
    except Exception:  # noqa: BLE001
        pass
    return None


def _dpi_scale() -> float:
    """DPI scale factor of the primary display (1.0 = 96 dpi = 100%)."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hdc = user32.GetDC(0)
        if not hdc:
            return 1.0
        try:
            # LOGPIXELSX = 88
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
        finally:
            user32.ReleaseDC(0, hdc)
        return dpi / 96.0 if dpi else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def _clamp_to_screen(width: int, height: int) -> tuple[int, int]:
    """Never open a window larger than the screen it appears on."""
    area = _screen_work_area()
    if not area:
        return (max(WINDOW_MIN[0], width), max(WINDOW_MIN[1], height))
    avail_w, avail_h = area
    # Leave a margin so the window does not look maximised and the drop shadow
    # stays visible.
    max_w = max(WINDOW_MIN[0], int(avail_w * 0.92))
    max_h = max(WINDOW_MIN[1], int(avail_h * 0.92))
    return (min(max(width, WINDOW_MIN[0]), max_w),
            min(max(height, WINDOW_MIN[1]), max_h))


def _geometry_file() -> Path:
    from .paths import user_state_dir

    return user_state_dir() / "window.json"


def _load_geometry() -> tuple[int, int] | None:
    try:
        raw = json.loads(_geometry_file().read_text(encoding="utf-8"))
        w, h = int(raw["width"]), int(raw["height"])
        if w >= WINDOW_MIN[0] and h >= WINDOW_MIN[1]:
            return (w, h)
    except Exception:  # noqa: BLE001 - a corrupt file must not block startup
        pass
    return None


def _save_geometry(width: int, height: int) -> None:
    try:
        d = _geometry_file()
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(json.dumps({"width": int(width), "height": int(height)}),
                     encoding="utf-8")
    except Exception:  # noqa: BLE001 - remembering the size is a nicety
        pass


def run(scan_interval: int = 30, dev_url: str | None = None,
        preferred_port: int | None = None) -> int:
    """Open the desktop window. Returns a process exit code.

    ``dev_url`` points the window at an already-running dev server instead of
    starting one -- handy while editing the frontend.
    """
    try:
        import webview
    except ImportError:
        sys.stderr.write(
            "the desktop window needs pywebview:  pip install pywebview\n"
            "otherwise use:  agent-token-radar serve\n"
        )
        return 2

    if not _claim_single_instance():
        # Second launch: bring the first window forward, then leave quietly.
        _focus_existing_window()
        return 0

    if dev_url:
        url = dev_url
        if "app=1" not in url:
            url += ("&" if "?" in url else "?") + "app=1"
    else:
        port = _pick_port(preferred_port)
        _start_server(port, scan_interval)
        # `app=1` tells the page it is inside the desktop window, which changes
        # only the chrome (padding, scrollbars) -- never the data.
        url = f"http://127.0.0.1:{port}/?app=1"
        if not _wait_until_up(f"http://127.0.0.1:{port}/"):
            sys.stderr.write("the local dashboard did not start; see the log above\n")
            return 3

    width, height = _window_geometry()
    window = webview.create_window(
        APP_TITLE,
        url,
        width=width,
        height=height,
        min_size=WINDOW_MIN,
        # Frameless would break the snap gestures users expect from a normal
        # window; keep the native frame.
        frameless=False,
        easy_drag=False,
        background_color="#0b1220",
    )

    def remember_size() -> None:
        """Persist the size the user left the window at.

        pywebview exposes no "resized" event on every backend, so this samples
        when the window is closing. A missed resize is harmless: the next launch
        simply re-fits to the screen.
        """
        try:
            w, h = window.width, window.height
            if w and h:
                _save_geometry(w, h)
        except Exception:  # noqa: BLE001
            pass

    try:
        webview.start()
    finally:
        remember_size()
        logger.info("window closed")
    return 0

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Windows desktop app.

Build with:  pyinstaller --noconfirm --clean agent-token-radar.spec

Two things here are load-bearing and easy to miss:

* ``hiddenimports`` gets pywebview's own PyInstaller hook (it ships one at
  ``webview/__pyinstaller``). Without it the native WebView2 DLLs are left out
  of the bundle and the window fails at runtime with "WebView2 not found".
* ``datas`` must carry ``static/``. The dashboard is served from real files, so
  a bundle that only contains .py files 404s on /static/app.css and the window
  shows an unstyled page.
* ``datas`` must also carry ``pricing.json``. ``pricing.py`` reads it from a real
  path next to the module, so a bundle without it loads an empty table and every
  model quietly reports "no public price". It degrades silently, not loudly.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

datas = []
# The single-page dashboard: index.html, app.css, app.js.
datas += [(str(ROOT / "agent_token_radar" / "static"), "agent_token_radar/static")]
# Official list prices, read from disk at import time by pricing.py.
datas += [(str(ROOT / "agent_token_radar" / "pricing.json"), "agent_token_radar")]
# pywebview's native WebView2 assemblies and JS bridge.
datas += collect_data_files("webview")

hiddenimports = [
    "webview.platforms.winforms",     # the Windows backend
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]
# Every adapter module. They are discovered at runtime by walking the package,
# so nothing imports them statically and PyInstaller cannot infer them.
# Measured: without this the packaged app reported /api/adapters == [].
hiddenimports += collect_submodules("agent_token_radar.adapters")

a = Analysis(
    [str(ROOT / "agent_token_radar" / "__app_entry__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Python brings in a lot of stdlib surface this app never touches. Excluding
    # it keeps the single-file exe from ballooning past ~40 MB.
    excludes=[
        "tkinter", "test", "unittest", "pydoc", "doctest",
        "numpy", "pandas", "matplotlib", "scipy", "PIL",
        "setuptools", "pip", "wheel",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AgentTokenRadar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed app: no console window behind the UI.
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "build" / "app.ico"),
)

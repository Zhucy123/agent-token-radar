# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Windows desktop app (onedir).

Build with:  python -m PyInstaller --noconfirm agent-token-radar-dir.spec

Three details here are load-bearing and easy to miss:

* ``collect_submodules("agent_token_radar.adapters")`` carries all 25 adapters.
  Adapter discovery walks the package at runtime, so a static import graph does
  not mention them and PyInstaller would leave them out. Measured: without this
  the packaged app reported ``/api/adapters == []``, lost every official icon
  and display name, and could only show agents that already had database rows.

* ``datas`` must carry ``static/``. The dashboard is served from real files, so
  a bundle that only contains .py files 404s on /static/app.css and the window
  shows an unstyled page.

* ``datas`` must also carry ``pricing.json``. ``pricing.py`` loads it from a real
  path next to the module (``Path(__file__).with_name``), not through
  ``importlib.resources``, so a bundle without it loads an empty price table:
  every model silently reports "no public price" and the whole cost column
  disappears. It fails soft, which is exactly why it went unnoticed.

* pywebview's own PyInstaller hook provides the native WebView2 assemblies.
  Without it the window fails at runtime with "WebView2 not found".

* ``EXE(version=...)`` must be set. Without it the built exe carried an EMPTY
  version resource: right-click -> Properties -> Details showed no version, no
  product name, no publisher. Setup.exe had 0.0.4.0, so the omission hid in
  plain sight on the file people actually run. Windows file identification,
  add/remove listings and enterprise allow-listing all read this resource. The
  number comes from ``agent_token_radar.__version__`` via build/make_version_info.py,
  not from a literal here, so there is still one source of truth.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# Generate the version resource from the package's own __version__. The
# generator lives in the package rather than build/, because build/ is
# gitignored as PyInstaller scratch space.
sys.path.insert(0, str(ROOT))
from agent_token_radar._make_version_info import version_file  # noqa: E402

VERSION_RESOURCE = version_file()

datas = [(str(ROOT / "agent_token_radar" / "static"), "agent_token_radar/static")]
# Official list prices, read from disk at import time by pricing.py.
datas += [(str(ROOT / "agent_token_radar" / "pricing.json"), "agent_token_radar")]
datas += collect_data_files("webview")

hiddenimports = [
    "webview.platforms.winforms",
    "uvicorn.logging", "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]
# Every adapter module. They are discovered at runtime, so nothing imports them
# statically and PyInstaller cannot infer them.
hiddenimports += collect_submodules("agent_token_radar.adapters")

a = Analysis(
    [str(ROOT / "agent_token_radar" / "__app_entry__.py")],
    pathex=[str(ROOT)], binaries=[], datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter","test","unittest","pydoc","doctest","numpy","pandas",
              "matplotlib","scipy","PIL","setuptools","pip","wheel"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="AgentTokenRadar", debug=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          version=VERSION_RESOURCE,
          icon=str(ROOT / "build" / "app.ico"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="AgentTokenRadar")

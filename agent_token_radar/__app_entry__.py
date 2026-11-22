"""Frozen-build entry point for the Windows executable.

PyInstaller needs a plain script to launch; pointing it at ``-m
agent_token_radar`` is awkward because the package's own ``__main__`` parses CLI
arguments that a double-clicked exe never passes. This module is the desktop
app, nothing else: no argv parsing, no console output, just the window.

Two things must happen before anything imports ``clr`` (which pywebview does
during ``webview.start()``):

1. A windowed (``--noconsole``) build has ``sys.stdout``/``sys.stderr`` set to
   ``None``. Any stray ``print`` in a dependency raises ``AttributeError`` on
   this Python build, so they are pointed at ``os.devnull`` first.

2. The .NET CLR cannot load an assembly from a path containing non-ASCII
   characters. ``clr_loader`` passes the path as UTF-8, the .NET Framework
   resolves it as ANSI, and the result is
   ``Failed to resolve Python.Runtime.Loader.Initialize``. This was verified by
   moving the *same* DLLs to ``C:\\atr_ascii`` (works) and back under a Chinese
   path (fails). ``_prepare_clr()`` copies the two natives to a short ASCII
   directory and prepends it to the DLL search path.

   This runs here rather than in a PyInstaller runtime hook because the hook
   was not reliably executed in this build; doing it in the entry script is
   order-guaranteed.
"""

from __future__ import annotations

import os
import shutil
import sys

STAGE_DIR_NAME = "atr-clr"
CLR_FILES = ("ClrLoader.dll", "Python.Runtime.dll", "Python.Runtime.deps.json")


def _silence_missing_console() -> None:
    """A --noconsole build has sys.stdout == None; libraries still print."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _bundle_dirs() -> list[str]:
    """Directories in the frozen bundle that hold the CLR natives."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return []
    out = []
    for rel in (
        ("clr_loader", "ffi", "dlls", "amd64"),
        ("pythonnet", "runtime"),
        ("pythonnet",),
        ("clr_loader",),
    ):
        p = os.path.join(base, *rel)
        if os.path.isdir(p):
            out.append(p)
    return out


def _ascii_stage_dir() -> str | None:
    """A short, ASCII-only, WRITABLE directory to hold the CLR natives.

    The candidate list is ordered by "already proven writable from inside a
    frozen process", not by path length:

    * ``C:\\`` is shortest but a standard user cannot create directories in it.
    * ``%LOCALAPPDATA%`` is ASCII but was observed to refuse writes from the
      packaged exe on this machine.
    * The exe's own folder is where a frozen app always has write access, so it
      is the reliable fallback. It is only non-ASCII when the user installed to
      such a path -- which is exactly the case this function exists for, so the
      ASCII check below still filters it out.
    """
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, STAGE_DIR_NAME))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates.append(os.path.join(exe_dir, STAGE_DIR_NAME))
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if tmp:
        candidates.append(os.path.join(tmp, STAGE_DIR_NAME))
    candidates.append(os.path.join(os.environ.get("SystemDrive", "C:") + os.sep,
                                   STAGE_DIR_NAME))

    for c in candidates:
        try:
            c.encode("ascii")
        except UnicodeEncodeError:
            continue        # a localized path does not fix this bug
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, ".write-probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return c
        except OSError:
            continue
    return None


def _prepare_clr() -> None:
    """Stage the CLR natives in an ASCII path and point the loader at them.

    No-op outside a frozen build, and harmless when the install path is already
    ASCII -- one code path is easier to reason about than a conditional one.
    """
    if sys.platform != "win32":
        return
    dirs = _bundle_dirs()
    if not dirs:
        return

    sources = []
    for d in dirs:
        for name in CLR_FILES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                sources.append(p)

    search: list[str] = []
    staged = _ascii_stage_dir()
    if sources and staged:
        try:
            os.makedirs(staged, exist_ok=True)
            for src in sources:
                shutil.copy2(src, os.path.join(staged, os.path.basename(src)))
            search.append(staged)
        except OSError:
            pass
    search.extend(dirs)

    for d in reversed(search):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    try:
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(search[0])
    except Exception:  # noqa: BLE001 - PATH already covers the normal case
        pass


def main() -> int:
    _silence_missing_console()
    _prepare_clr()
    # Imported only after the fixes above: importing pywebview pulls in clr.
    from agent_token_radar.desktop import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())



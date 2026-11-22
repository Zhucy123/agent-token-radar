"""Shared read-only helpers for adapters. Not an adapter itself (underscore
prefix means :mod:`registry` skips it)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..base import Source


def glob_sources(
    roots: Iterable[Path],
    pattern: str,
    *,
    kind: str,
    agent: str,
    recursive: bool = True,
) -> list[Source]:
    """Return existing files matching ``pattern`` under each root.

    Strictly read-only: only ``glob``/``stat``, never write. Missing roots and
    permission errors are silently skipped. Results are de-duplicated by
    resolved path (the same file can be reachable through several roots).
    """
    seen: set[str] = set()
    out: list[Source] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        try:
            matches = root.rglob(pattern) if recursive else root.glob(pattern)
            for p in matches:
                try:
                    key = str(p.resolve()).lower()
                    if key in seen or not p.is_file():
                        continue
                    seen.add(key)
                    out.append(Source(path=p, kind=kind, agent=agent))
                except OSError:
                    continue
        except OSError:
            continue
    return out


def newest_generation_per_dir(paths: Iterable[Path]) -> list[Path]:
    """For DSH: keep only the highest ``session.vN.jsonl[.zstd]`` per folder.

    Older generations (v0/v2/v3) are left beside the current one by DSH when
    it upgrades a session; reading all of them would double-count.
    """
    best: dict[Path, tuple[int, Path]] = {}
    for p in paths:
        name = p.name
        # session.v4.jsonl.zstd / session.v4.jsonl / session.jsonl
        import re

        m = re.search(r"session\.v(\d+)\.jsonl", name)
        gen = int(m.group(1)) if m else 0
        parent = p.parent
        cur = best.get(parent)
        if cur is None or gen > cur[0] or (gen == cur[0] and len(str(p)) > len(str(cur[1]))):
            best[parent] = (gen, p)
    return [v[1] for v in best.values()]

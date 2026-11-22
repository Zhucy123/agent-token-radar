"""Automatic adapter discovery.

Adding a new tool requires exactly ONE new file in ``adapters/`` containing a
concrete :class:`~agent_token_radar.base.BaseAdapter` subclass. It is imported
and registered automatically -- no central list to update, no decorator, no
manual wiring.

A module is skipped (with a warning) if it fails to import, so a broken
third-party adapter can never take down the whole scan.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Iterable

from . import adapters as _adapters_pkg
from .base import BaseAdapter

logger = logging.getLogger("agent_token_radar.registry")


def _adapter_module_names() -> list[str]:
    """Module names under ``adapters/``, found even inside a frozen bundle.

    ``pkgutil.iter_modules`` walks the filesystem. A PyInstaller build packs the
    adapter modules into the PYZ archive instead of leaving ``.py`` files on
    disk, so that call returns nothing and the app comes up with ZERO adapters
    -- measured: the packaged exe reported ``/api/adapters == []`` and could
    only display the agents that happened to have rows in an existing database.

    ``pkgutil.iter_modules`` is still the primary path because it also picks up
    a third-party adapter dropped into the directory. It is only when it finds
    nothing that the frozen bundle's own contents are consulted.
    """
    names = [
        info.name
        for info in pkgutil.iter_modules(_adapters_pkg.__path__)
        if not info.name.startswith("_") and not info.ispkg
    ]
    if names:
        return names

    # Frozen fallback: the PYZ table of contents lists every module that was
    # bundled, so filter it down to this package.
    try:
        import sys

        archive = getattr(sys.modules["__main__"], "__loader__", None)
        toc = getattr(archive, "toc", None)
        if toc:
            prefix = f"{_adapters_pkg.__name__}."
            return sorted(
                entry[len(prefix):]
                for entry in toc
                if isinstance(entry, str)
                and entry.startswith(prefix)
                and "." not in entry[len(prefix):]
            )
    except Exception:  # noqa: BLE001 - never block discovery on this
        pass
    return []


def discover_adapter_classes() -> list[type[BaseAdapter]]:
    """Import every ``adapters/*.py`` module and collect concrete subclasses."""
    found: dict[str, type[BaseAdapter]] = {}
    for stem in _adapter_module_names():
        mod_name = f"{_adapters_pkg.__name__}.{stem}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:  # noqa: BLE001 - isolate a broken adapter
            logger.warning("adapter module %s failed to import: %s: %s", mod_name, type(e).__name__, e)
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if not issubclass(obj, BaseAdapter) or obj is BaseAdapter:
                continue
            # Skip private/non-exported classes: they are shared bases or mixins
            # (e.g. ``_WorkBuddyBase``) imported by the real adapters, never
            # registered on their own.
            if obj.__name__.startswith("_"):
                continue
            # Check module ownership BEFORE abstractness: a shared abstract base
            # (``JsonlAdapter``, ``_WorkBuddyBase``) is imported into many
            # adapter modules, so it always shows up in ``getmembers``. It is
            # not "an abstract adapter that failed", it simply is not defined
            # here -- skipping it silently keeps the log honest.
            if obj.__module__ != mod_name:
                continue  # imported from elsewhere, not defined in this module
            if inspect.isabstract(obj):
                logger.warning("adapter %s.%s is abstract; skipped", mod_name, obj.__name__)
                continue
            name = getattr(obj, "name", "") or stem
            if not name:
                logger.warning("adapter %s.%s has no name; skipped", mod_name, obj.__name__)
                continue
            if name in found:
                logger.warning(
                    "duplicate adapter name %r (%s and %s); keeping the first",
                    name, found[name].__name__, obj.__name__,
                )
                continue
            found[name] = obj
    return list(found.values())


def instantiate(
    classes: Iterable[type[BaseAdapter]] | None = None,
    *,
    logger_: logging.Logger | None = None,
) -> list[BaseAdapter]:
    """Instantiate adapters. Unknown/broken constructors are skipped."""
    out: list[BaseAdapter] = []
    for cls in classes if classes is not None else discover_adapter_classes():
        try:
            out.append(cls(logger=logger_ or logger))
        except Exception as e:  # noqa: BLE001
            logger.warning("cannot instantiate %s: %s: %s", cls.__name__, type(e).__name__, e)
    return out


def all_adapters() -> list[BaseAdapter]:
    """Convenience: discovered + instantiated, ready to scan."""
    return instantiate()

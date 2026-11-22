"""FastAPI backend for the local single-page dashboard.

Endpoints (all read-only JSON except POST /api/refresh which triggers a scan):
    GET  /                       -> index.html
    GET  /api/overview?range=..  -> summary cards + by-agent/model/day + top sessions
    GET  /api/trend?range=..     -> daily series split by agent (stacked chart)
    GET  /api/scan/status        -> last scan info / background state
    POST /api/refresh            -> manual incremental scan

A daemon thread runs an incremental scan every ``scan_interval`` seconds.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .cli import _local_tz_offset_ms, _range_bounds
from .i18n import DEFAULT_LANG, STRINGS, SUPPORTED, normalize_lang
from .icons import icons_json
from .scanner import Scanner
from .store import Store

from . import __version__
from . import updates

logger = logging.getLogger("agent_token_radar.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _local_offset_ms() -> int:
    off = dt.datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() * 1000) if off else 0


def _bounds(rng: str) -> tuple[int | None, int | None]:
    try:
        return _range_bounds(rng)
    except ValueError:
        return None, None


#: Set AGENT_TOKEN_RADAR_NO_UPDATE_CHECK=1 to disable the startup check
#: entirely. Documented because an offline install should not retry every launch.
_NO_UPDATE_ENV = "AGENT_TOKEN_RADAR_NO_UPDATE_CHECK"


def _start_update_check() -> None:
    """Look for a newer release on a daemon thread.

    Runs in the background for two reasons: the request must never delay
    startup, and a network timeout must never be visible as a slow page. The
    daily cache inside :func:`updates.check_for_update` does the real work here
    -- a warm cache makes this a file read that returns immediately.
    """
    if os.environ.get(_NO_UPDATE_ENV, "").strip() not in ("", "0", "false"):
        logger.debug("update check disabled by %s", _NO_UPDATE_ENV)
        return

    def _run() -> None:
        try:
            updates.check_for_update(__version__)
        except Exception:  # pragma: no cover - must never kill the thread host
            logger.debug("startup update check failed", exc_info=True)

    threading.Thread(target=_run, name="update-check", daemon=True).start()


class DashboardState:
    """Owns one Store and serializes scans (manual + background)."""

    def __init__(self, scan_interval: int = 30) -> None:
        self.store = Store.default()
        self.scanner = Scanner(store=self.store)  # shares the same connection
        self.scan_interval = max(5, int(scan_interval))
        self.lock = threading.Lock()
        self.last_scan: dict[str, Any] = {"at": None, "inserted": 0, "updated": 0,
                                          "warnings": 0, "running": False}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def scan(self) -> dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            return {"skipped": "scan already in progress", **self.last_scan}
        try:
            self.last_scan["running"] = True
            rep = self.scanner.run()
            info = {
                "at": int(time.time() * 1000),
                "inserted": rep.inserted,
                "updated": rep.updated,
                "seen": rep.events_seen,
                "warnings": len(rep.warnings),
                "running": False,
                "adapters": [
                    {"name": a.name, "sources": a.sources, "seen": a.events_seen,
                     "inserted": a.inserted, "updated": a.updated,
                     "warnings": len(a.warnings), "error": a.error}
                    for a in rep.adapters
                ],
            }
            self.last_scan = info
            return info
        except Exception as e:  # noqa: BLE001
            logger.exception("scan failed")
            return {"error": f"{type(e).__name__}: {e}", "running": False}
        finally:
            self.last_scan["running"] = False
            self.lock.release()

    def start(self) -> None:
        def loop() -> None:
            # First pass at startup so the page is populated immediately.
            self.scan()
            while not self._stop.wait(self.scan_interval):
                self.scan()

        self._thread = threading.Thread(target=loop, name="atr-bg-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.store.close()

    def overview(self, rng: str) -> dict[str, Any]:
        since, until = _bounds(rng)
        s = self.store
        # One scan, three views. totals/by_agent/by_model used to be three
        # separate queries, each re-scanning usage_events and re-evaluating the
        # cost expression per row -- on a whole-history report that was ~800 ms
        # of duplicated work for the same numbers.
        snap = s.overview_snapshot(since_ms=since, until_ms=until)
        return {
            "range": rng,
            "generated_at": int(time.time() * 1000),
            "totals": snap["totals"],
            "by_agent": snap["by_agent"],
            # Grouped by (model, provider, agent): the same model served by a
            # different backend is different usage, and the dashboard shows one
            # group per agent with the provider as a sub-label.
            "by_model": snap["by_model"][:400],
            "top_sessions": s.top_sessions(limit=10, since_ms=since, until_ms=until),
            # measured vs inferred, so a guessed provider is never mistaken for
            # one the gateway actually recorded.
            "provider_stats": s.provider_stats(),
            # which project the tokens were spent on, plus how much of the data
            # can answer that at all.
            "by_project": s.by_project(since_ms=since, until_ms=until),
            "project_stats": s.project_stats(),
            "db_rows": s.count(),
            # Where the money figures come from, and what they do not cover.
            # The UI must be able to say "estimated from official list prices
            # for X% of usage" next to any cost, or the number reads as a bill.
            "pricing": self._pricing_meta(),
        }

    def _pricing_meta(self) -> dict[str, Any]:
        p = getattr(self.store, "prices", None)
        if p is None:
            return {}
        return {
            "updated": p.updated,
            "currency": "USD",
            "sources": {
                k: {"url": v.get("url"), "currency": v.get("currency"),
                    "read": v.get("read")}
                for k, v in p.sources.items()
            },
            "model_count": len(p.models),
            # Time-of-day billing, so the UI can say *why* a DeepSeek figure
            # moves without anyone having changed a setting: the window is
            # applied per request from that request's own timestamp.
            "peak_hours": {k: v.as_meta() for k, v in p.peak.items()},
        }

    def trend(self, rng: str) -> dict[str, Any]:
        since, until = _bounds(rng)
        tz = _local_offset_ms()

        # Choose the bucket width from the data's actual span, not from the
        # range label. "All" is unbounded -- one bar per day would be 730 bars
        # for someone with two years of history. Daily up to ~4 months,
        # then weekly, then monthly.
        span_days = self.store.span_days(since_ms=since, until_ms=until)
        if span_days <= 130:
            bucket_days, unit = 1, "day"
        elif span_days <= 900:
            bucket_days, unit = 7, "week"
        else:
            bucket_days, unit = 30, "month"

        rows = self.store.by_day(
            since_ms=since, until_ms=until, tz_offset_ms=tz,
            bucket_days=bucket_days,
        )
        buckets = sorted({r["day"] for r in rows})
        agents = sorted({r["agent"] for r in rows})
        series: dict[str, dict[str, int]] = {a: {d: 0 for d in buckets} for a in agents}
        for r in rows:
            series[r["agent"]][r["day"]] = int(r["total_tokens"] or 0)
        return {
            "range": rng,
            "days": buckets,
            "agents": agents,
            "unit": unit,
            "bucket_days": bucket_days,
            "series": {a: [series[a][d] for d in buckets] for a in agents},
        }


def create_app(scan_interval: int = 30) -> FastAPI:
    state = DashboardState(scan_interval=scan_interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.start()
        _start_update_check()
        yield
        state.stop()

    app = FastAPI(title="Agent Token Radar", version=__version__, lifespan=lifespan)
    app.state.radar = state

    @app.get("/")
    def index() -> FileResponse:
        # The page and its scripts must be revalidated together. Without this
        # header the browser happily kept an OLD index.html while loading a NEW
        # app.js, so a column added to the table never appeared (measured: the
        # cache-hit header was served but the rendered table still had the old
        # columns). StaticFiles below sets no-cache; the entry point needs it too.
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/overview")
    def overview(range: str = Query("7d", alias="range")) -> JSONResponse:
        return JSONResponse(state.overview(range))

    @app.get("/api/trend")
    def trend(range: str = Query("7d", alias="range")) -> JSONResponse:
        return JSONResponse(state.trend(range))

    @app.get("/api/scan/status")
    def status() -> JSONResponse:
        return JSONResponse({
            **state.last_scan,
            "interval_seconds": state.scan_interval,
            "db_rows": state.store.count(),
        })

    @app.post("/api/refresh")
    def refresh() -> JSONResponse:
        return JSONResponse(state.scan())

    @app.get("/api/adapters")
    def adapters() -> JSONResponse:
        return JSONResponse([
            {"name": a.name, "display_name": a.display_name}
            for a in state.scanner.adapters
        ])

    @app.get("/api/icons")
    def icons() -> JSONResponse:
        """Inline SVG path data for every agent (official where available)."""
        return JSONResponse(icons_json())

    @app.get("/api/i18n")
    def i18n(lang: str = Query(DEFAULT_LANG)) -> JSONResponse:
        """One language's string table. Separate dicts, never a fallback chain."""
        code = normalize_lang(lang)
        return JSONResponse({
            "lang": code,
            "supported": list(SUPPORTED),
            "strings": STRINGS[code],
        })

    @app.get("/api/update")
    def update(force: bool = Query(False)) -> JSONResponse:
        """Is there a newer release? Cached for a day unless ``force``.

        This is the ONE endpoint that can reach the internet. It is isolated
        behind ``updates.check_for_update`` so a build with no network behaves
        exactly like a normal one: the call returns ``state == "unavailable"``
        and the UI simply shows nothing.
        """
        return JSONResponse(updates.check_for_update(__version__, force=force))

    if STATIC_DIR.is_dir():
        # StaticFiles sends ETag/Last-Modified but no Cache-Control, so browsers
        # fall back to heuristic caching (roughly 10% of the file's age). On a
        # local tool that means editing app.js and pressing refresh can still
        # serve the previous build. "no-cache" forces revalidation -- the ETag
        # still gives a cheap 304 when nothing changed -- so an edit is always
        # picked up on a plain refresh, with no cache-busting query string.
        class RevalidatingStaticFiles(StaticFiles):
            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                response.headers["Cache-Control"] = "no-cache"
                return response

        app.mount(
            "/static",
            RevalidatingStaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )
    return app

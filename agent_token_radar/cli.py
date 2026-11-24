"""Command-line interface.

    python -m agent_token_radar scan [--only codex,claude]
    python -m agent_token_radar stats [--range today|7d|30d|all]
                                     [--by agent|model|day|project] [--json]
    python -m agent_token_radar serve [--port 8770]

No build step, no heavy dependencies. Only ``fastapi`` + ``uvicorn`` are
needed for the ``serve`` subcommand (both already present in this environment).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from typing import Any

from .scanner import Scanner
from .store import Store
from .i18n import DEFAULT_LANG, SUPPORTED, normalize_lang
from .i18n import t as tr

#: Preferred dashboard port. If it is taken, ``serve`` walks upwards
#: (8771, 8772, …) until it finds a free one — see :func:`cmd_serve`.
DEFAULT_PORT = 8770


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _configure_stdio() -> None:
    """Make Chinese output survive a console that is not UTF-8.

    Python picks the console code page for stdout on Windows (cp936 on a
    Chinese install, cp1252 on an English one).  ``stats`` prints Chinese
    headings by default, so on an English Windows box -- or anywhere stdout is
    redirected to a file -- ``print`` raises ``UnicodeEncodeError`` and the
    whole command dies.  Re-configuring the stream to UTF-8 with a replacing
    error handler keeps the command usable and degrades a glyph to ``?``
    instead of a traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # e.g. a replaced stream in an embedding
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _range_bounds(rng: str, tz_offset_minutes: int | None = None) -> tuple[int | None, int | None]:
    """Return (since_ms, until_ms) for a named range in the LOCAL timezone."""
    if rng == "all":
        return None, None
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    # Local midnight today.
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if rng == "today":
        start, end = today, None
    elif rng == "yesterday":
        # A single past day, bounded on BOTH sides. Without the upper bound the
        # window would run to now and quietly include today's usage -- and
        # without this branch at all the caller's ValueError fallback turned
        # "yesterday" into "all time", which is what shipped once and made the
        # button look correct while the chart drew the whole history.
        start = today - dt.timedelta(days=1)
        end = today
    elif rng == "7d":
        start, end = today - dt.timedelta(days=6), None
    elif rng == "30d":
        start, end = today - dt.timedelta(days=29), None
    else:
        raise ValueError(f"unknown range: {rng}")
    since = int(start.timestamp() * 1000)
    return since, (int(end.timestamp() * 1000) if end is not None else None)


def _local_tz_offset_ms() -> int:
    """Offset to ADD to UTC ms so SQLite's unixepoch formatting yields local days."""
    off = dt.datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() * 1000) if off else 0


def fmt(n: int) -> str:
    return f"{int(n):,}"


def _hit_cell(value: Any) -> str:
    """A cache hit rate as a percentage, or a dash when it cannot be computed.

    ``None`` means the underlying row contradicts itself (it claims more cache
    reads than its own total), so there is no honest percentage. Printing
    "0.0%" there would state a fact about the data that is not true.
    """
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _table(headers: list[str], rows: list[list[str]], *, right: set[int] | None = None) -> str:
    right = right or set()
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    def line(cells: list[str], header: bool = False) -> str:
        parts = []
        for i, c in enumerate(cells):
            s = str(c)
            parts.append(s.rjust(widths[i]) if i in right else s.ljust(widths[i]))
        return "  ".join(parts).rstrip()
    sep = "  ".join("-" * w for w in widths)
    out = [line(headers, True), sep]
    out += [line([str(c) for c in r]) for r in rows]
    return "\n".join(out)


def cmd_scan(args: argparse.Namespace) -> int:
    # ``scan`` accepted --lang but never read it, so the Chinese UI still
    # printed English here. Same rule as the dashboard: Chinese mode shows
    # Chinese, and only field names that ARE the domain vocabulary (agent,
    # model, token, provider) stay English.
    zh = normalize_lang(getattr(args, "lang", None)) == "zh"
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    with Scanner() as sc:
        rep = sc.run(only=only)
        rows = [
            [a.name, str(a.sources), str(a.events_seen), str(a.inserted),
             str(a.updated), str(a.unchanged), str(len(a.warnings)),
             (a.error or "")]
            for a in rep.adapters
        ]
        print("扫描完成：" if zh else "Scan complete:")
        print(_table(
            ["AGENT", "来源" if zh else "SOURCES", "扫描" if zh else "SEEN",
             "新增" if zh else "NEW", "更新" if zh else "UPD",
             "未变" if zh else "SAME", "警告" if zh else "WARN",
             "错误" if zh else "ERROR"],
            rows, right={1, 2, 3, 4, 5, 6},
        ))
        unchanged = sum(a.unchanged for a in rep.adapters)
        if zh:
            print(f"\n新增记录：{rep.inserted}（更新：{rep.updated}，未变：{unchanged}）")
            print(f"数据库行数：{sc.store.count()}  ->  {sc.store.db_path}")
            if rep.warnings:
                print(f"警告：{len(rep.warnings)} 条（加 -v 查看详情）")
        else:
            print(f"\nTotal new records: {rep.inserted}  (updated: {rep.updated}, "
                  f"unchanged: {unchanged})")
            print(f"Database rows: {sc.store.count()}  ->  {sc.store.db_path}")
            if rep.warnings:
                print(f"Warnings: {len(rep.warnings)} (run with -v to see details)")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    lang = normalize_lang(getattr(args, "lang", None))
    zh = lang == "zh"
    since, until = _range_bounds(args.range)
    with Store.default() as s:
        t = s.totals(since_ms=since, until_ms=until)
        payload: dict[str, Any] = {"range": args.range, "totals": t}

        if args.by == "agent":
            payload["breakdown"] = s.by_agent(since_ms=since, until_ms=until)
        elif args.by == "model":
            payload["breakdown"] = s.by_model(since_ms=since, until_ms=until)
        elif args.by == "day":
            payload["breakdown"] = s.by_day(
                since_ms=since, until_ms=until,
                tz_offset_ms=_local_tz_offset_ms(),
            )
        elif args.by == "project":
            payload["breakdown"] = s.by_project(since_ms=since, until_ms=until)
            payload["project_stats"] = s.project_stats()

        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        head = (
            f"Token 用量  [{args.range}]   （费用为按官方标价的参考估算）"
            if zh else
            f"Token usage  [{args.range}]   (cost is an estimate from official list prices)"
        )
        print(head)
        # A NULL hit rate means the data contradicts itself; print a dash
        # rather than a fabricated 0%.
        raw_hit = t.get("cache_hit")
        hit_cell = ("—" if raw_hit is None
                    else f"{float(raw_hit) * 100:.1f}%")
        # Cost and coverage belong in the human-readable report: the dashboard
        # has shown them all along, and a CLI that omits the money makes the
        # text report quietly less complete than the web one.
        cost = t.get("cost_usd")
        ratio = float(t.get("priced_ratio") or 0.0)
        cost_cell = "—" if cost is None else f"${cost:,.2f}"
        metric_rows = [
            ["events" if not zh else "记录数", fmt(t["events"])],
            ["total_tokens" if not zh else "总 token", fmt(t["total_tokens"])],
            ["cache_hit" if not zh else "缓存命中率", hit_cell],
            ["input_tokens" if not zh else "输入 token", fmt(t["uncached_input_tokens"])],
            ["cache_read_tokens" if not zh else "缓存读 token", fmt(t["cache_read_tokens"])],
            ["cache_write_tokens" if not zh else "缓存写 token", fmt(t["cache_write_tokens"])],
            ["output_tokens" if not zh else "输出 token", fmt(t["output_tokens"])],
            ["reasoning_tokens" if not zh else "推理 token", fmt(t["reasoning_tokens"])],
            ["cost" if not zh else "参考费用", cost_cell],
            ["coverage" if not zh else "费用覆盖率", f"{ratio * 100:.1f}%"],
        ]
        print(_table(
            ["METRIC" if not zh else "指标", "VALUE" if not zh else "数值"],
            metric_rows, right={1}))

        if args.by == "agent":
            b = payload["breakdown"]
            print("\n" + ("按 Agent 分布：" if zh else "By Agent:"))
            print(_table(
                ["AGENT", "EVENTS", "TOTAL", "HIT", "INPUT", "CACHE READ", "OUTPUT"],
                [[x["agent"], fmt(x["events"]), fmt(x["total_tokens"]),
                  _hit_cell(x.get("cache_hit")),
                  fmt(x["uncached_input_tokens"]),
                  fmt(x["cache_read_tokens"]),
                  fmt(x["output_tokens"])] for x in b],
                right={1, 2, 3, 4, 5, 6},
            ))
        elif args.by == "model":
            b = payload["breakdown"]
            print("\n" + ("按 Agent / Model / Provider 细分：" if zh
                          else "By Agent / Model / Provider:"))
            print(_table(
                ["AGENT", "MODEL", "PROVIDER", "EVENTS", "TOTAL", "HIT", "INPUT"],
                [[x["agent"], (x["model"] or "(unknown)")[:38],
                  x.get("provider") or "-", fmt(x["events"]),
                  fmt(x["total_tokens"]),
                  _hit_cell(x.get("cache_hit")),
                  fmt(x["uncached_input_tokens"])] for x in b[:60]],
                right={3, 4, 5, 6},
            ))
        elif args.by == "day":
            b = payload["breakdown"]
            print("\n" + ("按日分布：" if zh else "By day:"))
            print(_table(
                ["DAY", "AGENT", "TOTAL", "CACHE READ", "OUTPUT"],
                [[x["day"], x["agent"], fmt(x["total_tokens"]),
                  fmt(x["cache_read_tokens"]),
                  fmt(x["output_tokens"])] for x in b],
                right={2, 3, 4},
            ))
        elif args.by == "project":
            b = payload["breakdown"]
            print("\n" + ("按项目分布（Agent 的工作目录）：" if zh
                          else "By project (the Agent's working directory):"))
            print(_table(
                ["PROJECT", "AGENT", "EVENTS", "TOTAL", "INPUT", "OUTPUT"],
                [[(x["project"] or "-")[:46], x["agent"], fmt(x["events"]),
                  fmt(x["total_tokens"]),
                  fmt(x["uncached_input_tokens"]),
                  fmt(x["output_tokens"])] for x in b[:60]],
                right={2, 3, 4, 5},
            ))
            st = payload.get("project_stats") or {}
            if st.get("total"):
                print("\n" + (
                    f"有 {fmt(st['with_project'])} / {fmt(st['total'])} 条记录带项目目录"
                    if zh else
                    f"{fmt(st['with_project'])} of {fmt(st['total'])} records carry a project directory"
                ))
        else:
            print("\n" + (
                "（提示：加 --by agent|model|day|project 查看分组明细）" if zh
                else "(hint: add --by agent|model|day|project for a breakdown)"
            ))
    return 0


def _port_is_free(host: str, port: int) -> bool:
    """True when ``host:port`` can actually be bound right now.

    ``uvicorn`` reports a busy port as a raw ``[Errno 10048]`` traceback, which
    tells the user nothing actionable and looks like a crash. Probe first so we
    can move to the next port instead.

    The socket options are the whole point and differ per platform:

    * **Windows**: ``SO_REUSEADDR`` there means "let me bind a port even while
      another socket is listening on it", the opposite of POSIX. Setting it
      makes every probe succeed, so the port is chosen and ``uvicorn`` then
      dies with the very ``10048`` this function exists to avoid. Windows wants
      ``SO_EXCLUSIVEADDRUSE`` instead, which fails loudly on a live listener.
    * **POSIX**: ``SO_REUSEADDR`` is correct and necessary, so a socket in
      ``TIME_WAIT`` is not mistaken for a live server.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif hasattr(socket, "SO_REUSEADDR"):  # POSIX
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _free_port(host: str, start: int, tries: int = 40) -> int | None:
    """First bindable port at or after ``start`` (``None`` if all are taken)."""
    for candidate in range(start, start + tries):
        if _port_is_free(host, candidate):
            return candidate
    return None


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("serve needs fastapi + uvicorn:  pip install fastapi uvicorn", file=sys.stderr)
        return 1
    from .web import create_app

    host, port = args.host, args.port

    # Fixed default port with automatic fall-through: 8770, else 8771, 8772 …
    # A busy port is normal here (an earlier `serve` still running, or another
    # app using 8770), so picking the next one is the behaviour, not an error.
    # `uvicorn` would otherwise die with a bare `[Errno 10048]` traceback.
    if not _port_is_free(host, port):
        if args.no_fallback:
            print(
                f"port {port} on {host} is in use "
                f"(--no-fallback was given, so not switching ports)",
                file=sys.stderr,
            )
            return 2
        alt = _free_port(host, port + 1)
        if alt is None:
            print(
                f"ports {port}-{port + 39} on {host} are all in use; "
                f"pick one yourself with --port <n>",
                file=sys.stderr,
            )
            return 2
        print(
            f"port {port} is in use -> using {alt} instead "
            f"(another `agent-token-radar serve` may still be running; "
            f"stop it with `taskkill /F /PID <pid>` or `kill <pid>`)",
            file=sys.stderr,
        )
        port = alt

    app = create_app(scan_interval=args.interval)
    print(f"agent-token-radar dashboard: http://{host}:{port}")
    print(f"background incremental scan every {args.interval}s (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    """Open the dashboard in its own application window (the packaged exe)."""
    from .desktop import run as run_desktop

    return run_desktop(
        scan_interval=args.interval,
        dev_url=args.dev_url,
        preferred_port=args.port,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-token-radar",
        description=(
            "Local, read-only, multi-agent Token usage statistics with "
            "reference cost estimates from official list prices."
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true", help="show warning details")
    p.add_argument(
        "--lang", choices=list(SUPPORTED), default=DEFAULT_LANG,
        help="output language for human-readable text (zh | en); "
             "Token / Agent / Model stay in English in both",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ``--lang`` is also accepted after the subcommand (the natural place to
    # type it). SUPPRESS keeps the subparser from clobbering the global value
    # when the flag appears only before the subcommand.
    def _add_lang(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--lang", choices=list(SUPPORTED), default=argparse.SUPPRESS,
            help="output language (zh | en)",
        )

    ps = sub.add_parser("scan", help="run one incremental scan")
    ps.add_argument("--only", help="comma-separated adapter names, e.g. codex,claude")
    _add_lang(ps)
    ps.set_defaults(func=cmd_scan)

    pst = sub.add_parser("stats", help="print an aggregate report")
    pst.add_argument("--range",
                     choices=["today", "yesterday", "7d", "30d", "all"],
                     default="all")
    pst.add_argument("--by", choices=["agent", "model", "day", "project"], default=None)
    pst.add_argument("--json", action="store_true", help="machine-readable output")
    _add_lang(pst)
    pst.set_defaults(func=cmd_stats)

    pw = sub.add_parser("serve", help="start the local web dashboard")
    pw.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"preferred port (default {DEFAULT_PORT}); "
                         f"if taken, the next free one is used automatically")
    pw.add_argument("--host", default="127.0.0.1")
    pw.add_argument("--interval", type=int, default=30, help="background scan interval (seconds)")
    pw.add_argument(
        "--no-fallback", action="store_true",
        help="refuse to start when --port is taken (default: use the next free port)",
    )
    pw.set_defaults(func=cmd_serve)

    pd = sub.add_parser(
        "desktop",
        help="open the dashboard in its own application window (needs pywebview)",
    )
    pd.add_argument("--interval", type=int, default=30,
                    help="background scan interval (seconds)")
    pd.add_argument(
        "--port", type=int, default=None,
        help=f"preferred port (default {DEFAULT_PORT}); "
             f"if taken, the next free one is used automatically",
    )
    pd.add_argument(
        "--dev-url", default=None,
        help="point the window at an already-running server instead of starting one",
    )
    pd.set_defaults(func=cmd_desktop)
    return p


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

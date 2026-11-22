"""DSH adapter.

Authoritative source (DISCOVERY.md 2.5, corrected 2026-11-21)
-------------------------------------------------------------
``~/.dsh/sessions/<workspace-slug>/<session-dir>/session.vN.jsonl.zstd``

The file is an APPEND-ONLY JSONL stream physically encoded as a concatenation
of independent zstd frames (one frame per durable append batch). Python 3.14's
stdlib ``compression.zstd.decompress`` decodes all frames at once; a per-frame
fallback is provided for torn tails / older runtimes.

Only ``type == "assistant/message"`` -> ``data.usage`` is a per-request usage
record. The same numbers also appear in ``assistant/chunk`` and
``stream[].chunk.usage`` -- those are deliberately ignored. The projcache
``tokenUsage`` row is a CUMULATIVE snapshot and is ignored.

event_key = ``dsh|{header.id}|{seq}`` (seq is session-unique)

Incremental strategy: NO byte_offset (frame boundaries are not derivable from
an offset and DSH may truncate/re-encode the tail in place after a crash).
Gate on size+mtime; on change re-read the WHOLE file. Idempotency comes from
``event_key`` + UPSERT.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import home
from ..readers import decompress_zstd_frames, read_zstd_jsonl_full
from ._common import glob_sources, newest_generation_per_dir
from ._helpers import decode_dsh_project_dir, make_event, safe_int


def _strip_prefix(session_id: str) -> str:
    """DSH session ids appear both as ``session-<uuid>`` and bare ``<uuid>``."""
    return session_id[len("session-"):] if session_id.startswith("session-") else session_id


# Decoding every session log on each scan costs ~20s on this machine, and the
# answer only changes when a log changes. Keep it under our own data dir (never
# inside the agent's directory).
_CACHE_VERSION = 1


def _cache_path() -> Path:
    from ..paths import default_db_path

    return default_db_path().parent / "dsh_title_cache.json"


def _load_title_cache() -> dict[str, dict]:
    p = _cache_path()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_title_cache(files: dict[str, dict]) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"version": _CACHE_VERSION, "files": files}, fh, ensure_ascii=False)
        tmp.replace(p)
    except OSError:
        # A cache that cannot be written must never fail a scan.
        pass


class DshAdapter(BaseAdapter):
    name = "dsh"
    display_name = "DeepSeek Harness"
    home_env = "DSH_HOME"

    def detect(self) -> list[Source]:
        roots: list[Path] = [home() / ".dsh"]
        if self.home_env and os.environ.get(self.home_env):
            roots.insert(0, Path(os.environ[self.home_env]).expanduser())
        session_roots = [r / "sessions" for r in roots]
        found = glob_sources(
            session_roots, "session.v*.jsonl.zstd", kind="zstd-jsonl", agent=self.name
        )
        found += glob_sources(
            session_roots, "session.v*.jsonl", kind="jsonl", agent=self.name
        )
        found += glob_sources(
            session_roots, "session.jsonl*", kind="zstd-jsonl", agent=self.name
        )
        kept = {
            str(p.resolve()).lower()
            for p in newest_generation_per_dir([s.path for s in found])
        }
        return [s for s in found if str(s.path.resolve()).lower() in kept]

    def _titles_from_cost_ledger(self, roots: list[Path]) -> dict[str, str]:
        """``{session id: title}`` from DSH's own cost-meter ledger.

        DSH records the user-facing session title next to each session's token
        totals in ``storages/cost-meter/ledger.json`` (``days.<date>.sessions[]``,
        each entry ``{id, title, ...}``). That is the same source the DSH UI and
        magpie show, and it covers far more sessions than the rare in-log
        ``session/title`` event.

        The file is only read, and only the session titles are taken from it --
        the cost figures DSH writes there are ignored, so every agent is priced
        from the one shared pricing.json.
        """
        out: dict[str, str] = {}
        for root in roots:
            p = root / "storages" / "cost-meter" / "ledger.json"
            if not p.is_file():
                continue
            try:
                with p.open("r", encoding="utf-8", errors="replace") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            days = data.get("days") if isinstance(data, dict) else None
            if not isinstance(days, dict):
                continue
            for payload in days.values():
                if not isinstance(payload, dict):
                    continue
                for s in payload.get("sessions") or []:
                    if not isinstance(s, dict):
                        continue
                    sid = str(s.get("id") or "")
                    title = s.get("title")
                    if sid and isinstance(title, str) and title.strip():
                        # The ledger writes ids with and without the "session-"
                        # prefix for the same session; index both forms so the
                        # join cannot miss.
                        clean = title.strip()
                        out[sid] = clean
                        out.setdefault(_strip_prefix(sid), clean)
        return out

    def collect_titles(self, sources: list[Source]) -> dict[str, str]:
        """Read session titles from every place DSH keeps them.

        Title extraction must NOT sit behind the incremental-scan skip: a
        session's title is usually written once, long before the file stops
        changing, and an unchanged file is skipped entirely. Doing it here keeps
        titles available even when no new usage rows appear.
        """
        roots: list[Path] = [home() / ".dsh"]
        if self.home_env and os.environ.get(self.home_env):
            roots.insert(0, Path(os.environ[self.home_env]).expanduser())

        # Authoritative and cheap: one small JSON file, hundreds of titles.
        titles = self._titles_from_cost_ledger(roots)

        # In-log title events fill the gaps for sessions the ledger has not
        # recorded yet. Decoding a multi-megabyte zstd log is far too expensive
        # to repeat on every scan, so results are cached across scans on disk,
        # keyed by (path, size, mtime).
        cache = _load_title_cache()
        dirty = False
        result_map: dict[str, tuple[list[int | float], dict[str, str]]] = {}

        for src in sources:
            key = str(src.path)
            try:
                st = src.path.stat()
            except OSError:
                continue
            stamp = [int(st.st_size), round(st.st_mtime, 3)]

            hit = cache.get(key)
            if hit and hit.get("stamp") == stamp:
                for sid, title in hit.get("titles", {}).items():
                    titles.setdefault(sid, title)
                result_map[key] = {"stamp": stamp, "titles": hit.get("titles", {})}
                continue

            try:
                raw = src.path.read_bytes()
            except OSError:
                continue
            # Decoding is required even to know whether a title is present: zstd
            # compresses the literal "session/title", so a byte search over the
            # compressed blob silently skips every file that has one (measured:
            # it hid the titles of all the largest sessions).
            try:
                text = decompress_zstd_frames(raw)
            except Exception:  # noqa: BLE001 - never break a scan over a title
                continue
            found: dict[str, str] = {}
            sid = ""
            pending = ""
            for line in text.decode("utf-8", errors="replace").split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rtype = rec.get("type")
                if rtype == "session":
                    sid = str(rec.get("id") or "")
                    if pending and sid:
                        found.setdefault(sid, pending)
                        pending = ""
                elif rtype == "session/title":
                    d = rec.get("data") or {}
                    if isinstance(d, dict):
                        t = d.get("title")
                        if isinstance(t, str) and t.strip():
                            if sid:
                                found.setdefault(sid, t.strip())
                            else:
                                pending = t.strip()
            if pending and sid:
                found.setdefault(sid, pending)

            result_map[key] = {"stamp": stamp, "titles": found}
            dirty = True
            for sid, title in found.items():
                titles.setdefault(sid, title)

        if dirty:
            _save_title_cache(result_map)

        return titles

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append

        sources = self.detect()
        result.titles = self.collect_titles(sources)

        for src in sources:
            key = str(src.path)
            try:
                st = src.path.stat()
            except OSError as e:
                warn(f"{key}: cannot stat ({e.__class__.__name__}); skipped")
                continue

            # Layout: <sessions>/<encoded project dir>/<session id>/session.vN...
            # The directory name IS the working directory, encoded; it is the
            # only project evidence DSH writes, and it sits in the path rather
            # than in any record.
            project = ""
            try:
                project = decode_dsh_project_dir(src.path.parent.parent.name)
            except (IndexError, AttributeError):  # pragma: no cover - defensive
                project = ""

            old = cps.get(key)
            if (
                old is not None
                and old.size == st.st_size
                and abs(old.mtime - st.st_mtime) < 1e-6
            ):
                # Unchanged: keep the cursor, emit nothing. UPSERT would be a
                # no-op anyway, but skipping avoids re-decoding large logs.
                result.checkpoints[key] = old
                continue

            records = read_zstd_jsonl_full(src.path, on_warning=warn)
            header_id = ""
            fallback_model = ""
            fallback_provider = ""

            for rec in records:
                rtype = rec.get("type")
                if rtype == "session":
                    header_id = str(rec.get("id") or "")
                    continue
                # The provider is announced per request, not per usage record:
                # request/header carries config.{provider,model}, request/context
                # repeats provider/model. Either one refreshes the sticky value
                # used by the assistant/message records that follow.
                if rtype in ("request/header", "request/context", "model/selection"):
                    d = rec.get("data") or {}
                    if isinstance(d, dict):
                        cfg = d.get("header") if isinstance(d.get("header"), dict) else {}
                        cfg = cfg.get("config") if isinstance(cfg.get("config"), dict) else cfg
                        prov = d.get("provider") or (cfg.get("provider") if isinstance(cfg, dict) else "")
                        mdl = d.get("model") or (cfg.get("model") if isinstance(cfg, dict) else "")
                        if prov:
                            fallback_provider = str(prov)
                        if mdl:
                            fallback_model = str(mdl)
                    continue
                if rtype != "assistant/message":
                    continue

                data = rec.get("data") or {}
                if not isinstance(data, dict):
                    continue
                usage = data.get("usage")
                if not isinstance(usage, dict):
                    continue

                msg = data.get("message") if isinstance(data.get("message"), dict) else {}
                source = msg.get("source") if isinstance(msg.get("source"), dict) else {}
                model = str(source.get("model") or source.get("modelId") or fallback_model or "")
                declared = str(
                    source.get("provider")
                    or source.get("providerId")
                    or fallback_provider
                    or ""
                )
                # DSH announces the provider per request in its own log
                # (``request/header.config.provider``), so a value found here is
                # measured, not inferred. Only a record that stayed silent falls
                # back to the resolver's model-name guess.
                if declared:
                    provider, provider_source = self.resolve_provider(
                        self.name, model, declared=declared
                    )
                else:
                    provider, provider_source = self.resolve_provider(
                        self.name, model
                    )

                seq = safe_int(rec.get("seq"))
                ekey = f"dsh|{header_id}|{seq}"
                result.events.append(
                    make_event(
                        agent=self.name,
                        event_key=ekey,
                        source_file=key,
                        model=model,
                        provider=provider,
                        provider_source=provider_source,
                        session_id=header_id,
                        project=project,
                        timestamp=safe_int(rec.get("time")),
                        input_tokens=safe_int(usage.get("inputTokens")),
                        output_tokens=safe_int(usage.get("outputTokens")),
                        cache_read_tokens=safe_int(usage.get("cacheReadTokens")),
                        cache_write_tokens=safe_int(usage.get("cacheWriteTokens")),
                        reasoning_tokens=safe_int(usage.get("reasoningTokens")),
                        total_tokens=safe_int(usage.get("totalTokens")),
                    )
                )

            cp = Checkpoint(
                file_path=key,
                mtime=st.st_mtime,
                size=st.st_size,
                byte_offset=0,
            )
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result

"""Gemini / Antigravity adapter (protobuf BLOBs inside SQLite).

Authoritative source (verified against this machine + tokei's RE notes)
----------------------------------------------------------------------
``~/.gemini/{antigravity,antigravity-ide,antigravity-cli,gemini-cli}/conversations/*.db``

Each .db is a per-conversation SQLite database. Generation usage lives in
``gen_metadata.data`` as a protobuf BLOB with NO public schema -- the field
numbers below were reverse-engineered:

    gen_metadata.data
      f1                     = the generation record (outer wrapper)
        f1.f4                = usage
          4.2                = input tokens   (uncached)
          4.3                = output tokens   (INCLUDES thinking)
          4.5                = cache read tokens
          4.9                = thinking tokens
          4.10               = response tokens (visible answer)
        f1.f19               = model name (utf-8)
        f1.f9.f4.f1          = generation start time (unix seconds)
    steps.metadata
      f1.f1                  = google.protobuf.Timestamp seconds  (fallback ts)

Why the sanity bounds matter: the timestamps/token counts are decoded from
positional field numbers, so a format change silently produces garbage that
is numerically plausible. Anything out of range is DROPPED rather than stored
(a wrong number in a ledger is worse than a missing one).

Output accounting: field 4.3 already includes thinking. We store the visible
response as ``output_tokens`` (fallback ``4.3 - 4.9``) and keep thinking in
``reasoning_tokens``, so nothing is counted twice.

event_key = ``gemini|<conversation>|<step idx>``
"""

from __future__ import annotations

import os
from pathlib import Path

from ..base import BaseAdapter, Checkpoint, ScanResult, Source
from ..paths import home
from ..readers import readonly_sqlite_copy
from ._common import glob_sources
from ._helpers import make_event, safe_int
from ._protobuf import parse_proto_fields

_MAX_TOKENS = 100_000_000  # single-generation ceiling; above it the decode slipped
_MIN_TS = 1_577_836_800  # 2020-01-01; anything earlier is a mis-decode

_CONVERSATION_DIRS = (
    ("antigravity", "conversations"),
    ("antigravity-ide", "conversations"),
    ("antigravity-cli", "conversations"),
    ("gemini-cli", "conversations"),
)


def _usage_row(record: bytes) -> dict[str, object] | None:
    """Decode one generation record (already unwrapped from f1)."""
    model = ""
    inp = out = cached = thoughts = response = 0
    for field_num, wire_type, value in parse_proto_fields(record):
        if field_num == 19 and wire_type == 2:
            try:
                model = bytes(value).decode("utf-8")  # type: ignore[arg-type]
            except UnicodeDecodeError:
                model = ""
        elif field_num == 4 and wire_type == 2:
            for tfn, twt, tval in parse_proto_fields(bytes(value)):  # type: ignore[arg-type]
                if twt != 0:
                    continue
                n = int(tval)  # type: ignore[arg-type]
                if tfn == 2:
                    inp = n
                elif tfn == 3:
                    out = n
                elif tfn == 5:
                    cached = n
                elif tfn == 9:
                    thoughts = n
                elif tfn == 10:
                    response = n
    if max(inp, out, cached, thoughts, response) > _MAX_TOKENS:
        return None  # decode slipped -> drop rather than record garbage
    # A record with no token movement is not usage: Antigravity stores failed
    # generations too (measured: ``FAILED_PRECONDITION (code 400): User location
    # is not supported``). Those carry no model field and no counts, so keeping
    # them would invent an ``unknown`` model row that says nothing.
    if not (inp or out or cached or thoughts or response):
        return None
    if not model or len(model) > 120 or not model.isprintable():
        model = "unknown"
    visible = response if response else max(out - thoughts, 0)
    return {
        "model": model,
        "input": inp,
        "output": visible,
        "cache_read": cached,
        "reasoning": thoughts,
    }


def _step_timestamp(metadata: bytes) -> int | None:
    """``steps.metadata`` -> unix seconds (google.protobuf.Timestamp in f1.f1)."""
    for field_num, wire_type, value in parse_proto_fields(metadata or b""):
        if field_num != 1 or wire_type != 2:
            continue
        for sub_num, sub_wire, sub_value in parse_proto_fields(bytes(value)):  # type: ignore[arg-type]
            if sub_num == 1 and sub_wire == 0:
                secs = int(sub_value)  # type: ignore[arg-type]
                if _MIN_TS <= secs <= 1 << 34:
                    return secs
    return None


class GeminiAdapter(BaseAdapter):
    name = "gemini"
    display_name = "Gemini / Antigravity"
    home_env = "TOKEI_GEMINI_DIR"

    def detect(self) -> list[Source]:
        roots: list[Path] = [home() / ".gemini"]
        if self.home_env and os.environ.get(self.home_env):
            roots.insert(0, Path(os.environ[self.home_env]).expanduser())
        conv_roots = [r / sub / leaf for r in roots for sub, leaf in _CONVERSATION_DIRS]
        return glob_sources(conv_roots, "*.db", kind="sqlite", agent=self.name)

    def _session_titles(self, warn=None) -> dict[str, str]:
        """``{conversation id: title}`` from Antigravity's summary index.

        Each conversation is its own SQLite file, so there is no title inside
        it. Antigravity keeps them all in ``conversation_summaries.db``:
        ``conversation_summaries.conversation_id`` matches the per-conversation
        filename (without ``.db``).
        """
        del warn  # kept for symmetry; a missing index is not an error
        out: dict[str, str] = {}
        roots: list[Path] = [home() / ".gemini"]
        if self.home_env and os.environ.get(self.home_env):
            roots.insert(0, Path(os.environ[self.home_env]).expanduser())

        for root in roots:
            for sub, _leaf in _CONVERSATION_DIRS:
                idx = root / sub / "conversation_summaries.db"
                if not idx.is_file():
                    continue
                try:
                    with readonly_sqlite_copy(idx) as con:
                        if con is None:
                            continue
                        for cid, title in con.execute(
                            "SELECT conversation_id, title FROM conversation_summaries"
                        ):
                            cid = str(cid or "")
                            t = (title or "").strip() if isinstance(title, str) else ""
                            if cid and t:
                                out[cid] = t
                except Exception:  # noqa: BLE001 - never break a scan over a title
                    continue
        return out

    def scan(self, checkpoint=None) -> ScanResult:  # noqa: ANN001
        result = ScanResult()
        cps = checkpoint or {}
        warn = result.warnings.append
        result.titles = self._session_titles(warn)

        for src in self.detect():
            key = str(src.path)
            conversation = src.path.stem
            with readonly_sqlite_copy(src.path, on_warning=warn) as con:
                if con is None:
                    continue
                try:
                    gens = con.execute(
                        "SELECT idx, data FROM gen_metadata WHERE data IS NOT NULL ORDER BY idx"
                    ).fetchall()
                except Exception as e:  # noqa: BLE001 - schema drift must not abort
                    warn(f"{key}: cannot read gen_metadata ({e.__class__.__name__}); skipped")
                    continue

                # steps.metadata carries the conversation timestamps.
                steps: list[tuple[int, int]] = []
                try:
                    for idx, meta in con.execute(
                        "SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx"
                    ):
                        ts = _step_timestamp(bytes(meta)) if isinstance(meta, (bytes, bytearray)) else None
                        if ts:
                            steps.append((int(idx), ts))
                except Exception:  # noqa: BLE001
                    pass

                for gen_idx, blob in gens:
                    if not isinstance(blob, (bytes, bytearray)):
                        continue
                    raw = bytes(blob)
                    # Usage always sits inside the f1 wrapper. There is
                    # deliberately NO "try the raw blob too" fallback: when the
                    # wrapper is absent the field numbers no longer align, and
                    # guessing would produce plausible-but-wrong numbers.
                    row = None
                    for field_num, wire_type, value in parse_proto_fields(raw):
                        if field_num == 1 and wire_type == 2:
                            row = _usage_row(bytes(value))
                            if row:
                                break
                    if row is None:
                        continue

                    ts_ms = 0
                    # Best-effort time: the step nearest at/after this generation index.
                    for step_idx, secs in steps:
                        if step_idx >= int(gen_idx):
                            ts_ms = secs * 1000
                            break
                    if not ts_ms and steps:
                        ts_ms = steps[-1][1] * 1000

                    # Gemini records a model but never a provider. The model name
                    # is the only clue, so the value is flagged as inferred --
                    # unless magpie logged this exact conversation. Aggregates
                    # carry no per-request id, so measured lookups do not apply.
                    model = str(row["model"])
                    provider, provider_source = self.resolve_provider(
                        self.name, model
                    )

                    result.events.append(
                        make_event(
                            agent=self.name,
                            event_key=f"gemini|{conversation}|{int(gen_idx)}",
                            source_file=key,
                            model=model,
                            provider=provider,
                            provider_source=provider_source,
                            session_id=conversation,
                            timestamp=ts_ms,
                            input_tokens=safe_int(row["input"]),
                            output_tokens=safe_int(row["output"]),
                            cache_read_tokens=safe_int(row["cache_read"]),
                            cache_write_tokens=0,
                            reasoning_tokens=safe_int(row["reasoning"]),
                        )
                    )

            cp = Checkpoint.for_path(src.path)
            cp.byte_offset = 0
            cp.touch(byte_offset=0)
            result.checkpoints[key] = cp
        return result

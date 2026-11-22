"""Core contracts for agent-token-radar.

Phase 1 defines the architecture only: the normalized record shape, the
adapter interface, and the checkpoint/result containers. Actual token
extraction lives in the per-agent adapter modules and is implemented in
Phase 2.

Design rules (carried over from DISCOVERY.md):
  * Token counts only -- no price / cost fields anywhere in this package.
  * All third-party agent directories are treated as STRICTLY READ-ONLY.
  * Every adapter must be idempotent: repeated scans of the same data must
    never change the stored totals. This is achieved by a stable ``event_key``
    plus UPSERT in the storage layer.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Iterable, Mapping

__all__ = [
    "SCHEMA_VERSION",
    "UsageEvent",
    "Source",
    "Checkpoint",
    "CheckpointMap",
    "ScanResult",
    "AdapterReport",
    "ScanReport",
    "BaseAdapter",
]

SCHEMA_VERSION = 1

# The exact column set of ``usage_events``. Kept in one place so the store,
# the adapters and the tests cannot drift apart.
USAGE_COLUMNS: tuple[str, ...] = (
    "event_key",
    "agent",
    "model",
    "provider",
    "provider_source",
    "served_model",
    "session_id",
    "project",
    "timestamp",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "source_file",
)


def _as_int(value: Any) -> int:
    """Coerce a token-ish value to a non-negative int; never raise."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, n)


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One normalized, de-duplicated token-usage record.

    ``event_key`` is the idempotency identity. It MUST be stable across
    repeated scans of the same source record (see DISCOVERY.md section 2 for
    the recommended key per agent).

    ``timestamp`` is UTC epoch **milliseconds** (integer).

    ``reasoning_tokens`` is a *subdivision* of ``output_tokens`` for every
    supported agent; it is stored for reporting but is never added into
    ``total_tokens`` a second time.
    """

    event_key: str
    agent: str
    model: str = ""
    #: Which backend served the model (``workbuddy`` / ``tokenrhythm`` / ``openai``
    #: ...). Same model on different providers is genuinely different usage, so
    #: it is stored separately rather than folded into ``model``.
    provider: str = ""
    #: How ``provider`` was obtained: ``measured`` (read from a record that
    #: states it, or matched against the local magpie gateway's per-request log)
    #: or ``inferred`` (derived from the model name). The dashboard marks the
    #: inferred ones so a guess is never mistaken for a measurement.
    provider_source: str = ""
    #: The model the backend actually served, when a local gateway measured it.
    #: Several agents log a *routing alias* instead of a real model -- Claude
    #: Code writes ``claude-sonnet-5`` for a request the gateway answered with
    #: ``deepseek-v4.1-flash``. Kept beside ``model`` rather than replacing it,
    #: so the agent's own record is never lost.
    served_model: str = ""
    session_id: str = ""
    #: The working directory the agent was running in, i.e. which *project* the
    #: tokens were spent on. Tokei tracks this per tool and it is the one
    #: dimension this dashboard was missing: "67M tokens" says nothing, "67M
    #: tokens in Untitled Game" does. Empty when the agent records no cwd.
    project: str = ""
    timestamp: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    source_file: str = ""

    def normalized(self) -> "UsageEvent":
        """Return a sanitized copy with ``total_tokens`` filled in.

        Some agents omit ``total_tokens`` (Claude Code, many DSH events). The
        canonical formula is ``input + output + cache_read + cache_write``;
        reasoning is excluded because it is already inside ``output``.
        """
        total = _as_int(self.total_tokens)
        if total <= 0:
            total = (
                _as_int(self.input_tokens)
                + _as_int(self.output_tokens)
                + _as_int(self.cache_read_tokens)
                + _as_int(self.cache_write_tokens)
            )
        return replace(
            self,
            event_key=str(self.event_key),
            agent=str(self.agent),
            model=str(self.model or ""),
            provider=str(self.provider or ""),
            provider_source=str(self.provider_source or ""),
            served_model=str(self.served_model or ""),
            session_id=str(self.session_id or ""),
            project=str(self.project or ""),
            timestamp=_as_int(self.timestamp),
            input_tokens=_as_int(self.input_tokens),
            output_tokens=_as_int(self.output_tokens),
            cache_read_tokens=_as_int(self.cache_read_tokens),
            cache_write_tokens=_as_int(self.cache_write_tokens),
            reasoning_tokens=_as_int(self.reasoning_tokens),
            total_tokens=total,
            source_file=str(self.source_file or ""),
        )

    def to_row(self) -> tuple[Any, ...]:
        """Return values in ``USAGE_COLUMNS`` order, normalized."""
        n = self.normalized()
        return tuple(getattr(n, c) for c in USAGE_COLUMNS)


@dataclass(frozen=True, slots=True)
class Source:
    """A single data file discovered by an adapter. Discovery is read-only."""

    path: Path
    kind: str  # "jsonl" | "zstd-jsonl" | "sqlite" | "json"
    agent: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


@dataclass(slots=True)
class Checkpoint:
    """Incremental cursor for one source file.

    Required by the spec: ``file_path``, ``mtime``, ``byte_offset``.
    ``size`` / ``updated_at`` are additive extensions used by adapters whose
    format cannot be tailed by byte offset (DSH multi-frame zstd) and for
    diagnostics.
    """

    file_path: str
    mtime: float = 0.0
    byte_offset: int = 0
    size: int = 0
    updated_at: int = 0

    @classmethod
    def for_path(cls, path: Path | str) -> "Checkpoint":
        p = Path(path)
        try:
            st = p.stat()
            return cls(file_path=str(p), mtime=st.st_mtime, size=st.st_size)
        except OSError:
            return cls(file_path=str(p))

    def touch(self, *, byte_offset: int | None = None) -> "Checkpoint":
        if byte_offset is not None:
            self.byte_offset = max(0, int(byte_offset))
        try:
            st = Path(self.file_path).stat()
            self.mtime = st.st_mtime
            self.size = st.st_size
        except OSError:
            pass
        self.updated_at = int(time.time() * 1000)
        return self


# ``scan(checkpoint)`` receives a mapping keyed by absolute file path, because
# most agents have many session files. A single-file adapter may simply look
# up its own path (or ignore the mapping entirely).
CheckpointMap = dict[str, Checkpoint]


@dataclass(slots=True)
class ScanResult:
    """Everything one ``scan(checkpoint)`` call produces."""

    events: list[UsageEvent] = field(default_factory=list)
    checkpoints: CheckpointMap = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: session_id -> human-readable title. Titles are stored once per session,
    #: not repeated on every event, because the same session can hold thousands
    #: of usage rows.
    titles: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AdapterReport:
    name: str
    sources: int = 0
    events_seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class ScanReport:
    adapters: list[AdapterReport] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return sum(a.inserted for a in self.adapters)

    @property
    def updated(self) -> int:
        return sum(a.updated for a in self.adapters)

    @property
    def events_seen(self) -> int:
        return sum(a.events_seen for a in self.adapters)

    @property
    def warnings(self) -> list[str]:
        return [w for a in self.adapters for w in a.warnings]


class BaseAdapter(abc.ABC):
    """Abstract base for every agent adapter.

    Subclassing contract (this is all Phase 2 needs to do):

        class FooAdapter(BaseAdapter):
            name = "foo"                 # stable, unique id
            display_name = "Foo"        # human label

            def detect(self): ...       # -> list[Source], strictly read-only
            def scan(self, checkpoint): # -> ScanResult
                ...

    Dropping a new ``adapters/<name>.py`` file with one concrete subclass is
    enough: :mod:`agent_token_radar.registry` imports every module in that
    package and registers every concrete ``BaseAdapter`` subclass it finds.
    No central list, no manual import, no decorator required.
    """

    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    #: Set to an env var name if the tool honours a home override (e.g. DSH_HOME).
    home_env: ClassVar[str | None] = None

    def __init__(self, *, logger: Any = None) -> None:
        self.log = logger

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r}>"

    @abc.abstractmethod
    def detect(self) -> list[Source]:
        """Return the data files that currently exist and are readable.

        MUST NOT create, write, move or delete anything inside an agent's
        directory. Returning an empty list means "agent not installed here".
        """

    @abc.abstractmethod
    def scan(self, checkpoint: CheckpointMap | None = None) -> ScanResult:
        """Scan detected sources and return normalized events + new cursors.

        ``checkpoint`` maps absolute file path -> :class:`Checkpoint` from the
        previous run (``None`` on the very first run). Implementations should:

        * use ``byte_offset`` for append-only JSONL sources;
        * use ``size`` + ``mtime`` and re-read the whole file for formats
          whose frame boundaries are not derivable from a byte offset;
        * rely on ``event_key`` + UPSERT for idempotency, never on offsets
          alone;
        * skip corrupt / incomplete records with a warning, never abort.
        """

    # ---- small shared helpers (usable, not required, by subclasses) --------

    @staticmethod
    def warn(result: ScanResult, message: str) -> None:
        result.warnings.append(message)

    def sources_by_kind(self, kind: str) -> list[Source]:
        return [s for s in self.detect() if s.kind == kind]

    @staticmethod
    def resolve_provider(
        agent: str,
        model: str,
        request_id: str = "",
        *,
        declared: str = "",
    ) -> tuple[str, str]:
        """``(provider, source)``: measured first, then labelled inference.

        Imported lazily so ``base`` stays free of I/O at import time.
        """
        from .provider_sources import resolver

        return resolver.resolve(agent, model, request_id, declared=declared)

    @staticmethod
    def resolve_served_model(request_id: str = "") -> str:
        """The model a local gateway measured for this request, or ``""``."""
        from .provider_sources import resolver

        return resolver.served_model(request_id)

    def cursor_for(
        self, checkpoint: CheckpointMap | None, source: Source
    ) -> Checkpoint:
        cps = checkpoint or {}
        key = str(source.path)
        return cps.get(key) or Checkpoint.for_path(source.path)

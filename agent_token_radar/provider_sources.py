"""Provider resolution: real measurements first, labelled guesses last.

Why this module exists
----------------------
Most agents record *which model* answered a request but not *which backend*
served it. ``claude-sonnet-5`` says nothing about whether the request reached
Anthropic or a domestic gateway; ``deepseek-v4.1-flash`` can come from
``workbuddy``, ``workbuddy-ai`` or ``tokenrhythm`` depending on routing. Without
the provider the dashboard silently merges genuinely different usage.

Two sources of truth, in strict priority order
----------------------------------------------
1. **measured** -- the request id the agent logged is matched against the local
   ``magpie`` gateway's own usage log. Both write the provider id per request,
   so this is direct evidence, not a guess.

   ==========  =============================  ====================
   agent       request id in the agent log    magpie field
   ==========  =============================  ====================
   claude      ``message.id``                 ``response_id``
   codex       ``payload.response_id``        ``response_id``
   ==========  =============================  ====================

   Measured on this machine: Claude Code 587/591 requests matched (99.3%),
   Codex 380/3359 (11.3% -- most Codex traffic never goes through the gateway).

   An adapter that finds the provider in the agent's *own* record (DSH's
   ``request/header.config.provider``) also counts as measured: it is written by
   the tool itself.

2. **inferred** -- no measurement exists, so the model name is looked up in
   magpie's provider catalogue (``providers.json``: which provider lists which
   model ids) and, when magpie is not installed at all, in the static table
   below. Every row carrying an inferred provider is flagged in the dashboard so
   it can never be mistaken for a measured value.

Anything that neither source can explain stays empty. An empty provider is
honest; a fabricated one is not.

Read-only guarantee: every file here is opened for reading only, and nothing is
written outside this project's own ``data/`` directory.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "SOURCE_MEASURED",
    "SOURCE_PRODUCT",
    "SOURCE_INFERRED",
    "SOURCE_NONE",
    "SOURCE_STRENGTH",
    "ProviderResolver",
    "resolver",
    "request_id_from_event_key",
]

#: The provider was read from a record that states it (agent log or magpie log).
SOURCE_MEASURED = "measured"
#: The provider is the app's own backend. A single-product tool can only talk to
#: its own service, so this is a structural fact about the product rather than a
#: guess about a model name -- but it is still not a per-request measurement.
SOURCE_PRODUCT = "product"
#: The provider was derived from the model name. Always flagged in the UI.
SOURCE_INFERRED = "inferred"
#: Nothing is known. Stored and displayed as an empty provider.
SOURCE_NONE = ""

#: How much a provenance value is worth, for collapsing a mixed group onto its
#: weakest member. A group must never look better-evidenced than its worst row.
SOURCE_STRENGTH: dict[str, int] = {
    SOURCE_MEASURED: 3,
    SOURCE_PRODUCT: 2,
    SOURCE_INFERRED: 1,
    SOURCE_NONE: 0,
}

# Below ``providerData.model`` etc. the apps repeat the model name with a
# routing prefix (``workbuddy/kimi-k3.1-pro``); the prefix is the provider.
_MAGPIE_USAGE = Path(".config") / "magpie" / "usage.jsonl"
_MAGPIE_PROVIDERS = Path(".config") / "magpie" / "providers.json"

#: How long a parsed copy of magpie's files is reused before being re-stat'ed.
_CACHE_TTL_SECONDS = 60.0

#: Static fallback, used only when magpie is not installed (Linux CI, a fresh
#: machine, a different user). Ordered: the first matching prefix wins, so
#: specific names must come before their generic family prefix.
#:
#: These entries name who *owns* the model family. The domestic gateway can
#: serve ``gpt-*`` / ``claude-*`` too, but a name alone cannot prove that, and a
#: wrong guess is worse than a labelled one -- so the vendor is the default and
#: the per-app identity (``_AGENT_IS_PROVIDER``) handles the gateway case.
_STATIC_PREFIXES: tuple[tuple[str, str], ...] = (
    # ---- WorkBuddy's own model names (nobody else publishes these)
    ("hy4", "workbuddy"),
    ("hy3", "workbuddy"),
    ("space-bunny", "workbuddy"),
    ("step-", "workbuddy"),
    # ---- the vendors' own APIs
    ("deepseek-", "deepseek"),
    ("gpt-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "google"),
    ("grok-", "xai"),
    ("qwen", "alibaba"),
    ("kimi-", "moonshot"),
    ("glm-", "zhipu"),
    ("minimax-", "minimax"),
    ("mimo-", "xiaomi"),
)

#: Agents that ARE a provider: the app's own traffic served by its own backend.
#: A structural fact about the product, not a guess about a model name, so it
#: outranks the catalogue -- which describes what each service *could* serve and
#: lists some names under several of them.
#:
#: ``codex`` is here on measured evidence: of the Codex requests that appear in
#: magpie's routing ledger on this machine, 371 of 380 were served by provider
#: ``codex`` (the ChatGPT/Codex backend); the remaining 9 carried an explicit
#: ``workbuddy/...`` prefix and are handled before this table is consulted.
_AGENT_IS_PROVIDER: dict[str, str] = {
    "workbuddy": "workbuddy",
    "workbuddy-intl": "workbuddy-ai",
    "codex": "codex",
}


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _iter_routing_records(root: Path) -> Iterable[dict[str, Any]]:
    """Yield magpie's per-day routing ledger (``routing/*.jsonl[.gz]``).

    This is magpie's own routing ledger: one record per request, with the
    provider that actually answered and the model that was actually served.
    It is more authoritative than ``usage.jsonl``, which is the same events
    flattened for export and can lose a field or a failed attempt.
    """
    routing = root / "routing"
    try:
        entries = sorted(routing.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".jsonl.gz"):
            try:
                fh = gzip.open(path, "rt", encoding="utf-8", errors="replace")
            except OSError:
                continue
        elif name.endswith(".jsonl"):
            try:
                fh = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
        else:
            continue
        try:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
        finally:
            try:
                fh.close()
            except OSError:
                pass


def _routing_provider(rec: dict[str, Any]) -> tuple[str, str]:
    """``(provider, served model)`` for one routing record.

    ``usage[0]`` is what was billed; ``provider`` at the top level is the
    *requested* route and can name a group (``auto/...``) rather than a backend,
    so the billed entry wins.
    """
    usage = rec.get("usage")
    if isinstance(usage, list) and usage and isinstance(usage[0], dict):
        first = usage[0]
        return (
            str(first.get("provider") or "").strip(),
            str(first.get("model") or "").strip(),
        )
    return str(rec.get("provider") or "").strip(), str(rec.get("served") or "").strip()


def _config_roots() -> list[Path]:
    """Every plausible magpie config root (Windows and Linux)."""
    out: list[Path] = []
    for env in ("MAGPIE_CONFIG_DIR", "XDG_CONFIG_HOME"):
        raw = os.environ.get(env)
        if raw:
            p = Path(raw).expanduser()
            # XDG_CONFIG_HOME is the parent of `.config`'s contents.
            out.append(p / "magpie" if env == "XDG_CONFIG_HOME" else p)
    try:
        home = Path.home()
    except (RuntimeError, OSError):  # pragma: no cover - defensive
        home = Path(os.path.expanduser("~"))
    out.append(home / ".config" / "magpie")
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "magpie")
    uniq: list[Path] = []
    for p in out:
        if p not in uniq:
            uniq.append(p)
    return uniq


def request_id_from_event_key(agent: str, event_key: str) -> str:
    """Recover the provider request id from a stored ``event_key``.

    Historical rows were written before providers were resolved, so the only
    place the request id survives is the idempotency key. Each agent builds that
    key differently, so the shape is parsed per agent rather than guessed:

    ``claude|<message.id>``                        -> ``<message.id>``
    ``codex|<session>|<response_id>|<ordinal>``     -> ``<response_id>``
    """
    if not event_key or "|" not in event_key:
        return ""
    parts = event_key.split("|")
    if parts[0] != agent:
        return ""
    if agent == "claude":
        return parts[1] if len(parts) > 1 else ""
    if agent == "codex":
        return parts[2] if len(parts) > 2 else ""
    return ""


class ProviderResolver:
    """Resolves ``(agent, model, request_id) -> (provider, source)``.

    The magpie files are parsed at most once per :data:`_CACHE_TTL_SECONDS`;
    both are a few hundred KB, but a scan runs every few minutes and this keeps
    the cost off the critical path. A missing or unreadable file simply yields
    fewer measurements -- never an exception.
    """

    def __init__(self, *, config_roots: Iterable[Path] | None = None) -> None:
        self._roots = list(config_roots) if config_roots is not None else None
        self._loaded_at = 0.0
        self._by_request: dict[str, str] = {}
        #: Request id -> the model the gateway actually served. Claude Code's
        #: ``msg.model`` is sometimes the routed alias (``claude-sonnet-5``)
        #: rather than the real model (``deepseek-v4.1-flash``), so this is the
        #: only trustworthy name for those requests.
        self._served: dict[str, str] = {}
        self._catalog: dict[str, set[str]] = {}
        self._by_model_seen: dict[str, dict[str, int]] = {}
        self.available = False

    # ------------------------------------------------------------- loading
    def _roots_now(self) -> list[Path]:
        return self._roots if self._roots is not None else _config_roots()

    def _load_if_stale(self) -> None:
        now = time.monotonic()
        if self._by_request and now - self._loaded_at < _CACHE_TTL_SECONDS:
            return
        self._loaded_at = now
        by_request: dict[str, str] = {}
        served: dict[str, str] = {}
        by_model_seen: dict[str, dict[str, int]] = {}
        catalog: dict[str, set[str]] = {}
        found = False

        for root in self._roots_now():
            # 1. the routing ledger -- one record per request, most authoritative
            for rec in _iter_routing_records(root):
                found = True
                provider, served_model = _routing_provider(rec)
                rid = str(rec.get("responseId") or "").strip()
                if rid and provider:
                    by_request[rid] = provider
                    if served_model:
                        served[rid] = served_model
                if provider and served_model:
                    bucket = by_model_seen.setdefault(served_model, {})
                    bucket[provider] = bucket.get(provider, 0) + 1

            # 2. the export log -- same events, and it covers requests whose
            #    routing record was pruned.
            usage = root / "usage.jsonl"
            if usage.is_file():
                found = True
                for rec in _read_jsonl(usage):
                    provider = str(rec.get("provider") or "").strip()
                    if not provider:
                        continue
                    rid = str(rec.get("response_id") or "").strip()
                    if rid:
                        by_request.setdefault(rid, provider)
                        real = str(rec.get("served") or "").strip()
                        if real:
                            served.setdefault(rid, real)
                    model = str(rec.get("model") or rec.get("served") or "").strip()
                    if model:
                        bucket = by_model_seen.setdefault(model, {})
                        bucket[provider] = bucket.get(provider, 0) + 1

            prov_file = root / "providers.json"
            if prov_file.is_file():
                data = _read_json(prov_file)
                if isinstance(data, dict):
                    for entry in data.get("providers") or []:
                        if not isinstance(entry, dict):
                            continue
                        pid = str(entry.get("id") or "").strip()
                        if not pid:
                            continue
                        found = True
                        names: list[str] = []
                        for key in ("models", "pickedFrom", "delisted"):
                            names.extend(str(m) for m in entry.get(key) or [])
                        for m in names:
                            m = m.strip()
                            if m:
                                catalog.setdefault(m, set()).add(pid)

        self._by_request = by_request
        self._served = served
        self._by_model_seen = by_model_seen
        self._catalog = catalog
        self.available = found

    # ----------------------------------------------------------- resolving
    def measured(self, request_id: str) -> str:
        """Provider for one exact request, or ``""`` when unknown."""
        rid = (request_id or "").strip()
        if not rid:
            return ""
        self._load_if_stale()
        return self._by_request.get(rid, "")

    def served_model(self, request_id: str) -> str:
        """The model the gateway actually served for this request, or ``""``.

        Claude Code logs the routed alias in ``msg.model``. When the local
        gateway recorded a request, its ``usage[].model`` is the real answer
        (measured: ``cc_model=claude-sonnet-5`` -> ``served=deepseek-v4.1-flash``).
        """
        rid = (request_id or "").strip()
        if not rid:
            return ""
        self._load_if_stale()
        return self._served.get(rid, "")

    def infer(self, agent: str, model: str) -> str:
        """Backwards-compatible: the provider, ignoring how sure it is."""
        return self._infer(agent, model)[0]

    def _infer(self, agent: str, model: str) -> tuple[str, str]:
        """``(provider, source)`` for a record that states no provider.

        Two very different kinds of answer live here and must not share a label:

        * the app's **own backend** (``product``) -- a single-product tool can
          only talk to its own service, so ``gpt-*`` from Codex Desktop is
          ``codex`` with high confidence;
        * a **guess from the model name** (``inferred``) -- ``gpt-*`` seen in
          some other app could be OpenAI direct or any gateway in front of it.

        Reporting both as "inferred" made Codex's own models look unsupported,
        which is exactly what the user noticed.
        """
        name = (model or "").strip()
        own = _AGENT_IS_PROVIDER.get(agent, "")
        if not name:
            return own, (SOURCE_PRODUCT if own else SOURCE_NONE)

        # 1. ``provider/model`` -- written by the tool that made the request.
        #    That is the tool naming its route, not us guessing.
        if "/" in name:
            head, _, tail = name.partition("/")
            if head and tail:
                return head, SOURCE_MEASURED

        # 2. The app's own backend. Deliberately ahead of the catalogue, which
        #    describes what each service *could* serve and lists some names
        #    under several of them (WorkBuddy's ``hy3`` was reported as
        #    ``workbuddy-ai`` because that app also lists it).
        if own:
            return own, SOURCE_PRODUCT

        self._load_if_stale()

        # 3. Aggregated observation from the gateway's own log: real routing
        #    data from this machine, but summed over requests rather than
        #    attached to this one.
        seen = self._by_model_seen.get(name)
        if seen:
            return max(seen.items(), key=lambda kv: kv[1])[0], SOURCE_INFERRED

        # 4. Catalogue, only when unambiguous.
        candidates = self._catalog.get(name)
        if candidates and len(candidates) == 1:
            return next(iter(candidates)), SOURCE_INFERRED

        # 5. Vendor prefix.
        lowered = name.lower()
        for prefix, provider in _STATIC_PREFIXES:
            if lowered.startswith(prefix):
                return provider, SOURCE_INFERRED

        return "", SOURCE_NONE

    def resolve(
        self, agent: str, model: str, request_id: str = "", *, declared: str = ""
    ) -> tuple[str, str]:
        """``(provider, source)`` for one usage record.

        Priority, as the user asked for it: a measurement from the local gateway
        wins over everything (it observed the request itself), then whatever the
        agent states in its own record, then a labelled fallback.
        """
        measured = self.measured(request_id)
        if measured:
            return measured, SOURCE_MEASURED
        stated = (declared or "").strip()
        if stated:
            return stated, SOURCE_MEASURED
        return self._infer(agent, model)

    def stats(self) -> dict[str, Any]:
        """Small diagnostic payload for the dashboard footer / tests."""
        self._load_if_stale()
        return {
            "available": self.available,
            "measured_requests": len(self._by_request),
            "served_models": len(self._served),
            "catalog_models": len(self._catalog),
        }


#: Process-wide resolver. Module level so every adapter and the store's backfill
#: share one parsed copy instead of re-reading magpie's files per call.
resolver = ProviderResolver()

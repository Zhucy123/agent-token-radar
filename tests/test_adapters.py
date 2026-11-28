"""Phase 2: desensitized mock samples + unit tests for all 5 adapters.

Every fixture is synthetic: no conversation text, no keys, no real model
names, no real session ids. Only field names and token numbers.

Two things are verified for each adapter:
  1. all five token buckets map correctly (input / output / cache_read /
     cache_write / reasoning);
  2. scanning the SAME data twice yields byte-identical totals (idempotency),
     via the event_key + UPSERT contract.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from pathlib import Path

from _tmp import drop_temp_dir, make_temp_dir

from agent_token_radar.adapters.claude import ClaudeAdapter
from agent_token_radar.adapters.codex import CodexAdapter
from agent_token_radar.adapters.dsh import DshAdapter
from agent_token_radar.adapters.gemini import GeminiAdapter
from agent_token_radar.adapters.hermes import HermesAdapter
from agent_token_radar.adapters.workbuddy import WorkBuddyAdapter
from agent_token_radar.adapters.workbuddy_intl import WorkBuddyIntlAdapter
from agent_token_radar.base import Checkpoint, Source
from agent_token_radar.store import Store

# ---------------------------------------------------------------------------
# Synthetic fixtures (all values invented)
# ---------------------------------------------------------------------------
CODEX_LINES = [
    {"type": "session_meta", "timestamp": "2026-01-01T00:00:00.000Z",
     "payload": {"model": "mock-model-a", "session_id": "sess-cx-1"}},
    {"type": "token_usage_record", "ordinal": 1, "timestamp": "2026-01-01T00:00:01.000Z",
     "payload": {"session_id": "sess-cx-1", "thread_id": "thr-1", "turn_id": "turn-1",
                 "response_id": "resp-1",
                 "usage": {"input_tokens": 100, "output_tokens": 20,
                           "cached_input_tokens": 300, "cache_write_input_tokens": 5,
                           "reasoning_output_tokens": 7, "total_tokens": 425},
                 # CUMULATIVE rollups -- must be ignored
                 "info": {"total_token_usage": {"input_tokens": 999999, "output_tokens": 999999}},
                 "thread_token_usage": {"total_tokens": 999999}}},
    {"type": "token_usage_record", "ordinal": 2, "timestamp": "2026-01-01T00:00:02.000Z",
     "payload": {"session_id": "sess-cx-1", "response_id": "resp-2",
                 "usage": {"input_tokens": 50, "output_tokens": 10,
                           "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                           "reasoning_output_tokens": 2, "total_tokens": 60}}},
    {"type": "event_msg", "ordinal": 3, "timestamp": "2026-01-01T00:00:03.000Z",
     "payload": {"info": {"last_token_usage": {"input_tokens": 777}}}},
    "{BROKEN JSON LINE",  # malformed -> warning, never abort
]

CLAUDE_LINES = [
    {"type": "assistant", "sessionId": "sess-cl-1", "uuid": "u1",
     "timestamp": "2026-01-02T00:00:01.000Z",
     "message": {"id": "msg-1", "model": "mock-model-b", "role": "assistant",
                 "usage": {"input_tokens": 200, "output_tokens": 30,
                           "cache_read_input_tokens": 400,
                           "cache_creation_input_tokens": 6,
                           "output_tokens_details": {"thinking_tokens": 9}}}},
    {"type": "assistant", "sessionId": "sess-cl-1", "uuid": "u2",
     "timestamp": "2026-01-02T00:00:02.000Z",
     "message": {"id": "msg-2", "model": "mock-model-b",
                 "usage": {"input_tokens": 10, "output_tokens": 5,
                           "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0}}},
    # CUMULATIVE snapshot -- must be ignored
    {"type": "cost-state", "sessionId": "sess-cl-1",
     "modelUsage": {"mock-model-b": {"inputTokens": 888888, "outputTokens": 888888}}},
    "{BROKEN",
]

WORKBUDDY_LINES = [
    {"id": "wb-1", "sessionId": "sess-wb-1", "timestamp": 1789100544465,
     "providerData": {"rawUsage": {
         "prompt_tokens": 500, "completion_tokens": 40, "total_tokens": 740,
         "prompt_tokens_details": {"cached_tokens": 190},
         "completion_tokens_details": {"reasoning_tokens": 11},
         "prompt_cache_write_tokens": 10},
         "usage": {"inputTokens": 500, "outputTokens": 40, "totalTokens": 740}},
     "message": {"usage": {"input_tokens": 500, "output_tokens": 40,
                           "total_tokens": 740, "cache_read_input_tokens": 190}}},
    {"id": "wb-2", "sessionId": "sess-wb-1", "timestamp": 1789100545465,
     "providerData": {"rawUsage": {"prompt_tokens": 20, "completion_tokens": 3,
                                   "total_tokens": 23}}},
    "{BROKEN",
]

HERMES_ROWS = [
    # (session_id, model, task, in, out, cache_r, cache_w, reasoning, first, last, calls)
    ("sess-hm-1", "mock-model-c", "chat", 1000, 100, 2000, 20, 30, 1767225600.0, 1767225660.0, 5),
    ("sess-hm-1", "mock-model-c", "tool", 50, 10, 0, 0, 0, 1767225600.0, 1767225601.0, 1),
]

DSH_LINES = [
    {"type": "session", "version": 4, "id": "session-dsh-1",
     "createdAt": 1767225600000, "cwd": "C:\\mock", "isSeeded": False},
    {"type": "request/context", "seq": 1, "time": 1767225601000,
     "data": {"model": "mock-model-d", "provider": "mock-provider"}},
    {"type": "assistant/message", "seq": 2, "time": 1767225602000,
     "data": {"turn": 1, "step": 1,
              "message": {"source": {"model": "mock-model-d", "provider": "mock-provider"}},
              "usage": {"inputTokens": 300, "outputTokens": 25,
                        "cacheReadTokens": 700, "cacheWriteTokens": 4,
                        "reasoningTokens": 8, "totalTokens": 1029},
              # streamed duplicate of the same call -- must be ignored
              "stream": [{"type": "chunk", "chunk": {"usage": {"inputTokens": 300}}}]}},
    {"type": "assistant/message", "seq": 3, "time": 1767225603000,
     "data": {"turn": 1, "step": 2,
              "message": {"source": {"model": "mock-model-d"}},
              "usage": {"inputTokens": 10, "outputTokens": 2,
                        "cacheReadTokens": 0}}},  # no totalTokens -> computed
    {"type": "assistant/chunk", "seq": 4, "time": 1767225604000,
     "data": {"chunk": {"type": "usage", "usage": {"inputTokens": 999999}}}},
    "{BROKEN",
]


class _AdapterTestCase(unittest.TestCase):
    """Shared plumbing: temp DB, patched detect(), two scans, totals compare."""

    adapter_cls = None  # type: ignore
    expected = {}  # type: ignore

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_test_")
        self.db = self.root / "test.db"
        self.store = Store(self.db)
        self.adapter = self.adapter_cls()  # type: ignore

    def tearDown(self) -> None:
        self.store.close()
        drop_temp_dir(self.root)

    def _patch_detect(self, sources: list[Source]) -> None:
        self.adapter.detect = lambda: sources  # type: ignore[method-assign]

    def _run_twice(self) -> None:
        """Scan twice with the persisted checkpoints; assert identical totals."""
        r1 = self.adapter.scan(None)
        c1 = self.store.upsert_events(r1.events)
        self.store.save_checkpoints(r1.checkpoints)
        t1 = self.store.totals()
        cps = self.store.load_checkpoints()

        r2 = self.adapter.scan(cps)
        c2 = self.store.upsert_events(r2.events)
        self.store.save_checkpoints(r2.checkpoints)
        t2 = self.store.totals()

        self.assertEqual(t1, t2, "second scan changed stored totals (not idempotent)")
        self.assertEqual(c2["inserted"], 0, "second scan inserted duplicate rows")
        self.assertEqual(self.store.count(), c1["inserted"] + c1["updated"])
        return t1, r1

    def assertTokens(self, t: dict, **kw: int) -> None:
        for k, v in kw.items():
            self.assertEqual(t[k], v, f"{k}: expected {v}, got {t[k]}")


class TestCodex(_AdapterTestCase):
    adapter_cls = CodexAdapter

    def test_mapping_and_idempotency(self) -> None:
        p = self.root / "rollout-mock.jsonl"
        p.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in CODEX_LINES) + "\n", encoding="utf-8")
        self._patch_detect([Source(path=p, kind="jsonl", agent="codex")])
        t, r = self._run_twice()
        self.assertEqual(len(r.events), 2, "cumulative rollups must not become events")
        self.assertTrue(any("malformed" in w for w in r.warnings))
        # 100+50 / 20+10 / 300+0 / 5+0 / 7+2 ; totals 425+60
        self.assertTokens(t, events=2, input_tokens=150, output_tokens=30,
                          cache_read_tokens=300, cache_write_tokens=5,
                          reasoning_tokens=9, total_tokens=485)
        self.assertEqual(r.events[0].model, "mock-model-a")
        self.assertEqual(r.events[0].session_id, "sess-cx-1")
        self.assertEqual(r.events[0].timestamp, 1767225601000)


class TestClaude(_AdapterTestCase):
    adapter_cls = ClaudeAdapter

    def test_mapping_and_idempotency(self) -> None:
        p = self.root / "claude-mock.jsonl"
        p.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in CLAUDE_LINES) + "\n", encoding="utf-8")
        self._patch_detect([Source(path=p, kind="jsonl", agent="claude")])
        t, r = self._run_twice()
        self.assertEqual(len(r.events), 2, "cost-state snapshot must not become an event")
        self.assertTrue(any("malformed" in w for w in r.warnings))
        # total computed = in+out+cache_r+cache_w = 636 + 15 = 651
        self.assertTokens(t, events=2, input_tokens=210, output_tokens=35,
                          cache_read_tokens=400, cache_write_tokens=6,
                          reasoning_tokens=9, total_tokens=651)
        self.assertEqual(r.events[0].model, "mock-model-b")
        self.assertEqual(r.events[0].timestamp, 1767312001000)


class TestWorkBuddy(_AdapterTestCase):
    adapter_cls = WorkBuddyAdapter

    def test_mapping_and_idempotency(self) -> None:
        p = self.root / "wb-mock.jsonl"
        p.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in WORKBUDDY_LINES) + "\n", encoding="utf-8")
        self._patch_detect([Source(path=p, kind="jsonl", agent="workbuddy")])
        t, r = self._run_twice()
        self.assertEqual(len(r.events), 2)
        self.assertTrue(any("malformed" in w for w in r.warnings))
        # 500+20 / 40+3 / 190+0 / 10+0 / 11+0 ; total 740+23
        self.assertTokens(t, events=2, input_tokens=520, output_tokens=43,
                          cache_read_tokens=190, cache_write_tokens=10,
                          reasoning_tokens=11, total_tokens=763)
        self.assertEqual(r.events[0].timestamp, 1789100544465)


class TestHermes(_AdapterTestCase):
    adapter_cls = HermesAdapter

    def test_mapping_and_idempotency(self) -> None:
        p = self.root / "state.db"
        con = sqlite3.connect(p)
        con.execute("""CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
            billing_mode TEXT, task TEXT, api_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER,
            estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,
            cost_source TEXT, first_seen REAL, last_seen REAL)""")
        con.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(a, b, "p", "u", "m", c, k, i, o, cr, cw, rs, 12.34, 56.78,
                          "ok", "x", f, l) for (a, b, c, i, o, cr, cw, rs, f, l, k) in HERMES_ROWS])
        con.commit()
        con.close()
        self._patch_detect([Source(path=p, kind="sqlite", agent="hermes")])
        t, r = self._run_twice()
        self.assertEqual(len(r.events), 2)
        # 1000+50 / 100+10 / 2000+0 / 20+0 / 30+0 ; total = 1050+110+2000+20
        self.assertTokens(t, events=2, input_tokens=1050, output_tokens=110,
                          cache_read_tokens=2000, cache_write_tokens=20,
                          reasoning_tokens=30, total_tokens=3180)
        self.assertEqual(r.events[0].model, "mock-model-c")
        self.assertEqual(r.events[0].timestamp, 1767225660000)

    def test_growing_aggregate_row_updates_in_place(self) -> None:
        """A cumulative row that grows must overwrite, never add up.

        ``session_model_usage`` is an aggregate: while the session is active,
        ``last_seen`` and the token columns both move. The old event_key carried
        ``last_seen_ms``, so every scan minted a new key, the UPSERT missed, and
        the same lifetime totals were stored again on top of themselves --
        the session counted twice, three times, once per scan.

        Here one session is scanned at two points in its life: first with
        1000/100, then with the totals risen to 1500/150. The stored figures
        must be the LATEST totals, not 2500/250.
        """
        p = self.root / "state.db"
        con = sqlite3.connect(p)
        con.execute("""CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
            billing_mode TEXT, task TEXT, api_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER,
            estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,
            cost_source TEXT, first_seen REAL, last_seen REAL)""")
        con.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("s1", "m1", "p", "u", "m", "chat", 1,
                     1000, 100, 0, 0, 0, 0.0, 0.0, "ok", "x", 1767225600.0, 1767225660.0))
        con.commit()
        con.close()
        self._patch_detect([Source(path=p, kind="sqlite", agent="hermes")])

        r1 = self.adapter.scan(None)
        c1 = self.store.upsert_events(r1.events)
        self.assertEqual(c1["inserted"], 1)
        self.assertEqual(self.store.totals()["input_tokens"], 1000)

        # The session kept working: totals grew and last_seen moved forward.
        con = sqlite3.connect(p)
        con.execute("UPDATE session_model_usage SET input_tokens=1500, "
                    "output_tokens=150, last_seen=1767225900.0")
        con.commit()
        con.close()

        r2 = self.adapter.scan(None)
        c2 = self.store.upsert_events(r2.events)
        self.assertEqual(c2["inserted"], 0, "growth must update the row, not add one")
        t = self.store.totals()
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(t["input_tokens"], 1500)
        self.assertEqual(t["output_tokens"], 150)

    def test_event_key_is_stable_across_scans(self) -> None:
        """The key must not carry a value that changes as the row grows."""
        p = self.root / "state.db"
        con = sqlite3.connect(p)
        con.execute("""CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, task TEXT, api_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER,
            first_seen REAL, last_seen REAL)""")
        con.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("s1", "m1", "chat", 1, 10, 1, 0, 0, 0, 100.0, 200.0))
        con.commit()
        con.close()
        self._patch_detect([Source(path=p, kind="sqlite", agent="hermes")])

        first = self.adapter.scan(None).events[0].event_key
        con = sqlite3.connect(p)
        con.execute("UPDATE session_model_usage SET last_seen=999.0, input_tokens=99")
        con.commit()
        con.close()
        second = self.adapter.scan(None).events[0].event_key
        self.assertEqual(first, second)


class TestDsh(_AdapterTestCase):
    adapter_cls = DshAdapter

    def test_mapping_and_idempotency(self) -> None:
        from _zstd import zstd_compress

        p = self.root / "session.v4.jsonl.zstd"
        # Two independent frames, exactly like a real DSH append-only log.
        f1 = [x for x in DSH_LINES[:3]]
        f2 = [x for x in DSH_LINES[3:]]
        frame1 = "\n".join(json.dumps(x) if isinstance(x, dict) else x for x in f1) + "\n"
        frame2 = "\n".join(json.dumps(x) if isinstance(x, dict) else x for x in f2) + "\n"
        p.write_bytes(zstd_compress(frame1.encode("utf-8")) + zstd_compress(frame2.encode("utf-8")))
        self._patch_detect([Source(path=p, kind="zstd-jsonl", agent="dsh")])
        t, r = self._run_twice()
        self.assertEqual(len(r.events), 2, "assistant/chunk duplicates must be ignored")
        self.assertTrue(any("malformed" in w for w in r.warnings))
        # 300+10 / 25+2 / 700+0 / 4+0 / 8+0 ; total 1029 + 12
        self.assertTokens(t, events=2, input_tokens=310, output_tokens=27,
                          cache_read_tokens=700, cache_write_tokens=4,
                          reasoning_tokens=8, total_tokens=1041)
        self.assertEqual(r.events[0].model, "mock-model-d")
        self.assertEqual(r.events[0].session_id, "session-dsh-1")
        self.assertEqual(r.events[0].timestamp, 1767225602000)
        # seq-based event keys must be stable
        self.assertEqual(r.events[0].event_key, "dsh|session-dsh-1|2")


class TestWorkBuddyIntl(_AdapterTestCase):
    """Same record format as WorkBuddy, different data dir and event_key."""

    adapter_cls = WorkBuddyIntlAdapter

    def test_mapping_and_idempotency(self) -> None:
        p = self.root / "wb-intl-mock.jsonl"
        p.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in WORKBUDDY_LINES) + "\n", encoding="utf-8")
        self._patch_detect([Source(path=p, kind="jsonl", agent="workbuddy-intl")])
        t, r = self._run_twice()
        self.assertEqual(len(r.events), 2)
        self.assertTokens(t, events=2, input_tokens=520, output_tokens=43,
                          cache_read_tokens=190, cache_write_tokens=10,
                          reasoning_tokens=11, total_tokens=763)
        self.assertEqual(r.events[0].event_key.split("|")[0], "workbuddy-intl")


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _len_field(field: int, payload: bytes) -> bytes:
    return _key(field, 2) + _varint(len(payload)) + payload


def _varint_field(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value)


class TestGemini(_AdapterTestCase):
    """Gemini/Antigravity stores usage as protobuf BLOBs inside SQLite."""

    adapter_cls = GeminiAdapter

    def test_mapping_and_idempotency(self) -> None:
        p = self.root / "conv-mock.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
        con.execute(
            "CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, "
            "has_subtrajectory NUMERIC, metadata BLOB, error_details BLOB, "
            "permissions BLOB, task_details BLOB, render_info BLOB, "
            "step_payload BLOB, step_format INTEGER)"
        )

        # usage = {2:input, 3:output(incl. thinking), 5:cacheRead, 9:thinking, 10:reply}
        def gen(model: str, inp: int, out: int, cached: int, think: int, reply: int) -> bytes:
            usage = (
                _varint_field(2, inp)
                + _varint_field(3, out)
                + _varint_field(5, cached)
                + _varint_field(9, think)
                + _varint_field(10, reply)
            )
            record = _len_field(4, usage) + _len_field(19, model.encode())
            return _len_field(1, record)  # outer wrapper

        con.execute("INSERT INTO gen_metadata VALUES (?,?,?)",
                    (0, gen("mock-gemini-a", 1000, 200, 300, 75, 125), 0))
        con.execute("INSERT INTO gen_metadata VALUES (?,?,?)",
                    (1, gen("mock-gemini-a", 50, 20, 0, 5, 15), 0))

        # steps.metadata holds google.protobuf.Timestamp under f1.f1
        def step_meta(secs: int) -> bytes:
            ts = _varint_field(1, secs)
            return _len_field(1, ts)

        con.execute("INSERT INTO steps (idx, step_type, metadata) VALUES (?,?,?)",
                    (0, 15, step_meta(1767225601)))
        con.execute("INSERT INTO steps (idx, step_type, metadata) VALUES (?,?,?)",
                    (1, 15, step_meta(1767225602)))
        con.commit()
        con.close()

        self._patch_detect([Source(path=p, kind="sqlite", agent="gemini")])
        t, r = self._run_twice()
        self.assertEqual(len(r.events), 2, "one event per generation record")
        # output = reply (4.10), NOT 4.3 which already includes thinking
        # 1000+50 / 125+15 / 300+0 / 0 / 75+5
        self.assertTokens(t, events=2, input_tokens=1050, output_tokens=140,
                          cache_read_tokens=300, cache_write_tokens=0,
                          reasoning_tokens=80, total_tokens=1490)
        self.assertEqual(r.events[0].model, "mock-gemini-a")
        self.assertEqual(r.events[0].timestamp, 1767225601000)
        self.assertEqual(r.events[0].event_key, "gemini|conv-mock|0")

    def test_garbage_blob_is_dropped_not_stored(self) -> None:
        """A format change must produce NO event, never a plausible wrong number."""
        p = self.root / "conv-bad.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
        con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, metadata BLOB)")
        # absurd token counts -> must be rejected by the sanity bound
        usage = _varint_field(2, 999_999_999) + _varint_field(3, 1)
        record = _len_field(4, usage) + _len_field(19, b"mock-gemini-a")
        con.execute("INSERT INTO gen_metadata VALUES (?,?,?)", (0, _len_field(1, record), 0))
        con.commit()
        con.close()
        self._patch_detect([Source(path=p, kind="sqlite", agent="gemini")])
        r = self.adapter.scan(None)
        self.assertEqual(len(r.events), 0, "out-of-range tokens must be dropped")

    def test_failed_generation_without_tokens_is_not_usage(self) -> None:
        """A failed call must not surface as an ``unknown`` model row.

        Antigravity stores failed generations (measured: ``FAILED_PRECONDITION
        (code 400): User location is not supported``) that carry no model field
        and zero tokens. They are not usage of anything, so no event is made.
        """
        p = self.root / "conv-failed.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
        con.execute("CREATE TABLE steps (idx INTEGER, metadata BLOB)")
        # Exactly the shape a failed generation has: no f19 model, empty usage.
        record = _len_field(4, b"")
        con.execute("INSERT INTO gen_metadata VALUES (?,?,?)",
                    (0, _len_field(1, record), 0))
        con.commit()
        con.close()
        self._patch_detect([Source(path=p, kind="sqlite", agent="gemini")])
        r = self.adapter.scan(None)
        self.assertEqual(len(r.events), 0, "a zero-token failure is not a usage row")


class TestStoreContract(unittest.TestCase):
    def test_usage_event_normalization(self) -> None:
        from agent_token_radar.base import UsageEvent

        e = UsageEvent(event_key="k", agent="x", input_tokens=1, output_tokens=2,
                       cache_read_tokens=3, cache_write_tokens=4, reasoning_tokens=5)
        n = e.normalized()
        self.assertEqual(n.total_tokens, 10, "reasoning must not be added twice")
        self.assertEqual(n.reasoning_tokens, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

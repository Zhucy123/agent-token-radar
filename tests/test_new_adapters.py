"""Tests for the agents added in the tokei-sourced batch.

These adapters are for tools NOT installed on this machine, so the fixtures are
hand-built from the documented record shapes (tokei's parsing + our
DISCOVERY/TOKEI-NOTES specs). Each test asserts both the field mapping and
that a second scan changes nothing (idempotency via event_key + UPSERT).
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from _tmp import drop_temp_dir, make_temp_dir

from agent_token_radar.adapters.cmdcode import CommandCodeAdapter
from agent_token_radar.adapters.codebuddy import CodeBuddyAdapter
from agent_token_radar.adapters.copilot import CopilotAdapter
from agent_token_radar.adapters.grok import GrokBuildAdapter
from agent_token_radar.adapters.kimi import KimiCodeAdapter
from agent_token_radar.adapters.muse import MuseCodeAdapter
from agent_token_radar.adapters.pi_prime import PiAgentAdapter, PrimeAgentAdapter
from agent_token_radar.adapters.qoder_cli import QoderCliAdapter, QoderCliCnAdapter
from agent_token_radar.adapters.qwencode import QwenCodeAdapter
from agent_token_radar.adapters.sqlite_family import (
    DevinAdapter,
    MiMoCodeAdapter,
    MiniMaxAdapter,
    OpenCodeAdapter,
    QoderIdeAdapter,
    ZCodeAdapter,
)
from agent_token_radar.base import Source
from agent_token_radar.store import Store


class _Base(unittest.TestCase):
    adapter_cls = None  # type: ignore

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_new_")
        self.store = Store(self.root / "t.db")
        self.adapter = self.adapter_cls()  # type: ignore

    def tearDown(self) -> None:
        self.store.close()
        drop_temp_dir(self.root)

    def patch(self, sources: list[Source]) -> None:
        self.adapter.detect = lambda: sources  # type: ignore[method-assign]

    def twice(self):
        r1 = self.adapter.scan(None)
        c1 = self.store.upsert_events(r1.events)
        self.store.save_checkpoints(r1.checkpoints)
        t1 = self.store.totals()
        r2 = self.adapter.scan(self.store.load_checkpoints())
        c2 = self.store.upsert_events(r2.events)
        self.store.save_checkpoints(r2.checkpoints)
        t2 = self.store.totals()
        self.assertEqual(t1, t2, "second scan changed totals (not idempotent)")
        self.assertEqual(c2["inserted"], 0, "second scan inserted duplicates")
        return t1, r1

    def assertTok(self, t: dict, **kw: int) -> None:
        for k, v in kw.items():
            self.assertEqual(t[k], v, f"{k}: expected {v}, got {t[k]}")


def _dump(rows: list) -> str:
    return "\n".join(json.dumps(x) if isinstance(x, dict) else x for x in rows) + "\n"


class TestPi(_Base):
    adapter_cls = PiAgentAdapter

    def test_mapping_and_idempotency(self) -> None:
        rows = [
            {"type": "session", "id": "pi-s1", "cwd": "C:\\proj"},
            {"type": "message", "timestamp": "2026-01-05T00:00:01.000Z",
             "message": {"id": "m1", "role": "assistant", "responseModel": "pi-model",
                         "usage": {"input": 1000, "output": 200, "cacheRead": 400,
                                   "cacheWrite": 50, "reasoning": 30}}},
            {"type": "message", "timestamp": "2026-01-05T00:00:02.000Z",
             "message": {"id": "m2", "role": "user", "usage": {"input": 9}}},
            {"type": "message", "timestamp": "2026-01-05T00:00:03.000Z",
             "message": {"id": "m3", "role": "assistant",
                         "usage": {"input": 100, "output": 10}}},
            "{BROKEN",
        ]
        p = self.root / "pi.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="pi")])
        t, r = self.run_twice() if hasattr(self, "run_twice") else self.twice()
        self.assertEqual(len(r.events), 2, "non-assistant rows must be ignored")
        # inclusive input: (1000-400-50)=550, (100-0-0)=100 -> 650
        self.assertTok(t, events=2, input_tokens=650, output_tokens=210,
                       cache_read_tokens=400, cache_write_tokens=50,
                       reasoning_tokens=30, total_tokens=1310)
        self.assertEqual(r.events[0].model, "pi-model")
        self.assertEqual(r.events[0].session_id, "pi-s1")


class TestPrime(_Base):
    adapter_cls = PrimeAgentAdapter

    def test_model_change_and_mapping(self) -> None:
        rows = [
            {"type": "session", "id": "pr-s1"},
            {"type": "model_change", "provider": "anthropic", "modelId": "prime-m"},
            {"type": "message", "timestamp": "2026-01-06T00:00:01.000Z",
             "message": {"id": "p1", "role": "assistant",
                         "usage": {"input": 500, "output": 50, "cacheRead": 100}}},
        ]
        p = self.root / "prime.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="prime")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1)
        self.assertTok(t, events=1, input_tokens=400, output_tokens=50,
                       cache_read_tokens=100, total_tokens=550)
        self.assertEqual(r.events[0].model, "anthropic/prime-m")


class TestCodeBuddy(_Base):
    adapter_cls = CodeBuddyAdapter

    def test_three_usage_locations(self) -> None:
        rows = [
            {"id": "cb1", "sessionId": "cb-s1", "timestamp": 1767225601000,
             "providerData": {"requestModelId": "cb-model",
                              "messageId": "mid-1",
                              "rawUsage": {"prompt_tokens": 500, "completion_tokens": 40,
                                           "cache_read_input_tokens": 190,
                                           "cache_creation_input_tokens": 10}}},
            {"id": "cb2", "sessionId": "cb-s1", "timestamp": 1767225602000,
             "message": {"usage": {"input_tokens": 20, "output_tokens": 5}}},
            "{BROKEN",
        ]
        p = self.root / "cb.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="codebuddy")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 2)
        # additive here: no total field proving inclusivity
        self.assertTok(t, events=2, input_tokens=520, output_tokens=45,
                       cache_read_tokens=190, cache_write_tokens=10,
                       total_tokens=765)
        self.assertEqual(r.events[0].model, "cb-model")


class TestQoderCli(_Base):
    adapter_cls = QoderCliAdapter

    def test_inclusive_input(self) -> None:
        rows = [
            {"type": "runtime-config", "model": "qd-model"},
            {"type": "assistant", "timestamp": "2026-01-07T00:00:01.000Z",
             "message": {"model": "qd-model",
                         "usage": {"input_tokens": 1000, "output_tokens": 100,
                                   "cache_read_input_tokens": 600,
                                   "cache_creation_input_tokens": 50,
                                   "request_id": "rq-1"}}},
        ]
        p = self.root / "qd.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="qodercli")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1)
        # fresh = 1000 - 600 - 50 = 350
        self.assertTok(t, events=1, input_tokens=350, output_tokens=100,
                       cache_read_tokens=600, cache_write_tokens=50,
                       total_tokens=1100)
        self.assertEqual(r.events[0].event_key, "qodercli|rq-1")


class TestQoderCn(_Base):
    adapter_cls = QoderCliCnAdapter

    def test_separate_namespace(self) -> None:
        rows = [{"type": "assistant", "timestamp": "2026-01-07T00:00:01.000Z",
                 "message": {"usage": {"input_tokens": 100, "output_tokens": 10,
                                       "request_id": "cn-1"}}}]
        p = self.root / "qdcn.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="qodercli-cn")])
        t, r = self.twice()
        self.assertEqual(r.events[0].event_key, "qodercli-cn|cn-1")
        self.assertTok(t, events=1, input_tokens=100, output_tokens=10, total_tokens=110)


class TestCommandCode(_Base):
    adapter_cls = CommandCodeAdapter

    def test_additive_buckets(self) -> None:
        rows = [
            {"type": "session", "id": "cc-s1"},
            {"role": "assistant", "type": "message", "id": "c1",
             "timestamp": "2026-01-08T00:00:01.000Z",
             "usage": {"inputTokens": 300, "outputTokens": 30,
                       "cacheReadTokens": 100, "cacheWriteTokens": 5}},
        ]
        p = self.root / "cc.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="cmdcode")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1)
        self.assertTok(t, events=1, input_tokens=300, output_tokens=30,
                       cache_read_tokens=100, cache_write_tokens=5, total_tokens=435)


class TestKimi(_Base):
    adapter_cls = KimiCodeAdapter

    def test_both_protocols(self) -> None:
        rows = [
            {"type": "usage.record", "time": 1767225601000, "model": "kimi-m",
             "usage": {"inputOther": 400, "output": 40,
                       "inputCacheRead": 100, "inputCacheCreation": 10}},
            {"type": "StatusUpdate", "timestamp": 1767225602000,
             "payload": {"model": "kimi-m", "message_id": "k-2",
                         "token_usage": {"input_other": 50, "output": 5,
                                         "input_cache_read": 0,
                                         "input_cache_creation": 0}}},
        ]
        p = self.root / "sess-k1" / "agents" / "a1" / "wire.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="kimi")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 2)
        # inputOther is already fresh -> additive
        self.assertTok(t, events=2, input_tokens=450, output_tokens=45,
                       cache_read_tokens=100, cache_write_tokens=10, total_tokens=605)
        self.assertEqual(r.events[0].session_id, "sess-k1")


class TestMuse(_Base):
    adapter_cls = MuseCodeAdapter

    def test_microsecond_timestamp(self) -> None:
        rows = [
            {"payload_type": "runtime.session",
             "payload": {"kind": "run",
                         "run_id": "run-1",
                         "event": {"kind": "model_completed", "model": "muse-m",
                                   "usage": {"input_tokens": 800, "output_tokens": 80,
                                             "cached_tokens": 300,
                                             "cache_write_tokens": 20,
                                             "reasoning_tokens": 10}}},
             "record": {"recorded_at": 1767225601000000,  # microseconds
                        "source_run_record_id": "rec-1",
                        "stream": {"id": "muse-s1"}}},
        ]
        p = self.root / "muse.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="muse")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1)
        # input inclusive: 800-300=500
        self.assertTok(t, events=1, input_tokens=500, output_tokens=80,
                       cache_read_tokens=300, cache_write_tokens=20,
                       reasoning_tokens=10, total_tokens=900)
        self.assertEqual(r.events[0].timestamp, 1767225601000)
        self.assertEqual(r.events[0].event_key, "muse|rec-1")


class TestGroK(_Base):
    adapter_cls = GrokBuildAdapter

    def test_inference_done_lines(self) -> None:
        self.adapter._home = lambda: self.root  # type: ignore[method-assign]
        (self.root / "logs").mkdir()
        rows = [
            {"msg": "startup timing", "ts": "2026-01-09T00:00:00Z"},
            {"msg": "shell.turn.inference_done", "sid": "g-s1",
             "ts": "2026-01-09T00:00:01Z",
             "ctx": {"prompt_tokens": 1000, "cached_prompt_tokens": 400,
                     "completion_tokens": 120, "reasoning_tokens": 20,
                     "loop_index": 0, "attempts": 1}},
            {"msg": "auth: silent refresh"},
        ]
        p = self.root / "logs" / "unified.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="grok")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1, "only inference_done lines count")
        # in = prompt-cached = 600 ; out = completion-reasoning = 100
        self.assertTok(t, events=1, input_tokens=600, output_tokens=100,
                       cache_read_tokens=400, reasoning_tokens=20,
                       total_tokens=1100)


class TestCopilot(_Base):
    adapter_cls = CopilotAdapter

    def test_output_only(self) -> None:
        rows = [
            {"type": "session.shutdown", "sessionId": "cu-s1",
             "data": {"sessionStartTime": "2026-01-10T00:00:00Z",
                      "totalPremiumRequests": 3,
                      "modelMetrics": {"cop-model": {
                          "usage": {"outputTokens": 500, "reasoningTokens": 100,
                                    "inputTokens": 9999}}}}},
        ]
        p = self.root / "events.jsonl"
        p.write_text(_dump(rows), encoding="utf-8")
        self.patch([Source(path=p, kind="jsonl", agent="copilot")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1)
        # Copilot is output-only: input/cache are deliberately zeroed.
        self.assertTok(t, events=1, input_tokens=0, output_tokens=400,
                       reasoning_tokens=100, total_tokens=400)
        self.assertEqual(r.events[0].model, "cop-model")


class TestZCode(_Base):
    adapter_cls = ZCodeAdapter

    def test_inclusive_and_reasoning_split(self) -> None:
        p = self.root / "db.sqlite"
        con = sqlite3.connect(p)
        con.execute(
            "CREATE TABLE model_usage (id TEXT, session_id TEXT, model_id TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, "
            "cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER, "
            "started_at INTEGER, completed_at INTEGER)"
        )
        con.execute("INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("z1", "z-s1", "z-model", 1000, 200, 50, 100, 300,
                     1767225600000, 1767225601000))
        con.commit()
        con.close()
        self.patch([Source(path=p, kind="sqlite", agent="zcode")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1)
        # in = 1000-300-100 = 600
        self.assertTok(t, events=1, input_tokens=600, output_tokens=200,
                       cache_read_tokens=300, cache_write_tokens=100,
                       reasoning_tokens=50, total_tokens=1200)


class TestOpenCode(_Base):
    adapter_cls = OpenCodeAdapter

    def test_tokens_blob(self) -> None:
        p = self.root / "opencode.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, "
                    "type TEXT, data TEXT, time_created INTEGER)")
        data = {"role": "assistant", "modelID": "oc-model",
                "tokens": {"input": 900, "output": 90, "reasoning": 15,
                           "cache": {"read": 400, "write": 0}},
                "time": {"created": 1767225601000}}
        con.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                    ("m1", "oc-s1", "assistant", json.dumps(data), 1767225601000))
        con.commit()
        con.close()
        self.patch([Source(path=p, kind="sqlite", agent="opencode")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1)
        # inclusive: 900-400 = 500
        self.assertTok(t, events=1, input_tokens=500, output_tokens=90,
                       cache_read_tokens=400, reasoning_tokens=15, total_tokens=990)
        self.assertEqual(r.events[0].model, "oc-model")


class TestMiMoCode(_Base):
    adapter_cls = MiMoCodeAdapter

    def test_same_schema_as_opencode(self) -> None:
        p = self.root / "mimocode.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, "
                    "type TEXT, data TEXT, time_created INTEGER)")
        data = {"role": "assistant", "modelID": "mm-model",
                "tokens": {"input": 200, "output": 20,
                           "cache": {"read": 50, "write": 0}},
                "time": {"created": 1767225601000}}
        con.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                    ("m1", "mm-s1", "assistant", json.dumps(data), 1767225601000))
        con.commit()
        con.close()
        self.patch([Source(path=p, kind="sqlite", agent="mimocode")])
        t, r = self.twice()
        self.assertTok(t, events=1, input_tokens=150, output_tokens=20,
                       cache_read_tokens=50, total_tokens=220)


class TestDevin(_Base):
    adapter_cls = DevinAdapter

    def test_metrics_blob_and_dedup(self) -> None:
        p = self.root / "sessions.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE sessions (id TEXT, model TEXT, working_directory TEXT)")
        con.execute("CREATE TABLE message_nodes (session_id TEXT, chat_message TEXT, created_at INTEGER)")
        con.execute("INSERT INTO sessions VALUES (?,?,?)", ("d-s1", "dev-model", "/w"))
        cm = {"metadata": {"metrics": {"input_tokens": 500, "output_tokens": 50,
                                       "cache_read_tokens": 200,
                                       "cache_creation_tokens": 0},
                           "created_at": "2026-01-11T00:00:01.000000Z",
                           "generation_model": "dev-model"}}
        # written twice, as Devin really does
        for _ in range(2):
            con.execute("INSERT INTO message_nodes VALUES (?,?,?)",
                        ("d-s1", json.dumps(cm), 1767225601000))
        con.commit()
        con.close()
        self.patch([Source(path=p, kind="sqlite", agent="devin")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1, "sibling-node duplicate must collapse")
        self.assertTok(t, events=1, input_tokens=500, output_tokens=50,
                       cache_read_tokens=200, total_tokens=750)


class TestMiniMax(_Base):
    adapter_cls = MiniMaxAdapter

    def test_reasoning_only_when_total_proves_it(self) -> None:
        p = self.root / "runtime-state.sqlite"
        con = sqlite3.connect(p)
        con.execute(
            "CREATE TABLE local_runtime_token_usage (id TEXT, session_id TEXT, ts INTEGER, "
            "input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER, raw TEXT)"
        )
        # trustworthy: raw.totalTokens == sum incl. reasoning
        con.execute("INSERT INTO local_runtime_token_usage VALUES (?,?,?,?,?,?,?,?,?)",
                    ("x1", "mm-s1", 1767225601, 100, 20, 5, 30, 0,
                     json.dumps({"totalTokens": 155})))
        # NOT provable -> reasoning dropped
        con.execute("INSERT INTO local_runtime_token_usage VALUES (?,?,?,?,?,?,?,?,?)",
                    ("x2", "mm-s1", 1767225602, 10, 2, 7, 0, 0, None))
        con.commit()
        con.close()
        self.patch([Source(path=p, kind="sqlite", agent="minimax")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 2)
        self.assertTok(t, events=2, input_tokens=110, output_tokens=22,
                       cache_read_tokens=30, reasoning_tokens=5, total_tokens=162)


class TestQoderIde(_Base):
    adapter_cls = QoderIdeAdapter

    def test_token_info_json(self) -> None:
        p = self.root / "local.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE chat_message (id TEXT, session_id TEXT, "
                    "request_id TEXT, token_info TEXT, model_info TEXT, gmt_create INTEGER)")
        con.execute("INSERT INTO chat_message VALUES (?,?,?,?,?,?)",
                    ("qi1", "qi-s1", "r1",
                     json.dumps({"prompt_tokens": 400, "completion_tokens": 40,
                                 "cached_tokens": 100}),
                     json.dumps({"model_key": "qide-model"}), 1767225601000))
        con.commit()
        con.close()
        self.patch([Source(path=p, kind="sqlite", agent="qoder-ide")])
        t, r = self.twice()
        self.assertEqual(len(r.events), 1)
        self.assertTok(t, events=1, input_tokens=400, output_tokens=40,
                       cache_read_tokens=100, total_tokens=540)
        self.assertEqual(r.events[0].model, "qide-model")


class TestQwenCode(_Base):
    adapter_cls = QwenCodeAdapter

    def test_per_request_wins_over_summary(self) -> None:
        self.adapter._roots = lambda: [self.root]  # type: ignore[method-assign]
        (self.root / "usage").mkdir()
        # per-request file for session s1
        (self.root / "usage" / "token-usage-1.jsonl").write_text(_dump([
            {"schemaVersion": 1, "id": "q1", "sessionId": "q-s1", "model": "qw-model",
             "inputTokens": 1000, "cachedTokens": 400, "outputTokens": 100,
             "thoughtsTokens": 20, "timestamp": 1767225601000},
        ]), encoding="utf-8")
        # legacy summary ALSO has q-s1 (must be skipped) plus q-s2 (kept)
        (self.root / "usage_record.jsonl").write_text(_dump([
            {"version": 1, "sessionId": "q-s1",
             "models": {"qw-model": {"inputTokens": 9999, "outputTokens": 9999}}},
            {"version": 1, "sessionId": "q-s2",
             "models": {"qw-model": {"inputTokens": 300, "cachedTokens": 100,
                                     "outputTokens": 30}}},
        ]), encoding="utf-8")

        def detect():
            usage = self.root / "usage" / "token-usage-1.jsonl"
            summ = self.root / "usage_record.jsonl"
            return [Source(path=usage, kind="jsonl", agent="qwencode"),
                    Source(path=summ, kind="jsonl", agent="qwencode")]

        self.adapter.detect = detect  # type: ignore[method-assign]
        t, r = self.twice()
        self.assertEqual(len(r.events), 2, "summary must not re-count covered session")
        # q1: in = 1000-400 = 600 ; q-s2: in = 300-100 = 200
        self.assertTok(t, events=2, input_tokens=800, output_tokens=130,
                       cache_read_tokens=500, reasoning_tokens=20,
                       total_tokens=1430)


class TestIconCoverage(unittest.TestCase):
    """Every registered adapter must have an icon (spec requirement)."""

    def test_all_adapters_have_icons(self) -> None:
        from agent_token_radar import registry
        from agent_token_radar.icons import ICONS

        names = [c.name for c in registry.discover_adapter_classes()]
        self.assertTrue(names)
        missing = [n for n in names if n not in ICONS]
        self.assertEqual(missing, [], f"adapters without icon: {missing}")

    def test_icon_entries_are_wellformed(self) -> None:
        from agent_token_radar.icons import ICONS

        for name, ic in ICONS.items():
            self.assertIn("kind", ic, name)
            self.assertIn("official", ic, name)
            self.assertIn("label", ic, name)
            self.assertTrue(ic["label"], f"{name}: empty label")
            if ic["kind"] == "paths":
                self.assertTrue(ic["paths"], f"{name}: no paths")
                self.assertIn("viewBox", ic, name)
            else:
                self.assertTrue(ic["src"].startswith("data:image/"), f"{name}: bad src")


if __name__ == "__main__":
    unittest.main(verbosity=2)

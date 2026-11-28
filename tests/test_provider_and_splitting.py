"""Regression tests for the provider dimension and the JSONL line splitting.

Two bugs found while adding the provider breakdown to the dashboard:

1. ``str.splitlines()`` splits on U+0085 (NEL) and U+2028/2029 as well as LF.
   Agent logs legitimately contain those characters inside JSON strings, so a
   single record was torn into several "lines". A real DSH session had 66 NEL
   and produced 68 unparsable lines -- with ``split("\\n")`` it is zero.
2. ``provider`` (which backend served the model) was not stored at all, so the
   same model from ``workbuddy`` and from ``tokenrhythm`` collapsed into one row.

These tests use synthetic data only.
"""

from __future__ import annotations

import json
import unittest

from _tmp import drop_temp_dir, make_temp_dir

from agent_token_radar.adapters import dsh as dsh_mod
from agent_token_radar.adapters.dsh import DshAdapter
from agent_token_radar.base import USAGE_COLUMNS, UsageEvent
from agent_token_radar.readers import read_zstd_jsonl_full
from agent_token_radar.store import Store


class TestLineSplitting(unittest.TestCase):
    """A plain .jsonl file needs no zstd: the reader passes it straight through."""

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_split_")

    def tearDown(self) -> None:
        drop_temp_dir(self.root)

    def test_nel_inside_string_does_not_split_record(self) -> None:
        # U+0085 (NEL) is a line boundary for splitlines() but not for JSONL.
        payload = {"type": "assistant/message", "seq": 1,
                   "data": {"text": "line one\u0085line two\u2028still same record"}}
        path = self.root / "session.jsonl"
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

        records = read_zstd_jsonl_full(path)
        self.assertEqual(len(records), 1, "NEL/LS must not split a JSONL record")
        self.assertEqual(records[0]["seq"], 1)
        self.assertIn("\u0085", records[0]["data"]["text"])

    def test_multiple_records_still_parse(self) -> None:
        path = self.root / "session.jsonl"
        rows = [{"type": "x", "seq": i, "data": {"t": f"v{i}"}} for i in range(5)]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

        self.assertEqual([r["seq"] for r in read_zstd_jsonl_full(path)], [0, 1, 2, 3, 4])


class TestProviderDimension(unittest.TestCase):
    def test_usage_columns_include_provider(self) -> None:
        self.assertIn("provider", USAGE_COLUMNS)

    def test_provider_survives_normalization(self) -> None:
        ev = UsageEvent(event_key="k", agent="a", model="m", provider="workbuddy")
        self.assertEqual(ev.normalized().provider, "workbuddy")
        # and it is part of the stored row, in USAGE_COLUMNS order
        row = dict(zip(USAGE_COLUMNS, ev.to_row()))
        self.assertEqual(row["provider"], "workbuddy")

    def test_dsh_reads_provider_from_request_header(self) -> None:
        """The provider is announced by request/header, before the messages."""
        records = [
            {"type": "session", "id": "sess-1"},
            {"type": "request/header", "seq": 1,
             "data": {"header": {"config": {"provider": "workbuddy",
                                            "model": "mock-model-a"}}}},
            {"type": "assistant/message", "seq": 2, "time": 1,
             "data": {"message": {"role": "assistant", "source": {}},
                      "usage": {"inputTokens": 10, "outputTokens": 5}}},
            # a second request switches provider; the sticky value must follow
            {"type": "request/context", "seq": 3,
             "data": {"provider": "tokenrhythm", "model": "mock-model-b"}},
            {"type": "assistant/message", "seq": 4, "time": 2,
             "data": {"message": {"role": "assistant", "source": {}},
                      "usage": {"inputTokens": 20, "outputTokens": 6}}},
        ]
        got = self._scan(records)
        self.assertEqual([e.provider for e in got], ["workbuddy", "tokenrhythm"])
        self.assertEqual([e.model for e in got], ["mock-model-a", "mock-model-b"])

    def test_dsh_explicit_source_wins_over_sticky(self) -> None:
        records = [
            {"type": "session", "id": "sess-2"},
            {"type": "request/header", "seq": 1,
             "data": {"provider": "workbuddy", "model": "sticky-model"}},
            {"type": "assistant/message", "seq": 2, "time": 1,
             "data": {"message": {"role": "assistant",
                                  "source": {"provider": "xiaomi",
                                             "model": "mimo-v2.5"}},
                      "usage": {"inputTokens": 1}}},
        ]
        got = self._scan(records)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].provider, "xiaomi")
        self.assertEqual(got[0].model, "mimo-v2.5")

    def _scan(self, records: list[dict]):
        """Run the DSH adapter over in-memory records (no real file, no zstd)."""
        adapter = DshAdapter()

        root = make_temp_dir("atr_dsh_")
        src_path = root / "session.jsonl.zstd"
        src_path.write_bytes(b"")  # exists so the adapter's .stat() works
        src = type("S", (), {"path": src_path, "kind": "zstd-jsonl",
                             "agent": "dsh", "meta": {}})()

        adapter.detect = lambda: [src]  # type: ignore[method-assign]
        orig = dsh_mod.read_zstd_jsonl_full
        dsh_mod.read_zstd_jsonl_full = lambda *_a, **_k: records  # type: ignore[assignment]
        try:
            self.addCleanup(drop_temp_dir, root)
            return adapter.scan(None).events
        finally:
            dsh_mod.read_zstd_jsonl_full = orig


class TestProviderAggregation(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("atr_agg_")
        self.store = Store(self.root / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        drop_temp_dir(self.root)

    def test_same_model_different_provider_stays_separate(self) -> None:
        events = [
            UsageEvent(event_key="e1", agent="dsh", model="m1",
                       provider="workbuddy", total_tokens=100),
            UsageEvent(event_key="e2", agent="dsh", model="m1",
                       provider="tokenrhythm", total_tokens=200),
        ]
        self.store.upsert_events(events)
        rows = self.store.by_model()
        self.assertEqual(len(rows), 2, "different providers must not be merged")
        by_prov = {r["provider"]: r["total_tokens"] for r in rows}
        self.assertEqual(by_prov, {"workbuddy": 100, "tokenrhythm": 200})

    def test_upsert_fills_provider_on_rescan(self) -> None:
        """A row stored before the column existed is completed by a rescan."""
        self.store.upsert_events([
            UsageEvent(event_key="e1", agent="dsh", model="m1", total_tokens=50)
        ])
        self.assertEqual(self.store.by_model()[0]["provider"], "")
        self.store.upsert_events([
            UsageEvent(event_key="e1", agent="dsh", model="m1",
                       provider="workbuddy", total_tokens=50)
        ])
        rows = self.store.by_model()
        self.assertEqual(len(rows), 1, "same event_key must not duplicate")
        self.assertEqual(rows[0]["provider"], "workbuddy")

    def test_embedded_provider_prefix_is_split_and_merged(self) -> None:
        """`workbuddy/x` + `x` from the same provider are ONE row, not two."""
        self.store.upsert_events([
            UsageEvent(event_key="a", agent="hermes",
                       model="workbuddy/deepseek-v4.1-flash", total_tokens=10),
            UsageEvent(event_key="b", agent="hermes",
                       model="deepseek-v4.1-flash", provider="workbuddy",
                       total_tokens=20),
        ])
        rows = self.store.by_model()
        self.assertEqual(len(rows), 1, "the prefix must not create a second spelling")
        self.assertEqual(rows[0]["model"], "deepseek-v4.1-flash")
        self.assertEqual(rows[0]["provider"], "workbuddy")
        self.assertEqual(rows[0]["total_tokens"], 30, "totals must add up")

    def test_split_leaves_unrelated_names_alone(self) -> None:
        cases = [
            # (model, provider, expected model, expected provider)
            ("gpt-6.1-sol", "", "gpt-6.1-sol", ""),
            ("deepseek-v4.1-flash-expires-on-0910", "tokenrhythm",
             "deepseek-v4.1-flash-expires-on-0910", "tokenrhythm"),
            ("qwen2.5:7b-instruct-q6_K", "ollama-local",
             "qwen2.5:7b-instruct-q6_K", "ollama-local"),
            # prefix stripped only when it matches the provider exactly
            ("workbuddy/x", "tokenrhythm", "workbuddy/x", "tokenrhythm"),
        ]
        for model, provider, em, ep in cases:
            got_m, got_p = Store.split_embedded_provider(model, provider)
            self.assertEqual((got_m, got_p), (em, ep), f"input {model!r}/{provider!r}")


class TestSessionTitles(unittest.TestCase):
    """Session titles: read, stored once per session, and joined back on read.

    Regression guard for a real bug: a DSH title event lives inside a zstd
    frame, so searching the *compressed* bytes for "session/title" finds
    nothing and silently skips every file that has a title.
    """

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_titles_")

    def tearDown(self) -> None:
        drop_temp_dir(self.root)

    def test_store_keeps_one_title_per_session(self) -> None:
        store = Store(self.root / "t.db")
        try:
            store.upsert_titles("dsh", {"session-a": "First title"})
            # A later scan with a newer title replaces it.
            store.upsert_titles("dsh", {"session-a": "Better title"})
            self.assertEqual(store.titles_for("dsh"), {"session-a": "Better title"})

            # An empty title must never blank a good one.
            store.upsert_titles("dsh", {"session-a": ""})
            self.assertEqual(store.titles_for("dsh"), {"session-a": "Better title"})
        finally:
            store.close()

    def test_top_sessions_joins_title_by_either_id_form(self) -> None:
        store = Store(self.root / "t2.db")
        try:
            store.upsert_events([
                UsageEvent(event_key="k1", agent="dsh",
                           session_id="session-abc", total_tokens=100),
            ])
            # The ledger sometimes writes the id without the "session-" prefix.
            store.upsert_titles("dsh", {"abc": "安装 kimodo.cpp"})
            rows = store.top_sessions(limit=5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "安装 kimodo.cpp")
        finally:
            store.close()

    def test_title_event_inside_compressed_log_is_found(self) -> None:
        """The compressed bytes do not contain the literal marker."""
        import os

        from _zstd import zstd_compress

        records = [
            {"type": "session", "id": "session-x1"},
            {"type": "session/title", "seq": 2, "data": {"title": "标题在这里"}},
        ]
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
        blob = zstd_compress(body.encode("utf-8"))
        self.assertNotIn(b"session/title", blob,
                         "precondition: zstd must hide the marker")

        # DSH_HOME points the adapter at a throwaway tree, never the real one.
        sess_dir = self.root / "sessions" / "proj" / "session-x1"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "session.v4.jsonl.zstd").write_bytes(blob)

        adapter = DshAdapter()
        prev = os.environ.get("DSH_HOME")
        os.environ["DSH_HOME"] = str(self.root)
        try:
            sources = adapter.detect()
            titles = adapter.collect_titles(sources)
        finally:
            if prev is None:
                os.environ.pop("DSH_HOME", None)
            else:
                os.environ["DSH_HOME"] = prev

        self.assertEqual(titles.get("session-x1"), "标题在这里")


class TestTitleSourcesByAgent(unittest.TestCase):
    """Each agent keeps its session titles somewhere different.

    Claude  -> custom-title record inside the session .jsonl
    Codex   -> session_index.jsonl (thread_name)
    DSH     -> cost-meter ledger + session/title event
    WorkBuddy/AI -> workbuddy.db, sessions table (title / custom_title)
    Gemini  -> conversation_summaries.db (conversation_id == .db filename)
    """

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_titles2_")

    def tearDown(self) -> None:
        drop_temp_dir(self.root)

    def test_workbuddy_reads_titles_and_prefers_custom(self) -> None:
        import sqlite3

        from agent_token_radar.adapters.workbuddy import _read_session_titles

        db = self.root / "workbuddy.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sessions (id TEXT, title TEXT, custom_title TEXT)")
        con.execute("INSERT INTO sessions VALUES (?,?,?)", ("s1", "auto title", None))
        con.execute("INSERT INTO sessions VALUES (?,?,?)", ("s2", "auto title 2", "重命名后的标题"))
        con.execute("INSERT INTO sessions VALUES (?,?,?)", ("s3", None, None))
        con.commit()
        con.close()

        titles = _read_session_titles(db)
        self.assertEqual(titles.get("s1"), "auto title")
        self.assertEqual(titles.get("s2"), "重命名后的标题", "custom_title must win")
        self.assertNotIn("s3", titles, "a session with no title must not invent one")

    def test_gemini_reads_conversation_summaries(self) -> None:
        import sqlite3

        from agent_token_radar.adapters.gemini import GeminiAdapter

        conv_dir = self.root / "antigravity" / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        idx = self.root / "antigravity" / "conversation_summaries.db"
        con = sqlite3.connect(idx)
        con.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT)")
        con.execute("INSERT INTO conversation_summaries VALUES (?,?)",
                    ("7e767e9f-aaaa", "Change Language To Chinese"))
        con.commit()
        con.close()

        import os
        prev = os.environ.get("TOKEI_GEMINI_DIR")
        os.environ["TOKEI_GEMINI_DIR"] = str(self.root)
        try:
            titles = GeminiAdapter()._session_titles()
        finally:
            if prev is None:
                os.environ.pop("TOKEI_GEMINI_DIR", None)
            else:
                os.environ["TOKEI_GEMINI_DIR"] = prev

        self.assertEqual(titles.get("7e767e9f-aaaa"), "Change Language To Chinese")

    def test_claude_custom_title_record(self) -> None:
        import os
        from pathlib import Path

        from agent_token_radar.adapters.claude import ClaudeAdapter

        proj = self.root / "projects" / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        sess = proj / "sess-1.jsonl"
        rows = [
            {"type": "custom-title", "sessionId": "sess-1", "customTitle": "我的会话标题"},
        ]
        sess.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")

        prev = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root)
        try:
            result = ClaudeAdapter().scan({})
        finally:
            if prev is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = prev

        # Only the title is asserted: CLAUDE_CONFIG_DIR redirects the project
        # directory but not ~/.claude itself, so event counts are not isolated.
        self.assertEqual(result.titles.get("sess-1"), "我的会话标题")
    """Session titles: read, stored once per session, and joined back on read.

    Regression guard for a real bug: a DSH title event lives inside a zstd
    frame, so searching the *compressed* bytes for "session/title" finds
    nothing and silently skips every file that has a title.
    """

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_titles_")

    def tearDown(self) -> None:
        drop_temp_dir(self.root)

    def test_store_keeps_one_title_per_session(self) -> None:
        store = Store(self.root / "t.db")
        try:
            store.upsert_titles("dsh", {"session-a": "First title"})
            # A later scan with a newer title replaces it.
            store.upsert_titles("dsh", {"session-a": "Better title"})
            self.assertEqual(store.titles_for("dsh"), {"session-a": "Better title"})

            # An empty title must never blank a good one.
            store.upsert_titles("dsh", {"session-a": ""})
            self.assertEqual(store.titles_for("dsh"), {"session-a": "Better title"})
        finally:
            store.close()

    def test_top_sessions_joins_title_by_either_id_form(self) -> None:
        store = Store(self.root / "t2.db")
        try:
            store.upsert_events([
                UsageEvent(event_key="k1", agent="dsh",
                           session_id="session-abc", total_tokens=100),
            ])
            # The ledger sometimes writes the id without the "session-" prefix.
            store.upsert_titles("dsh", {"abc": "安装 kimodo.cpp"})
            rows = store.top_sessions(limit=5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "安装 kimodo.cpp")
        finally:
            store.close()

    def test_title_event_inside_compressed_log_is_found(self) -> None:
        """The compressed bytes do not contain the literal marker."""
        import os

        from _zstd import zstd_compress

        records = [
            {"type": "session", "id": "session-x1"},
            {"type": "session/title", "seq": 2, "data": {"title": "标题在这里"}},
        ]
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
        blob = zstd_compress(body.encode("utf-8"))
        self.assertNotIn(b"session/title", blob,
                         "precondition: zstd must hide the marker")

        # DSH_HOME points the adapter at a throwaway tree, never the real one.
        sess_dir = self.root / "sessions" / "proj" / "session-x1"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "session.v4.jsonl.zstd").write_bytes(blob)

        adapter = DshAdapter()
        prev = os.environ.get("DSH_HOME")
        os.environ["DSH_HOME"] = str(self.root)
        try:
            sources = adapter.detect()
            titles = adapter.collect_titles(sources)
        finally:
            if prev is None:
                os.environ.pop("DSH_HOME", None)
            else:
                os.environ["DSH_HOME"] = prev

        self.assertEqual(titles.get("session-x1"), "标题在这里")


class TestProviderProvenance(unittest.TestCase):
    """measured vs inferred, and the routing-alias problem.

    The user's report was "this does not look like WorkBuddy's": the dashboard
    showed ``claude-sonnet-5 | workbuddy``. Both halves of that row were
    explained by magpie's own routing ledger, and these tests lock in how.
    """

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_prov_")

    def tearDown(self) -> None:
        drop_temp_dir(self.root)

    def _resolver(self, routing_lines: list[dict], catalog: dict | None = None):
        from agent_token_radar.provider_sources import ProviderResolver

        magpie = self.root / "magpie"
        (magpie / "routing").mkdir(parents=True, exist_ok=True)
        (magpie / "routing" / "2026-11-15.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in routing_lines), encoding="utf-8"
        )
        if catalog is not None:
            (magpie / "providers.json").write_text(
                json.dumps(catalog), encoding="utf-8"
            )
        return ProviderResolver(config_roots=[magpie])

    def test_routing_ledger_measures_provider_and_served_model(self) -> None:
        """The ledger records what actually answered, per request."""
        r = self._resolver([
            {"agent": "claude", "responseId": "msg_abc",
             "model": "claude-sonnet-5",
             "usage": [{"provider": "workbuddy", "model": "deepseek-v4.1-flash",
                        "in": 10, "out": 2}]},
        ])
        provider, source = r.resolve("claude", "claude-sonnet-5", "msg_abc")
        self.assertEqual(provider, "workbuddy")
        self.assertEqual(source, "measured")
        # ``claude-sonnet-5`` was the alias; this is the real model.
        self.assertEqual(r.served_model("msg_abc"), "deepseek-v4.1-flash")

    def test_measured_beats_the_model_name_guess(self) -> None:
        """A ``claude-*`` name alone would say anthropic; the ledger says better."""
        r = self._resolver([
            {"agent": "claude", "responseId": "msg_xyz",
             "usage": [{"provider": "workbuddy", "model": "hy4free"}]},
        ])
        provider, source = r.resolve("claude", "claude-sonnet-5", "msg_xyz")
        self.assertEqual((provider, source), ("workbuddy", "measured"))

    def test_unmeasured_rows_fall_back_to_a_labelled_guess(self) -> None:
        r = self._resolver([])
        provider, source = r.resolve("claude", "claude-sonnet-5", "msg_unknown")
        self.assertEqual(provider, "anthropic")
        self.assertEqual(source, "inferred", "a guess must be labelled as one")

    def test_codex_own_models_are_not_labelled_inferred(self) -> None:
        """The user's report: ``gpt-6.2-sol | codex | 推断`` looked wrong.

        Codex Desktop's own models come from Codex's own backend, so calling them
        a name-based guess is misleading. They must read as the product's own
        backend, and they must NOT be split onto a second provider name just
        because ``session_meta`` says ``openai`` (the API vendor) -- magpie
        measures that same upstream as ``codex``, and splitting one model across
        two provider names would invent a distinction that does not exist.
        """
        r = self._resolver([])  # no gateway evidence at all
        provider, source = r.resolve("codex", "gpt-6.2-sol", "")
        self.assertEqual(provider, "codex")
        self.assertNotEqual(source, "inferred",
                            "Codex's own models are not a name-based guess")

    def test_unknown_model_stays_empty_instead_of_being_invented(self) -> None:
        r = self._resolver([])
        provider, source = r.resolve("gemini", "unknown", "")
        self.assertEqual(provider, "")
        self.assertEqual(source, "")

    def test_product_identity_outranks_a_foreign_catalogue_entry(self) -> None:
        """WorkBuddy's own ``hy3`` was reported as ``workbuddy-ai``.

        The answer is the product's own backend, and it is labelled ``product``
        rather than ``inferred``: a single-product app can only talk to its own
        service, which is a structural fact, not a guess from a model name.
        """
        r = self._resolver(
            [],
            catalog={"providers": [
                # both services list hy3; only one of them is the app we read
                {"id": "workbuddy-ai", "models": ["hy3"]},
                {"id": "workbuddy", "models": ["hy3"]},
            ]},
        )
        provider, source = r.resolve("workbuddy", "hy3")
        self.assertEqual(provider, "workbuddy", "the app's own service wins")
        self.assertEqual(source, "product")

    def test_embedded_prefix_is_a_stated_provider(self) -> None:
        """``workbuddy/kimi-k3.1-pro`` -- the tool wrote its own route."""
        r = self._resolver([])
        provider, source = r.resolve("hermes", "workbuddy/deepseek-v4.1-flash")
        self.assertEqual(provider, "workbuddy")
        self.assertEqual(source, "measured", "a written prefix is not a guess")

    def test_request_id_is_recovered_from_event_key(self) -> None:
        from agent_token_radar.provider_sources import request_id_from_event_key

        self.assertEqual(
            request_id_from_event_key("claude", "claude|msg_abc"), "msg_abc"
        )
        self.assertEqual(
            request_id_from_event_key("codex", "codex|sess|resp_1|7"), "resp_1"
        )
        self.assertEqual(request_id_from_event_key("dsh", "dsh|x|9"), "")
        self.assertEqual(request_id_from_event_key("claude", "codex|other"), "")


class TestServedModelPresentation(unittest.TestCase):
    """The row must show the model that ran, with the alias kept alongside."""

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_served_")
        self.store = Store(self.root / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        drop_temp_dir(self.root)

    def test_alias_rows_collapse_into_the_served_model(self) -> None:
        self.store.upsert_events([
            UsageEvent(event_key="a", agent="claude", model="claude-sonnet-5",
                       served_model="deepseek-v4.1-flash", provider="workbuddy",
                       provider_source="measured", total_tokens=10),
            UsageEvent(event_key="b", agent="claude", model="deepseek-v4.1-flash",
                       served_model="deepseek-v4.1-flash", provider="workbuddy",
                       provider_source="measured", total_tokens=20),
        ])
        rows = self.store.by_model()
        self.assertEqual(len(rows), 1, "one real model must be one row")
        self.assertEqual(rows[0]["model"], "deepseek-v4.1-flash")
        self.assertEqual(rows[0]["total_tokens"], 30)
        self.assertEqual(rows[0]["alias"], "claude-sonnet-5")

    def test_without_a_served_model_the_logged_name_stands(self) -> None:
        self.store.upsert_events([
            UsageEvent(event_key="c", agent="dsh", model="deepseek-v4-flash",
                       provider="tokenrhythm", total_tokens=5),
        ])
        rows = self.store.by_model()
        self.assertEqual(rows[0]["model"], "deepseek-v4-flash")
        self.assertEqual(rows[0]["alias"], "")

    def test_one_inferred_row_marks_the_whole_group(self) -> None:
        """A group must not read "measured" when part of it was guessed."""
        self.store.upsert_events([
            UsageEvent(event_key="d", agent="codex", model="m", provider="openai",
                       provider_source="measured", total_tokens=1),
            UsageEvent(event_key="e", agent="codex", model="m", provider="openai",
                       provider_source="inferred", total_tokens=1),
        ])
        rows = self.store.by_model()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider_source"], "inferred")

    def test_group_collapses_to_its_weakest_evidence(self) -> None:
        """measured + product -> product: never promote above the worst row."""
        self.store.upsert_events([
            UsageEvent(event_key="f", agent="codex", model="gpt-6.2-sol",
                       provider="codex", provider_source="measured", total_tokens=1),
            UsageEvent(event_key="g", agent="codex", model="gpt-6.2-sol",
                       provider="codex", provider_source="product", total_tokens=1),
        ])
        rows = self.store.by_model()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider_source"], "product")

    def test_backfill_labels_existing_rows_and_fills_served_model(self) -> None:
        """Historic rows have no label and no served model; the backfill adds both."""
        from agent_token_radar import provider_sources

        stored = list(provider_sources.resolver._by_request.items())
        had_served = dict(provider_sources.resolver._served)
        try:
            provider_sources.resolver._by_request = {"msg_1": "workbuddy"}
            provider_sources.resolver._served = {"msg_1": "deepseek-v4.1-flash"}
            provider_sources.resolver._loaded_at = 1e18  # never reload mid-test

            self.store.upsert_events([
                # already had a provider (written by the agent) but no label
                UsageEvent(event_key="claude|msg_1", agent="claude",
                           model="claude-sonnet-5", provider="workbuddy",
                           total_tokens=7),
            ])
            self.store.backfill_providers()

            row = self.store.con.execute(
                "SELECT provider_source, served_model FROM usage_events "
                "WHERE event_key='claude|msg_1'"
            ).fetchone()
            self.assertEqual(row["provider_source"], "measured")
            self.assertEqual(row["served_model"], "deepseek-v4.1-flash")
        finally:
            provider_sources.resolver._by_request = dict(stored)
            provider_sources.resolver._served = had_served


class TestProjectDimension(unittest.TestCase):
    """Which project the tokens were spent on.

    Ported from tokei's project-level tracking (tokei lists it per tool). The
    hard part is not the aggregation but getting one stable label out of agents
    that each record their working directory differently.
    """

    def setUp(self) -> None:
        self.root = make_temp_dir("atr_proj_")
        self.store = Store(self.root / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        drop_temp_dir(self.root)

    def test_windows_paths_normalise_to_one_label(self) -> None:
        from agent_token_radar.adapters._helpers import project_label

        forms = [
            r"D:\UEProjects\Untitled Game",
            "D:/UEProjects/Untitled Game",
            "d:\\UEProjects\\Untitled Game\\",
            "D://UEProjects//Untitled Game",
        ]
        labels = {project_label(x) for x in forms}
        self.assertEqual(len(labels), 1, f"one project must mean one row: {labels}")
        self.assertEqual(labels.pop(), "D:/UEProjects/Untitled Game")

    def test_label_leaves_posix_paths_alone(self) -> None:
        from agent_token_radar.adapters._helpers import project_label

        self.assertEqual(project_label("/home/me/proj"), "/home/me/proj")
        self.assertEqual(project_label(""), "")
        self.assertEqual(project_label(None), "")

    def test_dsh_encoded_directory_decodes_to_a_readable_path(self) -> None:
        """DSH stores the cwd as the session directory's name, encoded."""
        from agent_token_radar.adapters._helpers import decode_dsh_project_dir

        self.assertEqual(
            decode_dsh_project_dir("--D-UEProjects-Untitled~0020Game--"),
            "D:/UEProjects/Untitled Game",
        )
        self.assertEqual(
            decode_dsh_project_dir("--C-Users-27775-quiz-app--"),
            "C:/Users/27775/quiz/app",
        )
        # A trailing terminator must not leak into the label.
        self.assertFalse(
            decode_dsh_project_dir("--D-UEProjects-Untitled~0020Game--").endswith("/")
        )

    def test_by_project_merges_agents_and_keeps_their_split(self) -> None:
        """One folder, several Agents -> ONE row that expands to the Agents.

        The user's report: ``D:/UEProjects/Untitled Game`` appeared twice, once
        under Codex and once under DSH, each looking small. The project is the
        unit being asked about, so the headline is the merged total and the
        per-Agent numbers ride along for the expanded view.
        """
        self.store.upsert_events([
            UsageEvent(event_key="p1", agent="codex", model="m",
                       project="D:/UEProjects/Untitled Game", total_tokens=100),
            UsageEvent(event_key="p2", agent="codex", model="m",
                       project="D:/UEProjects/Untitled Game", total_tokens=50),
            # same folder, different agent -> merged into the same row
            UsageEvent(event_key="p3", agent="claude", model="m",
                       project="D:/UEProjects/Untitled Game", total_tokens=30),
            # no project -> must not become a "(none)" bucket
            UsageEvent(event_key="p4", agent="codex", model="m", total_tokens=7),
        ])
        rows = self.store.by_project()
        self.assertEqual(len(rows), 1, "one folder is one row")
        self.assertEqual(rows[0]["project"], "D:/UEProjects/Untitled Game")
        self.assertEqual(rows[0]["total_tokens"], 180, "merged total")
        self.assertEqual(rows[0]["events"], 3)
        agents = {a["agent"]: a["total_tokens"] for a in rows[0]["agents"]}
        self.assertEqual(agents, {"codex": 150, "claude": 30})
        # Ordered by each Agent's own spend, biggest first.
        self.assertEqual(rows[0]["agents"][0]["agent"], "codex")

    def test_merged_project_hit_rate_is_recomputed_not_averaged(self) -> None:
        """Two Agents must not have their ratios averaged.

        Averaging would give (100% + 0%) / 2 = 50%; the correct aggregate is
        total cached over total prompt = 900 / (900 + 10) ~= 98.9%. The one
        Agent with a tiny uncached prompt must not weigh as much as the other.
        """
        self.store.upsert_events([
            UsageEvent(event_key="h1", agent="a", model="m", project="X:/p",
                       total_tokens=1000, output_tokens=100,
                       cache_read_tokens=900),
            UsageEvent(event_key="h2", agent="b", model="m", project="X:/p",
                       total_tokens=10, output_tokens=0, cache_read_tokens=0),
        ])
        row = self.store.by_project()[0]
        # a: uncached 0 -> hit 1.0 ; b: uncached 10 -> hit 0.0.
        # Merged: cached 900, uncached 10 -> 900/910.
        self.assertAlmostEqual(row["cache_hit"], 900 / 910, places=6)
        self.assertNotAlmostEqual(row["cache_hit"], 0.5, places=2)

    def test_by_project_limit_counts_projects_not_agent_pairs(self) -> None:
        events = []
        for i in range(5):
            for agent in ("a", "b"):
                events.append(UsageEvent(
                    event_key=f"k{i}{agent}", agent=agent, model="m",
                    project=f"X:/p{i}", total_tokens=10 + i,
                ))
        self.store.upsert_events(events)
        rows = self.store.by_project(limit=3)
        self.assertEqual(len(rows), 3, "limit is projects, not (project, agent)")
        for r in rows:
            self.assertEqual(len(r["agents"]), 2)

    def test_project_stats_reports_coverage_not_a_zero(self) -> None:
        self.store.upsert_events([
            UsageEvent(event_key="q1", agent="a", project="X:/p", total_tokens=1),
            UsageEvent(event_key="q2", agent="a", total_tokens=1),
        ])
        stats = self.store.project_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["with_project"], 1)
        self.assertAlmostEqual(stats["coverage"], 0.5)

    def test_backfill_reads_cwd_from_payload_as_well_as_top_level(self) -> None:
        """Codex nests cwd under ``payload``; Claude keeps it at the top.

        Missing this made every Codex row keep an empty project (3359 rows on
        this machine) while the same code worked for Claude.
        """
        from agent_token_radar.adapters._helpers import project_label

        codex_file = self.root / "rollout-x.jsonl"
        codex_file.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"session_id": "sess-codex",
                            "cwd": r"D:\UEProjects\Untitled Game"},
            }) + "\n",
            encoding="utf-8",
        )
        claude_file = self.root / "claude-x.jsonl"
        claude_file.write_text(
            json.dumps({"type": "assistant", "sessionId": "sess-claude",
                        "cwd": r"C:\work\thing"}) + "\n",
            encoding="utf-8",
        )

        out: dict[tuple[str, str], str] = {}
        src = type("S", (), {"path": codex_file, "kind": "jsonl"})()
        self.store._read_projects_from_source(
            "codex", src, out,
            project_label=project_label, decode_dir=lambda x: x,
        )
        src = type("S", (), {"path": claude_file, "kind": "jsonl"})()
        self.store._read_projects_from_source(
            "claude", src, out,
            project_label=project_label, decode_dir=lambda x: x,
        )

        self.assertEqual(out[("codex", "sess-codex")], "D:/UEProjects/Untitled Game")
        self.assertEqual(out[("claude", "sess-claude")], "C:/work/thing")


if __name__ == "__main__":
    unittest.main()

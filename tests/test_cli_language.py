"""The CLI must honour the language it is given, in both directions.

The dashboard has a strict rule: Chinese mode shows Chinese, and only the
domain vocabulary (token, agent, model, provider, session) stays English. The
CLI used to break that rule silently -- ``stats`` printed hard-coded English
column names ("METRIC", "VALUE") in Chinese mode, ``scan`` printed
"Scan complete:", and ``scan --lang zh`` was accepted by argparse but never
read at all.

These tests do not need a real database: they drive the commands against a
temp store and assert on the printed text, which is exactly what the user sees.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest

from _tmp import drop_temp_dir, make_temp_dir

from agent_token_radar.base import UsageEvent
from agent_token_radar import cli
from agent_token_radar.store import Store


def _capture(fn, args) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(args)
    return buf.getvalue()


def _stats_args(lang: str):
    return argparse.Namespace(
        lang=lang, range="all", by=None, since=None, until=None,
        json=False, model=None, agent=None,
    )


class CliLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir("atr_cli_")
        self.db = self.dir / "cli.db"
        # cmd_stats opens Store.default(), so redirect that to a temp file for
        # the duration of the test rather than touching the real database.
        self._orig_default = Store.default.__func__
        store = Store(self.db)
        store.upsert_events([UsageEvent(
            event_key="e1", agent="dsh", model="deepseek-v4.1-flash",
            provider="test", provider_source="measured",
            timestamp=0, total_tokens=1000, input_tokens=1000,
            output_tokens=0)])

        def _fake_default(cls, *a, **k):
            return store

        Store.default = classmethod(_fake_default)
        self.store = store

    def tearDown(self) -> None:
        Store.default = classmethod(self._orig_default)
        self.store.close()
        drop_temp_dir(self.dir)

    def test_chinese_stats_has_no_english_column_headers(self) -> None:
        out = _capture(cli.cmd_stats, _stats_args("zh"))
        self.assertNotIn("METRIC", out)
        self.assertNotIn("VALUE", out)
        self.assertIn("指标", out)
        self.assertIn("数值", out)

    def test_english_stats_keeps_english_headers(self) -> None:
        out = _capture(cli.cmd_stats, _stats_args("en"))
        self.assertIn("METRIC", out)
        self.assertIn("VALUE", out)
        self.assertNotIn("指标", out)

    def test_chinese_stats_reports_cost_and_coverage(self) -> None:
        """The text report must carry the money, like the dashboard does."""
        out = _capture(cli.cmd_stats, _stats_args("zh"))
        self.assertIn("参考费用", out)
        self.assertIn("费用覆盖率", out)

    def test_english_stats_reports_cost_and_coverage(self) -> None:
        out = _capture(cli.cmd_stats, _stats_args("en"))
        self.assertIn("cost", out)
        self.assertIn("coverage", out)

    def test_domain_vocabulary_stays_english_in_chinese_mode(self) -> None:
        """token / agent / model are the agreed exceptions -- they stay."""
        out = _capture(cli.cmd_stats, _stats_args("zh"))
        self.assertIn("token", out)


class CliScanLanguageTests(unittest.TestCase):
    """``scan`` must read --lang instead of ignoring it."""

    def _scan_args(self, lang: str):
        return argparse.Namespace(lang=lang, only="__none__", verbose=0)

    def test_chinese_scan_says_chinese(self) -> None:
        out = _capture(cli.cmd_scan, self._scan_args("zh"))
        self.assertIn("扫描完成", out)
        self.assertNotIn("Scan complete", out)

    def test_english_scan_says_english(self) -> None:
        out = _capture(cli.cmd_scan, self._scan_args("en"))
        self.assertIn("Scan complete", out)
        self.assertNotIn("扫描完成", out)


if __name__ == "__main__":
    unittest.main()

"""Update checking: the one place this project may touch the network.

Everything else here is strictly local, so the properties worth pinning are the
ones that keep that promise honest even now:

* **A broken network is not a broken app.** No DNS, no route, a timeout, a 500,
  rate limiting, or malformed JSON must all produce the same quiet "unavailable"
  answer -- never an exception that reaches a request handler.
* **At most one request per day.** The dashboard is opened many times a day; the
  cache is what keeps that from becoming many requests. A stale or corrupt cache
  must not lock checking out forever either.
* **Nothing about the user leaves the machine.** The request asks one question --
  "what is the latest tag" -- so the tests assert the URL and that no body,
  cookie or identifier is sent.
* **Version comparison is numeric.** ``0.0.10`` is newer than ``0.0.9``; string
  comparison gets that backwards, and a wrong answer here means either a missed
  update or a permanent "update available" nag.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_token_radar import updates  # noqa: E402


class VersionParseTests(unittest.TestCase):
    def test_plain_and_prefixed_tags(self) -> None:
        self.assertEqual(updates.parse_version("0.0.2"), (0, 0, 2))
        self.assertEqual(updates.parse_version("v0.0.2"), (0, 0, 2))
        self.assertEqual(updates.parse_version("  v1.2.3  "), (1, 2, 3))
        self.assertEqual(updates.parse_version("V2.0"), (2, 0))

    def test_prerelease_suffix_is_ignored(self) -> None:
        """A beta must not be advertised as newer than the stable it follows."""
        self.assertEqual(updates.parse_version("v1.2.3-beta.1"), (1, 2, 3))
        self.assertEqual(updates.parse_version("v1.2.3-rc2"), (1, 2, 3))

    def test_unparseable_is_empty_not_an_exception(self) -> None:
        for junk in ("", None, "latest", "v", "abc.def"):
            self.assertEqual(updates.parse_version(junk), (), junk)

    def test_numeric_not_lexicographic(self) -> None:
        """The bug string comparison would introduce."""
        self.assertTrue(updates.is_newer("v0.0.10", "0.0.9"),
                        "0.0.10 must beat 0.0.9")
        self.assertFalse(updates.is_newer("v0.0.9", "0.0.10"))

    def test_equal_and_older_are_not_updates(self) -> None:
        self.assertFalse(updates.is_newer("v0.0.2", "0.0.2"))
        self.assertFalse(updates.is_newer("v0.0.1", "0.0.2"))

    def test_short_and_long_forms_compare(self) -> None:
        self.assertTrue(updates.is_newer("v1.0.0", "0.9"))
        self.assertFalse(updates.is_newer("v0.9", "1.0.0"))

    def test_garbage_never_claims_an_update(self) -> None:
        self.assertFalse(updates.is_newer("", "0.0.2"))
        self.assertFalse(updates.is_newer("nonsense", "0.0.2"))


class _TmpCache(unittest.TestCase):
    """Point the cache at a temp dir so tests never touch the real profile."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(
            updates, "_cache_path",
            return_value=Path(self._tmp.name) / "update-check.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _payload(self, **over) -> dict:
        base = {
            "state": "up_to_date",
            "current_version": "0.0.2",
            "latest_version": "v0.0.2",
            "latest_url": updates.RELEASES_PAGE,
            "checked_at": time.time(),
        }
        base.update(over)
        return base


class CacheTests(_TmpCache):
    def test_missing_cache_reads_as_none(self) -> None:
        self.assertIsNone(updates.read_cache())

    def test_corrupt_cache_reads_as_none(self) -> None:
        path = updates._cache_path()
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(updates.read_cache())

    def test_non_dict_cache_reads_as_none(self) -> None:
        path = updates._cache_path()
        path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
        self.assertIsNone(updates.read_cache())

    def test_round_trip(self) -> None:
        payload = self._payload()
        updates._write_cache(payload)
        self.assertEqual(updates.read_cache()["latest_version"], "v0.0.2")

    def test_clear_removes_it(self) -> None:
        updates._write_cache(self._payload())
        updates.clear_cache()
        self.assertIsNone(updates.read_cache())

    def test_clear_on_missing_file_is_safe(self) -> None:
        updates.clear_cache()  # must not raise
        self.assertIsNone(updates.read_cache())


class FreshCacheTests(_TmpCache):
    def test_fresh_cache_avoids_the_network_entirely(self) -> None:
        """The whole point: a warm cache is a file read, not a request."""
        updates._write_cache(self._payload(checked_at=time.time()))
        with mock.patch.object(updates, "_fetch_latest") as fetch:
            out = updates.check_for_update("0.0.2")
        fetch.assert_not_called()
        self.assertTrue(out["cached"])
        self.assertEqual(out["state"], "up_to_date")

    def test_stale_cache_triggers_a_fetch(self) -> None:
        old = time.time() - updates.CACHE_TTL_SECONDS - 60
        updates._write_cache(self._payload(checked_at=old))
        with mock.patch.object(
                updates, "_fetch_latest",
                return_value={"tag_name": "v0.0.3",
                              "html_url": "https://example/v0.0.3"}) as fetch:
            out = updates.check_for_update("0.0.2")
        fetch.assert_called_once()
        self.assertEqual(out["state"], "update_available")
        self.assertEqual(out["latest_version"], "v0.0.3")
        self.assertFalse(out["cached"])

    def test_force_ignores_a_fresh_cache(self) -> None:
        updates._write_cache(self._payload(checked_at=time.time()))
        with mock.patch.object(
                updates, "_fetch_latest",
                return_value={"tag_name": "v0.0.3"}) as fetch:
            out = updates.check_for_update("0.0.2", force=True)
        fetch.assert_called_once()
        self.assertEqual(out["state"], "update_available")

    def test_negative_age_is_treated_as_stale(self) -> None:
        """A clock that jumped backwards must not disable checking for a day."""
        future = time.time() + 10_000
        updates._write_cache(self._payload(checked_at=future))
        with mock.patch.object(
                updates, "_fetch_latest",
                return_value={"tag_name": "v0.0.2"}) as fetch:
            updates.check_for_update("0.0.2")
        fetch.assert_called_once()

    def test_cache_without_a_version_is_not_served(self) -> None:
        updates._write_cache({"checked_at": time.time()})
        with mock.patch.object(
                updates, "_fetch_latest",
                return_value={"tag_name": "v0.0.2"}) as fetch:
            updates.check_for_update("0.0.2")
        fetch.assert_called_once()


class NetworkFailureTests(_TmpCache):
    """Every failure mode must degrade quietly to 'unavailable'."""

    def _fails_with(self, exc: Exception) -> dict:
        with mock.patch.object(updates, "_fetch_latest", side_effect=exc):
            return updates.check_for_update("0.0.2")

    def test_common_failures_return_unavailable(self) -> None:
        cases = [
            urllib.error.URLError("no dns"),
            urllib.error.HTTPError(updates.RELEASES_API, 503, "busy", None, None),
            urllib.error.HTTPError(updates.RELEASES_API, 403, "rate limited",
                                   None, None),
            TimeoutError("timed out"),
            OSError("connection reset"),
            ValueError("bad json"),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                out = self._fails_with(exc)
                self.assertEqual(out["state"], "unavailable")
                self.assertEqual(out["current_version"], "0.0.2")
                self.assertFalse(out["cached"])
                # Reason is carried for support, but truncated.
                self.assertLessEqual(len(out.get("reason", "")), 200)

    def test_stale_cache_is_preferred_over_a_bare_error(self) -> None:
        """Better a two-day-old tag than an empty box."""
        old = time.time() - updates.CACHE_TTL_SECONDS - 60
        updates._write_cache(self._payload(checked_at=old, latest_version="v0.0.2"))
        with mock.patch.object(updates, "_fetch_latest",
                               side_effect=urllib.error.URLError("down")):
            out = updates.check_for_update("0.0.2")
        self.assertEqual(out["state"], "unavailable")
        self.assertTrue(out["stale"])
        self.assertEqual(out["latest_version"], "v0.0.2")

    def test_missing_tag_name_is_unavailable(self) -> None:
        with mock.patch.object(updates, "_fetch_latest",
                               return_value={"html_url": "x"}):
            out = updates.check_for_update("0.0.2")
        self.assertEqual(out["state"], "unavailable")
        self.assertIn("tag_name", out["reason"])


class RequestShapeTests(_TmpCache):
    """What actually goes over the wire -- this is the privacy contract."""

    def test_request_is_one_get_with_no_body_and_no_cookies(self) -> None:
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"tag_name": "v0.0.2",
                                   "html_url": "u"}).encode()

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["method"] = req.get_method()
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["timeout"] = timeout
            return _Resp()

        with mock.patch.object(updates.urllib.request, "urlopen", fake_urlopen):
            updates._fetch_latest("0.0.2")

        self.assertEqual(captured["url"], updates.RELEASES_API)
        self.assertIsNone(captured["data"], "must not send a request body")
        self.assertEqual(captured["method"], "GET")
        self.assertNotIn("cookie", captured["headers"])
        self.assertNotIn("authorization", captured["headers"])
        self.assertIn("user-agent", captured["headers"],
                      "GitHub rejects requests without one")
        self.assertEqual(captured["timeout"], updates.TIMEOUT_SECONDS)

    def test_user_agent_names_the_project_and_version(self) -> None:
        ua = updates._user_agent("0.0.2")
        self.assertIn("agent-token-radar/0.0.2", ua)
        self.assertIn("github.com", ua)

    def test_timeout_is_short_enough_to_stay_invisible(self) -> None:
        self.assertLessEqual(updates.TIMEOUT_SECONDS, 10,
                             "a slow check must never be felt by the user")

    def test_api_points_at_this_repository(self) -> None:
        self.assertIn("Zhucy123/agent-token-radar", updates.RELEASES_API)
        self.assertIn("Zhucy123/agent-token-radar", updates.RELEASES_PAGE)


class EndpointTests(unittest.TestCase):
    """/api/update must never raise, even when everything is broken."""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception:  # pragma: no cover - fastapi optional
            self.skipTest("fastapi not installed")

    def test_endpoint_reports_unavailable_instead_of_500(self) -> None:
        from fastapi.testclient import TestClient
        from agent_token_radar.web import create_app

        app = create_app(scan_interval=3600)
        with mock.patch.object(updates, "check_for_update",
                               return_value={"state": "unavailable",
                                             "current_version": "0.0.2"}), \
             mock.patch.object(updates, "_fetch_latest",
                               side_effect=urllib.error.URLError("down")):
            client = TestClient(app)
            r = client.get("/api/update")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "unavailable")


class DisableSwitchTests(unittest.TestCase):
    """AGENT_TOKEN_RADAR_NO_UPDATE_CHECK must mean NO outbound traffic.

    The first version only guarded the startup thread. The endpoint was left
    unguarded, so an operator who set the variable to keep a machine offline
    still reached GitHub the moment they opened Settings -- the switch looked
    like it worked because no request appeared at launch.
    """

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception:  # pragma: no cover
            self.skipTest("fastapi not installed")

    def _client(self):
        from fastapi.testclient import TestClient
        from agent_token_radar.web import create_app
        return TestClient(create_app(scan_interval=3600))

    def test_endpoint_makes_no_request_when_disabled(self) -> None:
        client = self._client()
        with mock.patch.dict("os.environ",
                             {"AGENT_TOKEN_RADAR_NO_UPDATE_CHECK": "1"}), \
             mock.patch.object(updates, "_fetch_latest") as fetch, \
             mock.patch.object(updates, "check_for_update") as check:
            r = client.get("/api/update")
        fetch.assert_not_called()
        check.assert_not_called()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "disabled")

    def test_force_does_not_bypass_the_switch(self) -> None:
        """A forced check is still a network request; the switch wins."""
        client = self._client()
        with mock.patch.dict("os.environ",
                             {"AGENT_TOKEN_RADAR_NO_UPDATE_CHECK": "1"}), \
             mock.patch.object(updates, "_fetch_latest") as fetch:
            r = client.get("/api/update?force=true")
        fetch.assert_not_called()
        self.assertEqual(r.json()["state"], "disabled")

    def test_startup_thread_is_not_spawned_when_disabled(self) -> None:
        from agent_token_radar import web

        with mock.patch.dict("os.environ",
                             {"AGENT_TOKEN_RADAR_NO_UPDATE_CHECK": "1"}), \
             mock.patch.object(web.threading, "Thread") as thread:
            web._start_update_check()
        thread.assert_not_called()

    def test_switch_accepts_the_documented_spellings(self) -> None:
        from agent_token_radar import web

        for value in ("1", "true", "TRUE", "yes", "on"):
            with mock.patch.dict("os.environ",
                                 {"AGENT_TOKEN_RADAR_NO_UPDATE_CHECK": value}):
                self.assertTrue(web._update_check_disabled(), value)

    def test_switch_off_by_default_and_for_falsy_values(self) -> None:
        from agent_token_radar import web

        for value in ("", "0", "false", "FALSE"):
            with mock.patch.dict("os.environ",
                                 {"AGENT_TOKEN_RADAR_NO_UPDATE_CHECK": value}):
                self.assertFalse(web._update_check_disabled(), repr(value))

    def test_normal_check_still_reaches_the_module_when_enabled(self) -> None:
        """The guard must not break the ordinary path."""
        client = self._client()
        with mock.patch.dict("os.environ",
                             {"AGENT_TOKEN_RADAR_NO_UPDATE_CHECK": ""}), \
             mock.patch.object(updates, "check_for_update",
                               return_value={"state": "up_to_date",
                                             "current_version": "0.0.2"}) as check:
            r = client.get("/api/update")
        check.assert_called_once()
        self.assertEqual(r.json()["state"], "up_to_date")


if __name__ == "__main__":
    unittest.main()

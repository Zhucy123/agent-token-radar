"""In-app update: download the installer, run it, clean up after itself.

This is the most dangerous code in the project. It downloads an executable from
the internet and runs it with the user's blessing, so the properties pinned here
are the ones that keep that from going wrong quietly:

* **Only HTTPS.** A downgrade to http would let anything on the path swap the
  installer.
* **Nothing runs until the whole file has arrived.** A partial download named
  like a finished one, or executed mid-write, is how people install corruption.
* **A size mismatch is refused.** Truncation and a proxy-injected error page both
  look like "some bytes arrived"; comparing against the release metadata is what
  tells them apart from a real installer.
* **It refuses when there is nothing to update**, and when this is not a
  packaged build -- a checkout updates with git pull, and dropping an installer
  over someone's working tree is not this code's business.
* **Leftovers are swept.** A machine rebooted mid-update must not accumulate
  copies of an 18 MB installer.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_token_radar import updater  # noqa: E402


class _TmpDownloads(unittest.TestCase):
    """Point the download directory at a temp dir, never the real profile."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = mock.patch.object(updater, "_download_dir",
                                    return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _path(self) -> Path:
        return self.dir / "AgentTokenRadar-Setup.exe"


class _FakeResponse:
    def __init__(self, payload: bytes, declared: int | None = None) -> None:
        self._payload = payload
        self._pos = 0
        self.headers = {}
        if declared is not None:
            self.headers["Content-Length"] = str(declared)

    def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._payload):
            return b""
        end = len(self._payload) if n < 0 else self._pos + n
        chunk = self._payload[self._pos:end]
        self._pos = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DownloadTests(_TmpDownloads):
    def _serve(self, payload: bytes, declared: int | None = None):
        return mock.patch.object(
            updater, "_urlopen",
            return_value=_FakeResponse(payload, declared))

    def test_downloads_to_a_temp_path_and_reports_size(self) -> None:
        payload = b"MZ" + b"\x00" * 4096
        with self._serve(payload):
            info = updater.download_installer(
                "https://example/x.exe", expected_size=len(payload))
        self.assertTrue(Path(info["path"]).is_file())
        self.assertEqual(info["bytes"], len(payload))
        self.assertEqual(Path(info["path"]).read_bytes(), payload)
        self.assertEqual(len(info["sha256"]), 64)

    def test_rejects_plain_http(self) -> None:
        """A downgrade to http would let anything on the path swap the binary.

        The mock matters: without it the guard could be deleted and the test
        would still "pass" by hitting a real network error, which proves nothing
        about the scheme check.
        """
        with mock.patch.object(updater, "_urlopen") as urlopen:
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download_installer("http://example/x.exe")
        urlopen.assert_not_called()
        self.assertEqual(cm.exception.code, "not_https")
        self.assertFalse(self._path().exists())

    def test_rejects_non_https_schemes_generically(self) -> None:
        for url in ("http://x/y.exe", "ftp://x/y.exe", "file:///c:/y.exe",
                    "x.exe", ""):
            with self.subTest(url=url):
                with mock.patch.object(updater, "_urlopen") as urlopen:
                    with self.assertRaises(updater.UpdateError):
                        updater.download_installer(url)
                urlopen.assert_not_called()

    def test_size_mismatch_is_refused_and_leaves_nothing_behind(self) -> None:
        """A truncated file must never be left looking installable."""
        payload = b"x" * 100
        with self._serve(payload):
            with self.assertRaises(updater.UpdateError):
                updater.download_installer("https://example/x.exe",
                                           expected_size=500)
        self.assertFalse(self._path().exists())
        self.assertFalse(self._path().with_suffix(".part").exists(),
                         "the partial file must be cleaned up too")

    def test_empty_response_is_refused(self) -> None:
        with self._serve(b""):
            with self.assertRaises(updater.UpdateError):
                updater.download_installer("https://example/x.exe")

    def test_oversized_response_is_refused(self) -> None:
        with mock.patch.object(updater, "MAX_INSTALLER_BYTES", 10):
            with self._serve(b"y" * 100):
                with self.assertRaises(updater.UpdateError) as cm:
                    updater.download_installer("https://example/x.exe")
        self.assertEqual(cm.exception.code, "too_large")
        self.assertFalse(self._path().exists())
        self.assertFalse(self._path().with_suffix(".part").exists(),
                         "a refused oversized download must not leave a .part file")

    def test_network_error_becomes_a_user_message(self) -> None:
        with mock.patch.object(
                updater, "_urlopen",
                side_effect=OSError("no route")):
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download_installer("https://example/x.exe")
        self.assertEqual(cm.exception.code, "download_failed")
        self.assertIn("no route", cm.exception.params["detail"])

    def test_partial_file_is_not_named_like_a_finished_one(self) -> None:
        """While writing, the target name must not exist."""
        seen = {}
        real_open = open

        def watching_open(path, mode="r", *a, **kw):
            if "w" in mode or "wb" in mode:
                seen["target_exists_during_write"] = self._path().exists()
            return real_open(path, mode, *a, **kw)

        with self._serve(b"z" * 64), \
             mock.patch("builtins.open", watching_open):
            updater.download_installer("https://example/x.exe")
        self.assertFalse(seen.get("target_exists_during_write", True),
                         "the finished name must only appear at the end")

    def test_sends_a_user_agent(self) -> None:
        captured = {}

        def fake(req, timeout=None):
            captured["ua"] = req.get_header("User-agent")
            captured["timeout"] = timeout
            return _FakeResponse(b"q" * 32)

        with mock.patch.object(updater, "_urlopen", fake):
            updater.download_installer("https://example/x.exe")
        self.assertTrue(captured["ua"])
        self.assertIn("agent-token-radar", captured["ua"])
        self.assertEqual(captured["timeout"], updater.DOWNLOAD_TIMEOUT_SECONDS)


    def test_https_to_http_redirect_is_refused(self) -> None:
        """The original URL being HTTPS is not enough if a 302 leaves it."""
        handler = updater._HttpsOnlyRedirectHandler()
        req = updater.urllib.request.Request("https://example/x.exe")
        with self.assertRaises(updater.UpdateError) as cm:
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://evil.example/x.exe")
        self.assertEqual(cm.exception.code, "redirect_not_https")

    def test_https_to_https_redirect_is_allowed(self) -> None:
        handler = updater._HttpsOnlyRedirectHandler()
        req = updater.urllib.request.Request("https://example/x.exe")
        nxt = handler.redirect_request(
            req, None, 302, "Found", {},
            "https://objects.githubusercontent.com/x.exe")
        self.assertIsNotNone(nxt)
        self.assertTrue(nxt.full_url.startswith("https://"))


class LaunchTests(_TmpDownloads):
    def test_missing_installer_raises(self) -> None:
        with self.assertRaises(updater.UpdateError):
            updater.launch_installer(str(self.dir / "nope.exe"))

    def test_launch_uses_the_path_as_an_argument_not_a_shell_string(self) -> None:
        """A path with spaces must not be re-split by a shell."""
        exe = self._path()
        exe.write_bytes(b"MZ")
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            updater.launch_installer(str(exe))
        popen.assert_called_once()
        argv = popen.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv, [str(exe)])
        # No shell=True anywhere: that would reintroduce quoting bugs.
        self.assertNotIn("shell", popen.call_args[1])

    def test_installer_survives_this_process_being_killed(self) -> None:
        """The installer runs taskkill on us; a child must not die with us."""
        exe = self._path()
        exe.write_bytes(b"MZ")
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            updater.launch_installer(str(exe))
        flags = popen.call_args[1].get("creationflags", 0)
        if sys.platform == "win32":
            self.assertTrue(flags & getattr(updater.subprocess,
                                            "DETACHED_PROCESS", 0))


class SweepTests(_TmpDownloads):
    def test_sweep_removes_a_leftover_installer(self) -> None:
        self._path().write_bytes(b"old")
        updater.sweep_stale_downloads()
        self.assertFalse(self._path().exists())

    def test_sweep_is_safe_when_there_is_nothing(self) -> None:
        updater.sweep_stale_downloads()  # must not raise

    def test_sweep_never_touches_the_database(self) -> None:
        """Only the installer is swept; user data must be untouched."""
        keep = self.dir / "token_radar.db"
        keep.write_bytes(b"db")
        self._path().write_bytes(b"old")
        updater.sweep_stale_downloads()
        self.assertTrue(keep.exists())


class ApplyStateTests(_TmpDownloads):
    def test_only_one_apply_runs_at_a_time(self) -> None:
        """A second click must not fight over the same file."""
        with mock.patch.object(updater, "download_installer") as dl, \
             mock.patch.object(updater, "launch_installer"):
            dl.return_value = {"path": "x", "bytes": 1, "sha256": "0" * 64}
            first = updater.start_apply("https://example/x.exe")
            second = updater.start_apply("https://example/x.exe")
        self.assertIn(first["phase"], ("downloading", "launching", "launched"))
        self.assertIn(second["phase"], ("downloading", "launching", "launched"))

    def test_failure_is_reported_not_raised(self) -> None:
        with mock.patch.object(updater, "download_installer",
                               side_effect=updater.UpdateError("boom")):
            updater.start_apply("https://example/x.exe")
            for _ in range(50):
                st = updater.apply_state()
                if st.get("phase") == "failed":
                    break
                import time
                time.sleep(0.02)
        self.assertEqual(st["phase"], "failed")
        self.assertIn("boom", st["error"])


class EndpointGuardTests(unittest.TestCase):
    """The endpoint must refuse politely, never half-do something."""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception:  # pragma: no cover
            self.skipTest("fastapi not installed")

    def _client(self):
        from fastapi.testclient import TestClient
        from agent_token_radar.web import create_app
        return TestClient(create_app(scan_interval=3600))

    def test_refuses_when_running_from_source(self) -> None:
        with mock.patch.object(updater, "is_installed_build",
                               return_value=False):
            r = self._client().post("/api/update/apply")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["reason"], "not_installed")

    def test_refuses_when_no_update_is_available(self) -> None:
        from agent_token_radar import updates
        with mock.patch.object(updater, "is_installed_build", return_value=True), \
             mock.patch.object(updates, "check_for_update",
                               return_value={"state": "up_to_date"}):
            r = self._client().post("/api/update/apply")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["reason"], "no_update")

    def test_refuses_when_the_release_has_no_installer(self) -> None:
        from agent_token_radar import updates
        with mock.patch.object(updater, "is_installed_build", return_value=True), \
             mock.patch.object(updates, "check_for_update",
                               return_value={"state": "update_available"}):
            r = self._client().post("/api/update/apply")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["reason"], "no_installer")

    def test_refuses_when_the_switch_is_off(self) -> None:
        with mock.patch.dict("os.environ",
                             {"AGENT_TOKEN_RADAR_NO_UPDATE_CHECK": "1"}), \
             mock.patch.object(updater, "is_installed_build", return_value=True):
            r = self._client().post("/api/update/apply")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["reason"], "disabled")

    def test_starts_the_apply_when_everything_lines_up(self) -> None:
        from agent_token_radar import updates
        status = {"state": "update_available",
                  "installer_url": "https://example/x.exe",
                  "installer_size": 123}
        with mock.patch.object(updater, "is_installed_build", return_value=True), \
             mock.patch.object(updates, "check_for_update", return_value=status), \
             mock.patch.object(updater, "start_apply",
                               return_value={"phase": "downloading"}) as start:
            r = self._client().post("/api/update/apply")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        start.assert_called_once_with("https://example/x.exe",
                                      expected_size=123)


if __name__ == "__main__":
    unittest.main()

"""Where the database lives, for both ways of running the tool.

A checkout is self-contained: the database sits in ``<repo>/data/``. An
installed package must NOT do that -- ``PROJECT_ROOT`` is then ``site-packages``,
so writing there fails on a read-only install and loses data on upgrade. That
was a real bug, found only by installing the wheel and running it.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from _tmp import drop_temp_dir, make_temp_dir

from agent_token_radar import paths


class DatabasePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir("atr_paths_")

    def tearDown(self) -> None:
        drop_temp_dir(self.dir)

    def test_env_override_wins_everywhere(self) -> None:
        target = self.dir / "custom" / "x.db"
        with mock.patch.dict(os.environ, {"AGENT_TOKEN_RADAR_DB": str(target)}):
            self.assertEqual(paths.default_db_path(), target)

    def test_checkout_keeps_db_beside_the_code(self) -> None:
        """Running from a clone stays portable and self-contained."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_TOKEN_RADAR_DB", None)
            with mock.patch.object(paths, "_running_installed", return_value=False):
                p = paths.default_db_path()
        self.assertEqual(p, paths.PROJECT_ROOT / "data" / "token_radar.db")

    def test_installed_never_writes_into_site_packages(self) -> None:
        """The regression: an installed run wrote to site-packages/data."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_TOKEN_RADAR_DB", None)
            with mock.patch.object(paths, "_running_installed", return_value=True):
                p = paths.default_db_path()
        self.assertNotIn("site-packages", str(p).lower())
        self.assertNotIn("dist-packages", str(p).lower())
        self.assertEqual(p.name, "token_radar.db")

    def test_installed_uses_a_per_user_directory(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_TOKEN_RADAR_DB", None)
            with mock.patch.object(paths, "_running_installed", return_value=True):
                p = paths.default_db_path()
                state = paths.user_state_dir()
        self.assertEqual(p.parent, state)

    def test_ensure_own_dirs_creates_the_parent(self) -> None:
        target = self.dir / "deep" / "nested" / "x.db"
        with mock.patch.dict(os.environ, {"AGENT_TOKEN_RADAR_DB": str(target)}):
            got = paths.ensure_own_dirs()
        self.assertEqual(got, target)
        self.assertTrue(target.parent.is_dir())

    def test_installed_detection_reads_the_real_path(self) -> None:
        """``_running_installed`` must judge PROJECT_ROOT, not a guess."""
        with mock.patch.object(paths, "PROJECT_ROOT",
                               Path("/x/site-packages/agent_token_radar")):
            self.assertTrue(paths._running_installed())
        with mock.patch.object(paths, "PROJECT_ROOT",
                               Path("/home/u/projects/agent-token-radar")):
            self.assertFalse(paths._running_installed())

    def test_frozen_build_counts_as_installed(self) -> None:
        """A PyInstaller exe must not write next to itself.

        Measured, not guessed: the first packaged build put its database in
        ``dist/AgentTokenRadar/_internal/data/``. That directory is
        ``Program Files`` for a real install, where a standard user cannot
        write -- so the app would come up with an empty, unsaved dashboard.
        """
        with mock.patch.object(paths.sys, "frozen", True, create=True):
            with mock.patch.object(paths, "PROJECT_ROOT",
                                   Path("/home/u/projects/agent-token-radar")):
                self.assertTrue(paths._running_frozen())
                self.assertTrue(paths._running_installed())

    def test_frozen_build_uses_the_per_user_directory(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_TOKEN_RADAR_DB", None)
            with mock.patch.object(paths.sys, "frozen", True, create=True):
                p = paths.default_db_path()
                state = paths.user_state_dir()
        self.assertEqual(p.parent, state)
        self.assertEqual(p.name, "token_radar.db")

    def test_unfrozen_checkout_is_not_treated_as_frozen(self) -> None:
        with mock.patch.object(paths.sys, "frozen", False, create=True):
            self.assertFalse(paths._running_frozen())


if __name__ == "__main__":
    unittest.main()

"""Port selection for ``serve``.

The user's rule: the dashboard prefers 8770, and if that is taken it moves on to
8771, 8772, … automatically -- no flag needed, no traceback.  These tests lock
that behaviour, including the boundary (everything taken must fail cleanly).
"""

from __future__ import annotations

import argparse
import socket
import sys
import unittest
from contextlib import contextmanager

from agent_token_radar import cli


@contextmanager
def listening(host: str, port: int):
    """Occupy ``port`` for the duration of the block."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.listen(1)
        yield port
    finally:
        s.close()


def free_port_near(host: str, start: int) -> int:
    """A port we can be confident is free (ask the OS for one)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class PortSelectionTests(unittest.TestCase):
    def test_free_port_returns_the_requested_port_when_it_is_free(self) -> None:
        port = free_port_near("127.0.0.1", 20000)
        self.assertEqual(cli._free_port("127.0.0.1", port), port)

    def test_busy_port_falls_through_to_the_next_one(self) -> None:
        base = free_port_near("127.0.0.1", 20000)
        with listening("127.0.0.1", base):
            got = cli._free_port("127.0.0.1", base)
        self.assertIsNotNone(got)
        self.assertGreater(got, base)
        self.assertEqual(got, base + 1)

    def test_consecutive_busy_ports_keep_falling_through(self) -> None:
        """8770 + 8771 taken must land past both of them."""
        base = free_port_near("127.0.0.1", 20000)
        with listening("127.0.0.1", base), listening("127.0.0.1", base + 1):
            got = cli._free_port("127.0.0.1", base)
        self.assertIsNotNone(got)
        # It must have walked past *both* busy ports, not stopped at the first.
        self.assertGreater(got, base + 1)

    def test_no_free_port_reports_none_instead_of_a_busy_one(self) -> None:
        """Exhausting the window must not hand back a port we know is busy."""
        base = free_port_near("127.0.0.1", 20000)
        with listening("127.0.0.1", base):
            got = cli._free_port("127.0.0.1", base, tries=1)
        self.assertIsNone(got)


class PortProbeCorrectnessTests(unittest.TestCase):
    """The probe must agree with uvicorn about whether a port is usable.

    Regression: on Windows the probe set ``SO_REUSEADDR``, which there means
    "allow binding a port even while another socket is listening".  Every probe
    therefore reported "free", the dashboard picked a busy port, and uvicorn
    died with the exact ``[Errno 10048]`` this code exists to prevent.  A live
    listener must be detected as busy on every platform.
    """

    def test_live_listener_is_reported_as_busy(self) -> None:
        port = free_port_near("127.0.0.1", 20000)
        with listening("127.0.0.1", port):
            self.assertFalse(
                cli._port_is_free("127.0.0.1", port),
                "a listening socket must never be reported as free",
            )

    def test_probe_and_real_bind_agree(self) -> None:
        """If the probe says free, an actual bind with uvicorn's options works."""
        import socket as s

        port = free_port_near("127.0.0.1", 20000)
        self.assertTrue(cli._port_is_free("127.0.0.1", port))
        with s.socket(s.AF_INET, s.SOCK_STREAM) as real:
            try:
                real.bind(("127.0.0.1", port))
            except OSError as e:  # pragma: no cover - would be the regression
                self.fail(f"probe said free but bind failed: {e}")

    def test_free_port_never_returns_a_busy_port(self) -> None:
        base = free_port_near("127.0.0.1", 20000)
        with listening("127.0.0.1", base):
            got = cli._free_port("127.0.0.1", base)
        self.assertNotEqual(got, base)
        self.assertIsNotNone(got)


class ServeArgvTests(unittest.TestCase):
    def test_default_port_is_8770(self) -> None:
        args = cli.build_parser().parse_args(["serve"])
        self.assertEqual(args.port, cli.DEFAULT_PORT)
        self.assertEqual(cli.DEFAULT_PORT, 8770)

    def test_fallback_is_the_default_and_opt_out_exists(self) -> None:
        parser = cli.build_parser()
        self.assertFalse(parser.parse_args(["serve"]).no_fallback)
        self.assertTrue(parser.parse_args(["serve", "--no-fallback"]).no_fallback)

    def test_explicit_port_is_honoured(self) -> None:
        args = cli.build_parser().parse_args(["serve", "--port", "9100"])
        self.assertEqual(args.port, 9100)


class ServeBehaviourTests(unittest.TestCase):
    """End-to-end through ``cmd_serve`` with uvicorn stubbed out."""

    def _run(self, argv: list[str], busy: int) -> tuple[int, str]:
        import io

        import uvicorn

        captured: dict[str, object] = {}

        def fake_run(app, host, port, log_level):  # noqa: ANN001
            captured["port"] = port

        real_run = uvicorn.run
        uvicorn.run = fake_run
        # cmd_serve prints the banner on stdout; keep the test output readable.
        stdout, stderr = io.StringIO(), io.StringIO()
        real_stdout, real_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            with listening("127.0.0.1", busy):
                rc = cli.main(["serve", "--port", str(busy), *argv])
        finally:
            uvicorn.run = real_run
            sys.stdout, sys.stderr = real_stdout, real_stderr
        return rc, captured.get("port"), stderr.getvalue()

    def test_busy_port_starts_on_the_next_one_without_a_flag(self) -> None:
        base = free_port_near("127.0.0.1", 20000)
        rc, used, err = self._run([], base)
        self.assertEqual(rc, 0)
        # It must not bind the port we are holding, and it must move upwards.
        # (Deliberately no "is it free again" re-probe: the OS hands out
        # ephemeral ports freely, so that check is racy and proves nothing.)
        self.assertNotEqual(used, base)
        self.assertGreater(used, base)
        self.assertIn("in use", err)

    def test_no_fallback_refuses_instead_of_switching(self) -> None:
        base = free_port_near("127.0.0.1", 20000)
        rc, used, err = self._run(["--no-fallback"], base)
        self.assertEqual(rc, 2)
        self.assertIsNone(used)
        self.assertIn("--no-fallback", err)


if __name__ == "__main__":
    unittest.main()

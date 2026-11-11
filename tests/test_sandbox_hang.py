"""A command must not be able to hang the run.

Measured on 2026-11-11: a real model wrote a command with an unbalanced
quote.  On Windows that makes ``cmd.exe`` wait for *more input* on stdin --
and the run sat there for fifteen minutes with no output and no child
process visible.  ``subprocess.run``'s own timeout did not save it either:
killing the shell leaves a grandchild holding the stdout pipe, so
``communicate()`` blocks forever.

The sandbox now gives stdin EOF (so cmd.exe stops waiting for input) and
kills the whole tree on timeout (so the pipes close).
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
import tempfile

from planloop.sandbox import HostSandbox


class SandboxHangTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="planloop-sandbox-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_unbalanced_quote_does_not_hang(self):
        sandbox = HostSandbox(timeout=20)
        started = time.monotonic()
        obs = sandbox.run('python -c "print(1', cwd=self.root)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 20, "the command hung instead of returning")
        self.assertFalse(obs.ok)

    def test_a_timeout_is_reported_not_hung(self):
        sandbox = HostSandbox(timeout=3)
        started = time.monotonic()
        obs = sandbox.run("python -c \"import time; time.sleep(30)\"", cwd=self.root)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 25, "the timeout did not fire")
        self.assertFalse(obs.ok)
        self.assertIn("timed out", obs.stderr)

    def test_an_ordinary_command_still_works(self):
        sandbox = HostSandbox(timeout=20)
        obs = sandbox.run("python -c \"print('hi')\"", cwd=self.root)
        self.assertTrue(obs.ok, obs.stderr)
        self.assertIn("hi", obs.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

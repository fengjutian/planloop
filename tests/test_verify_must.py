"""A criterion whose ``must`` cannot be parsed must not pass by default.

Measured on 2026-11-11: a model-proposed memory guard came back with
``must="exit status 0 with all tests passing"``.  ``parse_must`` looks for
``key=value`` pairs, found none, and returned an empty dict -- and an empty
dict meant "no constraint", so the criterion **always passed**.  A guard that
can never catch anything is worse than no guard: the report claims a hard
check was in place.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from planloop.spec import Criterion
from planloop.verify import Verifier, parse_must


class UnparsableMustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="planloop-must-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_must_finds_nothing_in_prose(self):
        self.assertEqual(parse_must("exit status 0 with all tests passing"), {})

    def test_a_failing_command_fails_even_with_an_unparsable_must(self):
        criterion = Criterion(
            id="M_guard",
            kind="command",
            spec='python -c "import sys; sys.exit(1)"',
            must="exit status 0 with all tests passing",
        )
        verdict = Verifier(self.root).l1_only([criterion]).verdicts[0]
        self.assertFalse(verdict.passed, "an unparsable must must not pass by default")
        self.assertIn("unparsable must", verdict.detail)

    def test_a_passing_command_still_passes(self):
        criterion = Criterion(
            id="M_guard",
            kind="command",
            spec='python -c "print(1)"',
            must="exit status 0 with all tests passing",
        )
        verdict = Verifier(self.root).l1_only([criterion]).verdicts[0]
        self.assertTrue(verdict.passed)

    def test_a_well_formed_must_is_untouched(self):
        self.assertEqual(parse_must("exit=0"), {"exit": ("=", "0")})
        criterion = Criterion(id="A1", kind="command", spec='python -c "print(1)"', must="exit=0")
        verdict = Verifier(self.root).l1_only([criterion]).verdicts[0]
        self.assertTrue(verdict.passed)
        self.assertNotIn("unparsable must", verdict.detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

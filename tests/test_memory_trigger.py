"""A lesson whose trigger carries an error pattern must still be reachable.

The execute path builds its memory context *before* the node runs, so it
never carries error text.  A trigger section that cannot be checked is
unknown, not false -- treating it as false made every such lesson
unfireable, which on a real model (2026-11-11) meant the lesson reached
**0 of 12** prompts: the store was write-only.
"""

from __future__ import annotations

import unittest

from planloop.memory import MemoryContext, Trigger


class TriggerRetrievalTest(unittest.TestCase):
    def test_error_pattern_does_not_block_retrieval_without_error_text(self):
        trigger = Trigger(
            files=["shop/pricing.py"],
            goals=["discount"],
            error_patterns=["AssertionError:.*0\\.1 != 0\\.9"],
        )
        ctx = MemoryContext(
            goal="add a coupon to the discount path of total()",
            files=["shop/pricing.py", "tests/test_pricing.py"],
            commands=[],
            error_text="",
        )
        ok, why = trigger.matches(ctx)
        self.assertTrue(ok, why)
        self.assertIn("file:", why)

    def test_error_pattern_still_blocks_when_there_is_error_text(self):
        trigger = Trigger(error_patterns=["AssertionError:.*0\\.1 != 0\\.9"])
        ctx = MemoryContext(goal="g", files=[], commands=[], error_text="ValueError: something else")
        ok, _ = trigger.matches(ctx)
        self.assertFalse(ok)

    def test_error_pattern_matches_when_the_text_is_there(self):
        trigger = Trigger(error_patterns=["AssertionError:.*0\\.1 != 0\\.9"])
        ctx = MemoryContext(
            goal="g", files=[], commands=[],
            error_text="AssertionError: 0.1 != 0.9 within 7 places",
        )
        ok, why = trigger.matches(ctx)
        self.assertTrue(ok)
        self.assertIn("err:", why)

    def test_a_missing_file_still_blocks(self):
        trigger = Trigger(files=["other/*.py"])
        ctx = MemoryContext(goal="g", files=["shop/pricing.py"], commands=[], error_text="")
        ok, _ = trigger.matches(ctx)
        self.assertFalse(ok)

    def test_a_trigger_with_only_error_patterns_needs_error_text(self):
        trigger = Trigger(error_patterns=["ModuleNotFoundError"])
        ctx = MemoryContext(goal="g", files=["a.py"], commands=[], error_text="")
        ok, _ = trigger.matches(ctx)
        self.assertFalse(ok, "nothing could be checked, so nothing may match")

    def test_a_command_section_does_not_block_when_there_are_no_commands(self):
        """The execute path builds its context before the node runs.

        It therefore has no command history, and a trigger that carries a
        command pattern -- which the lesson prompt invites -- was
        unfireable: measured on 2026-11-11 the lesson reached 0 of 12
        prompts.
        """
        trigger = Trigger(
            files=["shop/*.py"],
            commands=["unittest discover -s tests -q"],
            error_patterns=["AssertionError"],
        )
        ctx = MemoryContext(
            goal="add a coupon",
            files=["shop/pricing.py"],
            commands=[],
            error_text="",
        )
        ok, why = trigger.matches(ctx)
        self.assertTrue(ok, why)

    def test_a_command_section_still_blocks_when_the_commands_do_not_match(self):
        trigger = Trigger(files=["shop/*.py"], commands=["pytest"])
        ctx = MemoryContext(
            goal="g", files=["shop/pricing.py"],
            commands=["unittest discover -s tests -q"], error_text="",
        )
        ok, _ = trigger.matches(ctx)
        self.assertFalse(ok)


class BrokenRegexTest(unittest.TestCase):
    """The model writes the patterns, and it writes broken ones.

    Measured on 2026-11-11: a trigger came back with an unterminated group
    (``(?i)(pricing|discount|total``).  ``re.search`` raises on it, and the
    raise took the whole retrieval path down with it.
    """

    def test_a_broken_pattern_does_not_crash_the_matcher(self):
        trigger = Trigger(goals=["(?i)(pricing|discount|total"])
        ctx = MemoryContext(goal="pricing", files=["shop/pricing.py"])
        ok, _ = trigger.matches(ctx)
        self.assertFalse(ok)

    def test_a_broken_pattern_is_dropped_when_the_trigger_is_built(self):
        trigger = Trigger.from_dict(
            {"goals": ["(?i)(pricing|discount|total", "(?i)(pricing)"], "files": ["shop/*.py"]}
        )
        self.assertEqual(trigger.goals, ["(?i)(pricing)"])

    def test_a_good_pattern_next_to_a_broken_one_still_works(self):
        trigger = Trigger.from_dict(
            {"goals": ["(?i)(pricing|discount|total", "(?i)(pricing)"], "files": ["shop/*.py"]}
        )
        ctx = MemoryContext(goal="pricing work", files=["shop/pricing.py"])
        ok, why = trigger.matches(ctx)
        self.assertTrue(ok, why)

    def test_a_string_instead_of_a_list_stays_one_pattern(self):
        """The model writes `"goals": "(?i)(pricing|discount)"` sometimes.

        Iterating that string yields one pattern per character, so the
        trigger became a wildcard -- `'i'` matches almost any goal.
        """
        trigger = Trigger.from_dict({"goals": "(?i)(pricing|discount)"})
        self.assertEqual(trigger.goals, ["(?i)(pricing|discount)"])
        ctx = MemoryContext(goal="zzz unrelated task", files=["a.py"])
        ok, _ = trigger.matches(ctx)
        self.assertFalse(ok, "a per-character wildcard would have matched anything")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

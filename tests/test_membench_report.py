"""The report is written after *every* rep -- it must survive partial data.

2026-11-11: a real 8-rep campaign died with a ``DivisionError`` in the
report generator, **after** the model calls had already been paid for.  The
cause: the first rep produced no usable control run, so the control arm's
mean token count was 0 and the percentage divided by it.
"""
import unittest

from planloop.membench import (
    ArmResult,
    MemBenchResult,
    to_markdown,
)


def _arm(arm: str, rep: int, *, tokens: int = 0, calls: int = 2, green: bool = False,
         steps_to_pass: int = 0, tokens_to_pass: int = 0, prompts: int = 0,
         with_lesson: int = 0, with_facts: int = 0) -> ArmResult:
    return ArmResult(
        label="", arm=arm, rep=rep, status="blocked",
        steps=0, steps_to_pass=steps_to_pass, tokens_to_pass=tokens_to_pass,
        model_calls=calls, tokens=tokens, l1_failures=0, guard_catches=0,
        guard_injected=False, prompts=prompts, prompts_with_lesson=with_lesson,
        prompts_with_facts=with_facts, suite_green_after=green, verdict_green=green,
        tampered=False, duration_s=0.0,
    )


class PartialDataReportTest(unittest.TestCase):
    def _report(self, arms):
        return to_markdown(MemBenchResult(mode="real", reps=1, family="fee", arms=arms))

    def test_a_first_rep_with_no_control_data_does_not_crash(self):
        """The state right after the first rep: the control arm has no tokens."""
        arms = [
            _arm("no_memory", 1),                      # the model never answered
            _arm("lesson_and_guard", 1, tokens=5000, calls=12, green=True,
                 steps_to_pass=2, tokens_to_pass=1000, prompts=12, with_lesson=12),
            _arm("fact_only", 1, tokens=4000, calls=12, green=True,
                 steps_to_pass=2, tokens_to_pass=900, prompts=12, with_facts=12),
        ]
        text = self._report(arms)
        self.assertIn("成本口径", text)

    def test_no_arms_at_all_does_not_crash(self):
        self.assertIsInstance(self._report([]), str)

    def test_a_zero_token_arm_does_not_produce_a_nan_percentage(self):
        arms = [
            _arm("no_memory", 1, tokens=0, calls=2),
            _arm("fact_only", 1, tokens=1000, calls=12, green=True,
                 steps_to_pass=1, tokens_to_pass=500, prompts=12, with_facts=12),
        ]
        text = self._report(arms)
        self.assertNotIn("nan", text.lower())
        self.assertNotIn("inf%", text.lower())

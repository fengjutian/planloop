"""The fault-injection suite must stay sharp -- and must stay honest.

Two different claims are guarded here:

1. **The criterion catches what it claims to catch.** Every injected defect is
   a defect *by construction* (the behaviour probe proves it differs from the
   correct solution), so the strong criterion is expected to reject all of
   them. If one slips through, the criterion has a blind spot.
2. **The measurement does not flatter itself.** Equivalent mutants must not
   count as criterion misses, the correct solution must pass, and a neutral
   rewrite must not be killed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from planloop.faults import (
    CASES,
    FAMILIES,
    CaseResult,
    MutantResult,
    SuiteResult,
    run_case,
    suite_for_keys,
)


class CaseDefinitionTest(unittest.TestCase):
    """Static checks -- no subprocess, no model."""

    def test_every_case_has_a_probe(self):
        for case in CASES:
            self.assertTrue(case.probe.strip(), f"{case.key} has no behaviour probe")

    def test_every_case_has_at_least_ten_defects(self):
        for case in CASES:
            self.assertGreaterEqual(
                len(case.mutants), 10, f"{case.key} has fewer than 10 defects"
            )

    def test_mutant_keys_are_unique_per_case(self):
        for case in CASES:
            keys = [m.key for m in case.mutants]
            self.assertEqual(len(keys), len(set(keys)), f"{case.key} has duplicate mutant keys")

    def test_every_family_is_known(self):
        for case in CASES:
            for mutant in case.mutants:
                self.assertIn(mutant.family, FAMILIES, f"unknown family {mutant.family}")

    def test_ten_families_are_covered_on_two_cases(self):
        """A family only exercised on one task cannot claim general coverage."""
        per_family: dict[str, set[str]] = {}
        for case in CASES:
            for mutant in case.mutants:
                per_family.setdefault(mutant.family, set()).add(case.key)
        cross = [f for f, cases in per_family.items() if len(cases) >= 2]
        self.assertGreaterEqual(
            len(per_family), 10, "the taxonomy needs at least 10 defect classes"
        )
        self.assertGreaterEqual(
            len(cross), 10, "at least 10 classes must be exercised on two or more tasks"
        )

    def test_suite_for_keys_filters(self):
        self.assertEqual(len(suite_for_keys(["calc_divide"])), 1)
        self.assertEqual(len(suite_for_keys([])), 0)


class RecallMeasurementTest(unittest.TestCase):
    """Runs one case end to end -- the heavy test."""

    @classmethod
    def setUpClass(cls):
        cls.case = CASES[0]
        with tempfile.TemporaryDirectory(prefix="planloop-faults-test-") as tmp:
            cls.result = run_case(cls.case, Path(tmp) / cls.case.key)

    def test_correct_solution_passes_both_criteria(self):
        self.assertTrue(self.result.strong_passes_correct)
        self.assertTrue(self.result.weak_passes_correct)

    def test_neutral_rewrite_passes_and_is_equivalent(self):
        self.assertTrue(self.result.strong_passes_neutral)
        self.assertTrue(self.result.weak_passes_neutral)
        self.assertTrue(self.result.neutral_is_equivalent)

    def test_every_injected_defect_is_a_real_defect(self):
        for res in self.result.results:
            self.assertTrue(
                res.is_real_defect,
                f"{res.mutant.key} behaves like the correct solution -- it is not a defect",
            )

    def test_strong_criterion_catches_every_defect(self):
        missed = [r.mutant.key for r in self.result.results if not r.strong_caught]
        self.assertEqual(missed, [], f"strong criterion missed: {missed}")

    def test_weak_criterion_is_strictly_weaker(self):
        """The visible test cannot replace the held-out oracle."""
        strong = sum(1 for r in self.result.results if r.strong_caught)
        weak = sum(1 for r in self.result.results if r.weak_caught)
        self.assertGreater(strong, weak)


class AccountingTest(unittest.TestCase):
    """The arithmetic must not count equivalent mutants as misses."""

    def _result(self, *, equivalent: bool, caught: bool) -> MutantResult:
        mutant = CASES[0].mutants[0]
        return MutantResult(
            mutant=mutant,
            strong_caught=caught,
            weak_caught=caught,
            is_real_defect=not equivalent,
        )

    def _suite(self, results: list[MutantResult]) -> SuiteResult:
        case = CaseResult(
            case=CASES[0],
            strong_passes_correct=True,
            weak_passes_correct=True,
            strong_passes_neutral=True,
            weak_passes_neutral=True,
            results=results,
        )
        return SuiteResult(cases=[case])

    def test_equivalent_mutant_is_excluded_from_the_denominator(self):
        suite = self._suite([self._result(equivalent=True, caught=False)])
        self.assertEqual(suite._counts(True), (0, 0))
        self.assertEqual(suite.recall(True), 0.0)
        self.assertEqual(len(suite.equivalent()), 1)

    def test_missed_defect_counts_against_recall(self):
        suite = self._suite([self._result(equivalent=False, caught=False)])
        self.assertEqual(suite._counts(True), (0, 1))
        self.assertEqual(suite.recall(True), 0.0)
        self.assertEqual(len(suite.missed()), 1)

    def test_caught_defect_counts_towards_recall(self):
        suite = self._suite([self._result(equivalent=False, caught=True)])
        self.assertEqual(suite._counts(True), (1, 1))
        self.assertEqual(suite.recall(True), 1.0)
        self.assertEqual(suite.missed(), [])

    def test_false_positive_counter(self):
        suite = self._suite([])
        self.assertEqual(suite.false_positives(True), 0)
        case = suite.cases[0]
        case.strong_passes_correct = False
        self.assertEqual(suite.false_positives(True), 1)

    def test_report_lists_all_three_states(self):
        suite = self._suite(
            [
                self._result(equivalent=False, caught=True),
                self._result(equivalent=True, caught=False),
            ]
        )
        text = suite.to_markdown()
        self.assertIn("等价 mutant", text)
        self.assertIn("漏检", text)
        self.assertIn("强判据召回率", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

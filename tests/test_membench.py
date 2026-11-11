"""The memory-value measurement must keep measuring what it claims.

The interesting property of this benchmark is not a number -- it is the
*contrast*:

    without memory  -> the run "succeeds" and leaves the repository broken
    with memory     -> the same task is cheaper and the repository stays green

If the control arm stops being red, the whole measurement is measuring
nothing, and every other number in the report becomes meaningless. So the
tests guard the phenomenon first, then the per-subsystem numbers -- and
always check **injection**, not just outcome: a store nobody read is a store
with no value, and the report must be able to say so.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from planloop.membench import (
    CONTRACT_FAMILY,
    FACT_KEY,
    FEE_FAMILY,
    Spy,
    _build_specs,
    _oracle_green,
    _reset_repo,
    analyze_events,
    run_membench,
)


class MemoryValueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(prefix="planloop-membench-") as tmp:
            cls.result = run_membench(Path(tmp))

    def one(self, arm: str):
        items = self.result.by_arm(arm)
        self.assertEqual(len(items), 1, f"{arm} should have exactly one rep")
        return items[0]

    def test_first_task_leaves_a_lesson_and_a_verified_fact(self):
        self.assertGreaterEqual(self.result.lessons_after_setup[0], 1)
        self.assertGreaterEqual(self.result.facts_stored, 1)
        self.assertTrue(self.result.setup[0].suite_green_after)

    def test_control_arm_leaves_the_repository_broken(self):
        """The phenomenon: the run reports success and the repo is red."""
        control = self.one("no_memory")
        self.assertFalse(control.suite_green_after)
        self.assertGreaterEqual(control.l1_failures, 1)

    def test_control_arm_has_no_memory_injected(self):
        control = self.one("no_memory")
        self.assertFalse(control.guard_injected)
        self.assertEqual(control.prompts_with_lesson, 0)
        self.assertEqual(control.prompts_with_facts, 0)

    def test_lesson_arm_is_cheaper_and_keeps_the_repository_green(self):
        control = self.one("no_memory")
        lesson = self.one("lesson_and_guard")
        self.assertTrue(lesson.suite_green_after)
        self.assertGreaterEqual(lesson.prompts_with_lesson, 1)
        self.assertLessEqual(lesson.steps, control.steps)
        self.assertLessEqual(lesson.l1_failures, control.l1_failures)

    def test_guard_alone_blocks_the_regression_but_costs_a_step(self):
        guard = self.one("guard_only")
        self.assertTrue(guard.guard_injected)
        self.assertGreaterEqual(guard.guard_catches, 1)
        self.assertTrue(guard.suite_green_after)

    def test_fact_arm_reads_the_fact_and_keeps_the_repository_green(self):
        fact = self.one("fact_only")
        self.assertGreaterEqual(fact.prompts_with_facts, 1)
        self.assertTrue(fact.suite_green_after)
        self.assertFalse(fact.guard_injected)

    def test_every_arm_reports_a_status(self):
        for arm in self.result.arms:
            self.assertTrue(arm.status, f"{arm.label} has no status")
            self.assertGreaterEqual(arm.steps, 1)
            self.assertGreaterEqual(arm.prompts, 1)

    def test_steps_to_pass_is_the_work_and_never_exceeds_the_total(self):
        """``steps`` counts the model's habit of re-verifying; ``steps_to_pass``
        counts the work.  The cost comparison needs the second one."""
        for arm in self.result.arms:
            self.assertGreaterEqual(arm.steps_to_pass, 1, f"{arm.label} never passed")
            self.assertLessEqual(arm.steps_to_pass, arm.steps)


class RepetitionTest(unittest.TestCase):
    """Two reps must stay independent: no state leaks between them."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(prefix="planloop-membench2-") as tmp:
            cls.result = run_membench(Path(tmp), reps=2)

    def test_each_rep_produces_one_result_per_arm(self):
        self.assertEqual(len(self.result.setup), 2)
        for arm in self.result.arm_names():
            self.assertEqual(len(self.result.by_arm(arm)), 2, f"{arm} lost a rep")

    def test_control_arm_is_red_in_every_rep(self):
        for item in self.result.by_arm("no_memory"):
            self.assertFalse(item.suite_green_after, f"rep {item.rep} was green")


class FactKeyTest(unittest.TestCase):
    def test_fact_key_is_namespaced_to_the_repository(self):
        self.assertTrue(FACT_KEY.startswith("shop/pricing.py."))


class ContractFamilyTest(unittest.TestCase):
    """The family built for a real model: its *shape* is the measurement.

    A real model solves the demo family in one shot, and a lesson only
    exists when a task fails and is then fixed -- so the demo family cannot
    measure a real model at all.  The contract family puts the failure in the
    repository instead of in the model's carelessness, and three properties
    have to hold or the real run measures nothing:

    1. the held-out criteria live **outside** the workspace, so the model can
       neither read nor edit what it is judged by;
    2. the second task's *node* is judged by the **new feature only** -- the
       old behaviour is deliberately invisible to the node, which is the gap
       a memory guard is supposed to close;
    3. the run-level verdict **does** check the old behaviour, so a run that
       regressed it is a failed run.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="planloop-contract-")
        base = Path(cls.tmp.name)
        cls.repo = base / "repo"
        cls.repo.mkdir()
        _reset_repo(cls.repo, CONTRACT_FAMILY)
        cls.r1, cls.r2, cls.oracles = _build_specs(base, CONTRACT_FAMILY, 1)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_oracles_are_written_outside_the_workspace(self):
        for label, cmd in self.oracles.items():
            self.assertTrue(cmd, f"{label} oracle has no command")
            self.assertNotIn(str(self.repo).replace("\\", "/"), cmd.replace("\\", "/"))
        self.assertFalse((self.repo / "oracle").exists())

    def test_second_node_is_judged_by_the_new_feature_only(self):
        specs = [c["spec"] for c in self.r2["plan_template"]["nodes"][0]["acceptance"]]
        self.assertEqual(len(specs), 1)
        self.assertIn("r2_oracle", specs[0])
        self.assertNotIn("r1_oracle", specs[0])

    def test_run_level_verdict_checks_the_old_behaviour(self):
        specs = [c["spec"] for c in self.r2["success_criteria"]]
        self.assertEqual(len(specs), 1)
        self.assertIn("final_oracle", specs[0])

    def test_first_task_is_judged_by_the_visible_suite_and_the_contract(self):
        specs = [c["spec"] for c in self.r1["plan_template"]["nodes"][0]["acceptance"]]
        self.assertEqual(len(specs), 2)
        self.assertIn("r1_oracle", specs[1])

    def test_every_oracle_is_red_on_the_untouched_repository(self):
        for label in ("r1", "r2", "final"):
            self.assertFalse(
                _oracle_green(self.oracles[label], self.repo),
                f"the {label} oracle passes on a repository that has not been fixed",
            )

    def test_the_second_task_does_not_carry_the_contract_file(self):
        """The cost measurement needs a contract the model cannot read for free.

        The second task has to use ``pct()``; its definition lives in
        ``shop/helpers.py``.  If that file were in the prompt, the model
        would copy the contract out of it and a memory carrying the same
        contract could not save it anything.
        """
        self.assertIn("shop/helpers.py", self.r1["context_files"])
        self.assertNotIn("shop/helpers.py", self.r2["context_files"])

    def test_the_second_oracle_only_passes_with_the_right_contract(self):
        # 300 * 0.9 * 0.8 == 216: the "remaining factor" contract, not "value / 100"
        self.assertIn("216.0", CONTRACT_FAMILY.r2_oracle)
        self.assertIn("270.0", CONTRACT_FAMILY.final_oracle)


class EventAnalysisTest(unittest.TestCase):
    """An arm's behaviour must be readable back from its own event log.

    The report can say the arms differed in cost; it can only say whether the
    memory changed **what the model did** if the log carries the action's
    target and the observations that exposed the hidden contract.
    """

    def _log(self, base: Path, records: list[dict]) -> Path:
        import json

        path = base / "events.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    def test_counts_actions_and_finds_the_hidden_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log = self._log(base, [
                {"kind": "action", "data": {"action": "read", "target": "shop/helpers.py",
                                           "detail": "stdout:\ndef pct(value):"}},
                {"kind": "action", "data": {"action": "write", "target": "shop/pricing.py",
                                           "detail": "wrote 191 chars"}},
                {"kind": "action", "data": {"action": "run", "cmd": "x",
                                           "detail": "exit_code=0\nstdout:\ndef pct(value):"}},
                {"kind": "run_end", "data": {"status": "succeeded"}},
            ])
            stats = analyze_events(log, ["def pct("])
        self.assertEqual(stats.actions, 3)
        self.assertEqual(stats.reads, 1)
        self.assertEqual(stats.writes, 1)
        self.assertEqual(stats.runs, 1)
        self.assertEqual(stats.hidden_looks, 2)
        self.assertEqual(stats.first_label, "read shop/helpers.py")

    def test_a_log_without_targets_still_reports_the_first_action(self):
        """Older logs have no ``target``; the marker still finds the lookup."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log = self._log(base, [
                {"kind": "action", "data": {"action": "read",
                                           "detail": "stdout:\ndef pct(value):"}},
            ])
            stats = analyze_events(log, ["def pct("])
        self.assertEqual(stats.first_action, "read")
        self.assertEqual(stats.hidden_looks, 1)

    def test_a_missing_log_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = analyze_events(Path(tmp) / "nope.jsonl", ["x"])
        self.assertEqual(stats.actions, 0)


class FeeFamilyTest(unittest.TestCase):
    """The family that fixes the ``contract`` family's device flaw.

    The contract family's visible test pins ``total([100, 200], 10) == 270``,
    so a model that guesses the obvious formula (``1 - p/100``) gets
    ``pct(10) == 0.9`` *and* ``pct(20) == 0.8`` right without opening
    ``shop/helpers.py``.  The knowledge was never needed, so the measurement
    measured nothing.  The fee family pins one row of a tier table instead,
    and the properties below are what make the knowledge necessary.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="planloop-fee-")
        base = Path(cls.tmp.name)
        cls.repo = base / "repo"
        cls.repo.mkdir()
        _reset_repo(cls.repo, FEE_FAMILY)
        cls.r1, cls.r2, cls.oracles = _build_specs(base, FEE_FAMILY, 1)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_visible_test_pins_only_the_ten_percent_row(self):
        self.assertIn("264", FEE_FAMILY.initial["tests/test_pricing.py"])
        self.assertNotIn("198", FEE_FAMILY.initial["tests/test_pricing.py"])

    def test_the_naive_formula_would_be_wrong(self):
        """1 - p/100 gives 0.8 for a 20% discount; the truth is 0.75.

        If the naive formula were right, the control arm would pass without
        looking anything up and the memory arms would have nothing to save.
        """
        self.assertIn("0.75", FEE_FAMILY.r1_oracle)      # the truth
        self.assertIn("198.0", FEE_FAMILY.r2_oracle)     # 300 * 0.88 * 0.75
        # the naive formula would produce 300 * 0.88 * 0.8 == 211.2
        self.assertNotIn("211.2", FEE_FAMILY.r2_oracle)

    def test_the_oracles_do_not_print_the_expected_numbers(self):
        """A failure message that leaks the answer hands the control arm the
        knowledge it was supposed to have to look up.

        The numbers may of course appear in the oracle *source* -- the source
        lives outside the workspace.  What must not appear is the assertion
        **message**, because that is what the model gets to read.
        """
        import re

        for source in (FEE_FAMILY.r1_oracle, FEE_FAMILY.r2_oracle, FEE_FAMILY.final_oracle):
            messages = re.findall(r'msg="([^"]*)"', source)
            self.assertTrue(messages, "the oracle has no fixed messages")
            for message in messages:
                self.assertFalse(
                    re.search(r"\d+\.\d+", message),
                    f"the message leaks the answer: {message}",
                )

    def test_the_verified_fact_states_the_rule_not_the_answer(self):
        self.assertIn("surcharge", FEE_FAMILY.fact_value)
        self.assertNotIn("198", FEE_FAMILY.fact_value)
        self.assertTrue(FEE_FAMILY.fact_key)

    def test_the_second_task_still_hides_the_contract_file(self):
        self.assertNotIn("shop/helpers.py", FEE_FAMILY.r2_context_files)

    def test_every_oracle_is_red_on_the_untouched_repository(self):
        for label in ("r1", "r2", "final"):
            self.assertFalse(
                _oracle_green(self.oracles[label], self.repo),
                f"the {label} oracle passes on a repository that has not been fixed",
            )

    def test_a_model_that_never_reads_the_file_fails(self):
        """The knowledge has to be **necessary**, or nothing is measured.

        This writes the "obvious formula" a model can guess from the visible
        test -- ``1 - p/100`` -- and checks that the member oracle rejects it.
        If this passed, the device would be measuring nothing again, exactly
        like the contract family did.
        """
        repo = self.repo
        (repo / "shop" / "pricing.py").write_text(
            "def total(prices, discount_pct, member_pct=0):\n"
            "    subtotal = sum(prices)\n"
            "    return subtotal * (1 - discount_pct / 100) * (1 - member_pct / 100)\n",
            encoding="utf-8",
        )
        self.assertFalse(
            _oracle_green(self.oracles["r2"], repo),
            "the naive formula passes the member oracle: the contract is guessable again",
        )


class ModelFailureTest(unittest.TestCase):
    """A run whose model never answered is a **broken harness**, not a failed task.

    2026-11-11: a real 8-rep run lost three reps to provider errors --
    0 tokens, 2 calls, the repo never touched.  Those runs were counted as
    "the model got it wrong", which is how a provider hiccup turns into a
    conclusion about the model.
    """

    def _measure(self, usage):
        from planloop.membench import _measure

        class _Result:
            status = "blocked"
            duration_s = 0.0
            outcomes = []
            compile_report = None

        _Result.usage = usage
        with tempfile.TemporaryDirectory() as tmp:
            return _measure(_Result(), Spy(object()), Path(tmp),
                            guard_injected=False, tampered=False)

    def test_a_run_with_no_tokens_is_flagged_as_a_model_failure(self):
        self.assertTrue(self._measure({"calls": 2, "tokens": 0}).model_failed)

    def test_a_run_with_tokens_is_not_flagged(self):
        self.assertFalse(self._measure({"calls": 12, "tokens": 14000}).model_failed)

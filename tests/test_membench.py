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

import json
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
from planloop.membench_verify import recompute


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

    def test_the_criterion_is_never_written_to_disk(self):
        """A criterion that lives on disk is not held out.

        Measured on 2026-11-11: the model read the criterion file three times
        with ``cd /d ... && type ...`` -- the workspace lock stops the ``read``
        tool, not the shell -- and the source contains the expected numbers.
        So the criterion is not written anywhere: it is materialised for the
        duration of a check and deleted again.
        """
        for label, source in self.oracles.items():
            self.assertTrue(source, f"{label} criterion has no source")
        self.assertFalse((self.repo / "oracle").exists())
        for path in Path(self.tmp.name).rglob("*oracle*.py"):
            self.fail(f"the criterion was written to disk: {path}")

    def test_second_node_is_judged_by_the_new_feature_only(self):
        criteria = self.r2["plan_template"]["nodes"][0]["acceptance"]
        self.assertEqual(len(criteria), 1)
        self.assertEqual(criteria[0]["kind"], "held_out")
        self.assertEqual(criteria[0]["source"], CONTRACT_FAMILY.r2_oracle)
        self.assertNotIn(CONTRACT_FAMILY.r1_oracle, criteria[0]["source"])

    def test_run_level_verdict_checks_the_old_behaviour(self):
        criteria = self.r2["success_criteria"]
        self.assertEqual(len(criteria), 1)
        self.assertEqual(criteria[0]["kind"], "held_out")
        self.assertEqual(criteria[0]["source"], CONTRACT_FAMILY.final_oracle)

    def test_first_task_is_judged_by_the_visible_suite_and_the_contract(self):
        criteria = self.r1["plan_template"]["nodes"][0]["acceptance"]
        self.assertEqual(len(criteria), 2)
        self.assertEqual(criteria[0]["kind"], "command")
        self.assertEqual(criteria[1]["kind"], "held_out")
        self.assertEqual(criteria[1]["source"], CONTRACT_FAMILY.r1_oracle)

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

    def test_the_first_task_does_carry_the_contract_file(self):
        """The setup task must stay solvable -- and the cost of that is known.

        ``shop/helpers.py`` holds the contract: the tier table and the rule
        "the platform fee is deducted on top of the discount".  Taking it out
        of the first task's prompt was tried (2026-11-11, ``.membench-fee17p``):
        the model then read the same file 24 times without writing anything
        -- it spins instead of failing and fixing itself -- so the first task
        ended "blocked" and **no lesson was produced at all**.  With the file
        in the prompt the first task is sometimes solved on the first write
        (3 of 8 repetitions), and then there is no lesson either: the lesson
        yield is decided by the model's behaviour, not by the device.
        """
        self.assertIn("shop/helpers.py", self.r1["context_files"])
        self.assertNotIn("shop/helpers.py", self.r2["context_files"])
        self.assertTrue((self.repo / "shop" / "helpers.py").exists(),
                        "the contract file must stay in the repository")

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


class VerifyTest(unittest.TestCase):
    """The published numbers must be derivable from the raw artifacts.

    2026-11-11: the numbers of a real 8-rep run were **typed into the
    document by hand**, and four of them were wrong at once -- most of them
    because the failures were **not aligned across arms** (the control arm's
    rep 6 ran fine and was judged green, so dropping "rep 6/7/8" threw away a
    valid control run) and one because a total mixed denominators.

    A number that is typed twice is a number that will be wrong.  So the
    numbers are re-derived from the event logs and cross-checked against the
    machine report -- and this module is what does that.
    """

    def _fixture(self, tmp: Path, rows: list[dict], events: dict[str, list[str]]):
        """Write a tiny fake run: a machine report plus event logs."""
        run_dir = tmp / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        header = ("| rep | 臂 | 状态 | steps2pass | tok2pass | 总步数 | 模型调用 | 总 tokens | "
                  "L1 失败 | 护栏拦截 | 无效回复 | 带教训 | 带事实 | 外部判定 | 全量测试 |")
        lines = [header, "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for row in rows:
            cells = [str(row[c]) for c in (
                "rep", "arm", "status", "steps_to_pass", "tokens_to_pass", "total_steps",
                "model_calls", "tokens", "l1_failures", "guard_catches")]
            cells.append(str(row.get("invalid", 0)))
            cells += [str(row[c]) for c in (
                "prompts_lesson", "prompts_facts", "verdict", "suite")]
            lines.append("| " + " | ".join(cells) + " |")
        report = tmp / "report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for name, records in events.items():
            (run_dir / f"events_{name}.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                encoding="utf-8",
            )
        return run_dir, report

    def _action(self, target: str, detail: str = "ok"):
        return {"kind": "action", "data": {"action": "read", "target": target, "detail": detail}}

    def test_a_broken_run_leaves_the_denominator(self):
        """0 tokens and 2 calls: the model never answered -> the harness broke."""
        rows = [
            {"rep": 1, "arm": "R2_none", "status": "succeeded", "steps_to_pass": 2,
             "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12, "tokens": 1000,
             "l1_failures": 0, "guard_catches": 0, "prompts_lesson": "0/12",
             "prompts_facts": "0/12", "verdict": "绿", "suite": "绿"},
            {"rep": 2, "arm": "R2_none", "status": "blocked", "steps_to_pass": 0,
             "tokens_to_pass": 0, "total_steps": 2, "model_calls": 2, "tokens": 0,
             "l1_failures": 0, "guard_catches": 0, "prompts_lesson": "0/2",
             "prompts_facts": "0/2", "verdict": "红", "suite": "红"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            events = {
                "R2_none_1": [{"kind": "action", "data": {"action": "read", "target": "a.py"}}],
                "R2_none_2": [],
            }
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
            self.assertEqual(result.usable["no_memory"], [1])
            self.assertEqual(result.broken["no_memory"], [2])

    def test_failures_that_are_not_aligned_across_arms_are_reported(self):
        """The control arm had one valid rep more than the other two.

        That is the trap: dropping "rep 6/7/8" by hand would throw away a
        valid control run, and the comparison would stop being paired.
        """
        rows = []
        events = {}
        for rep, arm in ((1, "R2_none"), (2, "R2_none"), (6, "R2_none")):
            broken = rep == 6
            rows.append({
                "rep": rep, "arm": arm, "status": "blocked" if broken else "succeeded",
                "steps_to_pass": 0 if broken else 2, "tokens_to_pass": 0 if broken else 100,
                "total_steps": 2 if broken else 12, "model_calls": 2 if broken else 12,
                "tokens": 0 if broken else 1000, "l1_failures": 0, "guard_catches": 0,
                "prompts_lesson": "0/2" if broken else "0/12",
                "prompts_facts": "0/2" if broken else "0/12",
                "verdict": "红" if broken else "绿", "suite": "红" if broken else "绿",
            })
            events[f"{arm}_{rep}"] = (
                [] if broken
                else [{"kind": "action", "data": {"action": "read", "target": "a.py"}}]
            )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
            self.assertEqual(result.usable["no_memory"], [1, 2])
            self.assertEqual(result.broken["no_memory"], [6])

    def test_a_report_that_disagrees_with_the_event_log_is_flagged(self):
        """The event log is the truth: a failure count that does not match fails."""
        rows = [{
            "rep": 1, "arm": "R2_none", "status": "succeeded", "steps_to_pass": 2,
            "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12, "tokens": 1000,
            "l1_failures": 0, "guard_catches": 0, "prompts_lesson": "0/12",
            "prompts_facts": "0/12", "verdict": "绿", "suite": "绿",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            events = {"R2_none_1": [
                {"kind": "failure", "data": {"reason": "boom"}},
                {"kind": "action", "data": {"action": "read", "target": "a.py"}},
            ]}
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
            self.assertTrue(result.mismatches)
            self.assertIn("failure", result.mismatches[0])

    def test_an_old_report_gets_a_note_instead_of_a_mismatch(self):
        """A report from before the counting fix cannot be re-derived exactly.

        Old reports counted *distinct* failures per attempt; the log records
        every failing check.  Calling that a disagreement would make the gate
        permanently red on old runs -- and a gate that is always red is a gate
        nobody reads.  So it is a note, with the row spelled out.
        """
        header = ("| rep | 臂 | 状态 | steps2pass | tok2pass | 总步数 | 模型调用 | 总 tokens | "
                  "L1 失败 | 护栏拦截 | 带教训 | 带事实 | 外部判定 | 全量测试 |")
        row = "| 1 | R2_none | succeeded | 2 | 100 | 12 | 12 | 1000 | 0 | 0 | 0/12 | 0/12 | 绿 | 绿 |"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir = tmp / "run"
            run_dir.mkdir()
            report = tmp / "report.md"
            report.write_text("\n".join([header, row]) + "\n", encoding="utf-8")
            (run_dir / "events_R2_none_1.jsonl").write_text(
                json.dumps({"kind": "failure", "data": {"reason": "boom"}}) + "\n"
                + json.dumps({"kind": "action", "data": {"action": "read", "target": "a.py"}}) + "\n",
                encoding="utf-8",
            )
            result = recompute(run_dir, report, FEE_FAMILY)
            self.assertFalse(result.mismatches, result.mismatches)
            self.assertTrue(result.notes)
            self.assertIn("R2_none rep 1", result.notes[0])

    def test_the_hidden_looks_come_from_the_event_log_not_the_report(self):
        """Behaviour is re-derived, never copied from the report."""
        rows = [{
            "rep": 1, "arm": "R2_none", "status": "succeeded", "steps_to_pass": 2,
            "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12, "tokens": 1000,
            "l1_failures": 0, "guard_catches": 0, "prompts_lesson": "0/12",
            "prompts_facts": "0/12", "verdict": "绿", "suite": "绿",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            events = {"R2_none_1": [
                self._action("shop/helpers.py", "def pct(value): ..."),
                self._action("shop/pricing.py", "def total(...): ..."),
            ]}
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
            summary = result.summaries["R2_none"]
            self.assertEqual(summary.hidden_looks, 1,
                             "the hidden contract was read once in the event log")

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
    PUBLISHABLE_FAMILIES,
    Spy,
    _build_specs,
    _oracle_green,
    _reset_repo,
    analyze_events,
    run_membench,
)
from planloop.membench_publish import check_behaviour, check_floor, check_published, rebuild
from planloop.membench_verify import format_report, parse_report, recompute


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


class FamilyDeviceTest(unittest.TestCase):
    """Every family must prove that its knowledge is **necessary**.

    2026-11-11: the contract family's contract could be guessed from the
    visible test (``1 - p/100`` gets both rows right), so the memory had
    nothing to save -- the whole run measured nothing and had to be
    withdrawn.  The fee family fixed that by pinning one row of a tier
    table.

    So every family now ships the **shortcut a model could guess** and has to
    prove that the held-out oracle rejects it.  A family that cannot is not
    a device, and nothing may be published from it.
    """

    FAMILIES = {"fee": FEE_FAMILY, "contract": CONTRACT_FAMILY}

    def _shortcut_is_rejected(self, fam) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            _reset_repo(repo, fam)
            for path, content in fam.guessable_solution.items():
                (repo / path).write_text(content, encoding="utf-8")
            return not _oracle_green(fam.r2_oracle, repo)

    def test_every_family_declares_its_shortcut_and_why(self):
        for name, fam in self.FAMILIES.items():
            self.assertTrue(fam.guessable_solution,
                            f"{name} declares no guessable shortcut -- so nobody has "
                            f"checked whether its knowledge is necessary")
            self.assertTrue(fam.device_note,
                            f"{name} does not say why its knowledge is necessary")

    def test_the_oracle_says_the_same_thing_as_the_family(self):
        for name, fam in self.FAMILIES.items():
            rejected = self._shortcut_is_rejected(fam)
            self.assertEqual(
                rejected, fam.shortcut_rejected,
                f"{name}: the oracle {'rejects' if rejected else 'accepts'} the "
                f"guessable shortcut, but the family says the opposite",
            )

    def test_only_a_family_with_a_necessary_knowledge_may_be_published(self):
        for name in PUBLISHABLE_FAMILIES:
            fam = self.FAMILIES[name]
            self.assertTrue(
                fam.shortcut_rejected,
                f"{name} may not be published: its knowledge is not necessary, so the "
                f"measurement would be measuring nothing",
            )

    def test_the_known_bad_family_still_says_why_it_is_bad(self):
        """The withdrawn family stays in the code -- with the reason attached."""
        fam = self.FAMILIES["contract"]
        self.assertFalse(fam.shortcut_rejected)
        self.assertIn("never necessary", fam.device_note)


class ReportFormatTest(unittest.TestCase):
    """The report generator and the gate's parser must speak the same format.

    The gate reads the machine report's per-rep table and compares it with
    the event logs.  If the two drift apart -- a column added on one side
    only -- the gate either stops checking anything or starts checking the
    wrong numbers.  The report has **two** tables of the same width, so the
    parser has to know which one is the per-rep table.
    """

    def _arm(self, **kw) -> "ArmResult":
        from planloop.membench import ArmResult

        base = dict(
            label="R2_none", arm="no_memory", rep=1, status="succeeded",
            steps=12, steps_to_pass=2, tokens_to_pass=100, model_calls=12, tokens=1000,
            l1_failures=3, guard_catches=0, guard_injected=False, prompts=12,
            prompts_with_lesson=4, prompts_with_facts=0, suite_green_after=True,
            verdict_green=True, tampered=False, duration_s=1.0, invalid=2,
        )
        base.update(kw)
        return ArmResult(**base)

    def _report_text(self) -> str:
        from planloop.membench import MemBenchResult, to_markdown

        return to_markdown(MemBenchResult(mode="real", reps=1, family="fee",
                                          arms=[self._arm()]))

    def test_the_generated_report_can_be_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            path.write_text(self._report_text(), encoding="utf-8")
            rows = parse_report(path)
        self.assertEqual(len(rows), 1, "the per-rep table was not found")
        row = rows[0]
        self.assertEqual(row.arm, "R2_none")
        self.assertEqual(row.l1_failures, 3)
        self.assertEqual(row.invalid, 2, "the wasted-replies column must survive")
        self.assertEqual(row.prompts_with_lesson, 4)
        self.assertEqual(row.total_steps, 12)

    def test_the_summary_table_is_not_read_as_repetitions(self):
        """Both tables have the same width; only one of them is per-rep."""
        summary = ("| 臂 | n | 绿 | 步数 | tokens | 总步数 | 总 tokens | L1 | 护栏 | "
                   "无效回复 | 带教训 | 带事实 | 判定 | 全量 | 备注 |\n"
                   "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                   "| 1 | R2_none | 1/1 | 3.0 | 100 | 12 | 1000 | 3 | 0 | 2 | 4/12 | 0/12 |"
                   " 绿 | 绿 | x |\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            path.write_text(summary + self._report_text(), encoding="utf-8")
            rows = parse_report(path)
        self.assertEqual(len(rows), 1, "the summary row was read as a repetition")
        self.assertEqual(rows[0].rep, 1)

    def test_a_report_without_a_per_rep_table_is_flagged(self):
        """A gate that cannot read the report must not report success."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = tmp / "report.md"
            path.write_text("# 一份没有逐次表的报告\n\n什么都没有。\n", encoding="utf-8")
            run_dir = tmp / "run"
            run_dir.mkdir()
            result = recompute(run_dir, path, FEE_FAMILY)
        self.assertTrue(result.mismatches, "an unreadable report passed the gate")
        self.assertIn("no per-rep table", result.mismatches[0])


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
            interrupted = []
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

    def test_the_floor_percentage_is_computed_from_the_unrounded_means(self):
        """Rounding the means first turned a 50% floor into 48%.

        The floor is a headline number, and on the real run the means were
        2.667 and 4.0 -- rounded to 2.7 and 4.0 the percentage came out two
        points low.  The rounding is for *display*; the percentage has to be
        computed from the raw means.
        """
        rows = []
        events = {}
        for rep, steps in ((1, (3, 8)), (2, (3, 2)), (3, (2, 2))):
            for arm, value in (("R2_none", steps[0]), ("R2_memory", steps[1]),
                               ("R2_fact", 2)):
                rows.append({
                    "rep": rep, "arm": arm, "status": "succeeded", "steps_to_pass": value,
                    "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12,
                    "tokens": 1000, "l1_failures": 1, "guard_catches": 0,
                    "prompts_lesson": "0/12", "prompts_facts": "0/12",
                    "verdict": "绿", "suite": "绿",
                })
                events[f"{arm}_{rep}"] = [
                    {"kind": "action", "data": {"action": "read", "target": "a.py"}},
                ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
        self.assertEqual(result.noise_floor["steps_delta_pct"], "50%",
                         "the floor must come from the unrounded means")

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

    # -- an attempt that a policy check cut short -------------------------
    # 2026-11-12: a real run had its second attempt stopped by the risk
    # policy.  The attempt was dropped, so the report counted fewer steps
    # and fewer failures than the event log recorded -- and the gate said
    # nothing about it, because the gate only compared one column.

    def _dropped_attempt_events(self) -> list[dict]:
        return [
            {"kind": "node_start", "data": {"attempt": 1}},
            {"kind": "failure", "data": {"reason": "boom"}},
            {"kind": "node_end", "data": {"status": "failed", "steps": 1}},
            {"kind": "node_start", "data": {"attempt": 2}},
            {"kind": "failure", "data": {"reason": "boom again"}},
            {"kind": "action", "data": {"action": "read", "target": "a.py"}},
            {"kind": "approval_requested", "data": {"rule": "subshell"}},
        ]

    def _dropped_attempt_row(self) -> dict:
        return {
            "rep": 1, "arm": "R2_none", "status": "blocked", "steps_to_pass": 0,
            "tokens_to_pass": 0, "total_steps": 1, "model_calls": 20, "tokens": 1000,
            "l1_failures": 1, "guard_catches": 0, "prompts_lesson": "0/20",
            "prompts_facts": "0/20", "verdict": "红", "suite": "绿",
        }

    def test_a_dropped_attempt_is_flagged_with_its_cause(self):
        """The report counts less than the log -> the gate must say so."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(
                tmp, [self._dropped_attempt_row()], {"R2_none_1": self._dropped_attempt_events()}
            )
            result = recompute(run_dir, report, FEE_FAMILY)
        joined = " ".join(result.mismatches)
        self.assertTrue(result.mismatches, "the dropped attempt was accepted silently")
        self.assertIn("2 failure(s)", joined, "the log's failure count must be named")
        self.assertIn("2 step(s)", joined, "the log's step count must be named")

    def test_the_cut_short_attempt_is_explained_not_just_counted(self):
        """Every note has to say *why* the two books differ."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(
                tmp, [self._dropped_attempt_row()], {"R2_none_1": self._dropped_attempt_events()}
            )
            result = recompute(run_dir, report, FEE_FAMILY)
        notes = " ".join(result.notes)
        self.assertIn("cut short", notes)
        self.assertIn("policy check", notes)

    def test_the_failure_count_comes_from_the_log_not_the_report(self):
        """The aggregate is quoted in the document -- so it is the log's number."""
        rows = [self._dropped_attempt_row()]
        for arm in ("R2_memory", "R2_fact"):
            rows.append({
                "rep": 1, "arm": arm, "status": "succeeded", "steps_to_pass": 2,
                "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12, "tokens": 1000,
                "l1_failures": 1, "guard_catches": 0, "prompts_lesson": "0/12",
                "prompts_facts": "0/12", "verdict": "绿", "suite": "绿",
            })
        events = {"R2_none_1": self._dropped_attempt_events()}
        for arm in ("R2_memory", "R2_fact"):
            events[f"{arm}_1"] = [
                {"kind": "failure", "data": {"reason": "boom"}},
                self._action("a.py"),
            ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
        self.assertEqual(result.summaries["R2_none"].l1_failures, 2,
                         "the log has two failures; the report only counted one")

    # -- the noise floor ---------------------------------------------------

    def _three_arms(self, rep: int, with_lesson: int) -> tuple[list[dict], dict[str, list[dict]]]:
        rows = []
        events = {}
        for arm in ("R2_none", "R2_memory", "R2_fact"):
            rows.append({
                "rep": rep, "arm": arm, "status": "succeeded", "steps_to_pass": 2,
                "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12, "tokens": 1000,
                "l1_failures": 1, "guard_catches": 0,
                "prompts_lesson": f"{with_lesson}/12" if arm == "R2_memory" else "0/12",
                "prompts_facts": "12/12" if arm == "R2_fact" else "0/12",
                "verdict": "绿", "suite": "绿",
            })
            events[f"{arm}_{rep}"] = [self._action("shop/helpers.py")]
        return rows, events

    def test_the_noise_floor_uses_the_reps_that_carried_no_lesson(self):
        """The floor is the A/A comparison: both arms ran the same prompt."""
        rows, events = [], {}
        r1, e1 = self._three_arms(1, 0)     # no lesson to carry -> identical prompts
        r2, e2 = self._three_arms(2, 5)     # a lesson was carried -> not a floor
        rows += r1 + r2
        events.update(e1)
        events.update(e2)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
        self.assertEqual(result.lesson_paired_reps, [2])
        self.assertEqual(result.noise_floor["reps"], [1],
                         "the floor is the repetition where neither arm carried a lesson")

    def test_a_run_with_no_silent_repetition_says_it_has_no_floor(self):
        """No floor is not a floor of zero -- the report has to say so."""
        rows, events = self._three_arms(1, 5)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
            text = format_report(result)
        self.assertFalse(result.noise_floor)
        self.assertIn("NO NOISE FLOOR", text)
        self.assertNotIn("NOISE FLOOR (", text)

    def test_the_floor_is_printed_with_the_numbers_it_stands_on(self):
        rows, events = self._three_arms(1, 0)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
            text = format_report(result)
        self.assertIn("NOISE FLOOR", text)
        self.assertIn("model's own variance", text)

    # -- the wasted steps --------------------------------------------------

    def test_the_invalid_reply_column_is_cross_checked_against_the_log(self):
        """The column of unparsable replies must be re-derivable."""
        rows = [{
            "rep": 1, "arm": "R2_none", "status": "succeeded", "steps_to_pass": 2,
            "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12, "tokens": 1000,
            "l1_failures": 1, "guard_catches": 0, "invalid": 0, "prompts_lesson": "0/12",
            "prompts_facts": "0/12", "verdict": "绿", "suite": "绿",
        }]
        events = {"R2_none_1": [
            {"kind": "invalid_action", "data": {"raw": "thinking out loud"}},
            self._action("a.py"),
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            result = recompute(run_dir, report, FEE_FAMILY)
        joined = " ".join(result.mismatches)
        self.assertTrue(result.mismatches, "the wasted step was not cross-checked")
        self.assertIn("invalid_action", joined)


class PublishedTableTest(unittest.TestCase):
    """The published table must be the table the logs rebuild -- cell for cell.

    2026-11-12: the document quoted a machine report that had dropped an
    attempt the risk policy cut short, so two cells were smaller than the
    event log says.  The numbers are now rebuilt from the log
    (``planloop.membench_publish``) -- and this checks that the rebuild and
    the document agree, so a hand-edited cell cannot survive.
    """

    def _fixture(self, tmp: Path, rows: list[dict], events: dict[str, list[dict]]):
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

    def _dropped(self) -> tuple[list[dict], dict[str, list[dict]]]:
        rows = [{
            "rep": 1, "arm": "R2_none", "status": "blocked", "steps_to_pass": 0,
            "tokens_to_pass": 0, "total_steps": 1, "model_calls": 20, "tokens": 1000,
            "l1_failures": 1, "guard_catches": 0, "prompts_lesson": "0/20",
            "prompts_facts": "0/20", "verdict": "红", "suite": "绿",
        }]
        events = {"R2_none_1": [
            {"kind": "node_start", "data": {"attempt": 1}},
            {"kind": "failure", "data": {"reason": "boom"}},
            {"kind": "node_end", "data": {"status": "failed", "steps": 1}},
            {"kind": "node_start", "data": {"attempt": 2}},
            {"kind": "failure", "data": {"reason": "boom again"}},
            {"kind": "action", "data": {"action": "read", "target": "a.py"}},
            {"kind": "approval_requested", "data": {"rule": "subshell"}},
        ]}
        return rows, events

    def test_the_rebuild_uses_the_log_not_the_report(self):
        rows, events = self._dropped()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            table, changed = rebuild(run_dir, report)
        self.assertIn("| 1 | R2_none | blocked | 0 | 0 | 2 | 1000 | 2 | 0/20 | 0/20 | 红 | 绿 |",
                      table[0])
        self.assertEqual(len(changed), 2, changed)

    def test_a_document_that_matches_the_rebuild_passes(self):
        rows, events = self._dropped()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            table, _ = rebuild(run_dir, report)
            doc = tmp / "doc.md"
            doc.write_text("\n".join(table) + "\n", encoding="utf-8")
            problems = check_published(doc, run_dir, report)
        self.assertEqual(problems, [])

    def test_a_hand_edited_cell_is_caught(self):
        """The point of the check: a number typed in by hand must not survive."""
        rows, events = self._dropped()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, rows, events)
            table, _ = rebuild(run_dir, report)
            doc = tmp / "doc.md"
            doc.write_text("\n".join(table) + "\n", encoding="utf-8")
            # now "fix" the row by hand, back to the machine report's numbers
            doc.write_text(doc.read_text(encoding="utf-8").replace(
                "| 1 | R2_none | blocked | 0 | 0 | 2 | 1000 | 2 |",
                "| 1 | R2_none | blocked | 0 | 0 | 1 | 1000 | 1 |",
            ), encoding="utf-8")
            problems = check_published(doc, run_dir, report)
        self.assertTrue(problems, "a hand-edited cell passed the check")
        self.assertIn("rebuilt", problems[0])

    # -- the behaviour table (the headline effect) -------------------------

    def _behaviour_fixture(self, tmp: Path):
        """8 reps x 3 arms: the control arm looks the price table up `rep` times."""
        rows: list[dict] = []
        events: dict[str, list[dict]] = {}
        for rep in range(1, 9):
            for arm in ("R2_none", "R2_memory", "R2_fact"):
                rows.append({
                    "rep": rep, "arm": arm, "status": "succeeded", "steps_to_pass": 2,
                    "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12,
                    "tokens": 1000, "l1_failures": 1, "guard_catches": 0,
                    "prompts_lesson": "0/12", "prompts_facts": "0/12",
                    "verdict": "绿", "suite": "绿",
                })
                looks = rep if arm == "R2_none" else 0
                events[f"{arm}_{rep}"] = [
                    {"kind": "action", "data": {
                        "action": "read", "target": "shop/helpers.py",
                        "detail": "def pct(value): ...",
                    }}
                    for _ in range(looks)
                ]
        return self._fixture(tmp, rows, events)

    def _behaviour_doc(self, wrong: str = "") -> str:
        lines = ["| rep | 对照臂 查表/第一步 | 教训臂 查表/第一步 | 事实臂 查表/第一步 |",
                 "|---|---|---|---|"]
        for rep in range(1, 9):
            first = str(rep)
            if wrong and int(wrong) == rep:
                first = str(rep + 1)
            lines.append(f"| {rep} | {first} / read shop/helpers.py | 0 / read x | 0 / read x |")
        lines.append("| **合计** | **36** | 0 | 0 |")
        return "\n".join(lines) + "\n"

    def test_the_behaviour_table_matches_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._behaviour_fixture(tmp)
            doc = tmp / "doc.md"
            doc.write_text(self._behaviour_doc(), encoding="utf-8")
            problems = check_behaviour(doc, run_dir)
        self.assertEqual(problems, [])

    def test_a_hand_edited_behaviour_cell_is_caught(self):
        """The headline effect ("half as many lookups") is read back by hand here."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._behaviour_fixture(tmp)
            doc = tmp / "doc.md"
            doc.write_text(self._behaviour_doc(wrong="3"), encoding="utf-8")
            problems = check_behaviour(doc, run_dir)
        self.assertTrue(problems, "a hand-edited lookup count passed the check")
        self.assertIn("rep 3", problems[0])


class FloorLintTest(unittest.TestCase):
    """An effect inside the noise floor must not be published as a win.

    2026-11-11: a run claimed "-45% tokens" and nobody had measured how far
    two arms running **the same prompt** already differ -- the effect was
    inside the noise, and the number had to be withdrawn (fee9).  The gate
    computes the floor; this checks that the **document** uses it the same
    way.
    """

    #: steps / tokens / hidden-lookups per arm per repetition
    #: rep 1 is a *silent* repetition (the lesson arm carried no lesson, so
    #: both arms ran the same prompt) -- that is where the floor comes from.
    VALUES = {
        "R2_none": {"steps": [4, 3], "tokens": [100, 100], "hidden": [5, 5]},
        "R2_memory": {"steps": [6, 5], "tokens": [140, 130], "hidden": [6, 6]},
        "R2_fact": {"steps": [2, 2], "tokens": [70, 70], "hidden": [3, 3]},
    }

    def _fixture(self, tmp: Path):
        rows: list[dict] = []
        events: dict[str, list[dict]] = {}
        for rep in (1, 2):
            for arm, values in self.VALUES.items():
                rows.append({
                    "rep": rep, "arm": arm, "status": "succeeded",
                    "steps_to_pass": values["steps"][rep - 1],
                    "tokens_to_pass": values["tokens"][rep - 1],
                    "total_steps": 12, "model_calls": 12, "tokens": 1000,
                    "l1_failures": 1, "guard_catches": 0,
                    "prompts_lesson": ("5/12" if arm == "R2_memory" and rep == 2 else "0/12"),
                    "prompts_facts": "12/12" if arm == "R2_fact" else "0/12",
                    "verdict": "绿", "suite": "绿",
                })
                events[f"{arm}_{rep}"] = [
                    {"kind": "action", "data": {
                        "action": "read", "target": "shop/helpers.py",
                        "detail": "def pct(value): ...",
                    }}
                    for _ in range(values["hidden"][rep - 1])
                ]
        return self._fixture_rows(tmp, rows, events)

    def _fixture_rows(self, tmp: Path, rows: list[dict], events: dict[str, list[dict]]):
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

    def _doc(self, steps_verdict: str) -> str:
        return (
            "| 同 prompt 的两臂 | 步数 | tokens | 查表 |\n"
            "|---|---|---|---|\n"
            "| 差 | **+50%** | **+40%** | **+20%** |\n"
            "\n"
            "| 口径 | 对照 → 事实臂 | 相对噪声地板 |\n"
            "|---|---|---|\n"
            "| 到验收通过的步数 | 3.5 → 2.0（-43%） | " + steps_verdict + " |\n"
            "| 到验收通过的 tokens | 100 → 70（-30%） | ❌ 落在地板（±40%）内 |\n"
            "| 总 tokens | 1000 → 1000（0%） | ❌ 落在地板内 |\n"
            "| **查档位表次数** | **5 → 3（-40%）** | ✅ **在噪声之上** |\n"
        )

    def test_a_correct_effect_table_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp)
            doc = tmp / "doc.md"
            doc.write_text(self._doc("❌ 落在地板（±50%）内"), encoding="utf-8")
            problems = check_floor(doc, run_dir, report)
        self.assertEqual(problems, [])

    def test_an_effect_inside_the_floor_cannot_be_published_as_a_win(self):
        """This is the fee9 mistake, mechanised."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp)
            doc = tmp / "doc.md"
            doc.write_text(self._doc("✅ **在噪声之上**"), encoding="utf-8")
            problems = check_floor(doc, run_dir, report)
        self.assertTrue(problems, "an effect inside the noise floor passed the check")
        self.assertTrue(any("noise floor" in p for p in problems), problems)

    def test_an_effect_that_is_not_the_recomputed_one_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp)
            doc = tmp / "doc.md"
            doc.write_text(self._doc("❌ 落在地板（±50%）内").replace(
                "3.5 → 2.0（-43%）", "3.5 → 2.0（-99%）"), encoding="utf-8")
            problems = check_floor(doc, run_dir, report)
        self.assertTrue(problems, "a mis-stated effect passed the check")
        self.assertTrue(any("-43%" in p for p in problems), problems)

    def test_a_document_that_does_not_state_the_floor_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp)
            doc = tmp / "doc.md"
            doc.write_text(self._doc("❌ 落在地板（±50%）内").replace("**+20%**", "**+21%**"),
                           encoding="utf-8")
            problems = check_floor(doc, run_dir, report)
        self.assertTrue(problems, "an unstated noise floor passed the check")
        self.assertTrue(any("never states the noise floor" in p for p in problems), problems)

    def test_a_run_without_a_floor_cannot_be_checked_and_says_so(self):
        """No floor is not a floor of zero -- the check must not pass silently."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows: list[dict] = []
            events: dict[str, list[dict]] = {}
            for arm, values in self.VALUES.items():
                rows.append({
                    "rep": 1, "arm": arm, "status": "succeeded",
                    "steps_to_pass": values["steps"][0],
                    "tokens_to_pass": values["tokens"][0],
                    "total_steps": 12, "model_calls": 12, "tokens": 1000,
                    "l1_failures": 1, "guard_catches": 0,
                    # every repetition carries a lesson -> no silent repetition
                    "prompts_lesson": "5/12" if arm == "R2_memory" else "0/12",
                    "prompts_facts": "12/12" if arm == "R2_fact" else "0/12",
                    "verdict": "绿", "suite": "绿",
                })
                events[f"{arm}_1"] = []
            run_dir, report = self._fixture_rows(tmp, rows, events)
            doc = tmp / "doc.md"
            doc.write_text(self._doc("❌ 落在地板（±50%）内"), encoding="utf-8")
            problems = check_floor(doc, run_dir, report)
        self.assertTrue(problems, "a run without a noise floor passed the check")
        self.assertTrue(any("no noise floor" in p for p in problems), problems)

"""The criterion must be held out -- and that has to be checkable.

A measurement device is only worth its numbers if the ground truth cannot be
read by the thing being measured.  On 2026-11-11 that turned out to be false
twice over:

* the criterion lived in a file, and the model read it with a shell command
  (``cd /d ... && type ...``) -- the workspace lock stops the ``read`` tool,
  not the shell;
* the criterion's **failure output** was fed back to the model, and the
  traceback showed the failing source line -- the line that holds the
  expected number.

These tests pin the fixes: the criterion is fed to python on stdin (no file,
no source line in the traceback), its payload never reaches a prompt or a
persisted plan, and a leak is still detectable if it ever happens.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from planloop.decomp import TaskNode
from planloop.heldout import canary_token, find_canary, run_held_out, with_canary
from planloop.membench import FEE_FAMILY, _reset_repo
from planloop.membench_verify import find_contaminated, parse_report
from planloop.spec import Criterion, ProblemSpec
from planloop.verify import Verifier

ANSWERS = ("0.88", "0.75", "198.0")


class HeldOutCriterionTest(unittest.TestCase):
    """The criterion itself: not on disk, not in the output, still decisive."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="planloop-heldout-")
        cls.repo = Path(cls.tmp.name) / "repo"
        cls.repo.mkdir()
        _reset_repo(cls.repo, FEE_FAMILY)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_criterion_never_touches_the_disk(self):
        before = sorted(p.name for p in self.repo.rglob("*"))
        result = run_held_out(FEE_FAMILY.r2_oracle, self.repo, name="r2")
        after = sorted(p.name for p in self.repo.rglob("*"))
        self.assertEqual(before, after, "the criterion left a file behind")
        self.assertFalse(result.ok)

    def test_the_failure_output_does_not_carry_the_answer(self):
        """The feedback the model reads must not contain the expected numbers.

        The assertion messages are numeric-free by contract, but the
        *traceback* used to show the failing source line -- and the failing
        line is the one with the answer in it.  A criterion that runs from a
        file leaks through it; one that runs from stdin cannot.
        """
        for label, source in (("r1", FEE_FAMILY.r1_oracle),
                              ("r2", FEE_FAMILY.r2_oracle),
                              ("final", FEE_FAMILY.final_oracle)):
            result = run_held_out(source, self.repo, name=label)
            text = result.output
            self.assertFalse(result.ok, f"the {label} criterion is green on a broken repo")
            for answer in ANSWERS:
                self.assertNotIn(
                    answer, text,
                    f"the {label} criterion leaked {answer} through its failure output",
                )
            # the diagnosis has to survive: a criterion that says nothing
            # leaves the model flailing
            self.assertIn("msg", source, "the criterion lost its diagnostic message")

    def test_the_criterion_is_still_decisive(self):
        """No source line, no answer -- but it still tells red from green."""
        red = run_held_out(FEE_FAMILY.r2_oracle, self.repo, name="r2")
        self.assertFalse(red.ok)
        self.assertIn("FAILED", red.output)

        # the same criterion against a repository that got it right
        (self.repo / "shop" / "helpers.py").write_text(
            "def pct(value):\n"
            "    if value == 10:\n"
            "        return 0.88\n"
            "    if value == 20:\n"
            "        return 0.75\n"
            "    return 1 - value / 100\n",
            encoding="utf-8",
        )
        (self.repo / "shop" / "pricing.py").write_text(
            "from shop.helpers import pct\n\n\n"
            "def total(prices, discount_pct, member_pct=0):\n"
            "    subtotal = sum(prices) * pct(discount_pct)\n"
            "    return subtotal * pct(member_pct) if member_pct else subtotal\n",
            encoding="utf-8",
        )
        green = run_held_out(FEE_FAMILY.r2_oracle, self.repo, name="r2")
        self.assertTrue(green.ok, f"the criterion rejected a correct implementation:\n{green.output}")
        _reset_repo(self.repo, FEE_FAMILY)

    def test_the_verifier_runs_a_held_out_criterion(self):
        verifier = Verifier(self.repo)
        criterion = Criterion(
            id="A2", kind="held_out", spec="held-out criterion",
            source=FEE_FAMILY.r1_oracle, must="exit=0",
        )
        report = verifier.l1_only([criterion])
        self.assertFalse(report.passed)
        verdict = report.verdicts[0]
        self.assertEqual(verdict.layer, "L1")
        self.assertIn("exit=1", verdict.detail)

    def test_a_held_out_criterion_without_a_payload_fails_loudly(self):
        """A criterion whose payload was lost must not pass by accident."""
        verifier = Verifier(self.repo)
        criterion = Criterion(id="A2", kind="held_out", spec="held-out criterion", must="exit=0")
        report = verifier.l1_only([criterion])
        self.assertFalse(report.passed)
        self.assertEqual(report.verdicts[0].reason, "held_out_without_source")


class HeldOutStaysHiddenTest(unittest.TestCase):
    """The model must not be able to read the criterion, nor be shown it."""

    def test_the_requirement_is_published_but_the_answer_is_not(self):
        """The requirement may be shown; the answer may not.

        The criterion's ``spec`` is a numeric-free description of what it
        checks -- the model is told the requirement (the goal says it too),
        because a failure that does not say what was checked makes the model
        spin in place.  Its ``source`` is the answer key and never appears.

        (Taking the requirement away up front was tried and reverted: the
        model then spins on the file instead of failing and fixing it --
        see docs/bench.md 19.13.)
        """
        node = TaskNode.from_dict({
            "id": "T1",
            "goal": "fix the discount",
            "acceptance": [
                {"id": "A1", "kind": "command", "spec": "pytest -q", "must": "exit=0"},
                {"id": "A2", "kind": "held_out", "spec": "pct() must hold for every tier",
                 "source": FEE_FAMILY.r1_oracle, "must": "exit=0"},
            ],
        })
        text = node.acceptance_text()
        self.assertIn("pytest -q", text)
        self.assertIn("pct() must hold for every tier", text)
        self.assertNotIn("0.88", text)
        self.assertNotIn("assertAlmostEqual", text)

    def test_the_failure_still_says_what_was_checked(self):
        """Hiding the requirement up front must not hide it from the feedback."""
        with tempfile.TemporaryDirectory(prefix="planloop-holdout-fb-") as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _reset_repo(repo, FEE_FAMILY)
            verifier = Verifier(repo)
            criterion = Criterion(
                id="A2", kind="held_out", spec="pct() must hold for every tier",
                source=FEE_FAMILY.r1_oracle, must="exit=0",
            )
            report = verifier.l1_only([criterion])
            self.assertFalse(report.passed)
            self.assertIn("pct() must hold for every tier", report.verdicts[0].detail,
                          "the failure no longer says what was checked")

    def test_the_hints_are_numeric_free(self):
        """A hint that carries a number hands over the answer."""
        import re

        from planloop.membench import CONTRACT_FAMILY

        for family in (CONTRACT_FAMILY, FEE_FAMILY):
            for hint in (family.r1_hint, family.r2_hint, family.final_hint):
                self.assertTrue(hint, f"{family.name}: a criterion has no hint")
                self.assertIsNone(
                    re.search(r"\d+\.\d+", hint),
                    f"{family.name}: the hint leaks a number: {hint}",
                )

    def test_the_payload_is_not_persisted(self):
        """A persisted plan must not carry the answer key."""
        criterion = Criterion(
            id="A2", kind="held_out", spec="held-out criterion",
            source=FEE_FAMILY.r1_oracle, must="exit=0",
        )
        self.assertNotIn("source", criterion.to_dict())
        node = TaskNode.from_dict({
            "id": "T1",
            "goal": "fix the discount",
            "acceptance": [
                {"id": "A2", "kind": "held_out", "spec": "held-out criterion",
                 "source": FEE_FAMILY.r1_oracle, "must": "exit=0"},
            ],
        })
        self.assertNotIn("0.88", json.dumps(node.to_dict(), ensure_ascii=False))
        spec = ProblemSpec.from_dict({
            "goal": "fix the discount",
            "success_criteria": [
                {"id": "A2", "kind": "held_out", "spec": "held-out criterion",
                 "source": FEE_FAMILY.r1_oracle, "must": "exit=0"},
            ],
        })
        self.assertNotIn("0.88", json.dumps(spec.to_dict(), ensure_ascii=False))


class LeakDetectionTest(unittest.TestCase):
    """A leak must be detectable, not just unlikely."""

    def test_the_canary_finds_a_leak_in_an_event_log(self):
        token = canary_token()
        source = with_canary(FEE_FAMILY.r1_oracle, token)
        self.assertIn(token, source)
        with tempfile.TemporaryDirectory(prefix="planloop-canary-") as tmp:
            log = Path(tmp) / "events_R2_none_1.jsonl"
            log.write_text(
                json.dumps({"kind": "action", "data": {"action": "read", "target": "x",
                                                       "detail": f"stdout:\n{source}"}}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(find_canary([log], token), [str(log)])
            clean = Path(tmp) / "events_R2_none_2.jsonl"
            clean.write_text(json.dumps({"kind": "action", "data": {"detail": "nothing"}}) + "\n",
                              encoding="utf-8")
            self.assertEqual(find_canary([clean], token), [])

    def test_a_contaminated_rep_leaves_the_sample(self):
        token = canary_token()
        with tempfile.TemporaryDirectory(prefix="planloop-contam-") as tmp:
            run_dir = Path(tmp)
            (run_dir / "canaries.json").write_text(
                json.dumps({"1": token, "2": "other-token"}), encoding="utf-8"
            )
            log = run_dir / "events_R2_none_1.jsonl"
            log.write_text(
                json.dumps({"kind": "action", "data": {"detail": with_canary("x", token)}}) + "\n",
                encoding="utf-8",
            )
            (run_dir / "events_R2_none_2.jsonl").write_text(
                json.dumps({"kind": "action", "data": {"detail": "clean"}}) + "\n",
                encoding="utf-8",
            )
            report = run_dir / "report.md"
            report.write_text(
                "| rep | 臂 | 状态 | 到验收通过：步数 | 到验收通过：tokens | 总步数 | 模型调用 | 总 tokens | L1 失败 | 护栏拦截 | 带教训 | 带事实 | 外部判定 | 全量测试 |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| 1 | R2_none | succeeded | 2 | 100 | 3 | 3 | 300 | 0 | 0 | 0/3 | 0/3 | 绿 | 绿 |\n"
                "| 2 | R2_none | succeeded | 2 | 100 | 3 | 3 | 300 | 0 | 0 | 0/3 | 0/3 | 绿 | 绿 |\n",
                encoding="utf-8",
            )
            rows = parse_report(report)
            contaminated = find_contaminated(run_dir, rows)
            self.assertEqual(list(contaminated), [1])

    def test_the_canary_scans_the_setup_phase_too(self):
        """The setup phase is where the model is most likely to hunt.

        In the contaminated run the model read the criterion source in
        ``setup_1`` and ``setup_4`` -- and the setup phase's log is called
        ``events_setup_N.jsonl`` while the report calls that arm ``R1_learn``.
        A scan that looks for ``events_R1_learn_N.jsonl`` finds nothing there
        and reports "clean" for the very phase the leak happened in.
        """
        token = canary_token()
        with tempfile.TemporaryDirectory(prefix="planloop-setup-canary-") as tmp:
            run_dir = Path(tmp)
            (run_dir / "canaries.json").write_text(json.dumps({"1": token}), encoding="utf-8")
            (run_dir / "events_setup_1.jsonl").write_text(
                json.dumps({"kind": "action", "data": {"detail": with_canary("x", token)}}) + "\n",
                encoding="utf-8",
            )
            report = run_dir / "report.md"
            report.write_text(
                "| rep | 臂 | 状态 | 到验收通过：步数 | 到验收通过：tokens | 总步数 | 模型调用 | 总 tokens | L1 失败 | 护栏拦截 | 带教训 | 带事实 | 外部判定 | 全量测试 |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| 1 | R1_learn | succeeded | 2 | 100 | 3 | 3 | 300 | 0 | 0 | 0/3 | 0/3 | 绿 | 绿 |\n",
                encoding="utf-8",
            )
            rows = parse_report(report)
            contaminated = find_contaminated(run_dir, rows)
            self.assertEqual(list(contaminated), [1],
                             "the setup phase's log was not scanned for the canary")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

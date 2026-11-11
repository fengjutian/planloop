"""The judge must not be editable by the thing being judged.

Before this, every task was judged by the test suite that lives **inside** the
workspace -- the same files the model is asked to edit. A model that replaced
``tests/test_pricing.py`` with ``assertTrue(True)`` passed the judge without
implementing anything. The held-out oracle lives outside the workspace, where
the tools cannot reach it.
"""

import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from planloop import bench
from planloop.bench import Task, TASKS, fingerprint, run_b2, run_b2c, workspace_snapshot
from planloop.tools import ToolError, Tools


VISIBLE_TEST = (
    "import unittest\nfrom shop.pricing import total\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_total(self):\n"
    "        self.assertEqual(total([1, 2]), 3)\n"
)

WEAKENED_TEST = (
    "import unittest\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_total(self):\n"
    "        self.assertTrue(True)\n"
)

HIDDEN_TEST = (
    "import unittest\nfrom shop.pricing import total\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_total(self):\n"
    "        self.assertEqual(total([1, 2]), 3)\n"
)


def _task() -> Task:
    return Task(
        key="oracle_probe",
        goal="让 shop.pricing.total 返回元素之和",
        initial={
            "shop/__init__.py": "",
            "shop/pricing.py": "def total(items):\n    return 0\n",
            "tests/test_pricing.py": VISIBLE_TEST,
        },
        correct={"shop/pricing.py": "def total(items):\n    return sum(items)\n"},
        wrong={"shop/pricing.py": "def total(items):\n    return 0\n"},
        acceptance=[{"id": "A1", "kind": "command", "spec": "true", "must": "exit=0"}],
        hidden_tests={"test_hidden.py": HIDDEN_TEST},
    )


def _visible_suite_passes(root: Path) -> bool:
    proc = subprocess.run(
        f'"{sys.executable}" -m unittest discover -s tests -q',
        shell=True, cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    return proc.returncode == 0


class HiddenOracleTest(unittest.TestCase):
    def _workspace(self, tmp: str):
        base = Path(tmp)
        root = base / "ws"
        root.mkdir(parents=True, exist_ok=True)
        task = _task()
        task.build(root)
        task.prepare_oracle(base)
        return task, base, root

    def test_weakening_the_visible_test_no_longer_buys_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, base, root = self._workspace(tmp)
            # the model replaces the judge it can see with a trivial test
            (root / "tests" / "test_pricing.py").write_text(WEAKENED_TEST, encoding="utf-8")

            # the hole this closes: the visible suite alone says "pass" ...
            self.assertTrue(_visible_suite_passes(root))
            # ... but the held-out oracle still rejects the unimplemented function
            self.assertFalse(task.passes(root))

    def test_the_correct_implementation_passes_the_held_out_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, base, root = self._workspace(tmp)
            (root / "shop" / "pricing.py").write_text(
                "def total(items):\n    return sum(items)\n", encoding="utf-8"
            )
            self.assertTrue(task.passes(root))

    def test_the_wrong_implementation_fails_the_held_out_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, base, root = self._workspace(tmp)
            self.assertFalse(task.passes(root))

    def test_the_oracle_is_unreachable_from_the_workspace_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, base, root = self._workspace(tmp)
            oracle_file = base / "oracle" / "test_hidden.py"
            self.assertTrue(oracle_file.exists())
            self.assertNotIn(str(oracle_file), str(root))

            tools = Tools(root)
            with self.assertRaises(ToolError):
                tools.read(str(oracle_file))
            with self.assertRaises(ToolError):
                tools.write(str(oracle_file), "# rewritten\n")

    def test_a_task_without_hidden_tests_keeps_its_own_suite_as_the_judge(self):
        task = _task()
        task.hidden_tests = None
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "ws"
            root.mkdir(parents=True, exist_ok=True)
            task.build(root)
            task.prepare_oracle(base)
            self.assertEqual(task.judge, task.oracle)
            (root / "shop" / "pricing.py").write_text(
                "def total(items):\n    return sum(items)\n", encoding="utf-8"
            )
            self.assertTrue(task.passes(root))


class HiddenOracleTaskFamilyTest(unittest.TestCase):
    """Every task with a held-out oracle must have a valid oracle.

    A task whose ``correct`` branch fails, or whose ``wrong`` branch passes,
    measures nothing -- the harness reports a number that means the opposite
    of what it looks like.
    """

    def test_every_hidden_oracle_task_has_a_sane_oracle(self):
        checked = 0
        for task in TASKS:
            if not task.hidden_tests:
                continue
            checked += 1
            for branch, expected in (("correct", True), ("wrong", False)):
                with tempfile.TemporaryDirectory() as tmp:
                    base = Path(tmp)
                    root = base / "ws"
                    root.mkdir(parents=True, exist_ok=True)
                    task.build(root)
                    task.prepare_oracle(base)
                    for path, content in getattr(task, branch).items():
                        target = root / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding="utf-8")
                    self.assertEqual(
                        task.passes(root), expected,
                        f"{task.key}: {branch} branch should {'pass' if expected else 'fail'}",
                    )

    def test_hidden_oracle_is_the_node_acceptance_not_only_end_to_end(self):
        """The retry only learns anything if the node's own check is the oracle."""
        for task in TASKS:
            if not task.hidden_tests:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                task.prepare_oracle(base)
                spec = task.spec()
                node = spec.plan_template["nodes"][0]
                node_spec = node["acceptance"][0]
                node_spec = node_spec.spec if hasattr(node_spec, "spec") else node_spec["spec"]
                crit = spec.success_criteria[0]
                crit_spec = crit.spec if hasattr(crit, "spec") else crit["spec"]
                self.assertEqual(node_spec, task.judge)
                self.assertEqual(crit_spec, task.judge)

    def test_hidden_oracle_tasks_show_the_judge_to_both_configurations(self):
        for task in TASKS:
            if not task.hidden_tests:
                continue
            self.assertTrue(task.judge_visible, f"{task.key}: B0 must know how it is judged")


class QuietJudgeTest(unittest.TestCase):
    """B2D's judge must fail *without* explaining why.

    If it leaked the test output, B2D would be B2 again and the ablation
    would measure nothing.
    """

    def _task(self) -> Task:
        return [t for t in TASKS if t.key == "divide_zero_hidden"][0]

    def _run_quiet(self, task: Task, *, correct: bool) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "ws"
            root.mkdir(parents=True, exist_ok=True)
            task.build(root)
            task.prepare_oracle(base)
            branch = task.correct if correct else task.wrong
            for path, content in branch.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            proc = subprocess.run(
                task.quiet_judge, shell=True, cwd=str(root), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=120,
            )
            return proc.returncode, proc.stdout + proc.stderr

    def test_quiet_judge_fails_on_a_wrong_implementation(self):
        code, out = self._run_quiet(self._task(), correct=False)
        self.assertNotEqual(code, 0)

    def test_quiet_judge_passes_on_the_correct_implementation(self):
        code, out = self._run_quiet(self._task(), correct=True)
        self.assertEqual(code, 0)

    def test_quiet_judge_leaks_no_diagnostic(self):
        _, out = self._run_quiet(self._task(), correct=False)
        self.assertNotIn("AssertionError", out)
        self.assertNotIn("ValueError", out)
        self.assertNotIn("test_divide_checked", out)
        self.assertNotIn("divide_zero_hidden", out)
        self.assertIn("ACCEPTANCE FAILED", out)


class AblationTest(unittest.TestCase):
    """B2C must be the same executor with the *feedback* switched off.

    If B2C ever retried, the ablation would no longer isolate the failure
    output, and the "verification" conclusion would be untested.
    """

    def _task(self) -> Task:
        return [t for t in TASKS if t.key == "divide_zero_hidden"][0]

    def test_b2c_never_receives_the_correction(self):
        """No correction text -> the scripted model is never re-rolled.

        The scripted model re-rolls whenever the prompt carries the
        supervisor's correction, so the reset count is a direct probe of
        "did the model ever get told that it failed".
        """
        task = self._task()
        for p in (0.0, 1.0):
            outcome = run_b2c(task, p_correct=p, rng=random.Random(0))
            self.assertEqual(outcome.attempts, 1, f"p_correct={p} must not be re-rolled")

    def test_b2c_is_still_judged_by_the_held_out_oracle(self):
        task = self._task()
        good = run_b2c(task, p_correct=1.0, rng=random.Random(0))
        bad = run_b2c(task, p_correct=0.0, rng=random.Random(0))
        self.assertTrue(good.success)
        self.assertFalse(bad.success)

    def test_b2_does_receive_the_correction(self):
        """The contrast that makes the ablation meaningful."""
        task = self._task()
        outcome = run_b2(task, p_correct=0.0, rng=random.Random(0))
        self.assertGreater(outcome.attempts, 1, "B2 must tell the model it failed")
        self.assertFalse(outcome.success)


class FeedbackChannelTest(unittest.TestCase):
    """What the model is *told* when the acceptance fails.

    This is the difference the four-arm ablation rests on:

    * B2C -- the acceptance never fails, so the model is never corrected;
    * B2D -- the acceptance fails and the model is corrected, but the failure
      carries no diagnostic;
    * B2  -- the acceptance fails and the failure output (the real traceback)
      is handed back to the model.

    If any of those stopped being true, the arms would no longer isolate
    "the model is told what went wrong" -- the ablation would be measuring
    something else and the published numbers would mean nothing.
    """

    class _Spy:
        """Records every prompt the executor sends to the model."""

        def __init__(self, inner):
            self.inner = inner
            self.name = f"spy({inner.name})"
            self.prompts: list[str] = []

        def complete(self, prompt: str, *, max_tokens: int = 900):
            self.prompts.append(prompt)
            return self.inner.complete(prompt, max_tokens=max_tokens)

        def __getattr__(self, item):
            return getattr(self.inner, item)

    def _task(self) -> Task:
        return [t for t in TASKS if t.key == "divide_zero_hidden"][0]

    def _prompts(self, config: str) -> list[str]:
        spies: list[FeedbackChannelTest._Spy] = []

        def factory(task, rng=None):
            inner = bench.AttemptModel(task, p_correct=0.0, rng=rng or random.Random(0))
            spy = FeedbackChannelTest._Spy(inner)
            spies.append(spy)
            return spy

        runner = {"B2": bench.run_b2, "B2C": bench.run_b2c, "B2D": bench.run_b2d}[config]
        previous = bench.MODEL_FACTORY
        bench.MODEL_FACTORY = factory
        try:
            runner(self._task(), p_correct=0.0, rng=random.Random(0))
        finally:
            bench.MODEL_FACTORY = previous
        return spies[0].prompts

    def test_b2_hands_the_real_traceback_back_to_the_model(self):
        joined = "\n".join(self._prompts("B2"))
        self.assertIn("ZeroDivisionError", joined)
        self.assertIn("FAILED (", joined)

    def test_b2d_tells_the_model_it_failed_but_not_why(self):
        joined = "\n".join(self._prompts("B2D"))
        self.assertIn("ACCEPTANCE FAILED", joined)
        self.assertNotIn("ZeroDivisionError", joined)
        self.assertNotIn("FAILED (", joined)

    def test_b2d_is_still_corrected_and_b2c_is_not(self):
        """The retry happens in B2D; without it B2D would be measuring B2C."""
        b2d = "\n".join(self._prompts("B2D"))
        b2c = "\n".join(self._prompts("B2C"))
        self.assertIn("Correction from the supervisor", b2d)
        self.assertNotIn("Correction from the supervisor", b2c)


class BlockedRunTest(unittest.TestCase):
    """A run that was interrupted must not look like a run that failed.

    The bench used to floor the executor-call counter at 1, which made
    "the model tripped a policy rule on its first command" indistinguishable
    from "the node ran once and the acceptance failed".  The second one is a
    measurement; the first one never measured the acceptance at all -- and
    counting it as a failure biases every arm that gets interrupted.
    """

    class _RogueModel:
        """Always tries to run a command the policy denies."""

        name = "rogue"

        def __init__(self):
            self.calls = 0

        def complete(self, prompt: str, *, max_tokens: int = 900):
            from planloop.model import Completion

            self.calls += 1
            return Completion(
                text='{"action": "run", "cmd": "rm -rf /"}',
                tokens_in=10,
                tokens_out=10,
            )

    def _task(self) -> Task:
        return Task(
            key="denied_probe",
            goal="做一个无害的事",
            initial={"a.py": "x = 1\n"},
            correct={"a.py": "x = 2\n"},
            wrong={"a.py": "x = 1\n"},
            acceptance=[{"id": "A1", "kind": "command", "spec": "true", "must": "exit=0"}],
            hidden_tests={"test_hidden.py": HIDDEN_TEST},
        )

    def test_an_interrupted_run_reports_zero_executor_calls_and_a_reason(self):
        task = self._task()
        previous = bench.MODEL_FACTORY
        bench.MODEL_FACTORY = lambda _task, rng=None: BlockedRunTest._RogueModel()
        try:
            outcome = run_b2(task, p_correct=0.0, rng=random.Random(0))
        finally:
            bench.MODEL_FACTORY = previous

        self.assertEqual(outcome.attempts, 0, "an interrupted run completed no executor run")
        self.assertEqual(outcome.status, "blocked")
        self.assertTrue(outcome.blocked, "the reason for the interruption must be recorded")
        self.assertNotIn("persisted over", outcome.blocked)
        self.assertFalse(outcome.success)

    def test_a_normal_failure_is_distinguishable_from_an_interrupted_run(self):
        """Both end in `blocked` -- the executor-call count is what separates them.

        The scheduler reports an exhausted node as "blocked" too, so the
        status alone cannot tell "never ran" from "ran and kept failing".
        """
        outcome = run_b2(self._task(), p_correct=0.0, rng=random.Random(0))
        self.assertGreaterEqual(outcome.attempts, 1)
        self.assertIn("persisted over", outcome.blocked)


class WorkspaceSnapshotTest(unittest.TestCase):
    """B1 must not drop the edits a real model made outside the reference set."""

    def test_snapshot_keeps_files_the_reference_solution_does_not_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shop").mkdir()
            (root / "shop" / "pricing.py").write_text("x = 1\n", encoding="utf-8")
            (root / "notes.md").write_text("scratch\n", encoding="utf-8")
            snapshot = workspace_snapshot(root)
            self.assertIn("notes.md", snapshot)
            self.assertIn("shop/pricing.py", snapshot)

    def test_snapshot_ignores_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            snapshot = workspace_snapshot(root)
            self.assertEqual(list(snapshot), ["a.py"])

    def test_fingerprint_is_order_independent(self):
        one = fingerprint({"b.py": "2", "a.py": "1"})
        two = fingerprint({"a.py": "1", "b.py": "2"})
        self.assertEqual(one, two)


if __name__ == "__main__":
    unittest.main()

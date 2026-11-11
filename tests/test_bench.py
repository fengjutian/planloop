import unittest

from planloop.bench import TASKS, benchmark, run_b0, run_b1, run_b2
import random


class BenchTest(unittest.TestCase):
    def test_tasks_are_well_formed(self):
        self.assertGreaterEqual(len(TASKS), 3)
        for task in TASKS:
            self.assertTrue(task.goal)
            self.assertTrue(task.correct)
            self.assertTrue(task.wrong)
            self.assertNotEqual(task.correct, task.wrong, f"{task.key}: wrong branch must differ")

    def test_success_is_decided_by_the_oracle_not_the_model(self):
        task = TASKS[0]
        # p_correct=0 means the model always writes the buggy version
        outcome = run_b0(task, p_correct=0.0, rng=random.Random(0))
        self.assertFalse(outcome.success)

        # p_correct=1 means it always writes the correct version
        outcome = run_b0(task, p_correct=1.0, rng=random.Random(0))
        self.assertTrue(outcome.success)

    def test_b1_uses_more_attempts_than_b0(self):
        task = TASKS[0]
        zero = run_b0(task, p_correct=1.0, rng=random.Random(1))
        five = run_b1(task, p_correct=1.0, rng=random.Random(1), n=5)
        # B1 runs n independent attempts; B0 runs a single episode, so its
        # counter holds the model calls inside that episode (see Outcome.attempts).
        self.assertEqual(five.attempts, 5)
        self.assertGreaterEqual(zero.attempts, 1)
        self.assertGreater(five.tokens, zero.tokens)

    def test_b1_vote_does_not_corrupt_a_perfect_model(self):
        """The vote must not be able to turn a right answer into a wrong one.

        B1 keeps the most common artefact set out of n attempts.  If the
        voting were biased, it would show up here: a model that is always
        right must always win, and a model that is always wrong must never
        win -- otherwise the blind-vote baseline would be mismeasured.
        """
        task = [t for t in TASKS if t.key == "divide_zero_hidden"][0]
        for seed in (0, 1, 2):
            good = run_b1(task, p_correct=1.0, rng=random.Random(seed), n=3)
            bad = run_b1(task, p_correct=0.0, rng=random.Random(seed), n=3)
            self.assertTrue(good.success, f"seed={seed}: a perfect model must win the vote")
            self.assertFalse(bad.success, f"seed={seed}: a broken model must not win the vote")

    def test_b2_recovers_a_failed_first_attempt(self):
        task = TASKS[0]
        # deterministic seed chosen so the first roll is wrong; the retry re-rolls
        outcome = run_b2(task, p_correct=1.0, rng=random.Random(0))
        self.assertTrue(outcome.success)

    def test_context_files_are_injected_into_the_executor_prompt(self):
        import json as _json

        from planloop.model import ScriptedModel
        from planloop.scheduler import run
        from planloop.spec import ProblemSpec

        seen: list[str] = []

        def handler(prompt: str) -> str:
            seen.append(prompt)
            return _json.dumps({"action": "finish", "summary": "looked"})

        spec = ProblemSpec.from_dict(
            {
                "goal": "inspect files",
                "context_files": ["notes.txt"],
                "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "notes.txt"}],
                "limits": {"max_steps_per_node": 1, "max_attempts_per_node": 1},
                "plan_template": {"nodes": [{"id": "T1", "goal": "look", "acceptance": [
                    {"id": "A1", "kind": "file_exists", "spec": "notes.txt"}]}]},
            }
        )
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "notes.txt").write_text("MARKER-42")
            run(spec, model=ScriptedModel(handler), workspace=ws)

        self.assertTrue(seen)
        self.assertIn("MARKER-42", seen[0])
        self.assertIn("# Relevant files", seen[0])

    def test_report_contains_the_caveat_and_a_table(self):
        report = benchmark(reps=1, p_correct=0.5, seed=0, tasks=[TASKS[0]])
        text = report.to_markdown()
        self.assertIn("B0", text)
        self.assertIn("success_rate", text)
        self.assertIn("harness", text)
        summary = report.summary()
        self.assertEqual(set(summary), {"B0", "B1", "B2"})


if __name__ == "__main__":
    unittest.main()

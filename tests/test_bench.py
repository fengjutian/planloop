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
        self.assertEqual(zero.attempts, 1)
        self.assertEqual(five.attempts, 5)
        self.assertGreater(five.tokens, zero.tokens)

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

import tempfile
import unittest
from pathlib import Path

from planloop.blackboard import Blackboard
from planloop.compress import Compressor, extractive
from planloop.memdb import MemorySystem
from planloop.model import Completion, ScriptedModel, estimate_tokens
from planloop.persist import runs_root
from planloop.scheduler import run
from planloop.spec import ProblemSpec

LONG_TAIL = "\n".join(f"note {i}: this line is filler and should be dropped" for i in range(60))
SIGNAL_TEXT = (
    "Preparing workspace...\n"
    "Running the suite\n"
    "Traceback (most recent call last):\n"
    '  File "shop/pricing.py", line 42, in total\n'
    "ModuleNotFoundError: No module named 'shop.helpers'\n"
    + LONG_TAIL
)


class ExtractiveTest(unittest.TestCase):
    def test_keeps_error_and_paths_under_budget(self):
        out = extractive(SIGNAL_TEXT, 260)
        self.assertLessEqual(len(out), 260)
        self.assertIn("ModuleNotFoundError", out)
        self.assertIn("shop/pricing.py", out)
        self.assertIn("elided", out)

    def test_short_text_is_untouched(self):
        self.assertEqual(extractive("short", 100), "short")

    def test_budget_of_zero_is_empty(self):
        self.assertEqual(extractive(SIGNAL_TEXT, 0)[:0], "")


class CompressorTest(unittest.TestCase):
    def test_falls_back_without_a_model(self):
        comp = Compressor(None)
        out = comp.compress(SIGNAL_TEXT, 300)
        self.assertLessEqual(len(out), 300)
        self.assertIn("ModuleNotFoundError", out)
        self.assertEqual(comp.stats.calls, 0)
        self.assertEqual(comp.stats.fallbacks, 1)

    def test_uses_the_model_when_available(self):
        model = ScriptedModel(lambda p: "compressed: ModuleNotFoundError in shop/pricing.py")
        comp = Compressor(model)
        out = comp.compress(SIGNAL_TEXT, 600)
        self.assertTrue(out.startswith("compressed:"))
        self.assertEqual(comp.stats.calls, 1)

    def test_model_failure_falls_back(self):
        def boom(prompt: str) -> str:
            raise RuntimeError("model down")

        comp = Compressor(ScriptedModel(boom))
        out = comp.compress(SIGNAL_TEXT, 300)
        self.assertIn("ModuleNotFoundError", out)
        self.assertEqual(comp.stats.fallbacks, 1)

    def test_stats_report_ratio(self):
        comp = Compressor(None)
        comp.compress(SIGNAL_TEXT, 300)
        stats = comp.stats.to_dict()
        self.assertLess(stats["ratio"], 0.5)


class ContextBudgetTest(unittest.TestCase):
    def test_context_stays_within_budget_with_compressor(self):
        bb = Blackboard()
        bb.record_artifact("T1", SIGNAL_TEXT * 3)
        bb.failures["T2"] = [SIGNAL_TEXT] * 3
        bb.observations["T2"] = [SIGNAL_TEXT] * 2
        text = bb.context_for(
            node_id="T2",
            goal="fix the import",
            node_goal="make the tests pass",
            deps=["T1"],
            acceptance="- (L1/command) pytest",
            max_chars=900,
            compressor=Compressor(None),
        )
        self.assertLessEqual(len(text), 900)
        self.assertIn("Current node T2", text)

    def test_context_without_compressor_is_still_bounded(self):
        bb = Blackboard()
        bb.record_artifact("T1", "x" * 5000)
        text = bb.context_for(node_id="T2", goal="g", node_goal="n", deps=["T1"], max_chars=500)
        self.assertLessEqual(len(text), 500)


class FactHarvestTest(unittest.TestCase):
    """A finished node should leave verifiable facts behind, without being asked."""

    def spec(self) -> ProblemSpec:
        return ProblemSpec.from_dict(
            {
                "goal": "run the tests and record the command",
                "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "done.txt"}],
                "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 6},
                "plan_template": {
                    "nodes": [
                        {"id": "T1", "goal": "run the suite", "acceptance": [
                            {"id": "A1", "kind": "file_exists", "spec": "done.txt"}]}
                    ]
                },
            }
        )

    def test_test_command_is_harvested_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            mem = MemorySystem(Path(tmp))
            steps = [
                '{"action": "run", "cmd": "python -m unittest --help"}',
                '{"action": "write", "path": "done.txt", "content": "ok"}',
                '{"action": "finish", "summary": "ran the suite"}',
            ]
            calls = {"n": 0}

            def handler(prompt: str) -> str:
                idx = min(calls["n"], len(steps) - 1)
                calls["n"] += 1
                return steps[idx]

            result = run(self.spec(), model=ScriptedModel(handler), workspace=ws, memory_system=mem)
            mem.close()
            self.assertEqual(result.status, "succeeded")

            from planloop.memdb import MemorySystem as Again

            reopened = Again(Path(tmp))
            try:
                row = reopened.private.get("fact", "test_command")
                self.assertIsNotNone(row, "the test command should have been recorded")
                self.assertIn("unittest", row["value"])
                evidence = row["evidence"]
                self.assertIn("command", evidence)
            finally:
                reopened.close()

    def test_no_facts_when_the_command_does_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            mem = MemorySystem(Path(tmp))
            steps = [
                '{"action": "run", "cmd": "python -m unittest definitely-not-a-real-flag"}',
                '{"action": "write", "path": "done.txt", "content": "ok"}',
                '{"action": "finish", "summary": "done"}',
            ]
            calls = {"n": 0}

            def handler(prompt: str) -> str:
                idx = min(calls["n"], len(steps) - 1)
                calls["n"] += 1
                return steps[idx]

            run(self.spec(), model=ScriptedModel(handler), workspace=ws, memory_system=mem)
            self.assertIsNone(mem.private.get("fact", "test_command"))
            mem.close()


if __name__ == "__main__":
    unittest.main()

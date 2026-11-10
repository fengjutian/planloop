import json
import tempfile
import unittest
from pathlib import Path

from planloop.model import ScriptedModel
from planloop.routing import ModelRouter, QuestionKind
from planloop.scheduler import run
from planloop.spec import ProblemSpec


def fixed(name: str, reply: str) -> ScriptedModel:
    return ScriptedModel(lambda prompt: reply, name=name)


def writes(path: str) -> str:
    return json.dumps({"action": "write", "path": path, "content": "ok"})


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.router = ModelRouter(
            {
                "cheap": fixed("cheap", "{}"),
                "standard": fixed("standard", "{}"),
                "strong": fixed("strong", "{}"),
                "verify": fixed("verify", "{}"),
            }
        )

    def tier(self, prompt: str, **kw) -> str:
        return self.router.decide(prompt, **kw).tier

    def test_roles(self):
        self.assertEqual(self.tier("task=plan\nmake a plan"), "strong")
        self.assertEqual(self.tier("task=judge\nis it good"), "verify")
        self.assertEqual(self.tier("task=lesson\nextract"), "cheap")

    def test_trivial_node_goes_cheap(self):
        self.assertEqual(self.tier("task=act\nx", est_cost=0.2, risk="low"), "cheap")

    def test_risky_node_does_not_go_cheap(self):
        self.assertEqual(self.tier("task=act\nx", est_cost=0.2, risk="high"), "standard")
        self.assertEqual(self.tier("task=act\nx", est_cost=0.8, risk="low"), "standard")

    def test_second_attempt_escalates(self):
        self.assertEqual(self.tier("task=act\nx", attempt=1, est_cost=0.1), "strong")

    def test_memory_hit_skips_the_cheap_tier(self):
        decision = self.router.decide("task=act\nx", est_cost=0.1, lesson_hit=True)
        self.assertEqual(decision.tier, "standard")
        self.assertEqual(decision.reason, "memory_lesson")

    def test_missing_tier_falls_back(self):
        router = ModelRouter({"standard": fixed("s", "{}")})
        self.assertEqual(router.decide("task=plan\nx").tier, "standard")

    def test_reason_is_machine_readable(self):
        decision = self.router.decide("task=act\nx", attempt=2, est_cost=0.1)
        self.assertEqual(decision.kind, QuestionKind.IMPLEMENT)
        self.assertEqual(decision.reason, "escalate_after_failure")


class UsageTest(unittest.TestCase):
    def test_usage_and_weighted_cost(self):
        router = ModelRouter({"cheap": fixed("cheap", "a" * 100), "strong": fixed("strong", "b" * 100)})
        router.complete("task=lesson\nx")
        router.complete("task=act\nx", attempt=1, est_cost=0.1)
        usage = router.usage()
        self.assertEqual(usage["cheap"]["calls"], 1)
        self.assertEqual(usage["strong"]["calls"], 1)
        # strong is weighted 3x, so it must dominate the cost even at equal tokens
        cheap_tokens = usage["cheap"]["tokens"]
        strong_tokens = usage["strong"]["tokens"]
        expected = cheap_tokens * 0.2 + strong_tokens * 3.0
        self.assertAlmostEqual(router.cost_units(), expected, places=3)
        self.assertGreater(router.cost_units(), cheap_tokens * 0.2 + strong_tokens * 0.2)

    def test_verification_model_is_none_when_not_independent(self):
        shared = fixed("shared", "{}")
        router = ModelRouter({"standard": shared, "verify": shared})
        self.assertIsNone(router.verification_model())

    def test_verification_model_returned_when_distinct(self):
        router = ModelRouter({"standard": fixed("s", "{}"), "verify": fixed("v", "{}")})
        self.assertIsNotNone(router.verification_model())


class RoutingIntegrationTest(unittest.TestCase):
    """Cheap handles the easy node; the hard one escalates after failing."""

    def spec(self) -> ProblemSpec:
        return ProblemSpec.from_dict(
            {
                "goal": "two nodes of different difficulty",
                "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "b.txt"}],
                "limits": {"max_attempts_per_node": 2, "max_steps_per_node": 2},
                "plan_template": {
                    "nodes": [
                        {"id": "T1", "goal": "easy", "est_cost": 0.2, "acceptance": [
                            {"id": "A1", "kind": "file_exists", "spec": "a.txt"}]},
                        {"id": "T2", "goal": "hard", "deps": ["T1"], "est_cost": 0.6, "acceptance": [
                            {"id": "A2", "kind": "file_exists", "spec": "b.txt"}]},
                    ]
                },
            }
        )

    def test_escalation_recovers_the_hard_node(self):
        def handler(prompt: str) -> str:
            return json.dumps({"action": "finish", "summary": "did nothing"})

        weak = ScriptedModel(handler, name="weak")
        strong = ScriptedModel(
            lambda prompt: writes("a.txt") if "# Current node T1" in prompt else writes("b.txt"),
            name="strong",
        )
        router = ModelRouter({"cheap": strong, "standard": weak, "strong": strong})

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            result = run(self.spec(), model=strong, workspace=ws, router=router)

        self.assertEqual(result.status, "succeeded")
        usage = result.routing["usage"]
        self.assertGreaterEqual(usage["cheap"]["calls"], 1, "the trivial node should use the cheap tier")
        self.assertGreaterEqual(usage["standard"]["calls"], 1, "the hard node starts at standard")
        self.assertGreaterEqual(usage["strong"]["calls"], 1, "and escalates after failing")
        self.assertGreater(result.routing["cost_units"], 0)


if __name__ == "__main__":
    unittest.main()

import unittest

from planloop.budget import BudgetExceeded, Ledger
from planloop.spec import Budget, Criterion, Limits, ProblemSpec, SpecError


class SpecIntakeTest(unittest.TestCase):
    def test_criteria_are_required(self):
        with self.assertRaises(SpecError) as ctx:
            ProblemSpec.from_dict({"goal": "do something"})
        self.assertIn("success_criteria", ctx.exception.missing)

    def test_unknown_criterion_kind_is_rejected(self):
        with self.assertRaises(SpecError):
            ProblemSpec.from_dict(
                {"goal": "g", "success_criteria": [{"id": "S1", "kind": "vibes", "spec": "x"}]}
            )

    def test_blocking_unknowns_gate_planning(self):
        spec = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "true"}],
                "unknowns": [{"id": "U1", "question": "which env?", "blocking": True}],
            }
        )
        with self.assertRaises(SpecError):
            spec.require_ready()

    def test_non_blocking_unknowns_do_not_gate(self):
        spec = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "true"}],
                "unknowns": [{"id": "U1", "question": "nice to know", "blocking": False}],
            }
        )
        spec.require_ready()

    def test_layers(self):
        c = Criterion(id="a", kind="command", spec="x")
        r = Criterion(id="b", kind="rubric", spec="x")
        h = Criterion(id="c", kind="human", spec="x")
        self.assertEqual([c.layer, r.layer, h.layer], ["L1", "L2", "L3"])

    def test_json_has_keys_normalises_must(self):
        c = Criterion.from_dict({"id": "j", "kind": "json_has_keys", "spec": "a.json", "must": ["a", "b"]})
        self.assertEqual(c.must, "a,b")

    def test_limits_roundtrip(self):
        limits = Limits.from_dict({"max_depth": 2, "allocations": {"plan": 0.2, "execute": 0.8}})
        self.assertEqual(limits.max_depth, 2)
        self.assertEqual(dict(limits.allocations)["execute"], 0.8)


class BudgetTest(unittest.TestCase):
    def test_hard_token_cap(self):
        ledger = Ledger(Budget(tokens=100, tool_calls=10), {"execute": 1.0})
        with self.assertRaises(BudgetExceeded):
            ledger.charge("execute", tokens=101)

    def test_phase_borrowing_is_recorded_not_fatal(self):
        ledger = Ledger(Budget(tokens=100, tool_calls=10), {"execute": 0.5})
        ledger.charge("execute", tokens=80)
        self.assertGreater(ledger.snapshot()["borrowed_tokens"], 0)
        self.assertFalse(ledger.exhausted())

    def test_tool_call_cap(self):
        ledger = Ledger(Budget(tokens=1000, tool_calls=2), {"execute": 1.0})
        ledger.charge("execute", tool_calls=2)
        with self.assertRaises(BudgetExceeded):
            ledger.charge("execute", tool_calls=1)


if __name__ == "__main__":
    unittest.main()

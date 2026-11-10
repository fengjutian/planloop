import json
import tempfile
import unittest
from pathlib import Path

from planloop.compile import compile_plan
from planloop.decomp import Plan, PlanError, TaskNode
from planloop.memory import MemoryContext, MemoryStore, Trigger, Lesson
from planloop.model import Completion, ScriptedModel
from planloop.scheduler import run
from planloop.spec import Criterion, ProblemSpec
from planloop.verify import Verifier


def node(nid: str, deps=(), acceptance=None) -> TaskNode:
    return TaskNode(
        id=nid,
        goal=f"do {nid}",
        deps=list(deps),
        acceptance=acceptance
        or [Criterion(id=f"A{nid}", kind="command", spec="true", must="exit=0")],
        est_cost=0.2,
    )


class CompileTest(unittest.TestCase):
    def spec(self, **kw) -> ProblemSpec:
        base = {
            "goal": "g",
            "success_criteria": [{"id": "S1", "kind": "command", "spec": "true"}],
        }
        base.update(kw)
        return ProblemSpec.from_dict(base)

    def test_ok_plan(self):
        plan = Plan(nodes={"T1": node("T1")}, goal="g")
        report = compile_plan(self.spec(), plan)
        self.assertTrue(report.ok, report.render())

    def test_cycle_detected(self):
        plan = Plan(nodes={"T1": node("T1", ["T2"]), "T2": node("T2", ["T1"])}, goal="g")
        report = compile_plan(self.spec(), plan)
        self.assertIn("E_CYCLE", [i.code for i in report.issues])

    def test_depth_violation(self):
        plan = Plan(
            nodes={f"T{i}": node(f"T{i}", [f"T{i-1}"] if i else []) for i in range(6)},
            goal="g",
        )
        report = compile_plan(self.spec(), plan)
        self.assertIn("E_DEPTH", [i.code for i in report.issues])

    def test_missing_acceptance_is_an_error(self):
        bad = TaskNode(id="T1", goal="g", acceptance=[])
        report = compile_plan(self.spec(), Plan(nodes={"T1": bad}, goal="g"))
        self.assertIn("E_NO_ACCEPTANCE", [i.code for i in report.issues])

    def test_irreversible_needs_human_gate(self):
        bad = TaskNode(id="T1", goal="g", reversible=False,
                       acceptance=[Criterion(id="A", kind="command", spec="true")])
        report = compile_plan(self.spec(), Plan(nodes={"T1": bad}, goal="g"))
        self.assertIn("E_NO_GATE", [i.code for i in report.issues])

    def test_memory_guard_is_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.jsonl")
            store.add(
                Lesson(
                    id="e1",
                    signature="s",
                    summary="broke the suite before",
                    trigger=Trigger(files=["shop/*.py"]),
                    guard={"kind": "command", "spec": "python -V", "must": "exit=0"},
                    strength="guard",
                    trust=0.7,
                )
            )
            plan = Plan(nodes={"T1": node("T1")}, goal="g")
            report = compile_plan(self.spec(), plan, memory=store, workspace_files=["shop/pricing.py"])
            self.assertTrue(report.ok)
            ids = [c.id for c in plan.nodes["T1"].acceptance]
            self.assertIn("M_e1", ids)
            self.assertEqual(report.injections[0]["lesson"], "e1")


class VerifierTest(unittest.TestCase):
    def test_command_criterion(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = Verifier(tmp)
            ok = Criterion(id="c1", kind="command", spec='python -c "print(1)"', must="exit=0")
            bad = Criterion(id="c2", kind="command", spec='python -c "raise SystemExit(3)"')
            self.assertTrue(v.check([ok]).passed)
            self.assertFalse(v.check([bad]).passed)

    def test_file_and_json_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "out.json").write_text('{"a": 1}', encoding="utf-8")
            v = Verifier(tmp)
            report = v.check(
                [
                    Criterion(id="f", kind="file_exists", spec="out.json"),
                    Criterion(id="j", kind="json_has_keys", spec="out.json", must="a"),
                    Criterion(id="j2", kind="json_has_keys", spec="out.json", must="b"),
                ]
            )
            self.assertEqual([x.passed for x in report.verdicts], [True, True, False])

    def test_judge_must_quote_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            no_quote = ScriptedModel(lambda p: '{"pass": true, "quote": "", "reason": "looks fine"}')
            v = Verifier(tmp, judge=no_quote)
            crit = Criterion(id="r", kind="rubric", spec="clear writing")
            report = v.check([crit], evidence="some artifact text")
            self.assertFalse(report.passed)
            self.assertEqual(report.verdicts[0].reason, "judge_no_quote")

    def test_judge_quote_must_exist_in_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            invented = ScriptedModel(lambda p: '{"pass": true, "quote": "not in the artifact"}')
            v = Verifier(tmp, judge=invented)
            crit = Criterion(id="r", kind="rubric", spec="clear writing")
            report = v.check([crit], evidence="some artifact text")
            self.assertFalse(report.passed)
            self.assertEqual(report.verdicts[0].reason, "judge_quote_not_found")

    def test_executor_cannot_be_the_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ScriptedModel(lambda p: "{}")
            v = Verifier(tmp, judge=model)
            with self.assertRaises(ValueError):
                v.assert_independent(model)

    def test_human_gate_denies_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = Verifier(tmp)
            crit = Criterion(id="h", kind="human", spec="publish to prod")
            self.assertFalse(v.check([crit]).passed)


class SchedulerTest(unittest.TestCase):
    def test_missing_criteria_is_caught_at_intake(self):
        from planloop.spec import SpecError

        with self.assertRaises(SpecError):
            ProblemSpec.from_dict({"goal": "g"})

    def test_blocking_unknown_stops_before_planning(self):
        spec = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "true"}],
                "unknowns": [{"id": "U1", "question": "which env?", "blocking": True}],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run(spec, model=ScriptedModel(lambda p: "{}"), workspace=tmp)
        self.assertEqual(result.status, "needs_clarification")

    def test_irreversible_without_gate_is_rejected_at_compile(self):
        spec = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "true"}],
                "plan_template": {
                    "nodes": [
                        {
                            "id": "T1",
                            "goal": "delete everything",
                            "reversible": False,
                            "acceptance": [{"id": "A1", "kind": "command", "spec": "true"}],
                        }
                    ]
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run(spec, model=ScriptedModel(lambda p: "{}"), workspace=tmp)
        self.assertEqual(result.status, "rejected")
        codes = [i.code for i in result.compile_report.issues]
        self.assertIn("E_NO_GATE", codes)

    def test_persistent_failure_ends_in_blocked_report(self):
        spec = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "true"}],
                "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 2},
                "plan_template": {
                    "nodes": [
                        {
                            "id": "T1",
                            "goal": "fail",
                            "acceptance": [{"id": "A1", "kind": "file_exists", "spec": "never.txt"}],
                        }
                    ]
                },
            }
        )
        model = ScriptedModel(lambda p: '{"action": "finish", "summary": "done"}')
        with tempfile.TemporaryDirectory() as tmp:
            result = run(spec, model=model, workspace=tmp)
        self.assertEqual(result.status, "blocked")
        self.assertIsNotNone(result.blocked)
        self.assertEqual(result.blocked.node_id, "T1")


class DemoRegressionTest(unittest.TestCase):
    """The headline claim, as a test: memory turns a shipped regression into a catch."""

    def test_memory_prevents_the_repeat(self):
        from planloop.demo import run_demo

        with tempfile.TemporaryDirectory() as tmp:
            summary = run_demo(Path(tmp), quiet=True)
        rows = {r["run"]: r for r in summary["records"]}
        self.assertTrue(rows["A_learn"]["suite_green_after"])
        self.assertFalse(rows["B_no_memory"]["suite_green_after"], "control run should ship a regression")
        self.assertTrue(rows["C_memory_ignored"]["guard_injected"])
        self.assertGreaterEqual(rows["C_memory_ignored"]["guard_catches"], 1)
        self.assertTrue(rows["C_memory_ignored"]["suite_green_after"])
        self.assertEqual(rows["D_memory_read"]["l1_failures"], 0)
        self.assertTrue(rows["D_memory_read"]["suite_green_after"])


if __name__ == "__main__":
    unittest.main()

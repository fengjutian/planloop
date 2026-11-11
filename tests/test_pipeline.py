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


class RefusedActionTest(unittest.TestCase):
    """A refused action must not kill the run.

    2026-11-10: a model asked to `read` a path outside the workspace (it had
    seen the acceptance command's path in its prompt). ``Tools._resolve``
    raised ``ToolError``, nothing caught it, and the whole run died -- one
    stray action threw away the entire task.
    """

    def test_reading_outside_the_workspace_is_reported_not_fatal(self):
        replies = [
            json.dumps({"action": "read", "path": "C:/Windows/win.ini"}),
            json.dumps({"action": "read", "path": "../../etc/passwd"}),
            json.dumps({"action": "finish", "summary": "done"}),
        ]

        class _Model:
            name = "refused-actions"

            def __init__(self):
                self.i = 0

            def complete(self, prompt, *, max_tokens=900):
                text = replies[min(self.i, len(replies) - 1)]
                self.i += 1
                return Completion(text=text, tokens_in=10, tokens_out=5)

        spec = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "true", "must": "exit=0"}],
                "plan_template": {
                    "nodes": [{"id": "T1", "goal": "g", "est_cost": 0.2,
                               "acceptance": [{"id": "A1", "kind": "command", "spec": "true",
                                               "must": "exit=0"}]}]
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run(spec, model=_Model(), workspace=tmp)
        self.assertNotEqual(result.status, "needs_clarification")
        self.assertIsNotNone(result.outcomes)
        self.assertTrue(result.outcomes)


class ActionTargetTest(unittest.TestCase):
    """Every action event must say **what** it touched.

    2026-11-11: the memory benchmark asked "did the arm that carries the
    contract skip reading the contract file?", and the event log could not
    answer it -- an action event carried the *observation* (what the command
    printed) but neither the command nor the path.  So "the memory changed
    the behaviour" and "the memory changed nothing" looked identical in the
    log, and the benchmark could only report a difference in cost, never
    attribute it.
    """

    def test_action_events_carry_the_target(self):
        replies = [
            json.dumps({"action": "run", "cmd": "echo hello"}),
            json.dumps({"action": "read", "path": "notes.txt"}),
            json.dumps({"action": "write", "path": "notes.txt", "content": "x"}),
            json.dumps({"action": "write_many", "files": {"a.txt": "1", "b.txt": "2"}}),
            json.dumps({"action": "finish", "summary": "done"}),
        ]

        class _Model:
            name = "action-target"

            def __init__(self):
                self.i = 0

            def complete(self, prompt, *, max_tokens=900):
                text = replies[min(self.i, len(replies) - 1)]
                self.i += 1
                return Completion(text=text, tokens_in=10, tokens_out=5)

        spec = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "true", "must": "exit=0"}],
                "plan_template": {
                    "nodes": [{"id": "T1", "goal": "g", "est_cost": 0.2,
                               "acceptance": [{"id": "A1", "kind": "command", "spec": "true",
                                               "must": "exit=0"}]}]
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "notes.txt").write_text("hi", encoding="utf-8")
            log = Path(tmp, "events.jsonl")
            run(spec, model=_Model(), workspace=tmp, log_path=log)
            raw = log.read_text(encoding="utf-8")
        events = [
            json.loads(line)
            for line in raw.splitlines()
            if line.strip()
        ]
        targets = [
            e.get("data", {}).get("target")
            for e in events
            if e.get("kind") == "action"
        ]
        self.assertIn("echo hello", targets)
        self.assertIn("notes.txt", targets)
        self.assertTrue(
            any(t and "a.txt" in t and "b.txt" in t for t in targets),
            f"write_many target missing: {targets}",
        )


class RejectionStaysVisibleTest(unittest.TestCase):
    """The failure diagnostic must never be evicted from the prompt.

    The prompt shows only the last two observations, and the model's own
    actions push that window along.  A model that cannot see *why* it failed
    has nothing to act on, so the rejection is pinned until the acceptance
    passes.
    """

    def test_the_failure_text_survives_later_observations(self):
        prompts: list[str] = []

        class _Model:
            name = "rejection-window"

            def __init__(self):
                self.i = 0

            def complete(self, prompt, *, max_tokens=900):
                prompts.append(prompt)
                self.i += 1
                if self.i == 1:
                    # a write that leaves the acceptance failing
                    text = json.dumps({"action": "write", "path": "mod.py", "content": "x = 1\n"})
                elif self.i <= 4:
                    text = json.dumps({"action": "read", "path": "mod.py"})
                else:
                    text = json.dumps({"action": "finish", "summary": "done"})
                return Completion(text=text, tokens_in=10, tokens_out=5)

        spec = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "false", "must": "exit=0"}],
                "plan_template": {
                    "nodes": [{"id": "T1", "goal": "g", "est_cost": 0.2,
                               "acceptance": [{"id": "A1", "kind": "command", "spec": "false",
                                               "must": "exit=0"}]}]
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "mod.py").write_text("x = 0\n", encoding="utf-8")
            run(spec, model=_Model(), workspace=tmp)
        # the prompt after several observations must still carry the failure
        later = prompts[-1]
        self.assertIn("acceptance failed", later)
        self.assertIn("exit", later.lower())

    def test_a_passing_acceptance_clears_the_pinned_rejection(self):
        from planloop.blackboard import Blackboard

        bb = Blackboard()
        bb.record_rejection("T1", "acceptance failed:\nboom")
        self.assertIn("T1", bb.rejections)
        bb.clear_rejection("T1")
        self.assertNotIn("T1", bb.rejections)


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

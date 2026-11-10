import json
import tempfile
import unittest
from pathlib import Path

from planloop.decomp import Plan, TaskNode
from planloop.model import ScriptedModel
from planloop.persist import RunState, atomic_write, find_state, list_runs, new_run_id, runs_root
from planloop.scheduler import run
from planloop.spec import Criterion, ProblemSpec


def make_spec() -> ProblemSpec:
    return ProblemSpec.from_dict(
        {
            "goal": "create two files",
            "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "b.txt"}],
            "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 2},
            "plan_template": {
                "nodes": [
                    {
                        "id": "T1",
                        "goal": "create a",
                        "acceptance": [{"id": "A1", "kind": "file_exists", "spec": "a.txt"}],
                    },
                    {
                        "id": "T2",
                        "goal": "create b",
                        "deps": ["T1"],
                        "acceptance": [{"id": "A2", "kind": "file_exists", "spec": "b.txt"}],
                    },
                ]
            },
        }
    )


def writing_model(only: str) -> ScriptedModel:
    """Writes the file whose node contains `only`; finishes otherwise."""

    def handler(prompt: str) -> str:
        if f"Current node T" in prompt and only in prompt:
            target = "a.txt" if only == "create a" else "b.txt"
            return json.dumps({"action": "write", "path": target, "content": "x"})
        return json.dumps({"action": "finish", "summary": "nothing to do"})

    return ScriptedModel(handler)


class RunStateTest(unittest.TestCase):
    def test_roundtrip(self):
        plan = Plan(nodes={"T1": TaskNode(id="T1", goal="g", acceptance=[Criterion("A", "command", "true")])})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r1"
            run_dir.mkdir()
            state = RunState.create(run_dir, make_spec(), plan, Path(tmp))
            state.capture(plan, usage={"tokens": 12}, policy=None, status="running")
            state.save()
            again = RunState.load(run_dir / "state.json")
            self.assertEqual(again.run_id, "r1")
            self.assertEqual(again.usage["tokens"], 12)
            self.assertEqual(again.restored_plan().nodes["T1"].status, "pending")

    def test_atomic_write_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.json"
            atomic_write(target, '{"a": 1}')
            atomic_write(target, '{"a": 2}')
            self.assertEqual(json.loads(target.read_text())["a"], 2)
            self.assertFalse((Path(tmp) / "x.json.tmp").exists())

    def test_run_id_is_readable(self):
        rid = new_run_id("Fix the discount math!", now=1700000000)
        self.assertTrue(rid.startswith("r-"))
        self.assertIn("fix-the-discount", rid)


class ResumeTest(unittest.TestCase):
    def test_resume_does_not_redo_finished_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            run_dir = runs_root(ws) / "r-test-1"
            spec = make_spec()

            # first process: T1 succeeds, T2 fails and the run blocks
            first = run(spec, model=writing_model("create a"), workspace=ws, run_dir=run_dir)
            self.assertEqual(first.status, "blocked")
            self.assertTrue((ws / "a.txt").exists())
            self.assertFalse((ws / "b.txt").exists())
            state = RunState.load(run_dir / "state.json")
            self.assertEqual(state.node_states["T1"]["status"], "done")
            # the node that gave up is recorded as failed; resume turns it back into pending
            self.assertEqual(state.node_states["T2"]["status"], "failed")
            self.assertEqual(state.restored_plan().nodes["T2"].status, "pending")

            # second process: only T2 should run
            prompts: list[str] = []

            def handler(prompt: str) -> str:
                prompts.append(prompt)
                return json.dumps({"action": "write", "path": "b.txt", "content": "x"})

            second = run(
                None,
                model=ScriptedModel(handler),
                workspace=ws,
                run_dir=run_dir,
                resume=True,
            )
            self.assertEqual(second.status, "succeeded")
            self.assertEqual(second.run_id, "r-test-1")
            joined = "\n".join(prompts)
            self.assertIn("create b", joined)
            self.assertNotIn("# Current node T1", joined, "finished node must not be re-executed")
            self.assertEqual([o.node_id for o in second.outcomes], ["T2"])

            # state on disk reflects the finish, and the budget was carried over
            final = RunState.load(run_dir / "state.json")
            self.assertEqual(final.status, "succeeded")
            self.assertGreater(final.usage.get("tokens", 0), 0)

    def test_find_state_picks_the_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            for rid in ("r-a", "r-b"):
                run(make_spec(), model=writing_model("create a"), workspace=ws,
                    run_dir=runs_root(ws) / rid)
            self.assertEqual(find_state(ws).parent.name, "r-b")
            rows = list_runs(ws)
            self.assertEqual([r["run_id"] for r in rows], ["r-b", "r-a"])
            self.assertEqual(rows[0]["nodes_done"], 1)


if __name__ == "__main__":
    unittest.main()

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from planloop.model import ScriptedModel
from planloop.persist import runs_root
from planloop.scheduler import run
from planloop.spec import ProblemSpec
from planloop.workspace import GitWorkspace

GIT = GitWorkspace.available()


def spec(milestone_acceptance: dict | None = None) -> ProblemSpec:
    return ProblemSpec.from_dict(
        {
            "goal": "two nodes under one milestone",
            "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "a.txt"}],
            "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 3},
            "plan_template": {
                "milestones": [
                    {
                        "id": "M1",
                        "goal": "both files exist",
                        "acceptance": [milestone_acceptance or
                                       {"id": "MA1", "kind": "file_exists", "spec": "a.txt"}],
                    }
                ],
                "nodes": [
                    {"id": "T1", "milestone": "M1", "goal": "make a",
                     "acceptance": [{"id": "A1", "kind": "file_exists", "spec": "a.txt"}]},
                    {"id": "T2", "milestone": "M1", "deps": ["T1"], "goal": "make b",
                     "acceptance": [{"id": "A2", "kind": "file_exists", "spec": "b.txt"}]},
                ],
            },
        }
    )


def handler(prompt: str) -> str:
    path = "a.txt" if "# Current node T1" in prompt else "b.txt"
    return json.dumps({"action": "write", "path": path, "content": "ok"})


class MilestoneTest(unittest.TestCase):
    def test_gate_blocks_without_a_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            result = run(spec(), model=ScriptedModel(handler), workspace=ws)
            self.assertEqual(result.status, "blocked")
            self.assertIsNotNone(result.blocked)
            self.assertEqual(result.blocked.milestone, "M1")
            self.assertIn("human approval", result.blocked.reason)
            self.assertIn("milestones: M1:blocked", result.render())

    def test_auto_approve_passes_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            result = run(spec(), model=ScriptedModel(handler), workspace=ws, auto_approve=True)
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.plan.milestones["M1"].status, "passed")
            self.assertIn("milestones: M1:passed", result.render())

    def test_milestone_verification_can_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            bad = {"id": "MA1", "kind": "file_exists", "spec": "never.txt"}
            result = run(spec(bad), model=ScriptedModel(handler), workspace=ws, auto_approve=True)
            self.assertEqual(result.status, "blocked")
            self.assertIn("failed end-to-end", result.blocked.reason)
            self.assertEqual(result.plan.milestones["M1"].status, "failed")

    def test_custom_gate_can_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            result = run(
                spec(),
                model=ScriptedModel(handler),
                workspace=ws,
                milestone_gate=lambda ms: (False, f"{ms.id} looks wrong to me"),
            )
            self.assertEqual(result.status, "blocked")
            self.assertIn("looks wrong", result.blocked.needed)

    def test_undefined_milestone_is_a_compile_error(self):
        bad = ProblemSpec.from_dict(
            {
                "goal": "g",
                "success_criteria": [{"id": "S1", "kind": "command", "spec": "true"}],
                "plan_template": {
                    "nodes": [
                        {"id": "T1", "milestone": "M9", "goal": "x",
                         "acceptance": [{"id": "A1", "kind": "command", "spec": "true"}]}
                    ]
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run(bad, model=ScriptedModel(handler), workspace=tmp)
        self.assertEqual(result.status, "rejected")
        self.assertIn("E_UNKNOWN_MILESTONE", [i.code for i in result.compile_report.issues])


@unittest.skipUnless(GIT, "git not available")
class MilestoneCheckpointTest(unittest.TestCase):
    def test_passed_milestone_gets_its_own_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "readme.txt").write_text("base", encoding="utf-8")
            for cmd in (["git", "init"], ["git", "add", "-A"],
                        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "base"]):
                subprocess.run(cmd, cwd=ws, capture_output=True, check=True)
            git = GitWorkspace(ws)
            result = run(
                spec(),
                model=ScriptedModel(handler),
                workspace=ws,
                run_dir=runs_root(ws) / "r-ms",
                git=git,
                auto_approve=True,
            )
            self.assertEqual(result.status, "succeeded")
            self.assertIn("M1", result.plan.milestones)
            log = "\n".join(git.log())
            self.assertIn("planloop: M1", log)
            self.assertIn("planloop: T1", log)


if __name__ == "__main__":
    unittest.main()

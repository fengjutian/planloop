import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from planloop.blame import blame, find_run_dir
from planloop.model import ScriptedModel
from planloop.persist import runs_root
from planloop.scheduler import run
from planloop.spec import ProblemSpec
from planloop.workspace import GitWorkspace

GIT = GitWorkspace.available()


def spec() -> ProblemSpec:
    return ProblemSpec.from_dict(
        {
            "goal": "write two files",
            "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "a.txt"}],
            "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 2},
            "plan_template": {
                "nodes": [
                    {"id": "T1", "goal": "write a", "acceptance": [
                        {"id": "A1", "kind": "file_exists", "spec": "a.txt"}]},
                    {"id": "T2", "goal": "write b", "deps": ["T1"], "acceptance": [
                        {"id": "A2", "kind": "file_exists", "spec": "b.txt"}]},
                ]
            },
        }
    )


def handler(prompt: str) -> str:
    node = "T1" if "# Current node T1" in prompt else "T2"
    leaf = "a.txt" if node == "T1" else "b.txt"
    return json.dumps({"action": "write", "path": leaf, "content": f"{node} content\n"})


class BlameTest(unittest.TestCase):
    @unittest.skipUnless(GIT, "git not available")
    def test_commit_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "readme.txt").write_text("base", encoding="utf-8")
            for cmd in (["git", "init"], ["git", "add", "-A"],
                        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "base"]):
                subprocess.run(cmd, cwd=ws, capture_output=True, check=True)
            run(spec(), model=ScriptedModel(handler), workspace=ws,
                run_dir=runs_root(ws) / "r-blame", git=GitWorkspace(ws))

            report = blame(ws, "a.txt", run_dir=find_run_dir(ws))
            self.assertEqual(report.nodes[0], "T1")
            self.assertIn("planloop: T1", report.entries[0].detail)

    @unittest.skipUnless(GIT, "git not available")
    def test_line_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "readme.txt").write_text("base", encoding="utf-8")
            for cmd in (["git", "init"], ["git", "add", "-A"],
                        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "base"]):
                subprocess.run(cmd, cwd=ws, capture_output=True, check=True)
            run(spec(), model=ScriptedModel(handler), workspace=ws,
                run_dir=runs_root(ws) / "r-line", git=GitWorkspace(ws))

            report = blame(ws, "b.txt", line=1, run_dir=find_run_dir(ws))
            self.assertEqual(report.nodes[0], "T2")
            self.assertIn("Why b.txt:1", report.to_markdown())

    def test_event_fallback_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            run(spec(), model=ScriptedModel(handler), workspace=ws,
                run_dir=runs_root(ws) / "r-nogit")
            report = blame(ws, "a.txt", run_dir=runs_root(ws) / "r-nogit")
            self.assertTrue(report.entries)
            self.assertEqual(report.entries[0].kind, "action")
            self.assertEqual(report.entries[0].node_id, "T1")

    def test_unknown_path_explains_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            report = blame(ws, "nope.txt", run_dir=None)
            self.assertEqual(report.entries, [])
            self.assertIn("no commit", report.note)
            self.assertIn("no commit and no recorded action", report.to_markdown())

    def test_find_run_dir_returns_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            run(spec(), model=ScriptedModel(handler), workspace=ws, run_dir=runs_root(ws) / "r-1")
            self.assertEqual(find_run_dir(ws).name, "r-1")
            self.assertEqual(find_run_dir(ws, "r-1").name, "r-1")


if __name__ == "__main__":
    unittest.main()

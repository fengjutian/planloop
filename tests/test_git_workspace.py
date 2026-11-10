import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from planloop.model import ScriptedModel
from planloop.persist import RunState, runs_root
from planloop.scheduler import run
from planloop.spec import ProblemSpec
from planloop.workspace import GitError, GitWorkspace

GIT = GitWorkspace.available()


def git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "base", "--allow-empty"],
        cwd=root, capture_output=True, check=True,
    )


@unittest.skipUnless(GIT, "git not available")
class GitWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        git_init(self.root)
        self.git = GitWorkspace(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_and_head(self):
        self.assertTrue(self.git.is_repo())
        self.assertEqual(len(self.git.head()), 40)
        self.assertEqual(self.git.dirty_files(), [])

    def test_dirty_tree_is_detected(self):
        (self.root / "a.txt").write_text("changed", encoding="utf-8")
        self.assertEqual(self.git.dirty_files(), ["a.txt"])

    def test_start_refuses_dirty_tree(self):
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(GitError):
            self.git.start("r-1")

    def test_start_with_allow_dirty_stashes_and_switches_branch(self):
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        self.git.start("r-1", allow_dirty=True)
        self.assertEqual(self.git.current_branch(), "planloop/r-1")
        self.assertTrue(self.git.stashes())

    def test_commit_and_reset_roundtrip(self):
        base = self.git.head()
        (self.root / "a.txt").write_text("two", encoding="utf-8")
        commit = self.git.commit("T1", "change a")
        self.assertNotEqual(commit.sha, base)
        self.assertEqual(self.git.changed_files(base), ["a.txt"])
        self.git.reset(base)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "one")

    def test_state_dir_survives_reset_and_clean(self):
        """Regression: `stash -u` / `clean -fd` used to swallow .planloop/ and lose the audit log."""
        self.git.start("r-state")
        state_dir = self.root / ".planloop" / "runs" / "r-state"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text("{}", encoding="utf-8")

        exclude = (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(".planloop/", exclude)

        self.git.reset(self.git.head())
        self.assertTrue((state_dir / "state.json").exists(), "run state must survive a rollback")
        self.assertTrue((self.root / "a.txt").exists(), "tracked files must survive a rollback")

    def test_stash_keeps_failed_work(self):
        (self.root / "a.txt").write_text("broken", encoding="utf-8")
        (self.root / "new.txt").write_text("untracked", encoding="utf-8")
        self.git.stash("failed attempt")
        self.assertEqual(self.git.dirty_files(), [])
        self.assertFalse((self.root / "new.txt").exists())
        self.assertIn("failed attempt", "\n".join(self.git.stashes()))


@unittest.skipUnless(GIT, "git not available")
class GitSchedulerTest(unittest.TestCase):
    """A failed node must leave the workspace at the last green checkpoint."""

    def spec(self) -> ProblemSpec:
        return ProblemSpec.from_dict(
            {
                "goal": "two steps, second fails",
                "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "a.txt"}],
                "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 2},
                "plan_template": {
                    "nodes": [
                        {"id": "T1", "goal": "make a", "acceptance": [
                            {"id": "A1", "kind": "file_exists", "spec": "a.txt"}]},
                        {"id": "T2", "goal": "make b", "deps": ["T1"], "acceptance": [
                            {"id": "A2", "kind": "file_exists", "spec": "c.txt"}]},
                    ]
                },
            }
        )

    def test_failed_node_is_rolled_back_and_stashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "readme.txt").write_text("base", encoding="utf-8")
            git_init(ws)
            git = GitWorkspace(ws)

            def handler(prompt: str) -> str:
                if "# Current node T1" in prompt:
                    return json.dumps({"action": "write", "path": "a.txt", "content": "ok"})
                return json.dumps({"action": "write", "path": "b.txt", "content": "bad"})

            result = run(
                self.spec(),
                model=ScriptedModel(handler),
                workspace=ws,
                run_dir=runs_root(ws) / "r-git-1",
                git=git,
            )

            self.assertEqual(result.status, "blocked")
            self.assertTrue((ws / "a.txt").exists(), "committed work must survive")
            self.assertFalse((ws / "b.txt").exists(), "failed work must be rolled back")
            self.assertTrue(git.stashes(), "failed work must be stashed, not deleted")

            state = RunState.load(runs_root(ws) / "r-git-1" / "state.json")
            self.assertIn("T1", state.checkpoints)
            self.assertEqual(state.git_branch, "planloop/r-git-1")
            self.assertIn("planloop: T1", "\n".join(git.log()))


if __name__ == "__main__":
    unittest.main()

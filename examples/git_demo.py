"""Demonstration of git checkpoints: one commit per node, rollback on failure.

    python examples/git_demo.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planloop.model import ScriptedModel
from planloop.persist import RunState, runs_root
from planloop.scheduler import run
from planloop.spec import ProblemSpec
from planloop.workspace import GitWorkspace

SPEC = {
    "goal": "two steps, the second one fails",
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


def handler(prompt: str) -> str:
    if "# Current node T1" in prompt:
        return json.dumps({"action": "write", "path": "a.txt", "content": "ok"})
    return json.dumps({"action": "write", "path": "b.txt", "content": "never accepted"})


with tempfile.TemporaryDirectory() as tmp:
    ws = Path(tmp) / "ws"
    ws.mkdir()
    (ws / "readme.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=demo", "-c", "user.email=demo@local", "commit", "-m", "base"],
        cwd=ws, capture_output=True, check=True,
    )

    git = GitWorkspace(ws)
    result = run(
        ProblemSpec.from_dict(SPEC),
        model=ScriptedModel(handler),
        workspace=ws,
        run_dir=runs_root(ws) / "r-git-demo",
        git=git,
    )

    print("== run ==")
    print(result.render())
    print("\n== workspace ==")
    print("a.txt exists:", (ws / "a.txt").exists(), "(committed by T1)")
    print("b.txt exists:", (ws / "b.txt").exists(), "(rolled back)")
    print("\n== git log ==")
    for line in git.log(limit=5):
        print(" ", line)
    print("\n== stash (failed work kept for audit) ==")
    for line in git.stashes():
        print(" ", line)
    state = RunState.load(runs_root(ws) / "r-git-demo" / "state.json")
    print("\ncheckpoints:", {k: v[:10] for k, v in state.checkpoints.items()})
    print("branch:", state.git_branch)

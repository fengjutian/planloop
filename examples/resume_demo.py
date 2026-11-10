"""Demonstration of crash-safe resume: run, block, resume, finish.

    python examples/resume_demo.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planloop.model import ScriptedModel
from planloop.persist import RunState, list_runs, runs_root
from planloop.scheduler import run
from planloop.spec import ProblemSpec

SPEC = {
    "goal": "create two files",
    "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "b.txt"}],
    "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 2},
    "plan_template": {
        "nodes": [
            {"id": "T1", "goal": "create a", "acceptance": [{"id": "A1", "kind": "file_exists", "spec": "a.txt"}]},
            {"id": "T2", "goal": "create b", "deps": ["T1"],
             "acceptance": [{"id": "A2", "kind": "file_exists", "spec": "b.txt"}]},
        ]
    },
}


def model(only: str, target: str) -> ScriptedModel:
    def handler(prompt: str) -> str:
        if "# Current node T" in prompt and only in prompt:
            return json.dumps({"action": "write", "path": target, "content": "x"})
        return json.dumps({"action": "finish", "summary": "nothing to do"})

    return ScriptedModel(handler)


with tempfile.TemporaryDirectory() as tmp:
    ws = Path(tmp) / "ws"
    ws.mkdir()
    run_dir = runs_root(ws) / "r-demo-1"

    print("== run 1: T1 ok, T2 fails ==")
    first = run(ProblemSpec.from_dict(SPEC), model=model("create a", "a.txt"), workspace=ws, run_dir=run_dir)
    print(first.render())
    state = RunState.load(run_dir / "state.json")
    print("state:", json.dumps(state.node_states), "status:", state.status)

    print("\n== run 2: resume ==")
    second = run(None, model=model("create b", "b.txt"), workspace=ws, run_dir=run_dir, resume=True)
    print(second.render())
    print("outcomes:", [(o.node_id, o.status) for o in second.outcomes])
    print("b.txt exists:", (ws / "b.txt").exists())

    print("\n== runs list ==")
    for row in list_runs(ws):
        print(f"  {row['run_id']}  {row['status']:<10} {row['nodes_done']}/{row['nodes_total']}  {row['goal']}")

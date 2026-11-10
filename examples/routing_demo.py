"""Tier routing compared with the two baselines, at two workloads.

    python examples/routing_demo.py

    A  all-cheap    cheapest tier for everything
    B  all-strong   strongest tier for everything
    C  routed       cheap for trivial nodes, escalate on failure

The second table is the interesting one: routing's benefit is proportional to
how much of the work is trivial.  With one easy node it barely pays; with four
it pays a lot.  That is the honest shape of this feature.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planloop.model import ScriptedModel
from planloop.routing import ModelRouter
from planloop.scheduler import run
from planloop.spec import ProblemSpec


def make_spec(easy_nodes: int) -> ProblemSpec:
    nodes = []
    deps: list[str] = []
    for i in range(1, easy_nodes + 1):
        nodes.append(
            {
                "id": f"E{i}", "goal": f"write e{i}.txt", "est_cost": 0.1, "deps": list(deps),
                "acceptance": [{"id": f"AE{i}", "kind": "file_exists", "spec": f"e{i}.txt"}],
            }
        )
        deps = [f"E{i}"]
    nodes.append(
        {
            "id": "H1", "goal": "write result.txt", "est_cost": 0.5, "deps": list(deps),
            "acceptance": [{"id": "AH", "kind": "file_exists", "spec": "result.txt"}],
        }
    )
    return ProblemSpec.from_dict(
        {
            "goal": f"{easy_nodes} trivial nodes plus one hard node",
            "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "result.txt"}],
            "limits": {"max_attempts_per_node": 2, "max_steps_per_node": 2},
            "plan_template": {"nodes": nodes},
        }
    )


def easy_only(prompt: str) -> str:
    if "# Current node H1" in prompt:
        return json.dumps({"action": "finish", "summary": "gave up on the hard part"})
    node = prompt.split("# Current node ", 1)[1].split("\n", 1)[0].strip()
    return json.dumps({"action": "write", "path": f"{node.lower()}.txt", "content": "ok"})


def does_everything(prompt: str) -> str:
    node = prompt.split("# Current node ", 1)[1].split("\n", 1)[0].strip()
    leaf = "result.txt" if node.startswith("H") else f"{node.lower()}.txt"
    return json.dumps({"action": "write", "path": leaf, "content": "ok"})


def configs():
    return [
        ("A all-cheap", {"cheap": ScriptedModel(easy_only, name="cheap")}),
        ("B all-strong", {"strong": ScriptedModel(does_everything, name="strong")}),
        (
            "C routed",
            {
                "cheap": ScriptedModel(easy_only, name="cheap"),
                "standard": ScriptedModel(easy_only, name="standard"),
                "strong": ScriptedModel(does_everything, name="strong"),
            },
        ),
    ]


def run_case(label: str, tiers: dict, easy_nodes: int) -> dict:
    router = ModelRouter(tiers)
    spec = make_spec(easy_nodes)
    fallback_model = next(iter(tiers.values()))
    with tempfile.TemporaryDirectory() as tmp:
        result = run(spec, model=fallback_model, workspace=Path(tmp), router=router)
    return {
        "config": label,
        "status": result.status,
        "tokens": result.usage.get("tokens", 0),
        "cost": round(router.cost_units(), 1),
        "tiers": {k: v["calls"] for k, v in router.usage().items() if v["calls"]},
    }


def table(easy_nodes: int) -> None:
    print(f"\n=== {easy_nodes} trivial node(s) + 1 hard node ===")
    print(f"{'config':<14}{'status':<12}{'tokens':<9}{'cost_units':<12}tier calls")
    print("-" * 78)
    for label, tiers in configs():
        row = run_case(label, tiers, easy_nodes)
        print(f"{row['config']:<14}{row['status']:<12}{row['tokens']:<9}{row['cost']:<12}{row['tiers']}")


table(1)
table(4)
print(
    "\nreading: routing buys nothing when the hard node dominates the tokens;\n"
    "it buys a lot when most work is trivial. That is the tradeoff, not a win."
)

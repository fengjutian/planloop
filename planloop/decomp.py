"""Decomposition: a goal tree becomes a task DAG with contracts.

Every node carries four things, and a node without acceptance is not a node:
inputs, outputs, acceptance criteria, cost estimate.  Depth, fanout and size
are hard-limited so "decompose harder" cannot substitute for thinking.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from planloop.budget import Ledger
from planloop.model import Model, extract_json
from planloop.spec import Criterion, Limits, ProblemSpec, SpecError

PLAN_PROMPT = """task=plan
You are a planner. Decompose the goal into the smallest DAG of verifiable tasks.

Goal: {goal}

Global success criteria (the run is only done when these hold):
{criteria}

Constraints:
{constraints}

Rules:
- Each node must be atomic enough to finish and self-check in one session.
- Every node needs `acceptance`: at least one machine-checkable criterion
  (kind command | file_exists | file_contains | json_has_keys).
- `deps` lists node ids that must finish first. No cycles.
- depth <= {max_depth}, direct dependents per node <= {max_fanout}, nodes <= {max_nodes}.
- `est_cost` is a fraction of the total budget; all nodes together must stay <= 1.0.
- Mark `reversible: false` for anything destructive or externally visible; such a
  node must also include a {"kind": "human"} acceptance criterion.
- Prefer fewer, larger nodes over many coupled ones.

Reply with JSON only:
{{"nodes": [
  {{"id": "T1", "goal": "...", "deps": [], "inputs": {{}}, "outputs": {{"files": "..."}},
    "acceptance": [{{"id": "A1", "kind": "command", "spec": "pytest -q", "must": "exit=0"}}],
    "est_cost": 0.2, "risk": "low", "reversible": true}}
]}}
"""


class PlanError(ValueError):
    pass


@dataclass
class Milestone:
    """A checkpoint a human would care about.

    Nodes finish and commit individually; a milestone is where their *combined*
    effect is verified (usually the full test suite) and, unless auto-approved,
    where a person looks at the result before more work is piled on top.
    """

    id: str
    goal: str
    acceptance: list[Criterion] = field(default_factory=list)
    status: str = "pending"  # pending | passed | failed | blocked

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Milestone":
        mid = str(raw.get("id") or "").strip()
        if not mid:
            raise PlanError("milestone without id")
        return cls(
            id=mid,
            goal=str(raw.get("goal") or "").strip(),
            acceptance=[Criterion.from_dict(c) for c in raw.get("acceptance") or []],
            status=str(raw.get("status", "pending")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "acceptance": [c.to_dict() for c in self.acceptance],
            "status": self.status,
        }


@dataclass
class TaskNode:
    id: str
    goal: str
    deps: list[str] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    acceptance: list[Criterion] = field(default_factory=list)
    est_cost: float = 0.1
    risk: str = "low"
    reversible: bool = True
    status: str = "pending"
    attempts: int = 0
    depth: int = 0
    milestone: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskNode":
        if not isinstance(raw, dict):
            raise PlanError("node must be an object")
        nid = str(raw.get("id") or "").strip()
        if not nid:
            raise PlanError("node without id")
        goal = str(raw.get("goal") or "").strip()
        if not goal:
            raise PlanError(f"node {nid}: missing goal")
        deps = [str(d) for d in raw.get("deps") or []]
        if nid in deps:
            raise PlanError(f"node {nid}: depends on itself")
        criteria = [Criterion.from_dict(c) for c in raw.get("acceptance") or []]
        ids = [c.id for c in criteria]
        if len(ids) != len(set(ids)):
            raise PlanError(f"node {nid}: duplicate acceptance ids")
        return cls(
            id=nid,
            goal=goal,
            deps=deps,
            inputs={str(k): str(v) for k, v in (raw.get("inputs") or {}).items()},
            outputs={str(k): str(v) for k, v in (raw.get("outputs") or {}).items()},
            acceptance=criteria,
            est_cost=float(raw.get("est_cost", 0.1)),
            risk=str(raw.get("risk", "low")),
            reversible=bool(raw.get("reversible", True)),
            milestone=str(raw.get("milestone", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "goal": self.goal,
            "deps": list(self.deps),
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "acceptance": [c.to_dict() for c in self.acceptance],
            "est_cost": self.est_cost,
            "risk": self.risk,
            "reversible": self.reversible,
        }
        if self.milestone:
            out["milestone"] = self.milestone
        if self.depth:
            out["depth"] = self.depth
        return out

    def acceptance_text(self) -> str:
        return "\n".join(f"- ({c.layer}/{c.kind}) {c.spec}" + (f" must={c.must}" if c.must else "")
                         for c in self.acceptance)

    def contracts_text(self) -> str:
        bits = []
        if self.inputs:
            bits.append("inputs: " + json.dumps(self.inputs, ensure_ascii=False))
        if self.outputs:
            bits.append("outputs: " + json.dumps(self.outputs, ensure_ascii=False))
        return "\n".join(bits)


@dataclass
class Plan:
    nodes: dict[str, TaskNode] = field(default_factory=dict)
    milestones: dict[str, Milestone] = field(default_factory=dict)
    goal: str = ""
    source: str = "model"

    def nodes_of(self, milestone_id: str) -> list[TaskNode]:
        return [n for n in self.nodes.values() if n.milestone == milestone_id]

    def milestone_done(self, milestone_id: str) -> bool:
        members = self.nodes_of(milestone_id)
        return bool(members) and all(n.status in {"done", "skipped"} for n in members)

    # -- structure --------------------------------------------------------
    def add(self, node: TaskNode) -> None:
        if node.id in self.nodes:
            raise PlanError(f"duplicate node id {node.id}")
        self.nodes[node.id] = node

    def get(self, node_id: str) -> TaskNode:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise PlanError(f"unknown node {node_id}") from exc

    def dependents(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for node in self.nodes.values():
            for dep in node.deps:
                if dep in out:
                    out[dep].append(node.id)
        return out

    def depths(self) -> dict[str, int]:
        """Longest-path depth over deps; raises on cycles."""
        memo: dict[str, int] = {}
        visiting: set[str] = set()

        def visit(nid: str) -> int:
            if nid in memo:
                return memo[nid]
            if nid in visiting:
                raise PlanError(f"cycle through {nid}")
            visiting.add(nid)
            node = self.get(nid)
            d = 0 if not node.deps else 1 + max(visit(dep) for dep in node.deps)
            visiting.discard(nid)
            memo[nid] = d
            return d

        for nid in self.nodes:
            visit(nid)
        return memo

    def topo_order(self) -> list[str]:
        order: list[str] = []
        seen: set[str] = set()

        def visit(nid: str) -> None:
            if nid in seen:
                return
            for dep in self.get(nid).deps:
                visit(dep)
            seen.add(nid)
            order.append(nid)

        for nid in self.nodes:
            visit(nid)
        return order

    def ready(self) -> list[str]:
        done = {nid for nid, n in self.nodes.items() if n.status == "done"}
        return [
            nid
            for nid, node in self.nodes.items()
            if node.status == "pending" and all(d in done for d in node.deps)
        ]

    def is_complete(self) -> bool:
        return all(n.status in {"done", "skipped"} for n in self.nodes.values())

    # -- mutation ---------------------------------------------------------
    def clone(self) -> "Plan":
        return Plan(
            nodes=copy.deepcopy(self.nodes),
            milestones=copy.deepcopy(self.milestones),
            goal=self.goal,
            source=self.source,
        )

    def splice_subplan(self, node_id: str, replacement: Iterable[TaskNode], *, reason: str = "") -> None:
        """Replace a failing node with a sub-DAG, preserving external wiring.

        Used by replan: local repair instead of "throw the plan away".
        """
        old = self.get(node_id)
        subs = list(replacement)
        if not subs:
            raise PlanError("splice needs at least one node")
        names = {s.id for s in subs}
        if node_id in names:
            for s in subs:
                if s.id == node_id:
                    raise PlanError("replacement may not reuse the replaced id")

        dependents = self.dependents()[node_id]
        for s in subs:
            for dep in s.deps:
                if dep not in names:
                    self.get(dep)  # external dependency must already exist
        roots = [s for s in subs if not any(d in names for d in s.deps)]
        if not roots:
            raise PlanError("replacement subplan has no entry node")
        leaves = [s for s in subs if not any(s.id in other.deps for other in subs)]
        if not leaves:
            raise PlanError("replacement subplan has no terminal node")

        # entry nodes inherit the failed node's own dependencies
        for s in roots:
            s.deps = [d for d in old.deps if d not in names]

        del self.nodes[node_id]
        for s in subs:
            s.status = "pending"
            s.attempts = 0
            s.risk = s.risk or old.risk
            self.add(s)
        for dep_id in dependents:
            node = self.nodes[dep_id]
            node.deps = [leaves[0].id if d == node_id else d for d in node.deps]

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "source": self.source,
            "milestones": [m.to_dict() for m in self.milestones.values()],
            "nodes": [n.to_dict() for n in self.nodes.values()],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, goal: str = "", source: str = "model") -> "Plan":
        nodes_raw = raw.get("nodes") if isinstance(raw, dict) else None
        if not isinstance(nodes_raw, list) or not nodes_raw:
            raise PlanError("plan must contain a non-empty 'nodes' list")
        plan = cls(goal=goal or str(raw.get("goal", "")), source=str(raw.get("source", source)))
        for item in raw.get("milestones") or []:
            milestone = Milestone.from_dict(item)
            plan.milestones[milestone.id] = milestone
        for item in nodes_raw:
            plan.add(TaskNode.from_dict(item))
        return plan


def dep_none() -> str:  # pragma: no cover - readability shim
    return ""


def build_plan(
    spec: ProblemSpec,
    *,
    model: Model | None,
    ledger: Ledger | None = None,
    probe_notes: str = "",
) -> Plan:
    """Produce a raw plan, either from a template or from the planner model."""
    if spec.plan_template:
        plan = Plan.from_dict(spec.plan_template, goal=spec.goal, source="template")
        return plan
    if model is None:
        raise PlanError("no planner model available and no plan_template supplied")

    criteria = "\n".join(f"- {c.id} ({c.kind}): {c.spec}" + (f" must={c.must}" if c.must else "")
                         for c in spec.success_criteria)
    constraints = "\n".join(f"- {c}" for c in spec.constraints) or "- (none)"
    if probe_notes:
        constraints += "\n- probe findings:\n" + probe_notes
    limits: Limits = spec.limits
    prompt = PLAN_PROMPT.format(
        goal=spec.goal,
        criteria=criteria,
        constraints=constraints,
        max_depth=limits.max_depth,
        max_fanout=limits.max_fanout,
        max_nodes=limits.max_nodes,
    )
    if ledger is not None:
        if not ledger.can_afford(ledger.remaining_tokens * 0 + 1):
            raise SpecError("no budget left for planning")
    comp = model.complete(prompt, max_tokens=1400)
    if ledger is not None:
        ledger.charge("plan", tokens=comp.tokens, note="decompose")
    try:
        raw = extract_json(comp.text)
    except ValueError as exc:
        raise PlanError(f"planner did not return JSON: {exc}") from exc
    return Plan.from_dict(raw, goal=spec.goal, source="model")

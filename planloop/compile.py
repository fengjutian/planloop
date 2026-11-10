"""Static validation: the errors that must not survive until execution.

Pure code, no tokens.  It catches structure (cycles, dangling deps, depth,
fanout), contracts (unproduced inputs), economics (cost overrun) and safety
(irreversible actions without a human gate).

It also has one job that is not about the plan at all: injecting *guards* from
error memory into the acceptance criteria of the nodes they apply to.  That is
what turns a past mistake into a future check rather than a friendly reminder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from planloop.decomp import Plan, PlanError, TaskNode
from planloop.memory import MemoryContext, MemoryStore
from planloop.spec import ProblemSpec


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "error"
    node_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.node_id:
            out["node"] = self.node_id
        return out


@dataclass
class CompileReport:
    issues: list[Issue] = field(default_factory=list)
    injections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "injections": self.injections,
        }

    def render(self) -> str:
        if not self.issues:
            return "compile: ok (no issues)"
        lines = [f"compile: {'ok' if self.ok else 'FAILED'}" for _ in range(1)]
        for i in self.issues:
            lines.append(f"  [{i.severity}] {i.code} {i.node_id or '-'}: {i.message}")
        return "\n".join(lines)


def compile_plan(
    spec: ProblemSpec,
    plan: Plan,
    *,
    memory: MemoryStore | None = None,
    workspace_files: list[str] | None = None,
) -> CompileReport:
    report = CompileReport()
    limits = spec.limits

    if not plan.nodes:
        report.issues.append(Issue("E_EMPTY_PLAN", "plan has no nodes"))
        return report

    for node in plan.nodes.values():
        for dep in node.deps:
            if dep not in plan.nodes:
                report.issues.append(Issue("E_UNKNOWN_DEP", f"depends on missing node {dep}", node_id=node.id))
            if dep == node.id:
                report.issues.append(Issue("E_SELF_DEP", "depends on itself", node_id=node.id))

    try:
        depths = plan.depths()
    except PlanError as exc:
        report.issues.append(Issue("E_CYCLE", str(exc)))
        return report

    for nid, depth in depths.items():
        plan.nodes[nid].depth = depth
        if depth > limits.max_depth:
            report.issues.append(
                Issue("E_DEPTH", f"dependency depth {depth} exceeds {limits.max_depth}", node_id=nid)
            )

    for nid, kids in plan.dependents().items():
        if len(kids) > limits.max_fanout:
            report.issues.append(
                Issue("E_FANOUT", f"{len(kids)} direct dependents exceeds {limits.max_fanout}", node_id=nid)
            )

    if len(plan.nodes) > limits.max_nodes:
        report.issues.append(Issue("E_TOO_MANY_NODES", f"{len(plan.nodes)} nodes exceeds {limits.max_nodes}"))

    for node in plan.nodes.values():
        if node.milestone and node.milestone not in plan.milestones:
            report.issues.append(
                Issue("E_UNKNOWN_MILESTONE", f"references undefined milestone {node.milestone!r}", node_id=node.id)
            )
    for milestone in plan.milestones.values():
        if not milestone.acceptance:
            report.issues.append(
                Issue(
                    "W_MILESTONE_NO_ACCEPTANCE",
                    f"milestone {milestone.id} has no acceptance; it will pass without checking anything",
                    severity="warning",
                )
            )

    cost = 0.0
    for node in plan.nodes.values():
        cost += node.est_cost
        if not node.acceptance:
            report.issues.append(
                Issue("E_NO_ACCEPTANCE", "node has no acceptance criteria; it cannot be verified", node_id=node.id)
            )
        if not node.reversible and not any(c.kind == "human" for c in node.acceptance):
            report.issues.append(
                Issue("E_NO_GATE", "irreversible node without a human acceptance criterion", node_id=node.id)
            )
        for key in node.inputs:
            if key.startswith("spec:"):
                continue
            produced = set()
            for dep in node.deps:
                produced.update(plan.nodes[dep].outputs) if dep in plan.nodes else None
            if key not in produced:
                report.issues.append(
                    Issue(
                        "W_INTERFACE",
                        f"input {key!r} is not produced by any direct dependency",
                        severity="warning",
                        node_id=node.id,
                    )
                )

    if cost > 1.0 + 1e-9:
        report.issues.append(Issue("E_COST_OVERRUN", f"estimated cost {cost:.2f} exceeds budget 1.00"))

    if not any(c.layer == "L1" for n in plan.nodes.values() for c in n.acceptance):
        report.issues.append(
            Issue("E_NO_MACHINE_CHECK", "no deterministic (L1) criterion anywhere in the plan", severity="warning")
        )

    # -- memory guards ----------------------------------------------------
    if memory is not None:
        _inject_guards(plan, spec, memory, workspace_files or [], report)

    return report


def _inject_guards(
    plan: Plan,
    spec: ProblemSpec,
    memory: MemoryStore,
    workspace_files: list[str],
    report: CompileReport,
) -> None:
    for node in plan.nodes.values():
        ctx = MemoryContext(
            goal=f"{spec.goal} :: {node.goal}",
            files=workspace_files,
            commands=[],
            error_text="",
        )
        for lesson, why in memory.match(ctx):
            if not lesson.hard:
                report.issues.append(
                    Issue("I_MEMORY_ADVISORY", f"{lesson.id} applies ({why})", severity="info", node_id=node.id)
                )
                memory.record_application([lesson.id])
                continue
            crit = lesson.criterion()
            if crit is None:
                continue
            if any(c.spec == crit.spec and c.must == crit.must for c in node.acceptance):
                continue
            node.acceptance.append(crit)
            report.injections.append(
                {"node": node.id, "lesson": lesson.id, "criterion": crit.id, "spec": crit.spec}
            )
            report.issues.append(
                Issue(
                    "I_MEMORY_GUARD",
                    f"injected guard {crit.id} from lesson {lesson.id}: {lesson.summary}",
                    severity="info",
                    node_id=node.id,
                )
            )
            memory.record_application([lesson.id])


def require_ok(report: CompileReport) -> None:
    if not report.ok:
        raise PlanError("plan failed compilation:\n" + report.render())

"""Run state on disk: the difference between "stopped" and "restarted from zero".

Layout:

    <workspace>/.planloop/runs/<run_id>/
        state.json      spec, plan, node statuses, policy counters, usage
        events.jsonl    append-only event log (shared with Blackboard)

Writes are atomic (temp file + rename), so a crash leaves either the previous
state or the new one, never a half-written file.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from planloop.decomp import Plan
from planloop.spec import ProblemSpec


def new_run_id(goal: str, *, now: float | None = None) -> str:
    now = now or time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower())[:24].strip("-") or "run"
    return f"r-{stamp}-{slug}"


def runs_root(workspace: Path | str) -> Path:
    return Path(workspace) / ".planloop" / "runs"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class RunState:
    run_id: str
    workspace: str
    status: str
    spec: dict[str, Any]
    plan: dict[str, Any]
    node_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    lessons: list[str] = field(default_factory=list)
    checkpoints: dict[str, str] = field(default_factory=dict)
    git_branch: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    events_path: str = ""
    path: Path | None = None

    # -- construction -----------------------------------------------------
    @classmethod
    def create(cls, run_dir: Path, spec: ProblemSpec, plan: Plan, workspace: Path) -> "RunState":
        run_id = run_dir.name
        now = time.time()
        state = cls(
            run_id=run_id,
            workspace=str(workspace),
            status="running",
            spec=spec.to_dict(),
            plan=plan.to_dict(),
            node_states={n.id: {"status": n.status, "attempts": n.attempts} for n in plan.nodes.values()},
            events_path=str(run_dir / "events.jsonl"),
            created_at=now,
            updated_at=now,
            path=run_dir / "state.json",
        )
        state.save()
        return state

    # -- mutation ---------------------------------------------------------
    def capture(self, plan: Plan, *, usage: dict[str, Any] | None = None,
                policy: Any = None, artifacts: dict[str, Any] | None = None,
                status: str | None = None) -> None:
        self.plan = plan.to_dict()
        self.node_states = {n.id: {"status": n.status, "attempts": n.attempts} for n in plan.nodes.values()}
        if usage is not None:
            self.usage = usage
        if policy is not None:
            self.policy = {
                "global_replans": getattr(policy, "global_replans", 0),
                "subtree_counts": dict(getattr(policy, "subtree_counts", {})),
            }
        if artifacts is not None:
            self.artifacts = artifacts
        if status is not None:
            self.status = status
        self.updated_at = time.time()

    def save(self) -> None:
        if self.path is None:
            return
        atomic_write(self.path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workspace": self.workspace,
            "status": self.status,
            "spec": self.spec,
            "plan": self.plan,
            "node_states": self.node_states,
            "artifacts": self.artifacts,
            "usage": self.usage,
            "policy": self.policy,
            "lessons": self.lessons,
            "checkpoints": self.checkpoints,
            "git_branch": self.git_branch,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "events_path": self.events_path,
        }

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str) -> "RunState":
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            run_id=raw["run_id"],
            workspace=raw.get("workspace", ""),
            status=raw.get("status", "running"),
            spec=raw["spec"],
            plan=raw["plan"],
            node_states=raw.get("node_states", {}),
            artifacts=raw.get("artifacts", {}),
            usage=raw.get("usage", {}),
            policy=raw.get("policy", {}),
            lessons=raw.get("lessons", []),
            checkpoints=raw.get("checkpoints", {}),
            git_branch=raw.get("git_branch", ""),
            created_at=float(raw.get("created_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
            events_path=raw.get("events_path", ""),
            path=path,
        )

    def restored_plan(self) -> Plan:
        plan = Plan.from_dict(self.plan, goal=str(self.spec.get("goal", "")))
        for node in plan.nodes.values():
            saved = self.node_states.get(node.id)
            if not saved:
                continue
            node.status = saved.get("status", "pending")
            node.attempts = int(saved.get("attempts", 0))
            # a node that was mid-flight or gave up must be retried on resume
            if node.status in ("running", "failed"):
                node.status = "pending"
        return plan


def find_state(workspace: Path | str, run_id: str | None = None) -> Path:
    """Locate a run's state file, defaulting to the most recently updated one."""
    root = runs_root(workspace)
    if run_id:
        candidate = root / run_id / "state.json"
        if not candidate.exists():
            raise FileNotFoundError(f"no such run: {run_id}")
        return candidate
    states = sorted(root.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not states:
        raise FileNotFoundError(f"no runs under {root}")
    return states[0]


def list_runs(workspace: Path | str) -> list[dict[str, Any]]:
    out = []
    for state_path in sorted(runs_root(workspace).glob("*/state.json"), key=lambda p: p.parent.name, reverse=True):
        try:
            state = RunState.load(state_path)
        except (json.JSONDecodeError, KeyError):
            continue
        done = sum(1 for n in state.node_states.values() if n.get("status") == "done")
        out.append(
            {
                "run_id": state.run_id,
                "status": state.status,
                "nodes_done": done,
                "nodes_total": len(state.node_states),
                "updated_at": state.updated_at,
                "goal": str(state.spec.get("goal", ""))[:70],
            }
        )
    return out

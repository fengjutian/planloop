"""Audit report: a run's story, reconstructed from disk.

Sources, in order of trustworthiness:

1. ``state.json``   -- what the scheduler believed (plan, node status, checkpoints)
2. ``events.jsonl`` -- what actually happened (failures, replans, rollbacks)
3. git              -- what changed (one commit per finished node)

The report deliberately does not call a model.  A summary written by a model
would be a *narrative*, and the whole point of an audit is to disagree with the
agent's own account when the evidence does.

Evidence is graded per node (A command / B file / C judge / D self-report) and
the run grade is the weakest link among finished nodes, because a chain of
claims is only as strong as its weakest claim.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from planloop.persist import RunState

GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}
GRADE_LABEL = {
    "A": "A - executable check (command/exit code)",
    "B": "B - checkable content (file/JSON)",
    "C": "C - model judgement (rubric with quotes)",
    "D": "D - self-report / human only",
}
KIND_GRADE = {
    "command": "A",
    "file_exists": "B",
    "file_contains": "B",
    "json_has_keys": "B",
    "rubric": "C",
    "human": "D",
}
INTERESTING = {"node_start", "node_end", "failure", "replan", "checkpoint",
               "rollback", "guard_catch", "lesson_learned", "resume", "run_end",
               "approval_requested", "action_denied", "milestone_check", "milestone_passed",
               "facts_harvested"}


@dataclass
class NodeAudit:
    id: str
    goal: str
    status: str
    attempts: int
    checkpoint: str = ""
    grade: str = "-"
    failures: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    run_id: str
    goal: str
    status: str
    blocked_reason: str
    started: float
    updated: float
    usage: dict[str, Any]
    nodes: list[NodeAudit]
    timeline: list[tuple[float, str, str]]  # (ts, node, text)
    stashes: list[str] = field(default_factory=list)
    git_log: list[str] = field(default_factory=list)
    actions: int = 0

    @property
    def gaps(self) -> list[str]:
        out = []
        for node in self.nodes:
            if node.status == "done":
                if node.attempts > 1:
                    out.append(f"{node.id}: 用了 {node.attempts} 次尝试才通过")
                continue
            out.append(f"{node.id} 未完成（{node.status}）: {node.goal[:80]}")
        if self.stashes:
            out.append(f"{len(self.stashes)} 份失败产物保存在 stash（可审计、可恢复）")
        return out

    @property
    def grade(self) -> str:
        done = [n for n in self.nodes if n.status == "done" and n.grade in GRADE_ORDER]
        if not done:
            return "D" if any(n.status == "done" for n in self.nodes) else "-"
        weakest = max(GRADE_ORDER[n.grade] for n in done)
        return {v: k for k, v in GRADE_ORDER.items()}[weakest]

    def to_markdown(self) -> str:
        lines = [f"# Run {self.run_id}", ""]
        lines += [
            f"**目标**: {self.goal}",
            f"**状态**: `{self.status}`" + (f" — {self.blocked_reason}" if self.blocked_reason else ""),
            f"**耗时**: {self.updated - self.started:.1f}s · "
            f"**用量**: {self.usage.get('tokens', 0)} tokens / {self.usage.get('tool_calls', 0)} 次工具调用",
            "",
            "## 进度",
            "",
            "| 节点 | 状态 | 尝试 | 提交点 | 证据 | 目标 |",
            "|---|---|---|---|---|---|",
        ]
        for node in self.nodes:
            lines.append(
                f"| {node.id} | {node.status} | {node.attempts} | "
                f"{node.checkpoint[:10] or '—'} | {node.grade} | {node.goal[:50]} |"
            )
        lines += ["", "## 缺口", ""]
        gaps = self.gaps
        lines += [f"- {g}" for g in gaps] if gaps else ["- 无：计划内所有节点完成"]

        lines += ["", "## 时间线", ""]
        if self.timeline:
            for ts, node, text in self.timeline:
                stamp = time.strftime("%H:%M:%S", time.localtime(ts))
                lines.append(f"- `{stamp}` {node + ' ' if node else ''}{text}")
            if self.actions:
                lines.append(f"- （另有 {self.actions} 次工具动作未展开，见 events.jsonl）")
        else:
            lines.append("- （无事件记录）")

        lines += ["", "## 证据等级", ""]
        for node in self.nodes:
            if node.grade in GRADE_LABEL:
                lines.append(f"- {node.id}: {GRADE_LABEL[node.grade]}")
        overall = self.grade
        if overall in GRADE_LABEL:
            lines.append(f"- **整体: {GRADE_LABEL[overall]}**")
            if GRADE_ORDER.get(overall, 0) >= 2:
                lines.append("- ⚠️ 结论依赖模型评审或自述，**不可复现**；建议补一条可执行验收。")

        if self.git_log:
            lines += ["", "## 变更（git）", ""]
            lines += [f"- `{entry}`" for entry in self.git_log[:15]]
        if self.stashes:
            lines += ["", "## 失败产物（stash）", ""]
            lines += [f"- `{entry}`" for entry in self.stashes[:10]]
        return "\n".join(lines).rstrip() + "\n"


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _node_grade(criteria: list[dict[str, Any]]) -> str:
    grades = [KIND_GRADE.get(str(c.get("kind", "")), "D") for c in criteria]
    if not grades:
        return "D"
    return max(grades, key=lambda g: GRADE_ORDER[g])


def build_report(run_dir: Path | str, *, git=None) -> AuditReport:
    run_dir = Path(run_dir)
    state = RunState.load(run_dir / "state.json")
    events = _read_events(Path(state.events_path) if state.events_path else run_dir / "events.jsonl")

    plan_nodes = {n["id"]: n for n in (state.plan or {}).get("nodes", [])}
    nodes: list[NodeAudit] = []
    failures: dict[str, list[str]] = {}
    timeline: list[tuple[float, str, str]] = []
    actions = 0

    for event in events:
        kind = event.get("kind")
        if kind == "action":
            actions += 1
            continue
        if kind not in INTERESTING:
            continue
        data = event.get("data") or {}
        node_id = event.get("node_id", "")
        ts = float(event.get("ts", 0))
        text = ""
        if kind == "node_start":
            text = f"开始（第 {data.get('attempt', '?')} 次尝试）"
        elif kind == "failure":
            reason = str(data.get("reason", ""))[:160]
            failures.setdefault(node_id, []).append(reason)
            text = f"失败: {reason}"
        elif kind == "replan":
            text = f"重规划 → {data.get('action')}（{data.get('klass')}）: {str(data.get('reason', ''))[:120]}"
        elif kind == "checkpoint":
            text = f"提交检查点 {data.get('sha', '')}"
        elif kind == "rollback":
            text = f"回滚到 {data.get('to', '')}（保留 stash）"
        elif kind == "guard_catch":
            text = f"护栏拦截 lesson {data.get('lesson', '')}"
        elif kind == "lesson_learned":
            text = f"学到 lesson {data.get('id')} [{data.get('strength')}]"
        elif kind == "resume":
            text = f"续跑，已完成节点 {data.get('done')}"
        elif kind == "approval_requested":
            text = f"[!] 等待批准 {data.get('token')}: {data.get('target')}"
        elif kind == "action_denied":
            text = f"[X] 策略拒绝（{data.get('rule')}）: {data.get('target')}"
        elif kind == "milestone_check":
            text = f"里程碑验收 {'通过' if data.get('passed') else '失败'}"
        elif kind == "milestone_passed":
            text = f"里程碑通过（{data.get('approved_by')}）"
        elif kind == "facts_harvested":
            text = f"沉淀事实 {data.get('count')} 条"
        elif kind == "run_end":
            text = f"结束: {data.get('status')}"
        elif kind == "node_end":
            text = f"节点结束: {data.get('status')}（{data.get('steps')} 步）"
        timeline.append((ts, node_id, text))

    for node_id, raw in plan_nodes.items():
        node_state = state.node_states.get(node_id, {})
        nodes.append(
            NodeAudit(
                id=node_id,
                goal=str(raw.get("goal", "")),
                status=str(node_state.get("status", "pending")),
                attempts=int(node_state.get("attempts", 0)),
                checkpoint=state.checkpoints.get(node_id, ""),
                grade=_node_grade(raw.get("acceptance") or []),
                failures=failures.get(node_id, []),
            )
        )

    report = AuditReport(
        run_id=state.run_id,
        goal=str(state.spec.get("goal", "")),
        status=state.status,
        blocked_reason=_blocked_reason(events),
        started=state.created_at or (events[0]["ts"] if events else 0.0),
        updated=state.updated_at,
        usage=state.usage or {},
        nodes=nodes,
        timeline=timeline,
        actions=actions,
        git_log=git.log() if git is not None else [],
        stashes=git.stashes() if git is not None else [],
    )
    return report


def _blocked_reason(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("kind") == "replan":
            return str((event.get("data") or {}).get("reason", ""))
    return ""


def write_report(run_dir: Path | str, *, git=None, out: Path | None = None) -> Path:
    report = build_report(run_dir, git=git)
    target = Path(out) if out else Path(run_dir) / "AUDIT.md"
    target.write_text(report.to_markdown(), encoding="utf-8")
    return target

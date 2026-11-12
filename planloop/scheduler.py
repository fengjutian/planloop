"""Scheduler: spec -> plan -> compile -> execute/verify loop -> blocked report.

The loop is deliberately boring.  All the interesting policy lives in
``compile`` (what is allowed to run), ``verify`` (what counts as done) and
``replan`` (when to stop).  This file only wires them and keeps the books.

Since P0 it also owns durability: when ``run_dir`` is given, every state
transition is written to ``state.json`` atomically, so a crashed or killed run
can be resumed with ``resume=True`` instead of starting over.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from planloop.blackboard import Blackboard
from planloop.approvals import ApprovalStore
from planloop.budget import BudgetExceeded, Ledger
from planloop.compile import CompileReport, compile_plan
from planloop.compress import Compressor
from planloop.decomp import Plan, build_plan
from planloop.execute import Executor, NodeOutcome
from planloop.memdb import check_evidence
from planloop.memory import MemoryStore, propose_lesson
from planloop.model import Model
from planloop.persist import RunState, atomic_write, new_run_id
from planloop.policy import DeniedAction, NeedsApproval, default_policy
from planloop.replan import FailureClass, ReplanPolicy, classify
from planloop.routing import ModelRouter
from planloop.spec import Criterion, ProblemSpec, SpecError
from planloop.verify import VerificationReport, Verifier
from planloop.workspace import GitError, GitWorkspace


def _record_interrupted(result: RunResult, exc: Exception) -> None:
    """Record the outcome of an attempt that a policy check cut short.

    The attempt really ran: it spent steps and it recorded L1 failures, and
    both are in the event log.  Dropping it here is what made the *report*
    disagree with the *log* -- the report counts L1 failures out of the
    attempts it has, so a dropped attempt silently under-counted (a real run:
    12 steps and 1 failure in the report, 19 actions and 2 failures in the
    log).

    It goes into its own list, not into ``outcomes``: ``outcomes`` means
    "attempts that ran to a verdict", and the mutation bench counts its
    length as *completed executor runs* -- an attempt the policy cut short
    never measured the acceptance, and counting it as a failed attempt
    biases every arm that gets interrupted.
    """
    outcome = getattr(exc, "outcome", None)
    if outcome is not None:
        result.interrupted.append(outcome)


@dataclass
class BlockedReport:
    reason: str
    node_id: str = ""
    milestone: str = ""
    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    needed: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "node": self.node_id,
            "milestone": self.milestone,
            "completed": self.completed,
            "pending": self.pending,
            "needed": self.needed,
        }


@dataclass
class RunResult:
    status: str  # succeeded | blocked | rejected | needs_clarification
    spec: ProblemSpec | None = None
    plan: Plan | None = None
    compile_report: CompileReport | None = None
    verification: VerificationReport | None = None
    blocked: BlockedReport | None = None
    outcomes: list[NodeOutcome] = field(default_factory=list)
    #: the attempts a policy check cut short.  They spent steps and recorded
    #: failures -- the event log has both -- but they never ran to a verdict,
    #: so they are not ``outcomes`` (see :func:`_record_interrupted`).
    interrupted: list[NodeOutcome] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    memory_summary: dict[str, Any] = field(default_factory=dict)
    lessons_learned: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    run_id: str = ""
    run_dir: str = ""
    routing: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "verification": self.verification.to_dict() if self.verification else None,
            "blocked": self.blocked.to_dict() if self.blocked else None,
            "compile": self.compile_report.to_dict() if self.compile_report else None,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "interrupted": [o.to_dict() for o in self.interrupted],
            "usage": self.usage,
            "routing": self.routing,
            "memory": self.memory_summary,
            "lessons_learned": self.lessons_learned,
            "duration_s": round(self.duration_s, 3),
        }

    def render(self) -> str:
        lines = [f"status: {self.status}"]
        if self.run_id:
            lines.append(f"run: {self.run_id}  ({self.run_dir})")
        if self.compile_report:
            lines.append(self.compile_report.render())
        for outcome in self.outcomes:
            lines.append(f"  node {outcome.node_id}: {outcome.status} ({outcome.steps} steps)")
        if self.plan is not None and self.plan.milestones:
            lines.append(
                "milestones: "
                + ", ".join(f"{m.id}:{m.status}" for m in self.plan.milestones.values())
            )
        if self.verification:
            lines.append("final verification:\n" + self.verification.render())
        if self.blocked:
            lines.append(f"blocked: {self.blocked.reason}")
            if self.blocked.needed:
                lines.append(f"  needed: {self.blocked.needed}")
        usage = self.usage or {}
        lines.append(
            f"usage: {usage.get('tokens', 0)} tokens, {usage.get('tool_calls', 0)} tool calls, "
            f"{usage.get('elapsed_s', 0)}s"
        )
        if self.lessons_learned:
            lines.append("lessons learned: " + ", ".join(self.lessons_learned))
        return "\n".join(lines)


def _fix_excerpt(workspace: Path, files: list[str], limit: int = 2400) -> str:
    """Show what the files look like **now**, so the lesson can be true.

    The lesson proposer used to get a summary of the fix -- "wrote 239 chars
    to shop/helpers.py" plus the model's own one-line summary.  With nothing
    but that, the model **invents** the cause and the fix: measured on
    MiniMax-M3.1-Flash, a lesson claimed the fix was ``1 - p/100`` for every
    tier while the file it had just written contained the tier table.  A
    lesson like that is worse than no lesson -- it is injected with a
    matching trigger.
    """
    if not files:
        return ""
    parts: list[str] = []
    used = 0
    for rel in list(files)[:3]:
        target = (workspace / rel) if not Path(rel).is_absolute() else Path(rel)
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if used + len(text) > limit:
            text = text[: max(0, limit - used)]
        used += len(text)
        parts.append(f"--- {rel} (after the fix) ---\n{text}")
        if used >= limit:
            break
    if not parts:
        return ""
    return "\n" + "\n".join(parts)


def _account_guards(memory, node, outcome, bb) -> None:
    """Credit or debit each injected guard after the node finishes.

    A guard that fired is a *catch*; a guard that ran through a run in which the
    guarded failure never appeared counts as prevention.  Neither is inferred
    from the model's own report.
    """
    guard_ids = [c.id[2:] for c in node.acceptance if c.id.startswith("M_")]
    if not guard_ids:
        return
    hit = {f.get("lesson") for f in outcome.failures if f.get("guard_only")}
    for gid in guard_ids:
        if gid in hit:
            memory.record_guard_catch(gid)
            bb.append("guard_catch", node.id, lesson=gid)
        else:
            memory.record_prevented(gid)


def _record_episode(memory_system, spec: ProblemSpec, plan: Plan, result: "RunResult") -> None:
    """One line of history per run: what was attempted, what came out.

    Episodes are private by default: they contain repository detail, not
    knowledge worth sharing across projects.
    """
    run_key = f"run-{int(time.time())}"
    nodes = ",".join(f"{n.id}:{n.status}" for n in plan.nodes.values())
    value = (
        f"[{result.status}] {spec.goal[:120]} | nodes: {nodes} | "
        f"tokens: {result.usage.get('tokens', 0)} | duration: {result.duration_s:.1f}s"
    )
    try:
        memory_system.remember(
            "episode",
            run_key,
            value,
            ttl_days=30,
            confidence=0.6,
            source="scheduler",
            evidence={"type": "manual"},
        )
    except Exception:  # noqa: BLE001 - memory must never break a run
        pass


TEST_COMMAND = re.compile(
    r"(?i)\b(pytest|py\.test|unittest|npm\s+(run\s+)?test|pnpm\s+test|yarn\s+test|"
    r"cargo\s+test|go\s+test|mvn\s+test|gradle\s+test)\b"
)
FACTS_PER_RUN = 3


def _harvest_facts(memory_system, node, outcome, workspace: Path, budget: int, policy=None) -> int:
    """Turn observations into durable facts, but only ones we can verify now.

    Two extractors, both deterministic:

    * the first successful test command (re-run once to prove it still passes);
    * the files this node wrote (existence check).

    This exists because "the agent will remember to call `mem add`" is a fantasy:
    in a long run nobody writes memory by hand.
    """
    if memory_system is None or budget <= 0:
        return 0
    written = 0
    for cmd in outcome.commands:
        if written >= budget:
            break
        if not TEST_COMMAND.search(cmd):
            continue
        if policy is not None and policy.assess_command(cmd).level != "allow":
            # never re-run something the policy would stop; a memory is not consent
            continue
        evidence = {"type": "command", "spec": cmd, "exit": 0, "timeout": 60}
        ok, _ = check_evidence(evidence, Path(workspace))
        if not ok:
            continue
        memory_system.remember(
            "fact", "test_command", cmd, evidence=evidence, confidence=0.75, source="observation",
        )
        written += 1
        break
    for path in outcome.files[:2]:
        if written >= budget:
            break
        evidence = {"type": "file", "path": path}
        ok, _ = check_evidence(evidence, Path(workspace))
        if not ok:
            continue
        memory_system.remember(
            "fact", f"file:{path}", f"written by {node.id}: {node.goal[:60]}",
            evidence=evidence, confidence=0.6, source="observation",
        )
        written += 1
    return written


def _check_milestones(
    plan: Plan,
    verifier,
    bb: Blackboard,
    *,
    git,
    state,
    result: RunResult,
    auto_approve: bool,
    gate,
    budget_left: float,
) -> bool:
    """Verify finished milestones. Returns True if the run must stop.

    This is the layer that catches *direction* errors: individual nodes can all
    pass their own criteria while their combined effect breaks the suite.
    """
    for milestone in plan.milestones.values():
        if milestone.status != "pending" or not plan.milestone_done(milestone.id):
            continue
        members = plan.nodes_of(milestone.id)
        evidence = "\n".join(f"{n.id}: {n.goal}" for n in members)
        report = verifier.check(
            milestone.acceptance, evidence=evidence, node_id=milestone.id, budget_left=budget_left
        )
        bb.append("milestone_check", milestone.id, passed=report.passed,
                  checks=len(milestone.acceptance), nodes=[n.id for n in members])
        if not report.passed:
            milestone.status = "failed"
            failing = report.failures()[0] if report.failures() else None
            needed = report.render() + (f"\n{failing.evidence[:600]}" if failing and failing.evidence else "")
            result.status = "blocked"
            result.blocked = BlockedReport(
                reason=f"milestone {milestone.id} failed end-to-end verification",
                milestone=milestone.id,
                completed=[n.id for n in plan.nodes.values() if n.status == "done"],
                pending=[n.id for n in plan.nodes.values() if n.status != "done"],
                needed=needed,
            )
            return True

        approved, note = gate(milestone)
        if not approved:
            milestone.status = "blocked"
            result.status = "blocked"
            result.blocked = BlockedReport(
                reason=f"milestone {milestone.id} awaits human approval",
                milestone=milestone.id,
                completed=[n.id for n in plan.nodes.values() if n.status == "done"],
                pending=[n.id for n in plan.nodes.values() if n.status != "done"],
                needed=note or "review the milestone result, then resume",
            )
            return True

        milestone.status = "passed"
        if git is not None:
            checkpoint = git.commit(milestone.id, milestone.goal[:60], detail="milestone gate passed")
            if state is not None:
                state.checkpoints[milestone.id] = checkpoint.sha
        bb.append("milestone_passed", milestone.id,
                  approved_by="auto" if auto_approve else "human")
    return False


def _workspace_files(root: Path) -> list[str]:
    files = []
    for p in Path(root).rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts:
            files.append(p.relative_to(root).as_posix())
    return files


def _persist(state: RunState | None, plan: Plan | None, ledger: Ledger, policy: ReplanPolicy,
             result: RunResult, bb: Blackboard | None, status: str) -> None:
    if state is None or plan is None:
        return
    artifacts = {k: v.to_dict() for k, v in (bb.artifacts.items() if bb else [])}
    state.capture(
        plan,
        usage=ledger.snapshot(),
        policy=policy,
        artifacts=artifacts,
        status=status,
    )
    state.lessons = list(result.lessons_learned)
    state.save()


def run(
    spec: ProblemSpec | None,
    *,
    model: Model,
    workspace: Path | str,
    memory: MemoryStore | None = None,
    memory_system=None,
    judge: Model | None = None,
    human_gate: Callable[[Criterion], tuple[bool, str]] | None = None,
    log_path: Path | str | None = None,
    auto_learn: bool = True,
    run_dir: Path | str | None = None,
    resume: bool = False,
    git: GitWorkspace | None = None,
    allow_dirty: bool = False,
    auto_approve: bool = False,
    milestone_gate: Callable[[Any], tuple[bool, str]] | None = None,
    policy=None,
    sandbox=None,
    router=None,
) -> RunResult:
    started = time.monotonic()
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    result = RunResult(status="blocked", spec=spec)
    active_policy = policy if policy is not None else default_policy()
    approvals = ApprovalStore(workspace / ".planloop" / "approvals.jsonl")
    router = router if router is not None else ModelRouter.single(model)
    if judge is None:
        judge = router.verification_model()

    state: RunState | None = None
    plan: Plan | None = None
    bb: Blackboard | None = None
    ledger: Ledger | None = None

    if resume:
        if run_dir is None:
            raise ValueError("resume requires run_dir")
        run_dir = Path(run_dir)
        state = RunState.load(run_dir / "state.json")
        spec = ProblemSpec.from_dict(state.spec)
        result.spec = spec
        plan = state.restored_plan()
        result.plan = plan
        result.run_id, result.run_dir = state.run_id, str(run_dir)
        ledger = Ledger(spec.budget, dict(spec.limits.allocations))
        ledger.restore(state.usage)
        bb = Blackboard(log_path=state.events_path or (run_dir / "events.jsonl"), fresh=False)
        bb.restore_artifacts(state.artifacts)
        result.lessons_learned = list(state.lessons)
        bb.append("resume", "", run_id=state.run_id, done=sorted(
            n.id for n in plan.nodes.values() if n.status == "done"))
        if git is not None and state.git_branch:
            if git.current_branch() != state.git_branch and git.branch_exists(state.git_branch):
                git.checkout(state.git_branch)
            git.branch = state.git_branch
    else:
        if spec is None:
            raise ValueError("spec is required unless resuming")
        try:
            spec.require_ready()
        except SpecError as exc:
            result.status = "needs_clarification"
            result.blocked = BlockedReport(
                reason=str(exc),
                needed="answer the blocking unknowns before planning",
            )
            result.duration_s = time.monotonic() - started
            return result

        ledger = Ledger(spec.budget, dict(spec.limits.allocations))
        verifier = Verifier(workspace, judge=judge, human_gate=human_gate, ledger=ledger,
                            policy=active_policy, sandbox=sandbox)
        verifier.assert_independent(model)

        try:
            plan = build_plan(spec, model=router.tier("strong"), ledger=ledger)
        except Exception as exc:  # noqa: BLE001
            result.status = "rejected"
            result.blocked = BlockedReport(reason=f"decomposition failed: {exc}", needed="a valid plan")
            result.duration_s = time.monotonic() - started
            return result
        result.plan = plan

        report = compile_plan(spec, plan, memory=memory, workspace_files=_workspace_files(workspace))
        result.compile_report = report
        if not report.ok:
            result.status = "rejected"
            result.blocked = BlockedReport(reason="plan failed compilation", needed=report.render())
            result.usage = ledger.snapshot()
            result.duration_s = time.monotonic() - started
            return result

        if run_dir is not None:
            run_dir = Path(run_dir)
        run_key = run_dir.name if run_dir is not None else new_run_id(spec.goal)
        if git is not None:
            git.start(run_key, allow_dirty=allow_dirty)
        if run_dir is not None:
            run_dir.mkdir(parents=True, exist_ok=True)
            state = RunState.create(run_dir, spec, plan, workspace)
            if git is not None:
                state.git_branch = git.branch or ""
                state.save()
            result.run_id, result.run_dir = state.run_id, str(run_dir)
            log_path = state.events_path
        bb = Blackboard(log_path=log_path)
        bb.append("plan", "", nodes=list(plan.nodes))
        bb.append("compile", "", ok=report.ok, issues=[i.to_dict() for i in report.issues])

    assert spec is not None and plan is not None and ledger is not None and bb is not None
    if git is not None and resume:
        bb.append("git", "", branch=git.branch, head=git.head()[:10])
    verifier = Verifier(workspace, judge=judge, human_gate=human_gate, ledger=ledger,
                        policy=active_policy, sandbox=sandbox)
    verifier.assert_independent(model)

    compressor = Compressor(model, ledger=ledger)
    executor = Executor(
        workspace,
        model=model,
        ledger=ledger,
        blackboard=bb,
        verifier=verifier,
        limits=spec.limits,
        memory=memory,
        memory_system=memory_system,
        compressor=compressor,
        policy=active_policy,
        approvals=approvals,
        run_id=result.run_id,
        sandbox=sandbox,
        router=router,
        context_files=spec.context_files,
    )
    policy = ReplanPolicy(
        max_attempts=spec.limits.max_attempts_per_node,
        max_replans_per_subtree=spec.limits.max_replans_per_subtree,
        max_global_replans=spec.limits.max_global_replans,
    )
    if state is not None and state.policy:
        policy.global_replans = int(state.policy.get("global_replans", 0))
        policy.subtree_counts = dict(state.policy.get("subtree_counts", {}))

    guidance: dict[str, str] = {}
    harvested = 0
    gate = milestone_gate or (lambda ms: (auto_approve, "" if auto_approve else "ask a human to review"))
    while not plan.is_complete():
        if _check_milestones(
            plan, verifier, bb,
            git=git, state=state, result=result,
            auto_approve=auto_approve, gate=gate,
            budget_left=ledger.remaining_tokens,
        ):
            _persist(state, plan, ledger, policy, result, bb, "blocked")
            break
        if ledger.exhausted():
            result.status = "blocked"
            result.blocked = BlockedReport(
                reason=ledger.exhausted() or "budget exhausted",
                completed=[n.id for n in plan.nodes.values() if n.status == "done"],
                pending=[n.id for n in plan.nodes.values() if n.status != "done"],
                needed="raise the budget or narrow the goal",
            )
            break
        ready = plan.ready()
        if not ready:
            result.status = "blocked"
            result.blocked = BlockedReport(
                reason="no runnable node (dependency deadlock)",
                pending=[n.id for n in plan.nodes.values() if n.status != "done"],
            )
            break

        node = plan.get(ready[0])
        node.attempts += 1
        node.status = "running"
        bb.append("node_start", node.id, attempt=node.attempts)
        _persist(state, plan, ledger, policy, result, bb, "running")
        base_head = git.head() if git is not None else ""
        try:
            outcome = executor.run(node, plan, extra_context=guidance.pop(node.id, ""))
        except NeedsApproval as exc:
            _record_interrupted(result, exc)
            node.status = "pending"
            result.status = "blocked"
            result.blocked = BlockedReport(
                reason=f"high-risk action needs approval: {exc.assessment.reason}",
                node_id=node.id,
                completed=[n.id for n in plan.nodes.values() if n.status == "done"],
                pending=[n.id for n in plan.nodes.values() if n.status != "done"],
                needed=(
                    f"命令: {exc.assessment.target}\n"
                    f"批准: planloop approvals approve {exc.token} -w {workspace}\n"
                    f"然后: planloop run --resume -w {workspace}"
                ),
            )
            bb.append("approval_requested", node.id, token=exc.token, rule=exc.assessment.rule,
                      target=exc.assessment.target)
            _persist(state, plan, ledger, policy, result, bb, "blocked")
            break
        except DeniedAction as exc:
            _record_interrupted(result, exc)
            node.status = "failed"
            result.status = "blocked"
            result.blocked = BlockedReport(
                reason=f"action refused by policy ({exc.assessment.rule}): {exc.assessment.reason}",
                node_id=node.id,
                completed=[n.id for n in plan.nodes.values() if n.status == "done"],
                pending=[n.id for n in plan.nodes.values() if n.status != "done"],
                needed=(
                    f"命令: {exc.assessment.target}\n"
                    "这是不可批准的操作；请改任务目标或由人手工执行。"
                ),
            )
            bb.append("action_denied", node.id, rule=exc.assessment.rule, target=exc.assessment.target)
            _persist(state, plan, ledger, policy, result, bb, "blocked")
            break
        except BudgetExceeded as exc:
            _record_interrupted(result, exc)
            node.status = "pending"
            result.status = "blocked"
            result.blocked = BlockedReport(reason=str(exc), node_id=node.id, needed="raise the budget")
            result.usage = exc.snapshot
            _persist(state, plan, ledger, policy, result, bb, "blocked")
            break
        result.outcomes.append(outcome)

        if outcome.status == "done":
            node.status = "done"
            bb.record_artifact(node.id, f"{node.goal} -> {outcome.summary}", {"files": outcome.files})
            if memory_system is not None and harvested < FACTS_PER_RUN:
                learned = _harvest_facts(memory_system, node, outcome, workspace,
                                         FACTS_PER_RUN - harvested, active_policy)
                if learned:
                    harvested += learned
                    bb.append("facts_harvested", node.id, count=learned, total=harvested)
            if git is not None:
                checkpoint = git.commit(
                    node.id,
                    node.goal[:60],
                    detail=f"run: {result.run_id or '-'}\nacceptance: {len(node.acceptance)} criteria",
                )
                if state is not None:
                    state.checkpoints[node.id] = checkpoint.sha
                bb.append("checkpoint", node.id, sha=checkpoint.sha[:10])
            if memory is not None:
                _account_guards(memory, node, outcome, bb)
                if auto_learn and outcome.failures:
                    first = outcome.failures[0]
                    if first["signature"] in memory.lessons:
                        bb.append("lesson_recurred", node.id, id=first["signature"])
                    else:
                        lesson = propose_lesson(
                            model,
                            node_goal=node.goal,
                            failure_text=first["text"],
                            fix_text="commands: " + " ; ".join(outcome.commands[-4:])
                            + "\nsummary: " + outcome.summary
                            + _fix_excerpt(workspace, outcome.files),
                            workspace=workspace,
                            ledger=ledger,
                            verifier=verifier,
                        )
                        if lesson is not None:
                            stored = memory.add(lesson)
                            result.lessons_learned.append(stored.id)
                            bb.append("lesson_learned", node.id, id=stored.id,
                                      strength=stored.strength, summary=stored.summary)
            _persist(state, plan, ledger, policy, result, bb, "running")
            continue

        node.status = "pending"
        if git is not None:
            # keep the evidence, restore the last green state
            git.stash(f"planloop {node.id} attempt {node.attempts} failed")
            if base_head:
                git.reset(base_head)
            bb.append("rollback", node.id, to=base_head[:10] if base_head else "", attempt=node.attempts)
        klass = classify(outcome.verification, error=outcome.last_error)
        decision = policy.decide(node.id, klass, node.attempts, detail=outcome.last_error)
        bb.append("replan", node.id, klass=klass.value, action=decision.action, reason=decision.reason)
        if decision.action in {"retry", "repair", "probe"}:
            guidance[node.id] = (
                f"# Correction from the supervisor\n{decision.reason}\n{decision.extra_context}\n"
            )
            _persist(state, plan, ledger, policy, result, bb, "running")
            continue
        result.status = "blocked"
        node.status = "failed"
        result.blocked = BlockedReport(
            reason=decision.reason,
            node_id=node.id,
            completed=[n.id for n in plan.nodes.values() if n.status == "done"],
            pending=[n.id for n in plan.nodes.values() if n.status != "done"],
            needed="human decision: " + decision.reason if decision.action == "ask" else "widen budget or rescope",
        )
        _persist(state, plan, ledger, policy, result, bb, "blocked")
        break

    if plan.is_complete() and result.blocked is None:
        # the loop exits as soon as the last node finishes, so the final
        # milestone must be checked here as well as at the top of each pass
        if _check_milestones(
            plan, verifier, bb,
            git=git, state=state, result=result,
            auto_approve=auto_approve, gate=gate,
            budget_left=ledger.remaining_tokens,
        ):
            pass
        else:
            evidence = "\n\n".join(
                f"{o.node_id}: {o.summary}\n{o.evidence[-400:]}" for o in result.outcomes if o.status == "done"
            )
            final = verifier.check(spec.success_criteria, evidence=evidence, budget_left=ledger.remaining_tokens)
            result.verification = final
            if final.passed:
                result.status = "succeeded"
            else:
                result.status = "blocked"
                result.blocked = BlockedReport(
                    reason="end-to-end acceptance failed",
                    completed=[n.id for n in plan.nodes.values() if n.status == "done"],
                    needed=final.render(),
                )

    result.usage = ledger.snapshot()
    result.routing = router.summary()
    _persist(state, plan, ledger, policy, result, bb, result.status)
    if memory is not None:
        result.memory_summary = memory.summary()
        memory.save()
    if memory_system is not None:
        _record_episode(memory_system, spec, plan, result)
    result.duration_s = time.monotonic() - started
    bb.append("run_end", "", status=result.status)
    if run_dir is not None:
        try:
            from planloop.audit import write_report

            write_report(run_dir, git=git)
        except Exception as exc:  # noqa: BLE001 - an audit failure must not fail the run
            bb.append("audit_error", "", error=str(exc)[:200])
    return result

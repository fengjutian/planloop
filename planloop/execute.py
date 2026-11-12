"""The execution inner loop: one node, a few tools, a hard step cap.

Each step the model sees a *slice* (this node, upstream summaries, its own
failure history, applicable lessons), never the whole transcript.  Deterministic
acceptance is re-checked after every action, so the loop can stop as soon as
the work is actually done rather than when the model feels finished.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from planloop.blackboard import Blackboard
from planloop.budget import BudgetExceeded, Ledger
from planloop.decomp import Plan, TaskNode
from planloop.memory import MemoryContext, MemoryStore, signature
from planloop.model import Model, extract_json
from planloop.spec import Limits
from planloop.tools import Observation, ToolError, Tools
from planloop.verify import VerificationReport, Verifier

ACT_PROMPT = """task=act
{context}
{memory_block}
{extra}# Tools
Reply with exactly one JSON object, no prose:
{{"action": "run",  "cmd": "<shell command>"}}
{{"action": "write","path": "<file>", "content": "<full file content>"}}
{{action: write_many,files: {{<file>: <content>, <file2>: <content>}}}}
{{"action": "read", "path": "<file>"}}
{{"action": "finish","summary": "<what you did, <=2 sentences>"}}
Work in the workspace root. Prefer small, verifiable steps.
"""


@dataclass
class NodeOutcome:
    node_id: str
    status: str = "failed"  # done | failed
    verification: VerificationReport | None = None
    steps: int = 0
    #: the step at which the acceptance criteria were first satisfied (0 =
    #: never).  ``steps`` counts every step the loop ran, including the ones
    #: a model spends re-verifying work it has already done.
    steps_to_pass: int = 0
    #: the tokens charged up to that same step (0 = never).  The tokens
    #: charged afterwards are the model confirming what it already did.
    tokens_to_pass: int = 0
    commands: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    summary: str = ""
    evidence: str = ""
    failures: list[dict[str, Any]] = field(default_factory=list)
    #: how many acceptance failures were **recorded** -- one per distinct
    #: failure signature, which is exactly how many ``failure`` events the
    #: event log carries.  ``failures`` above is the same list with its
    #: details.  The published report quotes this number so that it can be
    #: re-derived from the raw artifacts.
    l1_failures: int = 0
    last_error: str = ""
    tokens: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node_id,
            "status": self.status,
            "steps": self.steps,
            "steps_to_pass": self.steps_to_pass,
            "tokens_to_pass": self.tokens_to_pass,
            "summary": self.summary,
            "failures": [{"signature": f["signature"], "text": f["text"][:300]} for f in self.failures],
            "verification": self.verification.to_dict() if self.verification else None,
        }


class Executor:
    def __init__(
        self,
        workspace,
        *,
        model: Model,
        ledger: Ledger,
        blackboard: Blackboard,
        verifier: Verifier,
        limits: Limits,
        memory: MemoryStore | None = None,
        memory_system=None,
        compressor=None,
        policy=None,
        approvals=None,
        run_id: str = "",
        sandbox=None,
        router=None,
        context_files=(),
    ):
        self.workspace = workspace
        self.model = model
        self.ledger = ledger
        self.bb = blackboard
        self.verifier = verifier
        self.limits = limits
        self.memory = memory
        self.memory_system = memory_system
        self.compressor = compressor
        self.run_id = run_id
        from planloop.routing import ModelRouter

        self.router = router if router is not None else ModelRouter.single(model)
        self._tier_by_node: dict[str, str] = {}
        self._file_snapshots: dict[str, dict[str, str]] = {}
        self.context_files = list(context_files)
        self.tools = Tools(
            workspace, ledger=ledger, phase="execute",
            policy=policy, approvals=approvals, run_id=run_id, sandbox=sandbox,
        )

    # -- helpers ----------------------------------------------------------
    def _memory_block(self, node: TaskNode) -> tuple[str, list[str]]:
        sections: list[str] = []
        lesson_ids: list[str] = []
        if self.memory is not None:
            ctx = MemoryContext.from_workspace(f"{node.goal}", self.workspace, commands=[])
            hits = self.memory.match(ctx, limit=3)
            if hits:
                lines = ["# Lessons from your past mistakes (apply them, do not rediscover)"]
                for lesson, why in hits:
                    lines.append(f"- ({why}) {lesson.advice()}")
                sections.append("\n".join(lines))
                lesson_ids = [lesson.id for lesson, _ in hits]
        if self.memory_system is not None:
            block, hit_ids = self.memory_system.context_block(
                node.goal, kinds=("fact", "procedure"), limit=3, max_chars=500
            )
            if block:
                sections.append("# Repository facts (verified earlier)\n" + block)
                self.memory_system.public.record_use(hit_ids)
                self.memory_system.private.record_use(hit_ids)
        if not sections:
            return "", []
        return "\n\n".join(sections) + "\n", lesson_ids

    def _files_block(self, node: TaskNode, *, limit: int = 8000) -> str:
        """Current contents of the files the task declared as context.

        Read every step on purpose: a file the node just wrote must not appear
        stale on the next turn.
        """
        chunks: list[str] = []
        used = 0
        wanted = node.context_files or self.context_files
        for rel in wanted:
            target = (self.workspace / rel) if not Path(rel).is_absolute() else Path(rel)
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if used + len(text) > limit:
                text = text[: max(0, limit - used)] + "\n... [truncated]"
            used += len(text)
            chunks.append(f"--- {rel} ---\n{text}")
            if used >= limit:
                break
        return "\n".join(chunks)

    def _prompt(self, node: TaskNode, plan: Plan, extra: str, memory_block: str, *, step: int = 0) -> str:
        """Stable content first, volatile content last.

        The provider caches prompt prefixes (measured: ~100% of a repeated
        prefix).  Anything that changes every step -- observations, corrections,
        memory hits -- therefore has to sit at the *end*, or it would invalidate
        the cache for the files that follow it.
        """
        stable = self.bb.context_for(
            node_id=node.id,
            goal=plan.goal,
            node_goal=node.goal,
            deps=node.deps,
            contracts=node.contracts_text(),
            acceptance=node.acceptance_text(),
            compressor=self.compressor,
            include_observations=False,
        )
        # measured: sending files only once raises cache hits (33% -> ~50%) but
        # breaks correctness -- the model does not re-read and writes stale code
        files = self._files_block(node)
        if files:
            stable += "\n\n# Relevant files (current contents)\n" + files
        volatile = self._observations_block(node)
        return ACT_PROMPT.format(
            context=stable,
            memory_block=volatile + memory_block,
            extra=extra or "",
        )

    def _observations_block(self, node: TaskNode) -> str:
        obs = self.bb.observations.get(node.id, [])[-2:]
        lines = [f"> {o[-400:]}" for o in obs]
        # the last acceptance failure is pinned: the model's own actions push
        # the observation window along, and a model that cannot see *why* it
        # failed has nothing to act on.  (This did not cause the 23-read
        # loop -- the failure was still in the window there -- it is a
        # guarantee that it always is.)
        rejection = self.bb.rejections.get(node.id, "")
        if rejection and not any(rejection[-200:] in o for o in obs):
            lines.append(f"> {rejection[-600:]}")
        if not lines:
            return ""
        return "# Recent observations\n" + "\n".join(lines) + "\n\n"

    def _l1(self, node: TaskNode, evidence: str) -> VerificationReport | None:
        if not any(c.layer == "L1" for c in node.acceptance):
            return None
        return self.verifier.l1_only(node.acceptance, evidence=evidence)

    # -- main -------------------------------------------------------------
    def run(self, node: TaskNode, plan: Plan, *, extra_context: str = "") -> NodeOutcome:
        outcome = NodeOutcome(node_id=node.id)
        self.tools.node_id = node.id
        memory_block, lesson_ids = self._memory_block(node)
        if lesson_ids and self.memory is not None:
            self.memory.record_application(lesson_ids)
            self.bb.append("memory_hit", node.id, lessons=lesson_ids)

        seen_signatures: set[str] = set()
        observations: list[str] = []
        finish_summary = ""
        finished = False

        for step in range(self.limits.max_steps_per_node):
            outcome.steps = step + 1
            if self.ledger.exhausted():
                outcome.last_error = self.ledger.exhausted() or "budget"
                break

            prompt = self._prompt(node, plan, extra_context, memory_block, step=step)
            try:
                comp, decision = self.router.complete(
                    prompt,
                    attempt=node.attempts - 1,
                    est_cost=node.est_cost,
                    risk=node.risk,
                    lesson_hit=bool(lesson_ids),
                    max_tokens=max(1200, 600 * max(1, len(node.context_files or self.context_files))),
                )
            except Exception as exc:  # noqa: BLE001 - a model failure is a step failure
                outcome.last_error = f"model error: {exc}"
                break
            if self._tier_by_node.get(node.id) != decision.tier:
                self._tier_by_node[node.id] = decision.tier
                self.bb.append("route", node.id, tier=decision.tier, reason=decision.reason)
            self.ledger.charge("execute", tokens=comp.tokens, note=f"act:{node.id}")
            outcome.tokens += comp.tokens

            try:
                action = extract_json(comp.text)
            except ValueError:
                observations.append("Your reply was not a JSON object. Reply with exactly one JSON object.")
                self.bb.record_observation(node.id, observations[-1])
                self.bb.append("invalid_action", node.id, raw=comp.text[:300])
                continue

            kind = str(action.get("action", "")).lower()
            obs: Observation
            #: what the action touched -- the command, the path, the paths.
            #: The observation only shows the *result*, so without this the
            #: event log cannot say "the model read the contract file" and a
            #: benchmark cannot tell "the memory changed the behaviour" from
            #: "the memory changed nothing".
            target = ""
            try:
                if kind == "run":
                    cmd = str(action.get("cmd", "")).strip()
                    target = cmd[:200]
                    if not cmd:
                        obs = Observation("run", False, 2, "", "empty command")
                    else:
                        outcome.commands.append(cmd)
                        obs = self.tools.run(cmd)
                elif kind == "write":
                    path = str(action.get("path", "")).strip()
                    target = path[:200]
                    obs = self.tools.write(path, str(action.get("content", "")))
                    if obs.ok:
                        outcome.files.append(path)
                elif kind == "write_many":
                    files = action.get("files") or {}
                    target = ", ".join(sorted(str(p) for p in files))[:200]
                    written, failed = [], []
                    for path, content in files.items():
                        single = self.tools.write(str(path), str(content))
                        if single.ok:
                            written.append(str(path))
                            outcome.files.append(str(path))
                        else:
                            failed.append(f"{path}: {single.stderr[:80]}")
                    obs = Observation(
                        "write_many", not failed, 0 if not failed else 2,
                        f"wrote {len(written)} files",
                        "; ".join(failed),
                    )
                elif kind == "read":
                    path = str(action.get("path", ""))
                    target = path[:200]
                    obs = self.tools.read(path)
                elif kind == "finish":
                    finish_summary = str(action.get("summary", "")).strip()
                    obs = Observation("finish", True, 0, "finishing; running full acceptance")
                    finished = True
                else:
                    obs = Observation("noop", False, 2, "", f"unknown action {kind!r}")
            except ToolError as exc:
                # A refused action (path outside the workspace, unreadable file)
                # is ordinary agent behaviour, not a fatal error: report it and
                # let the model correct itself. Escaping here killed the whole
                # run -- one stray action could throw away a long task.
                obs = Observation(kind or "action", False, 2, "", str(exc))

            self.bb.record_observation(node.id, obs.render())
            self.bb.append("action", node.id, action=kind, target=target,
                           detail=obs.render()[:600])
            observations.append(obs.render())
            outcome.evidence = obs.render()

            if bool(node.acceptance) and any(c.layer == "L1" for c in node.acceptance):
                report = self._l1(node, obs.render())
                if report is not None and not report.passed:
                    failing = report.failures()[0]
                    text = (failing.detail + "\n" + failing.evidence).strip() or obs.render()
                    observations.append("acceptance failed:\n" + text)
                    self.bb.record_observation(node.id, text)
                    # pin it: the model's own actions would otherwise push the
                    # failure out of the observation window within two steps
                    self.bb.record_rejection(node.id, "acceptance failed:\n" + text)
                    sig, norm = signature(text)
                    if finished or sig not in seen_signatures:
                        seen_signatures.add(sig)
                        repeat = False
                        if self.memory is not None:
                            repeat = self.memory.note_failure(text)
                        guard_hits = [v for v in report.failures() if v.criterion_id.startswith("M_")]
                        own_fails = [v for v in report.failures() if not v.criterion_id.startswith("M_")]
                        outcome.failures.append(
                            {
                                "text": text,
                                "signature": sig,
                                "normalized": norm,
                                "repeat": repeat,
                                "step": step,
                                "guard": bool(guard_hits),
                                "guard_only": bool(guard_hits) and not own_fails,
                                "lesson": guard_hits[0].criterion_id[2:] if guard_hits else "",
                            }
                        )
                        self.bb.record_failure(node.id, f"{sig}: {norm[:160]}")
                        # the same number the log records: one failure event
                        # per distinct signature (a repeated failure is not
                        # counted twice) -- so a report that quotes it can be
                        # re-derived from the event log
                        outcome.l1_failures += 1
                elif report is not None and report.passed:
                    # the work is accepted: the pinned rejection is stale now
                    self.bb.clear_rejection(node.id)
                    # The acceptance is satisfied -- this is the first step at
                    # which the work counts as done.  Recorded separately from
                    # ``steps`` because the loop keeps running until the model
                    # announces completion: a real model tends to re-run the
                    # same suite afterwards, and counting those turns would
                    # hide how much work the task actually took.
                    if outcome.steps_to_pass == 0:
                        outcome.steps_to_pass = step + 1
                        outcome.tokens_to_pass = int(outcome.tokens)
                    if finished:
                        break

            if finished:
                break

        evidence = "\n\n".join(
            [finish_summary] + ([outcome.evidence] if outcome.evidence else []) + outcome.files
        ).strip()
        report = self.verifier.check(
            node.acceptance,
            evidence=evidence or finish_summary,
            node_id=node.id,
            budget_left=self.ledger.remaining_tokens,
        )
        outcome.verification = report
        outcome.summary = finish_summary or (f"edited {', '.join(outcome.files)}" if outcome.files else "no summary")
        if report.passed:
            outcome.status = "done"
        else:
            outcome.status = "failed"
            outcome.last_error = report.render()

        # a guard that fails while the node's own criteria pass is a false alarm
        own = [c for c in node.acceptance if not c.id.startswith("M_")]
        mem_crits = [c for c in node.acceptance if c.id.startswith("M_")]
        if mem_crits and self.memory is not None:
            own_report = self.verifier.check(own, evidence=evidence, node_id=node.id) if own else None
            if own_report is not None and own_report.passed and not report.passed:
                for crit in mem_crits:
                    self.memory.record_guard_false_positive(crit.id[2:])

        self.bb.append(
            "node_end",
            node.id,
            status=outcome.status,
            steps=outcome.steps,
            tokens=round(outcome.tokens),
        )
        return outcome

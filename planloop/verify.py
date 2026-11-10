"""Three-layer verification.

L1 deterministic  -- tests, exit codes, file contents, JSON shape.  Cheap and
                     trustworthy; always preferred.
L2 model judge    -- rubrics only, and *only* with a quoted piece of evidence
                     from the artifact.  No quote, no pass: an ungrounded
                     "looks good" is the main source of false positives.
L3 human gate     -- irreversible actions, budget overruns, repeated failure.

The executor never grades itself: the scheduler refuses to share one model
instance between the two roles.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from planloop.model import Model, extract_json
from planloop.policy import DeniedAction, NeedsApproval, default_policy
from planloop.spec import Criterion
from planloop.tools import Tools

JUDGE_PROMPT = """task=judge
You are an independent verifier. You did not perform this work and you must
not assume it was done correctly.

Criterion ({cid}):
{spec}

Artifact summary / evidence:
---
{evidence}
---

Decide whether the criterion is satisfied. You may only judge what the evidence
shows. Reply with a single JSON object:
{{"pass": true|false, "quote": "<exact substring copied from the evidence>", "reason": "<one sentence>"}}
An empty quote means the criterion fails: ungrounded approval is invalid.
"""


@dataclass
class Verdict:
    criterion_id: str
    layer: str
    passed: bool
    detail: str
    evidence: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion_id,
            "layer": self.layer,
            "passed": self.passed,
            "reason": self.reason,
            "detail": self.detail[:600],
            "evidence": self.evidence[:300],
        }


@dataclass
class VerificationReport:
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.verdicts) and all(v.passed for v in self.verdicts)

    def failures(self, layer: str | None = None) -> list[Verdict]:
        return [v for v in self.verdicts if not v.passed and (layer is None or v.layer == layer)]

    def reasons(self) -> list[str]:
        return [v.reason for v in self.failures()]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "verdicts": [v.to_dict() for v in self.verdicts]}

    def render(self) -> str:
        lines = []
        for v in self.verdicts:
            mark = "PASS" if v.passed else "FAIL"
            lines.append(f"[{mark}] {v.layer} {v.criterion_id}: {v.detail[:200]}")
        return "\n".join(lines) or "(no criteria)"


def parse_must(must: str) -> dict[str, tuple[str, str]]:
    """``exit=0 stdout~=ok contains="a b"`` -> {exit:(=,0), stdout:(~=,ok)}."""
    out: dict[str, tuple[str, str]] = {}
    for m in re.finditer(r'(\w+)(~?=)("([^"]*)"|\S+)', must):
        key, op = m.group(1), m.group(2)
        val = m.group(4) if m.group(4) is not None else m.group(3)
        out[key] = (op, val)
    return out


class Verifier:
    def __init__(
        self,
        workspace: Path | str,
        *,
        judge: Model | None = None,
        human_gate: Callable[[Criterion], tuple[bool, str]] | None = None,
        ledger=None,
        clock=None,
        policy=None,
        sandbox=None,
    ):
        self.workspace = Path(workspace).resolve()
        self.judge = judge
        self.human_gate = human_gate or (lambda c: (False, "no human gate configured"))
        self.ledger = ledger
        self.clock = clock
        self.policy = policy if policy is not None else default_policy()
        self._tools = Tools(self.workspace, ledger=ledger, phase="verify", policy=self.policy, sandbox=sandbox)

    # -- independence -----------------------------------------------------
    def assert_independent(self, executor_model: Model | None) -> None:
        if executor_model is not None and self.judge is not None and self.judge is executor_model:
            raise ValueError(
                "verifier shares the executor's model instance: an executor must not grade itself"
            )

    # -- public -----------------------------------------------------------
    def check(
        self,
        criteria: Iterable[Criterion],
        *,
        evidence: str = "",
        node_id: str = "",
        budget_left: float | None = None,
    ) -> VerificationReport:
        report = VerificationReport()
        for crit in criteria:
            if budget_left is not None and budget_left <= 0:
                report.verdicts.append(
                    Verdict(crit.id, crit.layer, False, "budget exhausted before verification", reason="budget")
                )
                continue
            if crit.layer == "L1":
                report.verdicts.append(self._check_l1(crit))
            elif crit.layer == "L2":
                report.verdicts.append(self._check_l2(crit, evidence))
            else:
                report.verdicts.append(self._check_l3(crit))
        return report

    def l1_only(self, criteria: Iterable[Criterion], *, evidence: str = "") -> VerificationReport:
        """Cheap gate used inside the ReAct loop (no model calls)."""
        report = VerificationReport()
        for crit in criteria:
            if crit.layer == "L1":
                report.verdicts.append(self._check_l1(crit))
        return report

    # -- layers -----------------------------------------------------------
    def _check_l1(self, crit: Criterion) -> Verdict:
        if crit.kind == "command":
            try:
                obs = self._tools.run(crit.spec)
            except (DeniedAction, NeedsApproval) as exc:
                needs = isinstance(exc, NeedsApproval)
                return Verdict(
                    crit.id, "L1", False,
                    f"policy blocked this check: {exc.assessment.reason}",
                    reason="needs_approval" if needs else "denied_by_policy",
                )
            combined = obs.stdout + "\n" + obs.stderr
            wants = parse_must(crit.must or "exit=0")
            ok = True
            notes = []
            for key, (op, val) in wants.items():
                if key == "exit":
                    good = obs.exit_code == int(val)
                    notes.append(f"exit={obs.exit_code} (want {val})")
                elif key in {"stdout", "stderr"}:
                    hay = obs.stdout if key == "stdout" else obs.stderr
                    good = bool(re.search(val, hay)) if op == "~=" else (val in hay)
                    notes.append(f"{key} {op} {val!r}: {good}")
                elif key == "contains":
                    good = val in combined
                    notes.append(f"contains {val!r}: {good}")
                else:
                    good = False
                    notes.append(f"unknown must-key {key}")
                ok = ok and good
            return Verdict(
                crit.id, "L1", ok, "; ".join(notes) or f"exit={obs.exit_code}",
                evidence=combined[-1500:], reason="" if ok else "command_failed",
            )

        if crit.kind == "file_exists":
            ok = self._tools.exists(crit.spec)
            return Verdict(crit.id, "L1", ok, f"{crit.spec} exists={ok}", reason="" if ok else "file_missing")

        if crit.kind == "file_contains":
            obs = self._tools.read(crit.spec)
            ok = obs.ok and crit.must in obs.stdout
            return Verdict(
                crit.id, "L1", ok, f"{crit.spec} contains {crit.must!r}={ok}",
                evidence=obs.stdout[:300], reason="" if ok else "content_mismatch",
            )

        if crit.kind == "json_has_keys":
            obs = self._tools.read(crit.spec)
            if not obs.ok:
                return Verdict(crit.id, "L1", False, obs.stderr, reason="file_missing")
            try:
                data = json.loads(obs.stdout)
            except json.JSONDecodeError as exc:
                return Verdict(crit.id, "L1", False, f"invalid JSON: {exc}", reason="invalid_json")
            keys = [k.strip() for k in crit.must.split(",") if k.strip()]
            missing = [k for k in keys if not isinstance(data, dict) or k not in data]
            return Verdict(
                crit.id, "L1", not missing,
                "missing keys: " + ",".join(missing) if missing else "all keys present",
                reason="" if not missing else "schema_mismatch",
            )

        return Verdict(crit.id, "L1", False, f"unsupported L1 kind {crit.kind}", reason="unsupported")

    def _check_l2(self, crit: Criterion, evidence: str) -> Verdict:
        if self.judge is None:
            return Verdict(crit.id, "L2", False, "no judge model configured", reason="judge_missing")
        if not evidence.strip():
            return Verdict(crit.id, "L2", False, "no evidence supplied to judge", reason="no_evidence")
        prompt = JUDGE_PROMPT.format(cid=crit.id, spec=crit.spec, evidence=evidence[:4000])
        try:
            comp = self.judge.complete(prompt, max_tokens=300)
        except Exception as exc:  # noqa: BLE001 - a judge failure must not pass
            return Verdict(crit.id, "L2", False, f"judge error: {exc}", reason="judge_error")
        if self.ledger is not None:
            self.ledger.charge("verify", tokens=comp.tokens, note=f"judge:{crit.id}")
        try:
            data = extract_json(comp.text)
        except ValueError:
            return Verdict(crit.id, "L2", False, "judge reply was not JSON", reason="judge_unparsable")
        quote = str(data.get("quote") or "").strip()
        passed = bool(data.get("pass")) and bool(quote)
        reason = str(data.get("reason") or "")[:300]
        if data.get("pass") and not quote:
            return Verdict(crit.id, "L2", False, "judge approved without quoting evidence", reason="judge_no_quote")
        if quote and quote not in evidence:
            return Verdict(crit.id, "L2", False, f"judge quoted text not present in evidence: {quote[:80]!r}",
                           reason="judge_quote_not_found")
        return Verdict(crit.id, "L2", passed, reason or ("ok" if passed else "rubric_failed"),
                       evidence=quote[:300], reason="" if passed else "rubric_failed")

    def _check_l3(self, crit: Criterion) -> Verdict:
        approved, note = self.human_gate(crit)
        return Verdict(
            crit.id, "L3", approved, note or ("approved" if approved else "not approved"),
            reason="" if approved else "human_denied",
        )

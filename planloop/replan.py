"""Failure classification and the decision to retry, repair or stop.

Blind retry is how agents burn budget; blind replanning is how they oscillate.
The classifier maps an observed failure onto one of five causes, and each cause
has exactly one allowed response plus a hard cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from planloop.verify import VerificationReport


class FailureClass(str, Enum):
    EXECUTION = "execution"      # the step was wrong, the plan is fine
    PLAN = "plan"                # missing dependency / broken interface
    ASSUMPTION = "assumption"    # a probe finding contradicts a premise
    SPEC_GAP = "spec_gap"        # acceptance is ambiguous or undecidable
    ENVIRONMENT = "environment"  # the world moved under us
    BUDGET = "budget"            # cannot afford to continue


CLASSIFIERS: list[tuple[str, FailureClass]] = [
    ("budget", FailureClass.BUDGET),
    ("human_denied", FailureClass.SPEC_GAP),
    ("no_evidence", FailureClass.SPEC_GAP),
    ("rubric_failed", FailureClass.ASSUMPTION),
    ("judge_missing", FailureClass.SPEC_GAP),
    ("judge_error", FailureClass.ENVIRONMENT),
    ("needs_approval", FailureClass.SPEC_GAP),
    ("denied_by_policy", FailureClass.SPEC_GAP),
    # a missing deliverable is "the step did not do its job", not a wiring bug
    ("file_missing", FailureClass.EXECUTION),
    ("invalid_json", FailureClass.EXECUTION),
    ("schema_mismatch", FailureClass.EXECUTION),
    ("content_mismatch", FailureClass.EXECUTION),
    ("command_failed", FailureClass.EXECUTION),
]


def classify(report: VerificationReport | None, *, error: str = "") -> FailureClass:
    if report is not None:
        reasons = report.reasons()
        for reason in reasons:
            for prefix, klass in CLASSIFIERS:
                if reason.startswith(prefix):
                    return klass
        if reasons:
            return FailureClass.EXECUTION
    if "timed out" in error or "no such file" in error:
        return FailureClass.ENVIRONMENT
    return FailureClass.EXECUTION


@dataclass
class Decision:
    action: str          # retry | repair | probe | ask | stop
    reason: str
    klass: FailureClass
    extra_context: str = ""


@dataclass
class ReplanPolicy:
    max_attempts: int = 2
    max_replans_per_subtree: int = 3
    max_global_replans: int = 2
    subtree_counts: dict[str, int] = field(default_factory=dict)
    global_replans: int = 0

    def decide(
        self,
        node_id: str,
        klass: FailureClass,
        attempt: int,
        *,
        detail: str = "",
    ) -> Decision:
        if klass is FailureClass.BUDGET:
            return Decision("stop", "budget exhausted", klass)

        if klass is FailureClass.SPEC_GAP:
            return Decision("ask", f"acceptance cannot be decided: {detail[:200]}", klass)

        # every class is bounded by the per-node attempt cap; a "repair" is not a
        # license to retry forever
        if attempt > self.max_attempts:
            return Decision(
                "stop",
                f"{klass.value} failure persisted over {attempt} attempts (cap {self.max_attempts})",
                klass,
            )

        if klass is FailureClass.PLAN:
            used = self.subtree_counts.get(node_id, 0)
            if used >= self.max_replans_per_subtree:
                return Decision("stop", f"subtree {node_id} replanned {used} times", klass)
            if self.global_replans >= self.max_global_replans:
                return Decision("stop", "global replan cap reached", klass)
            self.subtree_counts[node_id] = used + 1
            self.global_replans += 1
            return Decision("repair", f"interface/dependency problem: {detail[:200]}", klass)

        if klass is FailureClass.ASSUMPTION:
            return Decision("probe", "a premise was falsified; re-probe before continuing", klass)

        if klass is FailureClass.ENVIRONMENT:
            return Decision("retry", "environment changed; re-observe then retry", klass,
                            extra_context="The environment changed since you last looked. Re-read the "
                                          "relevant files/commands before acting.")

        if attempt >= self.max_attempts:
            return Decision("stop", f"{klass.value} failure persisted over {attempt} attempts", klass)
        return Decision(
            "retry",
            f"{klass.value} failure (attempt {attempt}/{self.max_attempts})",
            klass,
            extra_context=(
                "Your previous approach failed. Do not repeat it: change the approach, "
                "re-read the failing output, and fix the root cause."
            ),
        )

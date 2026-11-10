"""Model tier routing: the question decides which tier answers it.

The decision is deterministic and explainable.  It uses only signals the system
actually owns:

* the prompt tag (``task=plan|act|judge|lesson``),
* the node's cost estimate and risk (from the plan),
* how many attempts this node has already had,
* whether a lesson matched (this class of work has burned us before).

It deliberately does **not** ask a model "how hard is this?" -- self-assessed
difficulty is unauditable and drifts between runs.

Costs are weighted, because token counts alone hide a 10-30x price difference:

    cost_units = sum(tokens_tier * weight_tier)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from planloop.model import Completion, Model, task_tag

DEFAULT_WEIGHTS = {"cheap": 0.2, "standard": 1.0, "strong": 3.0, "verify": 1.0}
TIER_ORDER = ("cheap", "standard", "strong")


class QuestionKind(str, Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    LESSON = "lesson"


TAG_KIND = {
    "plan": QuestionKind.PLAN,
    "act": QuestionKind.IMPLEMENT,
    "judge": QuestionKind.VERIFY,
    "lesson": QuestionKind.LESSON,
    "compress": QuestionKind.LESSON,
}


@dataclass(frozen=True)
class RouteDecision:
    kind: QuestionKind
    tier: str
    reason: str
    attempt: int = 0
    est_cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "tier": self.tier,
            "reason": self.reason,
            "attempt": self.attempt,
        }


@dataclass
class TierUsage:
    calls: int = 0
    tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"calls": self.calls, "tokens": self.tokens}


class ModelRouter:
    def __init__(
        self,
        tiers: dict[str, Model],
        *,
        weights: dict[str, float] | None = None,
        trivial_cost: float = 0.35,
    ):
        if not tiers:
            raise ValueError("router needs at least one tier")
        self.tiers = dict(tiers)
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.trivial_cost = trivial_cost
        self._usage: dict[str, TierUsage] = {name: TierUsage() for name in self.tiers}
        self.name = "router(" + ",".join(sorted(self.tiers)) + ")"

    # -- construction -----------------------------------------------------
    @classmethod
    def single(cls, model: Model) -> "ModelRouter":
        return cls({"standard": model})

    # -- decision ---------------------------------------------------------
    def _resolve(self, wanted: str) -> str:
        if wanted in self.tiers:
            return wanted
        for fallback in ("standard", "cheap", "strong"):
            if fallback in self.tiers:
                return fallback
        return next(iter(self.tiers))

    def decide(
        self,
        prompt: str,
        *,
        attempt: int = 0,
        est_cost: float = 0.5,
        risk: str = "low",
        lesson_hit: bool = False,
    ) -> RouteDecision:
        kind = TAG_KIND.get(task_tag(prompt), QuestionKind.IMPLEMENT)

        if kind is QuestionKind.VERIFY:
            return RouteDecision(kind, self._resolve("verify"), "verify_role", attempt, est_cost)
        if kind is QuestionKind.PLAN:
            return RouteDecision(kind, self._resolve("strong"), "plan_role", attempt, est_cost)
        if kind is QuestionKind.LESSON:
            return RouteDecision(kind, self._resolve("cheap"), "lesson_role", attempt, est_cost)

        if attempt > 0:
            return RouteDecision(kind, self._resolve("strong"), "escalate_after_failure", attempt, est_cost)
        if lesson_hit:
            return RouteDecision(kind, self._resolve("standard"), "memory_lesson", attempt, est_cost)
        if est_cost <= self.trivial_cost and risk == "low":
            return RouteDecision(kind, self._resolve("cheap"), "trivial_node", attempt, est_cost)
        return RouteDecision(kind, self._resolve("standard"), "default", attempt, est_cost)

    # -- execution --------------------------------------------------------
    def complete(
        self,
        prompt: str,
        *,
        attempt: int = 0,
        est_cost: float = 0.5,
        risk: str = "low",
        lesson_hit: bool = False,
        max_tokens: int = 900,
    ) -> tuple[Completion, RouteDecision]:
        decision = self.decide(prompt, attempt=attempt, est_cost=est_cost, risk=risk, lesson_hit=lesson_hit)
        model = self.tiers[decision.tier]
        completion = model.complete(prompt, max_tokens=max_tokens)
        usage = self._usage.setdefault(decision.tier, TierUsage())
        usage.calls += 1
        usage.tokens += completion.tokens
        return completion, decision

    # -- reporting --------------------------------------------------------
    def usage(self) -> dict[str, dict[str, int]]:
        return {name: u.to_dict() for name, u in self._usage.items()}

    def cost_units(self) -> float:
        return sum(u.tokens * self.weights.get(name, 1.0) for name, u in self._usage.items())

    def tier(self, name: str) -> Model:
        return self.tiers[self._resolve(name)]

    def verification_model(self) -> Model | None:
        """The judge, unless it would be the same instance that did the work."""
        if "verify" not in self.tiers:
            return None
        candidate = self.tiers["verify"]
        for name, model in self.tiers.items():
            if name != "verify" and model is candidate:
                return None
        return candidate

    def summary(self) -> dict[str, Any]:
        return {
            "tiers": {name: model.name for name, model in self.tiers.items()},
            "usage": self.usage(),
            "cost_units": round(self.cost_units(), 2),
        }

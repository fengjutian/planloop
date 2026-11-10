"""Problem intake: turn a fuzzy request into a decidable specification.

Rule of the design: no success criteria, no planning.  A spec that cannot be
checked is converted into a clarification request instead of a task graph.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

CRITERION_KINDS = {
    "command",
    "file_exists",
    "file_contains",
    "json_has_keys",
    "rubric",
    "human",
}

#: criteria that a machine can check without a model in the loop
DETERMINISTIC_KINDS = {"command", "file_exists", "file_contains", "json_has_keys"}
#: criteria that need an independent model call (layer 2)
JUDGED_KINDS = {"rubric"}
#: criteria that need a human (layer 3)
HUMAN_KINDS = {"human"}


class SpecError(ValueError):
    """Raised when a problem statement is not decidable yet."""

    def __init__(self, message: str, *, missing: list[str] | None = None):
        super().__init__(message)
        self.missing = missing or []


@dataclass(frozen=True)
class Criterion:
    """One checkable acceptance condition.

    ``kind`` selects the checker; ``spec`` is the checker payload and ``must``
    is the expectation (an exit code, a substring, a list of JSON keys...).
    """

    id: str
    kind: str
    spec: str
    must: str = ""
    weight: float = 1.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Criterion":
        if not isinstance(raw, dict):
            raise SpecError("criterion must be an object")
        cid = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        if kind not in CRITERION_KINDS:
            raise SpecError(f"criterion {cid or '?'}: unknown kind {kind!r}")
        if not cid:
            raise SpecError(f"criterion of kind {kind}: missing id")
        spec = raw.get("spec")
        if not isinstance(spec, str) or not spec.strip():
            raise SpecError(f"criterion {cid}: missing spec")
        must = raw.get("must", "")
        if must is None:
            must = ""
        if kind == "json_has_keys":
            must = ",".join(_as_str_list(must))
        return cls(
            id=cid,
            kind=kind,
            spec=spec.strip(),
            must=str(must),
            weight=float(raw.get("weight", 1.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        out = {"id": self.id, "kind": self.kind, "spec": self.spec}
        if self.must:
            out["must"] = self.must
        if self.weight != 1.0:
            out["weight"] = self.weight
        return out

    @property
    def layer(self) -> str:
        if self.kind in DETERMINISTIC_KINDS:
            return "L1"
        if self.kind in JUDGED_KINDS:
            return "L2"
        return "L3"


@dataclass(frozen=True)
class Budget:
    """Hard caps.  A run that cannot afford its next step must stop and report."""

    tokens: int = 200_000
    tool_calls: int = 200
    wall_clock_s: float = 1800.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
            "wall_clock_s": self.wall_clock_s,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Budget":
        if not raw:
            return cls()
        return cls(
            tokens=int(raw.get("tokens", 200_000)),
            tool_calls=int(raw.get("tool_calls", 200)),
            wall_clock_s=float(raw.get("wall_clock_s", 1800.0)),
        )


@dataclass(frozen=True)
class Limits:
    """Structural guards for decomposition and repair (all cheap to enforce)."""

    max_depth: int = 4
    max_fanout: int = 7
    max_nodes: int = 40
    max_steps_per_node: int = 12
    max_attempts_per_node: int = 2
    max_replans_per_subtree: int = 3
    max_global_replans: int = 2
    #: share of the token budget reserved for planning / execution / repair
    allocations: tuple[tuple[str, float], ...] = (
        ("plan", 0.10),
        ("probe", 0.05),
        ("execute", 0.65),
        ("verify", 0.10),
        ("repair", 0.10),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_depth": self.max_depth,
            "max_fanout": self.max_fanout,
            "max_nodes": self.max_nodes,
            "max_steps_per_node": self.max_steps_per_node,
            "max_attempts_per_node": self.max_attempts_per_node,
            "max_replans_per_subtree": self.max_replans_per_subtree,
            "max_global_replans": self.max_global_replans,
            "allocations": dict(self.allocations),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Limits":
        if not raw:
            return cls()
        base = cls()
        kwargs: dict[str, Any] = {}
        for name in (
            "max_depth",
            "max_fanout",
            "max_nodes",
            "max_steps_per_node",
            "max_attempts_per_node",
            "max_replans_per_subtree",
            "max_global_replans",
        ):
            if name in raw:
                kwargs[name] = int(raw[name])
        if "allocations" in raw and raw["allocations"]:
            kwargs["allocations"] = tuple(
                (str(k), float(v)) for k, v in dict(raw["allocations"]).items()
            )
        return base.__class__(**kwargs)


@dataclass
class Unknown:
    """An open question.  Blocking ones must be resolved before decomposition."""

    id: str
    question: str
    resolve_by: str = "probe"  # probe | ask | experiment
    blocking: bool = True
    resolved: bool = False
    answer: str = ""
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "resolve_by": self.resolve_by,
            "blocking": self.blocking,
            "resolved": self.resolved,
            "answer": self.answer,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Unknown":
        return cls(
            id=str(raw["id"]),
            question=str(raw["question"]),
            resolve_by=str(raw.get("resolve_by", "probe")),
            blocking=bool(raw.get("blocking", True)),
            resolved=bool(raw.get("resolved", False)),
            answer=str(raw.get("answer", "")),
            evidence=str(raw.get("evidence", "")),
        )


@dataclass
class ProblemSpec:
    goal: str
    success_criteria: list[Criterion]
    constraints: list[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    limits: Limits = field(default_factory=Limits)
    unknowns: list[Unknown] = field(default_factory=list)
    context: str = ""
    #: optional canned plan (used by offline demos and regression fixtures)
    plan_template: dict[str, Any] | None = None

    # -- construction -----------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProblemSpec":
        if not isinstance(raw, dict):
            raise SpecError("problem spec must be a JSON object")
        goal = str(raw.get("goal") or "").strip()
        if not goal:
            raise SpecError("missing goal", missing=["goal"])

        criteria_raw = raw.get("success_criteria")
        if not isinstance(criteria_raw, list) or not criteria_raw:
            raise SpecError(
                "no success_criteria: a task without acceptance cannot be planned",
                missing=["success_criteria"],
            )
        criteria = [Criterion.from_dict(c) for c in criteria_raw]
        _ensure_unique_ids([c.id for c in criteria], "success criterion")

        unknowns = [Unknown.from_dict(u) for u in raw.get("unknowns") or []]
        _ensure_unique_ids([u.id for u in unknowns], "unknown")

        return cls(
            goal=goal,
            success_criteria=criteria,
            constraints=[str(c) for c in raw.get("constraints") or []],
            budget=Budget.from_dict(raw.get("budget")),
            limits=Limits.from_dict(raw.get("limits")),
            unknowns=unknowns,
            context=str(raw.get("context") or ""),
            plan_template=raw.get("plan_template"),
        )

    @classmethod
    def from_json(cls, text: str) -> "ProblemSpec":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecError(f"problem spec is not valid JSON: {exc}") from exc
        return cls.from_dict(raw)

    # -- queries ----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "goal": self.goal,
            "success_criteria": [c.to_dict() for c in self.success_criteria],
            "constraints": list(self.constraints),
            "budget": self.budget.to_dict(),
            "limits": self.limits.to_dict(),
        }
        if self.unknowns:
            out["unknowns"] = [u.to_dict() for u in self.unknowns]
        if self.context:
            out["context"] = self.context
        if self.plan_template is not None:
            out["plan_template"] = self.plan_template
        return out

    def blocking_unknowns(self) -> list[Unknown]:
        return [u for u in self.unknowns if u.blocking and not u.resolved]

    def criteria_of(self, layer: str) -> list[Criterion]:
        return [c for c in self.success_criteria if c.layer == layer]

    def require_ready(self) -> None:
        """Gate before planning: unresolved blocking unknowns stop the run."""
        pending = self.blocking_unknowns()
        if pending:
            raise SpecError(
                "unresolved blocking unknowns: "
                + "; ".join(f"{u.id}: {u.question}" for u in pending),
                missing=[u.id for u in pending],
            )
        if not self.success_criteria:
            raise SpecError("no success_criteria", missing=["success_criteria"])


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, Iterable):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _ensure_unique_ids(ids: Iterable[str], what: str) -> None:
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            raise SpecError(f"duplicate {what} id: {i}")
        seen.add(i)

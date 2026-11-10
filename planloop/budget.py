"""Budget accounting: the reason a run stops instead of spiralling.

Two levels of protection:

* a per-phase share (plan / execute / repair ...), which makes cost visible
  and keeps repair from eating the whole run,
* a hard global cap, which raises :class:`BudgetExceeded` -- the scheduler
  turns that into a ``BlockedReport`` rather than another retry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from planloop.spec import Budget


class BudgetExceeded(RuntimeError):
    """Raised when a charge would cross a hard cap."""

    def __init__(self, message: str, *, phase: str, snapshot: dict[str, Any]):
        super().__init__(message)
        self.phase = phase
        self.snapshot = snapshot


@dataclass
class PhaseBudget:
    name: str
    cap_tokens: float
    used_tokens: float = 0.0
    used_tool_calls: int = 0
    borrowed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.name,
            "cap_tokens": round(self.cap_tokens),
            "used_tokens": round(self.used_tokens),
            "used_tool_calls": self.used_tool_calls,
            "borrowed": round(self.borrowed),
        }


@dataclass
class Ledger:
    budget: Budget = field(default_factory=Budget)
    allocations: dict[str, float] = field(
        default_factory=lambda: {
            "plan": 0.10,
            "probe": 0.05,
            "execute": 0.65,
            "verify": 0.10,
            "repair": 0.10,
        }
    )
    started_at: float = field(default_factory=time.monotonic)
    phases: dict[str, PhaseBudget] = field(default_factory=dict)
    total_tokens: float = 0.0
    total_tool_calls: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name, share in self.allocations.items():
            self.phases[name] = PhaseBudget(name=name, cap_tokens=self.budget.tokens * share)

    # -- accounting -------------------------------------------------------
    def charge(
        self,
        phase: str,
        *,
        tokens: float = 0,
        tool_calls: int = 0,
        note: str = "",
    ) -> None:
        pb = self.phases.get(phase)
        if pb is None:
            pb = PhaseBudget(name=phase, cap_tokens=0.0)
            self.phases[phase] = pb

        if tokens:
            if self.total_tokens + tokens > self.budget.tokens:
                raise BudgetExceeded(
                    f"token budget exhausted: {self.total_tokens:.0f}/{self.budget.tokens}",
                    phase=phase,
                    snapshot=self.snapshot(),
                )
            if pb.used_tokens + tokens > pb.cap_tokens:
                # soft: borrow from the global pool, but record it
                pb.borrowed += (pb.used_tokens + tokens) - pb.cap_tokens
            pb.used_tokens += tokens
            self.total_tokens += tokens

        if tool_calls:
            if self.total_tool_calls + tool_calls > self.budget.tool_calls:
                raise BudgetExceeded(
                    f"tool-call budget exhausted: {self.total_tool_calls}/{self.budget.tool_calls}",
                    phase=phase,
                    snapshot=self.snapshot(),
                )
            pb.used_tool_calls += tool_calls
            self.total_tool_calls += tool_calls

        self.events.append(
            {"phase": phase, "tokens": round(tokens), "tool_calls": tool_calls, "note": note}
        )

    # -- restart ----------------------------------------------------------
    def restore(self, usage: dict[str, Any] | None) -> None:
        """Re-apply spend recorded in a previous process (resume)."""
        usage = usage or {}
        tokens = float(usage.get("tokens", 0) or 0)
        calls = int(usage.get("tool_calls", 0) or 0)
        self.total_tokens = tokens
        self.total_tool_calls = calls
        pb = self.phases.get("execute")
        if pb is not None:
            pb.used_tokens = max(pb.used_tokens, tokens * 0.8)
            pb.used_tool_calls = calls

    # -- queries ----------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_tokens(self) -> float:
        return self.budget.tokens - self.total_tokens

    @property
    def remaining_tool_calls(self) -> int:
        return self.budget.tool_calls - self.total_tool_calls

    @property
    def wall_clock_left(self) -> float:
        return self.budget.wall_clock_s - self.elapsed

    def exhausted(self) -> str | None:
        """Return a human reason when no further work is affordable."""
        if self.remaining_tokens <= 0:
            return "token budget exhausted"
        if self.remaining_tool_calls <= 0:
            return "tool-call budget exhausted"
        if self.wall_clock_left <= 0:
            return "wall-clock budget exhausted"
        return None

    def can_afford(self, tokens: float) -> bool:
        return self.remaining_tokens >= tokens and self.wall_clock_left > 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "tokens": round(self.total_tokens),
            "token_budget": self.budget.tokens,
            "tool_calls": self.total_tool_calls,
            "tool_call_budget": self.budget.tool_calls,
            "elapsed_s": round(self.elapsed, 3),
            "wall_clock_budget_s": self.budget.wall_clock_s,
            "phases": [p.to_dict() for p in self.phases.values()],
            "borrowed_tokens": round(sum(p.borrowed for p in self.phases.values())),
        }

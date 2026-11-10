"""Approvals: a durable, one-shot, context-bound record of human consent.

The store is an append-only JSONL event log (``request`` / ``approve`` /
``deny`` / ``consume``); the current state is a replay of those events.  That
matches the rest of the system: nothing is mutated in place, so "who approved
what, when" survives a crash and shows up in ``planloop audit``.

An approval binds to:

* the exact action text (hashed) -- approving A does not approve B;
* the run id -- a different run needs its own approval;
* an expiry -- consent is not permanent;
* ``max_uses`` (default 1) -- deliberately one-shot.

In a headless run there is no prompt: the action is refused, a pending approval
is written, and the run stops.  The human approves later and resumes.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATUSES = ("pending", "approved", "denied", "consumed")


def digest(action: str, target: str) -> str:
    raw = f"{action}\x00{target}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class Approval:
    token: str
    action: str
    target: str
    reason: str
    rule: str
    run_id: str = ""
    node_id: str = ""
    status: str = "pending"
    created_at: float = 0.0
    expires_at: float = 0.0
    approved_by: str = ""
    decided_at: float = 0.0
    max_uses: int = 1
    uses: int = 0

    @property
    def target_hash(self) -> str:
        return digest(self.action, self.target)

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() > self.expires_at

    @property
    def usable(self) -> bool:
        return self.status == "approved" and not self.expired and self.uses < self.max_uses

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "action": self.action,
            "target": self.target,
            "reason": self.reason,
            "rule": self.rule,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "approved_by": self.approved_by,
            "decided_at": self.decided_at,
            "max_uses": self.max_uses,
            "uses": self.uses,
        }

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "Approval":
        return cls(
            token=event["token"],
            action=event.get("action", ""),
            target=event.get("target", ""),
            reason=event.get("reason", ""),
            rule=event.get("rule", ""),
            run_id=event.get("run_id", ""),
            node_id=event.get("node_id", ""),
            created_at=float(event.get("at", 0)),
            expires_at=float(event.get("expires_at", 0)),
            max_uses=int(event.get("max_uses", 1)),
        )

    def describe(self) -> str:
        head = f"{self.token}  [{self.status}]  {self.action}: {self.target}"
        return (
            f"{head}\n    why: {self.reason} (rule {self.rule})\n"
            f"    run: {self.run_id or '-'}  node: {self.node_id or '-'}  "
            f"uses {self.uses}/{self.max_uses}"
        )


class ApprovalStore:
    def __init__(self, path: Path | str, *, default_ttl_s: float = 24 * 3600):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl_s = default_ttl_s

    # -- plumbing ---------------------------------------------------------
    def _append(self, event: dict[str, Any]) -> None:
        event = {"at": time.time(), **event}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _replay(self) -> dict[str, Approval]:
        state: dict[str, Approval] = {}
        for event in self.events():
            kind = event.get("event")
            if kind == "request":
                approval = Approval.from_event(event)
                state[approval.token] = approval
                continue
            approval = state.get(event.get("token", ""))
            if approval is None:
                continue
            if kind == "approve":
                approval.status = "approved"
                approval.approved_by = str(event.get("by", "human"))
                approval.decided_at = float(event.get("at", 0))
                approval.expires_at = float(event.get("expires_at", approval.expires_at))
            elif kind == "deny":
                approval.status = "denied"
                approval.approved_by = str(event.get("by", "human"))
                approval.decided_at = float(event.get("at", 0))
            elif kind == "consume":
                approval.uses += 1
                if approval.uses >= approval.max_uses:
                    approval.status = "consumed"
        return state

    # -- api --------------------------------------------------------------
    def request(
        self,
        *,
        action: str,
        target: str,
        reason: str,
        rule: str,
        run_id: str = "",
        node_id: str = "",
        ttl_s: float | None = None,
    ) -> Approval:
        existing = self.find_usable(action, target, run_id)
        if existing is not None:
            return existing
        token = "ap-" + secrets.token_hex(4)
        ttl = self.default_ttl_s if ttl_s is None else ttl_s
        approval = Approval(
            token=token,
            action=action,
            target=target,
            reason=reason,
            rule=rule,
            run_id=run_id,
            node_id=node_id,
            created_at=time.time(),
            expires_at=time.time() + ttl,
        )
        self._append({**approval.to_dict(), "event": "request"})
        return approval

    def approve(self, token: str, *, by: str = "human", ttl_s: float | None = None) -> Approval:
        approval = self.get(token)
        if approval is None:
            raise KeyError(f"no such approval: {token}")
        if approval.status not in ("pending", "approved"):
            raise ValueError(f"approval {token} is {approval.status}, cannot approve")
        ttl = self.default_ttl_s if ttl_s is None else ttl_s
        self._append({"event": "approve", "token": token, "by": by,
                      "expires_at": time.time() + ttl})
        return self.get(token)

    def deny(self, token: str, *, by: str = "human") -> Approval:
        approval = self.get(token)
        if approval is None:
            raise KeyError(f"no such approval: {token}")
        self._append({"event": "deny", "token": token, "by": by})
        return self.get(token)

    def consume(self, token: str) -> bool:
        approval = self.get(token)
        if approval is None or not approval.usable:
            return False
        self._append({"event": "consume", "token": token})
        return True

    def get(self, token: str) -> Approval | None:
        return self._replay().get(token)

    def all(self) -> list[Approval]:
        return sorted(self._replay().values(), key=lambda a: a.created_at, reverse=True)

    def pending(self) -> list[Approval]:
        return [a for a in self.all() if a.status == "pending" and not a.expired]

    # -- lookup -----------------------------------------------------------
    def find_usable(self, action: str, target: str, run_id: str = "") -> Approval | None:
        wanted = digest(action, target)
        for approval in self.all():
            if not approval.usable:
                continue
            if approval.target_hash != wanted:
                continue
            if run_id and approval.run_id and approval.run_id != run_id:
                continue
            return approval
        return None

    def find_pending(self, action: str, target: str, run_id: str = "") -> Approval | None:
        wanted = digest(action, target)
        for approval in self.pending():
            if approval.target_hash == wanted and (not run_id or approval.run_id in ("", run_id)):
                return approval
        return None

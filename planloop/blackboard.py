"""Blackboard: externalised state plus a context compiler.

The whole point is that no node ever sees the full transcript.  A node gets

* its own goal, contracts and acceptance criteria,
* summaries of its predecessors' artifacts,
* its own failure history, capped,
* project facts and known dead ends that match it.

That slice is the single strongest lever on long-run stability, and it is also
what makes the run replayable: the event log is the source of truth.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MAX_SUMMARY_CHARS = 600


@dataclass
class Event:
    seq: int
    ts: float
    kind: str
    node_id: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": round(self.ts, 4),
            "kind": self.kind,
            "node_id": self.node_id,
            **({"data": self.data} if self.data else {}),
        }


@dataclass
class Artifact:
    node_id: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "summary": self.summary, "data": self.data}


class Blackboard:
    """Event log + artifacts + memory, with a token-frugal context compiler."""

    def __init__(self, *, log_path: Path | str | None = None, fresh: bool = True):
        self.events: list[Event] = []
        self.artifacts: dict[str, Artifact] = {}
        self.facts: dict[str, str] = {}
        self.dead_ends: list[str] = []
        self.failures: dict[str, list[str]] = {}
        self.observations: dict[str, list[str]] = {}
        #: the last acceptance failure per node -- pinned, never evicted
        self.rejections: dict[str, str] = {}
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if fresh:
                self.log_path.write_text("", encoding="utf-8")
            else:
                # resuming: keep what happened before, append to it
                self.log_path.touch(exist_ok=True)

    # -- writes -----------------------------------------------------------
    def append(self, kind: str, node_id: str = "", **data: Any) -> Event:
        ev = Event(seq=len(self.events), ts=time.time(), kind=kind, node_id=node_id, data=data)
        self.events.append(ev)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        return ev

    def record_artifact(
        self, node_id: str, summary: str, data: dict[str, Any] | None = None, tokens: int = 0
    ) -> Artifact:
        art = Artifact(node_id=node_id, summary=summary.strip()[:MAX_SUMMARY_CHARS], data=data or {}, tokens=tokens)
        self.artifacts[node_id] = art
        self.append("artifact", node_id, summary=art.summary, keys=list(art.data))
        return art

    def record_failure(self, node_id: str, reason: str) -> None:
        self.failures.setdefault(node_id, []).append(reason.strip()[:400])
        self.append("failure", node_id, reason=reason.strip()[:400])

    def record_observation(self, node_id: str, text: str) -> None:
        bucket = self.observations.setdefault(node_id, [])
        bucket.append(text[:2000])
        del bucket[:-6]  # keep the last few only

    def record_rejection(self, node_id: str, text: str) -> None:
        """Remember the last acceptance failure of a node.

        The prompt shows only the last couple of observations, so a failure
        can be pushed out of the window by the model's own actions -- and the
        one thing a model needs while it is failing is *why* it failed.  The
        rejection is therefore pinned separately and re-shown until the
        acceptance passes.

        (Measured separately: a 23-read loop was **not** caused by this --
        the failure was still in the window.  The pin is a guarantee, not the
        explanation of that loop.)
        """
        self.rejections[node_id] = text[:2000]

    def clear_rejection(self, node_id: str) -> None:
        self.rejections.pop(node_id, None)

    def add_fact(self, key: str, value: str) -> None:
        self.facts[key.strip()] = value.strip()
        self.append("fact", "", key=key, value=value)

    def add_dead_end(self, note: str) -> None:
        self.dead_ends.append(note.strip()[:300])
        self.append("dead_end", "", note=note[:300])

    def restore_artifacts(self, artifacts: dict[str, Any]) -> None:
        """Rebuild the artifact shelf after a restart (no events are replayed)."""
        for node_id, payload in (artifacts or {}).items():
            if isinstance(payload, dict):
                self.artifacts[node_id] = Artifact(
                    node_id=node_id,
                    summary=str(payload.get("summary", "")),
                    data=payload.get("data") or {},
                )

    # -- context compiler -------------------------------------------------
    def context_for(
        self,
        *,
        node_id: str,
        goal: str,
        node_goal: str,
        deps: Iterable[str] = (),
        contracts: str = "",
        acceptance: str = "",
        max_chars: int = 1400,
        compressor=None,
        include_observations: bool = True,
    ) -> str:
        """Assemble the minimal, relevant slice for one node."""
        parts: list[str] = [f"# Global goal\n{goal}"]
        node_block = [f"# Current node {node_id}", node_goal]
        if contracts:
            node_block.append(contracts)
        if acceptance:
            node_block.append("Acceptance (must hold before you finish):\n" + acceptance)
        parts.append("\n".join(node_block))

        upstream = [self.artifacts[d].summary for d in deps if d in self.artifacts]
        if upstream:
            parts.append("# Upstream results\n" + "\n".join(f"- {u}" for u in upstream))

        fails = self.failures.get(node_id, [])[-3:]
        if fails:
            parts.append(
                "# Your previous failed attempts (do not repeat them)\n"
                + "\n".join(f"- {f}" for f in fails)
            )
        if self.dead_ends:
            parts.append("# Known dead ends\n" + "\n".join(f"- {d}" for d in self.dead_ends[-5:]))

        relevant = self._relevant_facts(f"{node_goal} {contracts}")
        if relevant:
            parts.append("# Project facts\n" + "\n".join(f"- {k}: {v}" for k, v in relevant))

        obs = self.observations.get(node_id, [])[-2:] if include_observations else []
        if obs:
            parts.append("# Recent observations\n" + "\n".join(f"> {o[-400:]}" for o in obs))

        text = "\n\n".join(parts)
        if len(text) <= max_chars:
            return text
        if compressor is not None:
            sections = text.split("\n\n")
            share = max(140, max_chars // max(1, len(sections)))
            fitted = [
                section if len(section) <= share
                else compressor.compress(section, share, label=section.splitlines()[0][:40])
                for section in sections
            ]
            text = "\n\n".join(fitted)
            if len(text) <= max_chars:
                return text
        return text[: max_chars - 26] + "\n... [truncated]"

    def _relevant_facts(self, text: str) -> list[tuple[str, str]]:
        lowered = text.lower()
        out = []
        for k, v in self.facts.items():
            if k.lower() in lowered or any(tok in lowered for tok in k.lower().split("_")):
                out.append((k, v))
        return out[:6]

    # -- introspection ----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "events": len(self.events),
            "artifacts": len(self.artifacts),
            "facts": len(self.facts),
            "dead_ends": len(self.dead_ends),
            "failure_records": sum(len(v) for v in self.failures.values()),
        }

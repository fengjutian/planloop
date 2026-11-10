"""Error memory: remember what went wrong, and make it hard to repeat.

Two channels, on purpose:

* **advisory** -- a structured lesson injected into the context slice of a
  later, similar node.  Cheap, but only works if the model reads it.
* **guard** -- a machine-checkable acceptance criterion carried by the lesson
  and injected into the plan.  Works whether or not the model reads anything.

A lesson is only allowed to carry a guard if that guard **passes right now** in
the workspace.  Fabricated memories are therefore rejected at the door, and a
guard that keeps failing on correct work loses trust until it is retired.

Retrieval is deterministic: file globs, goal regexes, command regexes and
error regexes must all match.  Semantic similarity alone is not good enough to
decide whether to block a run.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from planloop.model import Model, extract_json
from planloop.spec import Criterion
from planloop.tools import Tools

LESSON_PROMPT = """task=lesson
A task just succeeded after failing first. Extract one reusable lesson so the
same mistake does not happen again.

Node goal:
{goal}

The failure (verbatim excerpt):
---
{failure}
---

What finally made it pass (commands / files written):
---
{fix}
---

Rules:
- `summary`: one line, what went wrong.
- `cause`: why it went wrong (mechanism, not blame).
- `fix`: the concrete corrective action that worked.
- `trigger`: when this lesson applies. Give at least one of `files` (glob),
  `goals` (regex over a future task's goal), `error_patterns` (regex over a
  failure text), `commands` (regex over a command line).
- `guard`: a machine-checkable check that would have caught this class of
  mistake, or null. It must be cheap and must pass on the current workspace.
  Shape: {{"kind": "command|file_exists|file_contains|json_has_keys",
           "spec": "...", "must": "..."}}
- Generalise one level: "run the full test suite after cross-module edits" is
  a better lesson than "create helpers.py".

Reply with one JSON object only.
"""

VOLATILE: list[tuple[str, str]] = [
    (r"\x1b\[[0-9;]*m", ""),
    (r"[A-Za-z]:\\\\?[^\s:'\"<>|]+", "<path>"),
    (r"(?<![\w.])/(?:[\w.+@-]+/)+[\w.+@-]+", "<path>"),
    (r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?\b", "<ts>"),
    (r"\b\d{2}:\d{2}:\d{2}\b", "<ts>"),
    (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>"),
    (r"\b0x[0-9a-fA-F]{4,}\b", "<hex>"),
    (r":\d+:\d+:", ":<l>:<c>:"),
    (r"\bline \d+\b", "line <n>"),
    (r"\bpid[= ]\d+\b", "pid=<n>"),
    (r"\b\d{6,}\b", "<id>"),
]

INTERESTING = re.compile(r"(Error|Exception|error:|FAILED|assert|Traceback|not found|denied)", re.I)

#: ordered by how specific the line is; the last match of the best class wins,
#: because tracebacks end with the actual cause.
PRIORITY = [
    re.compile(r"(?i)\b[\w.]*(?:Error|Exception)\s*:"),
    re.compile(r"(?i)^FAIL(?:ED)?[:\s]"),
    re.compile(r"(?i)^assert\w*.*"),
]


# --------------------------------------------------------------------------
# signatures
# --------------------------------------------------------------------------
def normalize(text: str) -> str:
    out = text
    for pattern, repl in VOLATILE:
        out = re.sub(pattern, repl, out)
    out = re.sub(r"\s+", " ", out)
    return out.strip()[:500]


def salient_line(text: str) -> str:
    """Pick the line that identifies *this* failure, not just any failure.

    "FAILED (failures=1)" is a useless identity: every failing suite prints it.
    Prefer the concrete exception / FAIL / assert line instead.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    for pattern in PRIORITY:
        for line in reversed(lines):
            if pattern.search(line):
                return line[:400]
    for line in reversed(lines):
        if INTERESTING.search(line):
            return line[:400]
    return lines[-1][:400]


def signature(text: str) -> tuple[str, str]:
    """(fingerprint, normalized salient text).

    Exact-normalised on purpose: a stable identity.  Generality comes from a
    lesson's trigger conditions, not from collapsing numbers, which would make
    unrelated failures share one memory.
    """
    norm = normalize(salient_line(text) or text)
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    return f"e{digest}", norm


def family(norm: str) -> str:
    """Coarse cluster label, for statistics only -- never for hard guards."""
    return re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", norm)


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------
@dataclass
class Trigger:
    files: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    error_patterns: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.files or self.goals or self.commands or self.error_patterns)

    def matches(self, ctx: "MemoryContext") -> tuple[bool, str]:
        """Every populated section must match; report why it did."""
        reasons: list[str] = []
        if self.files:
            hit = next(
                (f for f in ctx.files if any(fnmatch.fnmatch(f, g) for g in self.files)),
                None,
            )
            if hit is None:
                return False, ""
            reasons.append(f"file:{hit}")
        if self.goals:
            hit = next((g for g in self.goals if re.search(g, ctx.goal, re.I)), None)
            if hit is None:
                return False, ""
            reasons.append(f"goal:{hit}")
        if self.commands:
            blob = "\n".join(ctx.commands)
            hit = next((c for c in self.commands if re.search(c, blob, re.I | re.M)), None)
            if hit is None:
                return False, ""
            reasons.append(f"cmd:{hit}")
        if self.error_patterns:
            hit = next((e for e in self.error_patterns if re.search(e, ctx.error_text, re.I)), None)
            if hit is None:
                return False, ""
            reasons.append(f"err:{hit}")
        return True, ", ".join(reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "goals": self.goals,
            "commands": self.commands,
            "error_patterns": self.error_patterns,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Trigger":
        raw = raw or {}
        return cls(
            files=[str(x) for x in raw.get("files") or []],
            goals=[str(x) for x in raw.get("goals") or []],
            commands=[str(x) for x in raw.get("commands") or []],
            error_patterns=[str(x) for x in raw.get("error_patterns") or []],
        )


@dataclass
class MemoryContext:
    goal: str
    files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    error_text: str = ""

    @classmethod
    def from_workspace(cls, goal: str, root: Path | str, *, commands=(), error_text="") -> "MemoryContext":
        root = Path(root)
        files = []
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts:
                    files.append(p.relative_to(root).as_posix())
        return cls(goal=goal, files=files, commands=list(commands), error_text=error_text)


@dataclass
class Lesson:
    id: str
    signature: str
    summary: str
    cause: str = ""
    fix: str = ""
    trigger: Trigger = field(default_factory=Trigger)
    guard: dict[str, Any] | None = None
    family: str = ""
    strength: str = "advisory"  # advisory | guard
    trust: float = 0.5
    status: str = "active"  # active | refuted | archived
    hits: int = 0
    prevented: int = 0
    recurrences: int = 0
    guard_false_positives: int = 0
    catches: int = 0
    evidence: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    source: str = "model"

    # -- helpers ----------------------------------------------------------
    @property
    def hard(self) -> bool:
        return self.strength == "guard" and self.guard is not None and self.status == "active"

    def criterion(self) -> Criterion | None:
        if not self.hard:
            return None
        g = self.guard or {}
        try:
            return Criterion(
                id=f"M_{self.id}",
                kind=str(g.get("kind", "command")),
                spec=str(g["spec"]),
                must=str(g.get("must", "")),
            )
        except (KeyError, Exception):  # noqa: BLE001
            return None

    def advice(self, max_chars: int = 320) -> str:
        text = f"[{self.id}] {self.summary} | cause: {self.cause} | fix: {self.fix}"
        return text[:max_chars]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "signature": self.signature,
            "family": self.family,
            "summary": self.summary,
            "cause": self.cause,
            "fix": self.fix,
            "trigger": self.trigger.to_dict(),
            "guard": self.guard,
            "strength": self.strength,
            "trust": round(self.trust, 3),
            "status": self.status,
            "hits": self.hits,
            "prevented": self.prevented,
            "recurrences": self.recurrences,
            "guard_false_positives": self.guard_false_positives,
            "catches": self.catches,
            "evidence": self.evidence[-4:],
            "created_at": round(self.created_at, 3),
            "last_seen": round(self.last_seen, 3),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Lesson":
        return cls(
            id=str(raw["id"]),
            signature=str(raw.get("signature", "")),
            summary=str(raw.get("summary", "")),
            cause=str(raw.get("cause", "")),
            fix=str(raw.get("fix", "")),
            trigger=Trigger.from_dict(raw.get("trigger")),
            guard=raw.get("guard"),
            family=str(raw.get("family", "")),
            strength=str(raw.get("strength", "advisory")),
            trust=float(raw.get("trust", 0.5)),
            status=str(raw.get("status", "active")),
            hits=int(raw.get("hits", 0)),
            prevented=int(raw.get("prevented", 0)),
            recurrences=int(raw.get("recurrences", 0)),
            guard_false_positives=int(raw.get("guard_false_positives", 0)),
            catches=int(raw.get("catches", 0)),
            evidence=[str(e) for e in raw.get("evidence") or []],
            created_at=float(raw.get("created_at", time.time())),
            last_seen=float(raw.get("last_seen", time.time())),
            source=str(raw.get("source", "model")),
        )


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------
class MemoryStore:
    """A small, inspectable, file-backed lesson store."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self.lessons: dict[str, Lesson] = {}
        self.stats = {
            "matches": 0,
            "applied": 0,
            "prevented": 0,
            "catches": 0,
            "recurrences": 0,
            "repeat_failures": 0,
            "failures": 0,
        }
        if self.path and self.path.exists():
            self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lesson = Lesson.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError):
                continue
            self.lessons[lesson.id] = lesson

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            "\n".join(json.dumps(l.to_dict(), ensure_ascii=False) for l in self.lessons.values()) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # -- reads ------------------------------------------------------------
    def match(self, ctx: MemoryContext, *, limit: int = 3) -> list[tuple[Lesson, str]]:
        found: list[tuple[Lesson, str]] = []
        for lesson in self.lessons.values():
            if lesson.status != "active" or lesson.trigger.empty():
                continue
            ok, why = lesson.trigger.matches(ctx)
            if ok:
                found.append((lesson, why))
        found.sort(key=lambda pair: (pair[0].trust, pair[0].prevented), reverse=True)
        found = found[:limit]
        if found:
            self.stats["matches"] += 1
        return found

    def active(self) -> list[Lesson]:
        return [l for l in self.lessons.values() if l.status == "active"]

    # -- writes -----------------------------------------------------------
    def add(self, lesson: Lesson) -> Lesson:
        existing = self.lessons.get(lesson.id)
        if existing:
            existing.recurrences += 1
            existing.last_seen = time.time()
            existing.evidence = (existing.evidence + lesson.evidence)[-4:]
            if lesson.guard and not existing.guard:
                existing.guard = lesson.guard
                existing.strength = "guard"
            existing.trust = min(1.0, existing.trust + 0.15)
            self.stats["recurrences"] += 1
            self.save()
            return existing
        self.lessons[lesson.id] = lesson
        self.save()
        return lesson

    def note_failure(self, text: str) -> bool:
        """Count a failure; return True when it is a *repeat* of a known lesson."""
        self.stats["failures"] += 1
        sig, _ = signature(text)
        known = self.lessons.get(sig)
        repeat = bool(known and known.status == "active")
        if repeat:
            self.stats["repeat_failures"] += 1
            known.recurrences += 1
            known.trust = min(1.0, known.trust + 0.1)
            known.last_seen = time.time()
            self.stats["recurrences"] += 1
            self.save()
        return repeat

    def record_application(self, lesson_ids: Iterable[str]) -> None:
        for lid in lesson_ids:
            lesson = self.lessons.get(lid)
            if lesson:
                lesson.hits += 1
                self.stats["applied"] += 1
                self.save()

    def record_prevented(self, lesson_id: str) -> None:
        lesson = self.lessons.get(lesson_id)
        if not lesson:
            return
        lesson.prevented += 1
        lesson.trust = min(1.0, lesson.trust + 0.05)
        lesson.last_seen = time.time()
        self.stats["prevented"] += 1
        # a guard that keeps working on correct work earns permanence
        if lesson.prevented >= 5 and 0 < lesson.recurrences <= 1:
            lesson.trust = max(lesson.trust, 0.9)
        self.save()

    def record_guard_catch(self, lesson_id: str) -> None:
        """The guard fired and stopped a repeat before delivery."""
        lesson = self.lessons.get(lesson_id)
        if not lesson:
            return
        lesson.catches += 1
        lesson.trust = min(1.0, lesson.trust + 0.1)
        lesson.last_seen = time.time()
        self.stats["catches"] += 1
        self.save()

    def record_guard_false_positive(self, lesson_id: str) -> None:
        lesson = self.lessons.get(lesson_id)
        if not lesson:
            return
        lesson.guard_false_positives += 1
        lesson.trust = max(0.0, lesson.trust - 0.3)
        if lesson.trust < 0.2:
            lesson.status = "refuted"
            lesson.strength = "advisory"
        self.save()

    def retire(self, lesson_id: str) -> None:
        if lesson_id in self.lessons:
            self.lessons[lesson_id].status = "archived"
            self.save()

    # -- reporting --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        lessons = list(self.lessons.values())
        active = [l for l in lessons if l.status == "active"]
        failures = self.stats["failures"]
        return {
            "lessons": len(lessons),
            "active": len(active),
            "guard_lessons": len([l for l in active if l.hard]),
            "applied": self.stats["applied"],
            "prevented": self.stats["prevented"],
            "guard_catches": self.stats["catches"],
            "recurrences": self.stats["recurrences"],
            "repeat_failures": self.stats["repeat_failures"],
            "failures_observed": failures,
            "repeat_failure_rate": round(self.stats["repeat_failures"] / failures, 3) if failures else 0.0,
        }


# --------------------------------------------------------------------------
# learning
# --------------------------------------------------------------------------
def propose_lesson(
    model: Model | None,
    *,
    node_goal: str,
    failure_text: str,
    fix_text: str,
    workspace: Path | str,
    ledger=None,
    verifier=None,
    evidence_commands: Iterable[str] = (),
) -> Lesson | None:
    """Turn one failure-then-fix trajectory into a stored lesson.

    The guard is executed before it is trusted: a lesson whose guard does not
    pass here is downgraded to advisory rather than becoming a false alarm.
    """
    sig, norm = signature(failure_text)
    trigger: Trigger
    summary = ""
    cause = ""
    fix = "worked around it"
    guard: dict[str, Any] | None = None

    if model is not None:
        prompt = LESSON_PROMPT.format(
            goal=node_goal,
            failure=salient_line(failure_text)[:800],
            fix=fix_text[:1200],
        )
        try:
            comp = model.complete(prompt, max_tokens=500)
            if ledger is not None:
                ledger.charge("verify", tokens=comp.tokens, note="lesson")
            data = extract_json(comp.text)
            summary = str(data.get("summary") or "").strip()
            cause = str(data.get("cause") or "").strip()
            fix = str(data.get("fix") or fix).strip()
            trigger = Trigger.from_dict(data.get("trigger"))
            g = data.get("guard")
            if isinstance(g, dict) and g.get("spec"):
                guard = {
                    "kind": str(g.get("kind", "command")),
                    "spec": str(g["spec"]),
                    "must": str(g.get("must", "")),
                }
        except (ValueError, RuntimeError):
            summary = summary or f"failure repeated: {norm[:120]}"
            trigger = Trigger(files=_guess_files(norm), error_patterns=[re.escape(norm[:80])])
    else:
        summary = f"failure: {norm[:120]}"
        trigger = Trigger(error_patterns=[re.escape(norm[:80])])

    if not summary:
        summary = f"failure: {norm[:120]}"
    if trigger.empty():
        trigger = Trigger(error_patterns=[re.escape(norm[:60])])

    strength = "advisory"
    trust = 0.45
    validated: dict[str, Any] | None = None
    if guard:
        crit = Criterion(id="validate", kind=guard["kind"], spec=guard["spec"], must=guard["must"])
        tools = Tools(workspace, ledger=None, phase="verify")
        probe = verifier or None
        ok = False
        try:
            if probe is not None:
                ok = probe.l1_only([crit]).passed
            else:
                from planloop.verify import Verifier

                ok = Verifier(workspace).l1_only([crit]).passed
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            validated = guard
            strength = "guard"
            trust = 0.7
        else:
            guard = None
        _ = tools

    lesson = Lesson(
        id=sig,
        signature=norm,
        family=family(norm),
        summary=summary,
        cause=cause,
        fix=fix,
        trigger=trigger,
        guard=validated,
        strength=strength,
        trust=trust,
        evidence=[salient_line(failure_text)[:300], fix_text[:300]],
        source="model" if model is not None else "heuristic",
    )
    return lesson


def _guess_files(norm: str) -> list[str]:
    found = re.findall(r"[\w./-]+\.(?:py|rs|ts|js|json|toml|md)", norm)
    return [f"*/{Path(f).name}" for f in found][:3]

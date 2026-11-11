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

What finally made it pass (commands / files written, and the files as they
look now):
---
{fix}
---

The `fix` field must describe **what is in those files**, not what you guess
the change was: a lesson that names the wrong change is worse than no
lesson, because it gets injected into the next task with a matching trigger.

Rules:
- `summary`: one line, what went wrong.
- `cause`: why it went wrong (mechanism, not blame).
- `fix`: the concrete corrective action that worked.
- `trigger`: when this lesson applies. Give at least one of `files` (glob),
  `goals` (regex over a future task's goal), `error_patterns` (regex over a
  failure text), `commands` (regex over a command line).
  **Every section you give must match, so give only the ones you can make
  general.**  Two traps, both measured:
  - `error_patterns` and `commands` are matched against text that only exists
    while a failure is being handled; a trigger that carries them can never
    fire on a *new* task.  Prefer `files` and `goals`.
  - a `goals` regex that only matches the goal you can see right now makes the
    lesson unusable: the next task in the same area is worded differently.
    Write it for the **area**, not for this sentence -- e.g. `(?i)(pricing|discount|total)`
    rather than `total\\([^)]*discount_pct[^)]*\\)`.
- `guard`: a machine-checkable check that would have caught this class of
  mistake, or null. It must be cheap and must pass on the current workspace.
  Shape: {{"kind": "command|file_exists|file_contains|json_has_keys",
           "spec": "...", "must": "..."}}
  For `command`, `must` must be machine-readable: `exit=0`, `stdout~=ok`,
  `stdout~="text"`, `contains="text"`.  Prose like "exit status 0" cannot be
  parsed and makes the guard pass by default.
- Generalise one level: "run the full test suite after cross-module edits" is
  a better lesson than "create helpers.py".

Reply with one JSON object only.
"""

#: The budget for the lesson call.  It has to cover the *reasoning* tokens as
#: well: measured on MiniMax-M3.1-Flash, 500 tokens produced 499 reasoning
#: tokens and no content at all, so every lesson silently degraded to the
#: heuristic fallback -- no advice, no trigger, no guard.  The same prompt
#: with 4000 tokens produced a full lesson in 3667 completion tokens.
LESSON_MAX_TOKENS = 4096

#: Appended when the first lesson reply was not a JSON object.  The model
#: usually just needs to be told that the prose was not the answer.
LESSON_RETRY_SUFFIX = """

Your previous reply was not a JSON object.  Reply with **only** the JSON
object, starting with { and ending with }.  No prose, no explanation, no
thinking.
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


def _valid_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def _as_list(value: Any) -> list[str]:
    """A list of strings, whether the model wrote a list or a bare string.

    Measured on 2026-11-11: the model returned ``"goals": "(?i)(pricing|discount)"``
    -- a string.  Iterating it yields **one pattern per character**, so the
    trigger silently became a wildcard (``'i'`` matches almost any goal).
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if str(x).strip()]
    return [str(value)]


def _first_match(patterns: list[str], text: str, flags: int = re.I) -> str | None:
    """The first pattern that matches ``text``.

    A broken pattern must not take the retrieval path down with it: the
    model writes these, and it writes broken ones.
    """
    for pattern in patterns:
        try:
            if re.search(pattern, text, flags):
                return pattern
        except re.error:
            continue
    return None


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

    def sanitized(self) -> "Trigger":
        """Drop the patterns that are not valid regular expressions.

        The model writes the trigger, and a model writes broken regexes:
        measured on 2026-11-11 one of them came back as
        ``(?i)(pricing|discount|total`` -- an unterminated group.  Left in
        place, ``re.search`` raises and takes the whole retrieval path down
        with it.
        """
        return Trigger(
            files=list(self.files),
            goals=[g for g in self.goals if _valid_regex(g)],
            commands=[c for c in self.commands if _valid_regex(c)],
            error_patterns=[e for e in self.error_patterns if _valid_regex(e)],
        )

    def matches(self, ctx: "MemoryContext") -> tuple[bool, str]:
        """Every section the context can supply must match; report why.

        A section the context **cannot** supply must not veto.  The execute
        path builds its context before the node runs, so it carries the goal
        and the files but no commands and no error text -- and a section that
        cannot be checked is unknown, not false.  Treating it as false made
        every lesson whose trigger carries an error pattern or a command
        pattern unfireable: measured on 2026-11-11, a real model's lesson
        never reached a single one of 12 prompts, which silently turned the
        lesson store into a write-only log.  A trigger whose sections cannot
        be checked at all still matches nothing.
        """
        reasons: list[str] = []
        checked = False
        if self.files:
            checked = True
            hit = next(
                (f for f in ctx.files if any(fnmatch.fnmatch(f, g) for g in self.files)),
                None,
            )
            if hit is None:
                return False, ""
            reasons.append(f"file:{hit}")
        if self.goals:
            checked = True
            hit = _first_match(self.goals, ctx.goal)
            if hit is None:
                return False, ""
            reasons.append(f"goal:{hit}")
        if self.commands and ctx.commands:
            checked = True
            blob = "\n".join(ctx.commands)
            hit = _first_match(self.commands, blob, re.I | re.M)
            if hit is None:
                return False, ""
            reasons.append(f"cmd:{hit}")
        if self.error_patterns and ctx.error_text:
            checked = True
            hit = _first_match(self.error_patterns, ctx.error_text)
            if hit is None:
                return False, ""
            reasons.append(f"err:{hit}")
        if not checked:
            # nothing in this trigger could be checked against this context
            return False, ""
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
        trigger = cls(
            files=_as_list(raw.get("files")),
            goals=_as_list(raw.get("goals")),
            commands=_as_list(raw.get("commands")),
            error_patterns=_as_list(raw.get("error_patterns")),
        )
        # the model writes these; a broken pattern must not reach the matcher
        return trigger.sanitized()


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
    source = "heuristic"
    notes: list[str] = []

    if model is not None:
        prompt = LESSON_PROMPT.format(
            goal=node_goal,
            failure=salient_line(failure_text)[:800],
            fix=fix_text[:1200],
        )
        # A reasoning model sometimes spends the whole budget thinking and
        # returns **no content**; the client then hands back the reasoning
        # text, which is prose, not JSON.  Parsed as an answer it fails, and
        # the lesson silently degrades to the fallback template -- no advice,
        # no trigger, no guard.  Measured on MiniMax-M3.1-Flash: a reply
        # starting with "We need answer JSON only. Need infer lesson..." and
        # no ``{`` anywhere in it.  So: one strict retry.
        for attempt in range(2):
            ask = prompt if attempt == 0 else prompt + LESSON_RETRY_SUFFIX
            try:
                comp = model.complete(ask, max_tokens=LESSON_MAX_TOKENS)
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
                if summary:
                    source = "model"
                    if attempt:
                        notes.append(
                            "the lesson needed a second attempt: the first reply "
                            "was not a JSON object"
                        )
                    break
                notes.append("the model returned no summary")
                if attempt:
                    summary = summary or f"failure repeated: {norm[:120]}"
                    if trigger.empty():
                        trigger = Trigger(files=_guess_files(norm),
                                          error_patterns=[re.escape(norm[:80])])
            except (ValueError, RuntimeError) as exc:
                # A reasoning model spends its budget on thinking: measured on
                # MiniMax-M3.1-Flash, a 500-token budget produced 499 reasoning
                # tokens and **no content at all**, so the lesson had to be built
                # without the model.  That used to be swallowed silently and the
                # lesson was still labelled ``source="model"`` -- a store that
                # claims the model wrote it when nobody did.
                notes.append(f"lesson model unusable ({type(exc).__name__}: {exc})")
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
        evidence=[salient_line(failure_text)[:300], fix_text[:300]] + notes,
        source=source,
    )
    return lesson


def _guess_files(norm: str) -> list[str]:
    found = re.findall(r"[\w./-]+\.(?:py|rs|ts|js|json|toml|md)", norm)
    return [f"*/{Path(f).name}" for f in found][:3]

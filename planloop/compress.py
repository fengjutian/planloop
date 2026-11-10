"""Context compression: keep the information, drop the transcript.

Two levels, in order of preference:

1. **Model summary** (``task=compress``) -- best fidelity, costs tokens, so it is
   only used when the overflow is large.
2. **Extractive fallback** -- keeps the lines that carry signal (errors, paths,
   commands, exit codes) and drops prose.  No model, no cost, deterministic.

Both are bounded by a character budget.  Characters are used as a proxy for
tokens on purpose: the estimate is stable, and the budget exists to catch
runaway growth, not to bill anyone.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

FILE_PATH = re.compile(r"(?:[\w.-]+/)+[\w.-]+\.\w{1,5}\b")
COMMANDISH = re.compile(
    r"(?i)\b(python|pytest|unittest|npm|pnpm|cargo|go|make|git|docker|pytest -q|"
    r"ruff|mypy|eslint|tsc|javac|gradle|mvn)\b"
)
SIGNAL = re.compile(r"(?i)(error|exception|failed|traceback|assert|exit_code|denied|missing)")


@dataclass
class CompressionStats:
    calls: int = 0
    tokens: int = 0
    chars_in: int = 0
    chars_out: int = 0
    fallbacks: int = 0

    def to_dict(self) -> dict[str, Any]:
        ratio = (self.chars_out / self.chars_in) if self.chars_in else 0.0
        return {
            "calls": self.calls,
            "tokens": self.tokens,
            "chars_in": self.chars_in,
            "chars_out": self.chars_out,
            "ratio": round(ratio, 3),
            "fallbacks": self.fallbacks,
        }


class Compressor:
    def __init__(self, model=None, *, min_chars: int = 700, ledger=None):
        self.model = model
        self.min_chars = min_chars
        self.ledger = ledger
        self.stats = CompressionStats()

    def compress(self, text: str, budget: int, *, label: str = "context") -> str:
        """Reduce ``text`` to roughly ``budget`` characters."""
        if budget <= 0:
            return ""
        if len(text) <= budget:
            return text

        self.stats.chars_in += len(text)
        out = None
        if self.model is not None and len(text) >= self.min_chars and budget >= 200:
            out = self._summarise(text, budget, label)
        if out is None:
            self.stats.fallbacks += 1
            out = extractive(text, budget)
        self.stats.chars_out += len(out)
        return out

    def _summarise(self, text: str, budget: int, label: str) -> str | None:
        prompt = (
            "task=compress\n"
            "Condense the text below for an agent that must keep working.\n"
            "Keep exactly: error messages, file paths, commands, exit codes, and any\n"
            "constraint that would be expensive to rediscover. Drop prose and repetition.\n"
            f"Reply with plain text, at most {budget} characters, no preamble.\n\n"
            f"[{label}]\n{text[:6000]}\n"
        )
        try:
            comp = self.model.complete(prompt, max_tokens=max(80, budget // 3))
        except Exception:  # noqa: BLE001 - compression must never break a run
            return None
        if self.ledger is not None:
            self.ledger.charge("execute", tokens=comp.tokens, note=f"compress:{label}")
        self.stats.calls += 1
        self.stats.tokens += comp.tokens
        reply = comp.text.strip()
        if not reply:
            return None
        return reply[:budget]


def extractive(text: str, budget: int) -> str:
    """Deterministic fallback: score lines, keep the best ones in order."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    def score(line: str) -> int:
        s = 0
        if SIGNAL.search(line):
            s += 3
        if FILE_PATH.search(line):
            s += 2
        if COMMANDISH.search(line):
            s += 2
        if line.lstrip().startswith(("-", "*", "|", ">", "#")):
            s += 1
        return s

    budget = max(80, budget)
    if sum(len(line) + 1 for line in lines) <= budget:
        return "\n".join(lines)

    # keep the head (context) and the highest-signal lines
    head = lines[:2]
    used = sum(len(line) + 1 for line in head) + 20
    ranked = sorted(range(len(lines)), key=lambda i: (-score(lines[i]), i))
    keep: set[int] = set(range(len(head)))
    for idx in ranked:
        if idx in keep:
            continue
        cost = len(lines[idx]) + 1
        if used + cost > budget - 20:
            continue
        keep.add(idx)
        used += cost
    kept = [lines[i] for i in sorted(keep)]
    marker = f"... [{len(lines) - len(kept)} lines elided] ..."
    out = "\n".join(kept[:2] + [marker] + kept[2:]) if len(kept) > 2 else "\n".join(kept)
    return out[:budget]

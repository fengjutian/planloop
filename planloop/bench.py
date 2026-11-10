"""Benchmarks: B0 / B1 / B2 on the same tasks and the same model.

The comparison isolates one variable -- **verification + bounded retry**:

    B0  single shot   one attempt, no verification; whatever the model says is final
    B1  best-of-N     N independent attempts, majority vote on the artefact set,
                      still no verification
    B2  planloop      plan -> execute -> verify -> bounded retry

Two caveats printed in every report:

* without a real model (``PLANLOOP_MODEL``) these numbers validate the *harness*
  and the measurement pipeline, not a model's ability;
* success is decided by an independent oracle (running the tests), never by the
  configuration being measured.

Everything is seeded, so a run is reproducible.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from planloop.model import Completion, estimate_tokens
from planloop.scheduler import run as planloop_run
from planloop.spec import ProblemSpec
from planloop.tools import Tools

PY = f'"{sys.executable}"'
FULL_SUITE = f"{PY} -m unittest discover -s tests -q"


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------
@dataclass
class Task:
    key: str
    goal: str
    initial: dict[str, str]
    correct: dict[str, str]
    wrong: dict[str, str]
    acceptance: list[dict[str, Any]]
    oracle: str = FULL_SUITE

    def build(self, root: Path) -> None:
        for path, content in self.initial.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def spec(self) -> ProblemSpec:
        return ProblemSpec.from_dict(
            {
                "goal": self.goal,
                "success_criteria": [
                    {"id": "SC1", "kind": "command", "spec": self.oracle, "must": "exit=0"}
                ],
                "limits": {"max_attempts_per_node": 2, "max_steps_per_node": 4},
                "plan_template": {
                    "nodes": [
                        {"id": "T1", "goal": self.goal, "est_cost": 0.5, "acceptance": self.acceptance}
                    ]
                },
            }
        )

    def passes(self, root: Path) -> bool:
        proc = subprocess.run(
            self.oracle, shell=True, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        return proc.returncode == 0


TASKS: list[Task] = [
    Task(
        key="fix_discount",
        goal="修复 total() 的折扣计算，让单元测试通过",
        initial={
            "shop/__init__.py": "",
            "shop/pricing.py": "def total(prices, pct):\n    return sum(prices) * pct / 100\n",
            "tests/test_pricing.py": (
                "import unittest\nfrom shop.pricing import total\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_total(self):\n"
                "        self.assertEqual(total([100, 200], 10), 270)\n"
            ),
        },
        correct={"shop/pricing.py": "def total(prices, pct):\n    return sum(prices) * (100 - pct) / 100\n"},
        wrong={"shop/pricing.py": "def total(prices, pct):\n    return sum(prices) - pct\n"},
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    ),
    Task(
        key="add_feature",
        goal="给 calc 增加 mul() 并补测试，不要破坏 add()",
        initial={
            "calc/__init__.py": "",
            "calc/core.py": "def add(a, b):\n    return a + b\n",
            "tests/test_add.py": (
                "import unittest\nfrom calc.core import add\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 2), 4)\n"
            ),
        },
        correct={
            "calc/core.py": (
                "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
            ),
            "tests/test_mul.py": (
                "import unittest\nfrom calc.core import mul\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_mul(self):\n"
                "        self.assertEqual(mul(3, 4), 12)\n"
            ),
        },
        wrong={
            "calc/core.py": (
                "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
            ),
            "tests/test_mul.py": (
                "import unittest\nfrom calc.core import mul\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_mul(self):\n"
                "        self.assertEqual(mul(3, 4), 12)\n"
            ),
        },
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    ),
    Task(
        key="rename_symbol",
        goal="把 fmt_name 重命名为 format_name，并更新所有调用点",
        initial={
            "lib/__init__.py": "",
            "lib/text.py": "def fmt_name(value):\n    return value.title()\n",
            "lib/api.py": "from lib.text import fmt_name\n\n\ndef greet(name):\n    return fmt_name(name)\n",
            "tests/test_api.py": (
                "import unittest\nfrom lib.api import greet\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_greet(self):\n"
                "        self.assertEqual(greet('bob'), 'Bob')\n"
            ),
        },
        correct={
            "lib/text.py": "def format_name(value):\n    return value.title()\n",
            "lib/api.py": (
                "from lib.text import format_name\n\n\ndef greet(name):\n    return format_name(name)\n"
            ),
        },
        wrong={
            "lib/text.py": "def format_name(value):\n    return value.title()\n",
            "lib/api.py": "from lib.text import fmt_name\n\n\ndef greet(name):\n    return fmt_name(name)\n",
        },
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    ),
]


# --------------------------------------------------------------------------
# the scripted model
# --------------------------------------------------------------------------
class AttemptModel:
    """Writes every file of one attempt (correct or buggy), then finishes.

    ``p_correct`` is the only stochastic input in the benchmark.  On a retry --
    recognised by the supervisor's correction in the prompt -- it re-rolls,
    which is what a real model would effectively do with new information.
    """

    def __init__(self, task: Task, *, p_correct: float, rng: random.Random, name: str = "scripted"):
        self.task = task
        self.p_correct = p_correct
        self.rng = rng
        self.name = name
        self._reset()

    def _reset(self) -> None:
        chosen = self.task.correct if self.rng.random() < self.p_correct else self.task.wrong
        self._items = list(chosen.items())
        self._idx = 0
        self.attempts = getattr(self, "attempts", 0) + 1

    def complete(self, prompt: str, *, max_tokens: int = 900) -> Completion:
        if "Correction from the supervisor" in prompt:
            self._reset()
        if self._idx < len(self._items):
            path, content = self._items[self._idx]
            self._idx += 1
            text = json.dumps({"action": "write", "path": path, "content": content})
        else:
            text = json.dumps({"action": "finish", "summary": "done"})
        return Completion(text=text, tokens_in=estimate_tokens(prompt), tokens_out=estimate_tokens(text))


def _act(task: Task, root: Path, model: AttemptModel, *, max_steps: int = 4) -> int:
    """The B0 primitive: act until the model says finish. No verification."""
    tools = Tools(root)
    prompt = f"task=act\n# Current node T1\n{task.goal}\n"
    tokens = 0
    for _ in range(max_steps):
        comp = model.complete(prompt)
        tokens += comp.tokens
        try:
            action = json.loads(comp.text)
        except json.JSONDecodeError:
            break
        kind = action.get("action")
        if kind == "write":
            tools.write(str(action.get("path", "")), str(action.get("content", "")))
        elif kind == "run":
            tools.run(str(action.get("cmd", "")))
        elif kind == "finish":
            break
    return tokens


# --------------------------------------------------------------------------
# runners
# --------------------------------------------------------------------------
@dataclass
class Outcome:
    config: str
    task: str
    success: bool
    tokens: int
    attempts: int
    seconds: float


def run_b0(task: Task, *, p_correct: float, rng: random.Random) -> Outcome:
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        task.build(root)
        model = AttemptModel(task, p_correct=p_correct, rng=rng)
        tokens = _act(task, root, model)
        success = task.passes(root)
    return Outcome("B0", task.key, success, tokens, 1, time.monotonic() - started)


def run_b1(task: Task, *, p_correct: float, rng: random.Random, n: int = 5) -> Outcome:
    """N attempts, keep the most common artefact set. Still no verification."""
    started = time.monotonic()
    votes: dict[str, int] = {}
    tokens = 0
    for _ in range(n):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task.build(root)
            model = AttemptModel(task, p_correct=p_correct, rng=rng)
            tokens += _act(task, root, model)
            fingerprint = "\n".join(
                f"{p}:{(root / p).read_text(encoding='utf-8')}"
                for p in sorted(task.correct) if (root / p).exists()
            )
            votes[fingerprint] = votes.get(fingerprint, 0) + 1
    winner = max(votes.items(), key=lambda kv: kv[1])[0]
    correct = "\n".join(f"{p}:{task.correct[p]}" for p in sorted(task.correct))
    return Outcome("B1", task.key, winner == correct, tokens, n, time.monotonic() - started)


def run_b2(task: Task, *, p_correct: float, rng: random.Random) -> Outcome:
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        task.build(root)
        model = AttemptModel(task, p_correct=p_correct, rng=rng)
        result = planloop_run(task.spec(), model=model, workspace=root)
        tokens = result.usage.get("tokens", 0)
        attempts = model.attempts or 1
        success = task.passes(root)
    return Outcome("B2", task.key, success, tokens, attempts, time.monotonic() - started)


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
@dataclass
class Report:
    reps: int
    p_correct: float
    seed: int
    outcomes: list[Outcome] = field(default_factory=list)

    def summary(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {}
        for config in ("B0", "B1", "B2"):
            rows = [o for o in self.outcomes if o.config == config]
            if not rows:
                continue
            table[config] = {
                "success_rate": round(sum(o.success for o in rows) / len(rows), 3),
                "mean_tokens": round(sum(o.tokens for o in rows) / len(rows)),
                "runs": len(rows),
            }
        return table

    def to_markdown(self) -> str:
        lines = [
            "# Benchmark: B0 / B1 / B2",
            "",
            f"- repetitions per cell: **{self.reps}**",
            f"- per-attempt success probability of the scripted model: **{self.p_correct}**",
            f"- seed: {self.seed}",
            "",
            "> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。",
            "> 成功由独立 oracle（跑测试）判定，不由被测配置自评。",
            "",
            "| config | success_rate | mean_tokens | runs |",
            "|---|---|---|---|",
        ]
        for config, row in self.summary().items():
            lines.append(
                f"| {config} | {row['success_rate']:.0%} | {row['mean_tokens']:.0f} | {row['runs']} |"
            )
        lines += ["", "## 按任务", "", "| task | config | success | tokens |", "|---|---|---|---|"]
        for outcome in self.outcomes:
            lines.append(
                f"| {outcome.task} | {outcome.config} | {'yes' if outcome.success else 'no'} | {outcome.tokens} |"
            )
        return "\n".join(lines) + "\n"


def benchmark(*, reps: int = 5, p_correct: float = 0.6, seed: int = 0, tasks: list[Task] | None = None) -> Report:
    rng = random.Random(seed)
    report = Report(reps=reps, p_correct=p_correct, seed=seed)
    for _ in range(reps):
        for task in tasks or TASKS:
            report.outcomes.append(run_b0(task, p_correct=p_correct, rng=rng))
            report.outcomes.append(run_b1(task, p_correct=p_correct, rng=rng))
            report.outcomes.append(run_b2(task, p_correct=p_correct, rng=rng))
    return report

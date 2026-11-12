"""End-to-end demo: the same class of mistake, three ways.

The story is deliberately small and real -- a real workspace, real subprocesses,
real exit codes -- because the thing being demonstrated is not model quality but
whether a remembered mistake can change what happens next.

    A  learn   : fail with ModuleNotFoundError, recover, store a lesson + guard
    B  no memory: a *later* task "succeeds" while silently breaking the suite
    C  memory, model ignores the advice: the injected guard catches it
    D  memory, model reads the advice: no failure at all

B is the control.  If B also came out clean, the demo would prove nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from planloop.memory import MemoryStore
from planloop.model import Completion, estimate_tokens, task_tag
from planloop.scheduler import RunResult, run
from planloop.spec import ProblemSpec

FULL_SUITE = f'"{sys.executable}" -m unittest discover -s tests -q'
EXPORT_SUITE = f'"{sys.executable}" -m unittest discover -s tests -p "test_export.py" -q'

PRICING_BUGGY = '''\
def subtotal(prices):
    return sum(prices)


def total(prices, discount_pct):
    return subtotal(prices) * discount_pct / 100
'''

TEST_PRICING = '''\
import unittest

from shop.pricing import total


class PricingTest(unittest.TestCase):
    def test_total_applies_discount(self):
        self.assertEqual(total([100, 200], 10), 270)
'''

PRICING_T1_BROKEN = '''\
from shop.helpers import pct


def subtotal(prices):
    return sum(prices)


def total(prices, discount_pct):
    return subtotal(prices) * (100 - pct(discount_pct)) / 100
'''

HELPERS = '''\
def pct(value):
    """Percentage value, kept as a named helper for call-site clarity."""
    return value
'''

TEST_EXPORT = '''\
import unittest

from shop.pricing import export_csv


class ExportTest(unittest.TestCase):
    def test_export_csv(self):
        self.assertEqual(export_csv([1, 2]), "0,1\\n1,2")
'''

PRICING_T2_BROKEN = '''\
def subtotal(prices):
    return sum(prices)


def total(prices, discount_pct):
    return subtotal(prices) - discount_pct


def export_csv(prices):
    return "\\n".join(f"{i},{p}" for i, p in enumerate(prices))
'''

PRICING_T2_FIXED = '''\
def subtotal(prices):
    return sum(prices)


def total(prices, discount_pct):
    return subtotal(prices) * (100 - discount_pct) / 100


def export_csv(prices):
    return "\\n".join(f"{i},{p}" for i, p in enumerate(prices))
'''

TASK_LEARN = {
    "goal": "Fix shop/pricing.py total() so the discount test passes, via a pct() helper in shop/helpers.py",
    "success_criteria": [
        {"id": "SC1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"},
        {"id": "SC2", "kind": "file_exists", "spec": "shop/helpers.py"},
    ],
    "budget": {"tokens": 60_000, "tool_calls": 60, "wall_clock_s": 300},
    "plan_template": {
        "nodes": [
            {
                "id": "T1",
                "goal": "Make shop/pricing.py apply the discount correctly using shop/helpers.py",
                "deps": [],
                "outputs": {"files": "shop/pricing.py, shop/helpers.py"},
                "acceptance": [{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
                "est_cost": 0.8,
                "risk": "low",
                "reversible": True,
            }
        ]
    },
}

TASK_EXPORT = {
    "goal": "Add export_csv(prices) to shop/pricing.py with a test in tests/test_export.py",
    "success_criteria": [
        {"id": "SC1", "kind": "command", "spec": EXPORT_SUITE, "must": "exit=0"},
    ],
    "budget": {"tokens": 60_000, "tool_calls": 60, "wall_clock_s": 300},
    "plan_template": {
        "nodes": [
            {
                "id": "T1",
                "goal": "Implement export_csv in shop/pricing.py and test it",
                "deps": [],
                "outputs": {"files": "shop/pricing.py, tests/test_export.py"},
                "acceptance": [{"id": "A1", "kind": "command", "spec": EXPORT_SUITE, "must": "exit=0"}],
                "est_cost": 0.8,
                "risk": "low",
                "reversible": True,
            }
        ]
    },
}

def build_repo(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    (root / "shop").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "shop" / "pricing.py").write_text(PRICING_BUGGY, encoding="utf-8")
    (root / "tests" / "test_pricing.py").write_text(TEST_PRICING, encoding="utf-8")
    return root


class DemoModel:
    """A deterministic stand-in for an LLM, scripted per task and persona.

    ``careless`` writes the cross-module import before the module exists (and
    later the regression); ``attentive`` reads the lesson block in its prompt.
    """

    def __init__(self, persona: str = "careless"):
        self.persona = persona
        self.name = f"demo-{persona}"
        self.step: dict[str, int] = {}
        self.fixed = False
        self.calls = 0

    # -- prompt helpers ---------------------------------------------------
    @staticmethod
    def _goal(prompt: str) -> str:
        for line in prompt.splitlines():
            if line.startswith("Make shop/pricing.py") or line.startswith("Implement export_csv"):
                return line.strip()
        return ""

    def complete(self, prompt: str, *, max_tokens: int = 900) -> Completion:
        self.calls += 1
        tag = task_tag(prompt)
        if tag == "lesson":
            text = self._lesson(prompt)
        elif tag == "act":
            text = self._act(prompt)
        else:
            text = "{}"
        return Completion(text=text, tokens_in=estimate_tokens(prompt), tokens_out=estimate_tokens(text))

    def _lesson(self, prompt: str) -> str:
        """Derive the lesson from the failure excerpt, as a real model would."""
        import re

        m = re.search(r"The failure \(verbatim excerpt\):\n---\n(.*?)\n---", prompt, re.S)
        excerpt = (m.group(1).strip() if m else "").lower()
        if "cannot import name" in excerpt or "modulenotfounderror" in excerpt:
            summary = "Imported a symbol from another module before that module provided it"
            cause = "added the caller and the provider in separate steps without creating the provider"
            fix = "create the providing module first, then run the whole suite"
        else:
            summary = "Broke shared pricing logic while adding an unrelated feature"
            cause = "edited total() as a side effect of adding export_csv"
            fix = "leave unrelated functions untouched, then run the whole suite"
        return json.dumps(
            {
                "summary": summary,
                "cause": cause,
                "fix": fix,
                "trigger": {"files": ["shop/*.py"], "goals": ["pricing|export|shop"]},
                "guard": {"kind": "command", "spec": FULL_SUITE, "must": "exit=0"},
            }
        )

    def _act(self, prompt: str) -> str:
        goal = self._goal(prompt)
        key = "learn" if goal.startswith("Make shop") else "export"
        self.step[key] = self.step.get(key, 0) + 1
        n = self.step[key]
        hears_lesson = "# Lessons from your past mistakes" in prompt

        if key == "learn":
            if n == 1:
                return _write("shop/pricing.py", PRICING_T1_BROKEN)
            if n == 2:
                return _write("shop/helpers.py", HELPERS)
            if n == 3:
                return _run(FULL_SUITE)
            return _finish("added pct() helper and fixed total()")

        # export task
        if n == 1:
            if self.persona == "attentive" and hears_lesson:
                return _write("shop/pricing.py", PRICING_T2_FIXED)
            return _write("tests/test_export.py", TEST_EXPORT)
        if n == 2:
            if self.persona == "attentive" and hears_lesson:
                return _write("tests/test_export.py", TEST_EXPORT)
            return _write("shop/pricing.py", PRICING_T2_BROKEN)
        if self.persona == "attentive" and hears_lesson:
            return _finish("export_csv added, discount logic left intact")
        if self.fixed:
            return _finish("export_csv added; discount regression repaired")
        if "FAIL:" in prompt or "AssertionError" in prompt or "ModuleNotFoundError" in prompt:
            self.fixed = True
            return _write("shop/pricing.py", PRICING_T2_FIXED)
        if "OK" in prompt and "Ran " in prompt:
            return _finish("export_csv added")
        guard_present = "discover -s tests -q" in prompt and "test_export.py" not in prompt
        return _run(FULL_SUITE if guard_present else EXPORT_SUITE)


def _run(cmd: str) -> str:
    return '{"action": "run", "cmd": %s}' % _json(cmd)


def _write(path: str, content: str) -> str:
    return '{"action": "write", "path": %s, "content": %s}' % (_json(path), _json(content))


def _finish(summary: str) -> str:
    return '{"action": "finish", "summary": %s}' % _json(summary)


def _json(value: str) -> str:
    import json

    return json.dumps(value)


def suite_green(root: Path) -> bool:
    proc = subprocess.run(
        FULL_SUITE,
        shell=True,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode == 0


@dataclass
class DemoRecord:
    label: str
    result: RunResult
    green_after: bool
    guard_injected: bool
    guard_catches: int
    failures: int
    steps: int


def run_demo(base: Path, *, quiet: bool = False) -> dict[str, Any]:
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    memory_path = base / "memory.jsonl"
    if memory_path.exists():
        memory_path.unlink()
    memory = MemoryStore(memory_path)

    records: list[DemoRecord] = []

    def execute(label: str, spec_dict: dict, persona: str, use_memory: bool) -> DemoRecord:
        ws = build_repo(base / f"ws_{label}")
        spec = ProblemSpec.from_dict(spec_dict)
        result = run(
            spec,
            model=DemoModel(persona),
            workspace=ws,
            memory=memory if use_memory else None,
            log_path=base / f"events_{label}.jsonl",
        )
        guard = bool(result.compile_report and result.compile_report.injections)
        # every attempt that ran, including the ones a policy check cut short
        attempts = list(result.outcomes) + list(getattr(result, "interrupted", []))
        failures = sum(len(o.failures) for o in attempts)
        catches = sum(1 for o in attempts for f in o.failures if f.get("guard_only"))
        steps = sum(o.steps for o in attempts)
        rec = DemoRecord(label, result, suite_green(ws), guard, catches, failures, steps)
        records.append(rec)
        if not quiet:
            print(f"\n=== {label} ===")
            print(result.render())
            print(f"full suite green afterwards: {rec.green_after}")
        return rec

    execute("A_learn", TASK_LEARN, "careless", True)
    execute("B_no_memory", TASK_EXPORT, "careless", False)
    execute("C_memory_ignored", TASK_EXPORT, "careless", True)
    execute("D_memory_read", TASK_EXPORT, "attentive", True)

    summary = {
        "memory_path": str(memory_path),
        "memory": memory.summary(),
        "records": [
            {
                "run": r.label,
                "status": r.result.status,
                "guard_injected": r.guard_injected,
                "guard_catches": r.guard_catches,
                "l1_failures": r.failures,
                "steps": r.steps,
                "suite_green_after": r.green_after,
            }
            for r in records
        ],
    }
    if not quiet:
        print("\n=== summary ===")
        header = f"{'run':<20}{'status':<12}{'guard':<7}{'catches':<9}{'L1 fails':<10}{'steps':<7}{'suite green'}"
        print(header)
        print("-" * len(header))
        for row in summary["records"]:
            print(
                f"{row['run']:<20}{row['status']:<12}{str(row['guard_injected']):<7}"
                f"{row['guard_catches']:<9}{row['l1_failures']:<10}{row['steps']:<7}{row['suite_green_after']}"
            )
        print("\nmemory:", summary["memory"])
        print(f"\nstored lessons ({memory_path}):")
        for lesson in memory.active():
            print(f"  {lesson.id}  [{lesson.strength} trust={lesson.trust:.2f} catches={lesson.catches}]")
            print(f"    summary : {lesson.summary}")
            print(f"    cause   : {lesson.cause}")
            print(f"    fix     : {lesson.fix}")
            print(f"    when    : files={lesson.trigger.files} goals={lesson.trigger.goals}")
            print(f"    guard   : {(lesson.guard or {}).get('spec')}")
    return summary

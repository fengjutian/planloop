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
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from planloop.model import Completion, OpenAICompatModel, estimate_tokens
from planloop.scheduler import run as planloop_run
from planloop.spec import ProblemSpec
from planloop.tools import Tools

PY = f'"{sys.executable}"'
FULL_SUITE = f"{PY} -m unittest discover -s tests -q"

#: marker written into every held-out test file. It rides in a comment, so
#: unittest never prints it -- but it travels with the file if the model ever
#: copies the judge into the workspace. Used to detect a contaminated run.
ORACLE_MARKER_PREFIX = "planloop-oracle:"

#: A judge that reports pass/fail but hides the reason. Used by the B2D
#: ablation to separate "the model gets another attempt" from "the model is
#: told what went wrong".
QUIET_JUDGE = '''\
"""Runs the held-out oracle and reports pass/fail without the reason."""
import subprocess
import sys

ORACLE = {oracle!r}

proc = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "-s", ORACLE, "-q"],
    capture_output=True, text=True,
)
if proc.returncode == 0:
    sys.exit(0)
sys.stderr.write("ACCEPTANCE FAILED: the acceptance criteria are not satisfied.\\n")
sys.exit(1)
'''


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
    #: optional multi-node plan; default is a single node
    plan: list[dict[str, Any]] | None = None
    #: held-out tests, written **outside** the workspace and used as the judge.
    #: The model can neither read nor modify them, so it cannot edit the test
    #: it is judged by -- the oracle stays independent of the thing measured.
    hidden_tests: dict[str, str] | None = None
    #: show the judge command to the B0 prompt as well, so both configurations
    #: know how they will be judged and only differ in getting feedback
    judge_visible: bool = False
    #: command that judges the workspace; set by :meth:`prepare_oracle`
    _judge: str | None = None
    #: command that judges the workspace *without* saying why it failed
    _quiet: str = ""

    def build(self, root: Path) -> None:
        for path, content in self.initial.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def prepare_oracle(self, base: Path) -> None:
        """Materialise the held-out tests and point the judge at them.

        ``base`` must be a directory *outside* the workspace: the tools lock
        every read and write to the workspace root, so the model cannot reach
        the oracle even if it learns the path.
        """
        if not self.hidden_tests:
            self._judge = self.oracle
            self._quiet = ""
            return
        oracle_dir = base / "oracle"
        oracle_dir.mkdir(parents=True, exist_ok=True)
        for path, content in self.hidden_tests.items():
            target = oracle_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            # The marker rides in a comment on the first line: unittest never
            # prints it, but it travels with the file if the model copies it.
            target.write_text(f"# {ORACLE_MARKER_PREFIX}{self.key}\n{content}", encoding="utf-8")
        # cwd (the workspace) is on sys.path for `python -m unittest`, so the
        # held-out test can import the package under test.
        self._judge = f'{PY} -m unittest discover -s "{oracle_dir}" -q'
        # a judge that fails without saying why -- used by the B2D ablation
        quiet = oracle_dir / "judge.py"
        quiet.write_text(QUIET_JUDGE.format(oracle=str(oracle_dir)), encoding="utf-8")
        self._quiet = f'{PY} "{quiet}"'

    @property
    def judge(self) -> str:
        return self._judge or self.oracle

    @property
    def quiet_judge(self) -> str:
        """Command that fails the acceptance without revealing the reason."""
        return self._quiet or self.oracle

    def spec(self, *, node_acceptance: list[dict[str, Any]] | None = None) -> ProblemSpec:
        # A held-out oracle has to be the *node* acceptance, not just the
        # end-to-end criterion: the in-loop L1 check and the bounded retry both
        # read node.acceptance, and that is where the failure text the model
        # needs on the second attempt comes from.
        if node_acceptance is not None:
            acceptance = node_acceptance
        elif self.hidden_tests:
            acceptance = [{"id": "A1", "kind": "command", "spec": self.judge, "must": "exit=0"}]
        else:
            acceptance = self.acceptance
        return ProblemSpec.from_dict(
            {
                "goal": self.goal,
                "success_criteria": [
                    {"id": "SC1", "kind": "command", "spec": self.judge, "must": "exit=0"}
                ],
                "context_files": sorted(self.initial),
                "limits": {"max_attempts_per_node": 2, "max_steps_per_node": 8},
                "plan_template": {
                    "nodes": self.plan
                    or [
                        {"id": "T1", "goal": self.goal, "est_cost": 0.5, "acceptance": acceptance}
                    ]
                },
            }
        )

    def passes(self, root: Path) -> bool:
        proc = subprocess.run(
            self.judge, shell=True, cwd=str(root), capture_output=True, text=True,
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
        key="add_feature_checked",
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
        # the judge is held out: a model that writes `assertTrue(True)` as its
        # "test" cannot buy a pass by editing the test it is judged by
        judge_visible=True,
        hidden_tests={
            "test_mul_checked.py": (
                "import unittest\nfrom calc.core import add, mul\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_mul_really_multiplies(self):\n"
                "        self.assertEqual(mul(3, 4), 12)\n"
                "    def test_add_still_adds(self):\n"
                "        self.assertEqual(add(2, 2), 4)\n"
            ),
        },
    ),
    Task(
        key="division_convention",
        goal="给 calc 增加 divide(a, b)，返回 a / b，并补测试，不要破坏 add()",
        initial={
            "calc/__init__.py": "",
            "calc/core.py": "def add(a, b):\n    return a + b\n",
            "calc/README.md": (
                "# calc 约定\n\n"
                "- 除零时统一抛 `ValueError`，不要让 `ZeroDivisionError` 冒到调用方。\n"
                "- 对外只暴露 `add` 与 `divide`。\n"
            ),
            "tests/test_add.py": (
                "import unittest\nfrom calc.core import add\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 2), 4)\n"
            ),
        },
        correct={
            "calc/core.py": (
                "def add(a, b):\n    return a + b\n\n\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return a / b\n"
            ),
            "tests/test_divide.py": (
                "import unittest\nfrom calc.core import divide\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_divide(self):\n"
                "        self.assertEqual(divide(6, 3), 2)\n"
            ),
        },
        wrong={
            "calc/core.py": (
                "def add(a, b):\n    return a + b\n\n\n"
                "def divide(a, b):\n    return a / b\n"
            ),
            "tests/test_divide.py": (
                "import unittest\nfrom calc.core import divide\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_divide(self):\n"
                "        self.assertEqual(divide(6, 3), 2)\n"
            ),
        },
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
        judge_visible=True,
        hidden_tests={
            "test_divide_checked.py": (
                "import unittest\nfrom calc.core import add, divide\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_divide(self):\n"
                "        self.assertEqual(divide(6, 3), 2)\n"
                "    def test_divide_by_zero_raises_value_error(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            divide(1, 0)\n"
                "    def test_add_still_adds(self):\n"
                "        self.assertEqual(add(2, 2), 4)\n"
            ),
        },
    ),
    Task(
        key="legacy_copy",
        goal="把计费逻辑改成按 TAX_RATE 计税（结算金额 = 小计 − 税额），app/pricing.py 与 app/legacy/pricing.py 两处都要改",
        initial={
            "app/__init__.py": "",
            "app/pricing.py": (
                "TAX_RATE = 0.2\n\n\ndef total(items):\n    return sum(items)\n"
            ),
            "app/legacy/__init__.py": "",
            "app/legacy/pricing.py": (
                "TAX_RATE = 0.2\n\n\ndef total(items):\n    return sum(items)\n"
            ),
            "app/service.py": (
                "from app.pricing import total\n\n\ndef checkout(items):\n    return total(items)\n"
            ),
            "app/legacy_service.py": (
                "from app.legacy.pricing import total\n\n\n"
                "def legacy_checkout(items):\n    return total(items)\n"
            ),
            "docs/BILLING.md": (
                "# 计费约定\n\n"
                "- 税额 = 小计 × `TAX_RATE`（默认 0.2），结算金额 = 小计 − 税额。\n"
                "- `app/pricing.py` 与 `app/legacy/pricing.py` 是同一份逻辑的两份拷贝，改动要保持一致。\n"
            ),
            # the checked-in test still points at the archived copy
            "tests/test_legacy_service.py": (
                "import unittest\nfrom app.legacy_service import legacy_checkout\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_legacy_checkout(self):\n"
                "        self.assertEqual(legacy_checkout([100]), 80)\n"
            ),
        },
        correct={
            "app/pricing.py": (
                "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                "    subtotal = sum(items)\n"
                "    return subtotal - subtotal * TAX_RATE\n"
            ),
            "app/legacy/pricing.py": (
                "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                "    subtotal = sum(items)\n"
                "    return subtotal - subtotal * TAX_RATE\n"
            ),
        },
        wrong={
            # the systematic error: the visible test points at the archived
            # copy, so only that one gets fixed
            "app/legacy/pricing.py": (
                "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                "    subtotal = sum(items)\n"
                "    return subtotal - subtotal * TAX_RATE\n"
            ),
        },
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
        judge_visible=True,
        hidden_tests={
            "test_billing_checked.py": (
                "import unittest\nfrom app.service import checkout\n"
                "from app.legacy_service import legacy_checkout\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_checkout(self):\n"
                "        self.assertEqual(checkout([100]), 80)\n"
                "    def test_legacy_checkout(self):\n"
                "        self.assertEqual(legacy_checkout([100]), 80)\n"
            ),
        },
    ),
    Task(
        key="divide_zero_hidden",
        goal="给 calc 增加 divide(a, b)，返回 a / b，并补测试，不要破坏 add()",
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
                "def add(a, b):\n    return a + b\n\n\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return a / b\n"
            ),
            "tests/test_divide.py": (
                "import unittest\nfrom calc.core import divide\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_divide(self):\n"
                "        self.assertEqual(divide(6, 3), 2)\n"
            ),
        },
        wrong={
            "calc/core.py": (
                "def add(a, b):\n    return a + b\n\n\n"
                "def divide(a, b):\n    return a / b\n"
            ),
            "tests/test_divide.py": (
                "import unittest\nfrom calc.core import divide\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_divide(self):\n"
                "        self.assertEqual(divide(6, 3), 2)\n"
            ),
        },
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
        # the judge knows an acceptance rule the model cannot read: the command
        # is visible, its assertions are not. The only channel that tells the
        # model about it is the failure output -- which is exactly what an
        # enforced acceptance check produces and a self-report does not.
        judge_visible=True,
        hidden_tests={
            "test_divide_checked.py": (
                "import unittest\nfrom calc.core import add, divide\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_divide(self):\n"
                "        self.assertEqual(divide(6, 3), 2)\n"
                "    def test_divide_by_zero_raises_value_error(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            divide(1, 0)\n"
                "    def test_add_still_adds(self):\n"
                "        self.assertEqual(add(2, 2), 4)\n"
            ),
        },
    ),
    Task(
        key="mean_empty_hidden",
        goal="给 stats 增加 mean(values)，返回所有数的平均值，并补测试",
        initial={
            "stats/__init__.py": "",
            "stats/core.py": "def median(values):\n    return sorted(values)[len(values) // 2]\n",
            "tests/test_median.py": (
                "import unittest\nfrom stats.core import median\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_median(self):\n"
                "        self.assertEqual(median([1, 3, 2]), 2)\n"
            ),
        },
        correct={
            "stats/core.py": (
                "def median(values):\n    return sorted(values)[len(values) // 2]\n\n\n"
                "def mean(values):\n"
                "    if not values:\n"
                "        return 0\n"
                "    return sum(values) / len(values)\n"
            ),
            "tests/test_mean.py": (
                "import unittest\nfrom stats.core import mean\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_mean(self):\n"
                "        self.assertEqual(mean([1, 2, 3]), 2)\n"
            ),
        },
        wrong={
            "stats/core.py": (
                "def median(values):\n    return sorted(values)[len(values) // 2]\n\n\n"
                "def mean(values):\n    return sum(values) / len(values)\n"
            ),
            "tests/test_mean.py": (
                "import unittest\nfrom stats.core import mean\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_mean(self):\n"
                "        self.assertEqual(mean([1, 2, 3]), 2)\n"
            ),
        },
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
        judge_visible=True,
        hidden_tests={
            "test_mean_checked.py": (
                "import unittest\nfrom stats.core import mean, median\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_mean(self):\n"
                "        self.assertEqual(mean([1, 2, 3]), 2)\n"
                "    def test_mean_of_empty_is_zero(self):\n"
                "        self.assertEqual(mean([]), 0)\n"
                "    def test_median_still_works(self):\n"
                "        self.assertEqual(median([1, 3, 2]), 2)\n"
            ),
        },
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
    Task(
        key="compat_rename",
        goal="把 area 改名为 compute_area，更新调用点，但保留 area 作为兼容别名",
        initial={
            "lib/__init__.py": "",
            "lib/core.py": "def area(w, h):\n    return w * h\n",
            "lib/api.py": (
                "from lib.core import area\n\n\ndef rect(w, h):\n    return area(w, h)\n"
            ),
            "tests/test_api.py": (
                "import unittest\nfrom lib.api import rect\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_rect(self):\n"
                "        self.assertEqual(rect(2, 3), 6)\n"
            ),
            "tests/test_compat.py": (
                "import unittest\nfrom lib.core import area\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_old_name_still_works(self):\n"
                "        self.assertEqual(area(2, 3), 6)\n"
            ),
        },
        correct={
            "lib/core.py": (
                "def compute_area(w, h):\n    return w * h\n\n\n"
                "area = compute_area  # deprecated alias\n"
            ),
            "lib/api.py": (
                "from lib.core import compute_area\n\n\ndef rect(w, h):\n    return compute_area(w, h)\n"
            ),
        },
        wrong={
            "lib/core.py": "def compute_area(w, h):\n    return w * h\n",
            "lib/api.py": (
                "from lib.core import compute_area\n\n\ndef rect(w, h):\n    return compute_area(w, h)\n"
            ),
        },
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    ),
    Task(
        key="noisy_fix",
        goal="修复折扣计算，让 tests/test_pricing.py 通过",
        initial={
            "app/__init__.py": "",
            "app/pricing.py": "def total(prices, pct):\n    return sum(prices) * pct / 100\n",
            "app/legacy/pricing.py": (
                "# old implementation, no longer imported\ndef total(prices, pct):\n"
                "    return sum(prices) * pct / 100\n"
            ),
            "app/legacy/__init__.py": "",
            "tests/test_pricing.py": (
                "import unittest\nfrom app.pricing import total\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_total(self):\n"
                "        self.assertEqual(total([100, 200], 10), 270)\n"
            ),
        },
        correct={
            "app/pricing.py": "def total(prices, pct):\n    return sum(prices) * (100 - pct) / 100\n",
        },
        wrong={
            "app/legacy/pricing.py": (
                "# old implementation, no longer imported\ndef total(prices, pct):\n"
                "    return sum(prices) * (100 - pct) / 100\n"
            ),
        },
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    ),
    Task(
        key="long_rename_chain",
        goal="把 ledger.core.total 改名为 compute_total，更新全部 5 个调用点；兼容别名只放在 ledger/__init__.py",
        initial={'ledger/__init__.py': '', 'ledger/core.py': 'def total(items):\n    return sum(items)\n', 'ledger/cart.py': 'from ledger.core import total\n\n\ndef cart_total(items):\n    return total(items)\n', 'ledger/checkout.py': 'from ledger.core import total\n\n\ndef checkout(items, pct):\n    return total(items) * (100 - pct) / 100\n', 'ledger/report.py': 'from ledger.core import total\n\n\ndef summary(items):\n    return f"total={total(items)}"\n', 'ledger/admin.py': 'from ledger.core import total\n\n\ndef audit(items):\n    return total(items) > 0\n', 'ledger/export.py': 'from ledger.core import total\n\n\ndef as_row(items):\n    return [str(total(items))]\n', 'tests/__init__.py': '', 'tests/test_core.py': 'import unittest\nfrom ledger import total\n\n\nclass T(unittest.TestCase):\n    def test_total(self):\n        self.assertEqual(total([1, 2]), 3)\n', 'tests/test_checkout.py': 'import unittest\nfrom ledger.checkout import checkout\n\n\nclass T(unittest.TestCase):\n    def test_checkout(self):\n        self.assertEqual(checkout([100, 200], 10), 270)\n', 'tests/test_report.py': "import unittest\nfrom ledger.export import as_row\n\n\nclass T(unittest.TestCase):\n    def test_export(self):\n        self.assertEqual(as_row([5]), ['5'])\n", 'tests/test_admin.py': 'import unittest\nfrom ledger.admin import audit\n\n\nclass T(unittest.TestCase):\n    def test_audit(self):\n        self.assertTrue(audit([5]))\n'},
        correct={'ledger/cart.py': 'from ledger.core import compute_total\n\n\ndef cart_total(items):\n    return compute_total(items)\n', 'ledger/checkout.py': 'from ledger.core import compute_total\n\n\ndef checkout(items, pct):\n    return compute_total(items) * (100 - pct) / 100\n', 'ledger/report.py': 'from ledger.core import compute_total\n\n\ndef summary(items):\n    return f"total={compute_total(items)}"\n', 'ledger/admin.py': 'from ledger.core import compute_total\n\n\ndef audit(items):\n    return compute_total(items) > 0\n', 'ledger/export.py': 'from ledger.core import compute_total\n\n\ndef as_row(items):\n    return [str(compute_total(items))]\n', 'ledger/core.py': 'def compute_total(items):\n    return sum(items)\n', 'ledger/__init__.py': 'from ledger.core import compute_total\n\ntotal = compute_total  # deprecated alias\n'},
        wrong={'ledger/cart.py': 'from ledger.core import compute_total\n\n\ndef cart_total(items):\n    return compute_total(items)\n', 'ledger/checkout.py': 'from ledger.core import compute_total\n\n\ndef checkout(items, pct):\n    return compute_total(items) * (100 - pct) / 100\n', 'ledger/report.py': 'from ledger.core import compute_total\n\n\ndef summary(items):\n    return f"total={compute_total(items)}"\n', 'ledger/admin.py': 'from ledger.core import total\n\n\ndef audit(items):\n    return total(items) > 0\n', 'ledger/export.py': 'from ledger.core import compute_total\n\n\ndef as_row(items):\n    return [str(compute_total(items))]\n', 'ledger/core.py': 'def compute_total(items):\n    return sum(items)\n', 'ledger/__init__.py': 'from ledger.core import compute_total\n\ntotal = compute_total  # deprecated alias\n'},
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
        plan=[
            {"id": "T1", "goal": "改名 core 并把别名放进 ledger/__init__.py", "est_cost": 0.3,
             "context_files": ["ledger/core.py", "ledger/__init__.py", "tests/test_core.py"],
             "acceptance": [{"id": "A1", "kind": "command",
                             "spec": f"{PY} -m unittest tests.test_core -q", "must": "exit=0"}]},
            {"id": "T2", "deps": ["T1"], "goal": "更新全部 5 个调用点，让全量测试通过",
             "est_cost": 0.6,
             "context_files": ["ledger/cart.py", "ledger/checkout.py", "ledger/report.py",
                                "ledger/admin.py", "ledger/export.py", "ledger/core.py"],
             "acceptance": [{"id": "A2", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}]},
        ],
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


PROTOCOL = """You are working in a small repository. Reply with exactly ONE JSON object:
{"action": "write", "path": "<file>", "content": "<full file content>"}
{"action": "write_many", "files": {"<file>": "<content>", "<file2>": "<content>"}}
{"action": "run", "cmd": "<shell command>"}
{"action": "finish", "summary": "<what you did>"}

Current files:
{files}

Goal: {goal}
"""


#: context budget of the B0 prompt. The B2 executor injects up to 8000
#: characters of context files (execute._files_block); B0 used to get half
#: that, which handed the larger tasks to B2 for free.
CONTEXT_BUDGET = 8000


def task_prompt(task: Task, root: Path, *, limit: int = CONTEXT_BUDGET) -> str:
    chunks = []
    budget = limit
    for path in sorted(task.initial):
        target = root / path
        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8")
        budget -= len(text)
        if budget < 0:
            break
        chunks.append(f"--- {path} ---\n{text}")
    prompt = PROTOCOL.replace("{files}", "\n".join(chunks)).replace("{goal}", task.goal)
    if task.judge_visible:
        # Both configurations must know how they will be judged; the only
        # difference between them is whether the failure is reported back.
        prompt += f"\nAcceptance (this command decides pass/fail):\n{task.judge}\n"
    return prompt


def real_model_factory(model_name: str, base_url: str, api_key: str, temperature: float = 0.2):
    def build(task: Task, rng=None):
        return OpenAICompatModel(
            model_name, base_url=base_url, api_key=api_key, temperature=temperature
        )

    return build


def scripted_model_factory(p_correct: float):
    def build(task: Task, rng):
        return AttemptModel(task, p_correct=p_correct, rng=rng)

    return build


#: set by the CLI when a real endpoint is configured
MODEL_FACTORY = None


def _make(task: Task, p_correct: float, rng: random.Random):
    if MODEL_FACTORY is not None:
        return MODEL_FACTORY(task, rng)
    return AttemptModel(task, p_correct=p_correct, rng=rng)


def _act(task: Task, root: Path, model, *, max_steps: int = 12) -> tuple[int, int]:
    """The B0 primitive: act until the model says finish. No verification.

    Returns ``(tokens, calls)`` -- the call count is the only attempt signal
    this arm has, and it has to be measured rather than assumed to be 1.
    """
    tools = Tools(root)
    tokens = 0
    calls = 0
    for _ in range(max_steps):
        prompt = task_prompt(task, root)
        comp = model.complete(prompt, max_tokens=4000)
        calls += 1
        tokens += comp.tokens
        try:
            action = json.loads(_extract(comp.text))
        except (ValueError, json.JSONDecodeError):
            break
        kind = action.get("action")
        if kind == "write":
            tools.write(str(action.get("path", "")), str(action.get("content", "")))
        elif kind == "write_many":
            for path, content in (action.get("files") or {}).items():
                tools.write(str(path), str(content))
        elif kind == "run":
            tools.run(str(action.get("cmd", "")))
        elif kind == "finish":
            break
    return tokens, calls


def _extract(text: str) -> str:
    """Reasoning models prefix a <think> block; keep the first JSON object."""
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else text


# --------------------------------------------------------------------------
# runners
# --------------------------------------------------------------------------
@dataclass
class Outcome:
    config: str
    task: str
    success: bool
    tokens: int
    #: how many times the executor was invoked.  The unit differs per arm and
    #: the report has to say which is which:
    #:   B0  -> model calls inside the single episode (no verification gate)
    #:   B1  -> independent attempts (n)
    #:   B2* -> executor invocations; on a single-node task that is
    #:          1 + (number of retries after a failed acceptance)
    #: It can be 0: a run that was blocked before any node finished (policy
    #: approval, denied action) never completed an executor run, and that is
    #: **not** the same thing as "the node ran once and failed".
    attempts: int
    seconds: float
    #: the model copied the held-out judge into the workspace -> the run is
    #: invalid, because the requirement was no longer invisible to it
    contaminated: bool = False
    #: status of the planloop run ("succeeded" / "blocked" / ...)
    status: str = ""
    #: why the run was blocked, when it was
    blocked: str = ""


def run_b0(task: Task, *, p_correct: float, rng: random.Random) -> Outcome:
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "ws"
        root.mkdir(parents=True, exist_ok=True)
        task.build(root)
        task.prepare_oracle(base)
        model = _make(task, p_correct, rng)
        tokens, calls = _act(task, root, model)
        success = task.passes(root)
    return Outcome("B0", task.key, success, tokens, calls, time.monotonic() - started, scan_contamination(root, task.key))


def workspace_snapshot(root: Path) -> dict[str, str]:
    """Every file the model produced, so no edit is silently dropped.

    The reference solution lists the files that *should* change; a real model
    rewrites whatever it likes. Fingerprinting only ``task.correct`` would let
    the winning attempt materialise without the model's other edits.
    """
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        files[rel] = path.read_text(encoding="utf-8", errors="replace")
    return files


def fingerprint(files: dict[str, str]) -> str:
    return "\n".join(f"{p}:{files[p]}" for p in sorted(files))


def scan_contamination(root: Path, task_key: str) -> bool:
    """Did the model copy the held-out judge into the workspace?

    A shell-capable model *could* read the oracle (the sandbox is not an
    isolation boundary). The tool layer blocks direct reads, but a copy would
    show up in the workspace -- and a copied judge means the model saw the
    requirement, which invalidates the "it could not have known" premise.
    """
    marker = f"{ORACLE_MARKER_PREFIX}{task_key}"
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def run_b1(task: Task, *, p_correct: float, rng: random.Random, n: int = 3) -> Outcome:
    """N attempts, keep the most common artefact set. Still no verification.

    The winner is judged by the **oracle**, not by string equality with the
    reference solution: a real model may fix the bug in a different but equally
    valid way, and scoring that as a failure would be wrong.
    """
    started = time.monotonic()
    votes: dict[str, int] = {}
    artefacts: dict[str, dict[str, str]] = {}
    tokens = 0
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        task.prepare_oracle(base)
        for attempt in range(n):
            root = base / f"attempt_{attempt}"
            root.mkdir(parents=True, exist_ok=True)
            task.build(root)
            model = _make(task, p_correct, rng)
            tokens += _act(task, root, model)[0]
            files = workspace_snapshot(root)
            key = fingerprint(files)
            votes[key] = votes.get(key, 0) + 1
            artefacts[key] = files
    winner = max(votes.items(), key=lambda kv: kv[1])[0]

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "ws"
        root.mkdir(parents=True, exist_ok=True)
        task.build(root)
        task.prepare_oracle(base)
        for path, content in artefacts[winner].items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        success = task.passes(root)
    return Outcome("B1", task.key, success, tokens, n, time.monotonic() - started, scan_contamination(root, task.key))


def run_b2(task: Task, *, p_correct: float, rng: random.Random) -> Outcome:
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "ws"
        root.mkdir(parents=True, exist_ok=True)
        task.build(root)
        task.prepare_oracle(base)
        model = _make(task, p_correct, rng)
        result = planloop_run(task.spec(), model=model, workspace=root)
        tokens = result.usage.get("tokens", 0)
        attempts = _node_executions(result)
        status, blocked = _run_status(result)
        success = task.passes(root)
    return Outcome("B2", task.key, success, tokens, attempts, time.monotonic() - started,
                   scan_contamination(root, task.key), status, blocked)


def _node_executions(result) -> int:
    """How many times the executor was actually invoked.

    This is the model-agnostic attempt signal: the scheduler appends one
    outcome per node execution, so a single-node task that was retried once
    reports 2.  Reading it off ``model.attempts`` instead would be wrong --
    a real model object has no such attribute, and the scripted one counts
    prompts rather than node runs.

    It is deliberately **not** floored at 1: a run that was blocked before any
    node finished (an approval request, a denied action) completed zero
    executor runs, and reporting that as 1 would make it look like an
    ordinary failure.
    """
    outcomes = getattr(result, "outcomes", None) or []
    return len(outcomes)


def _run_status(result) -> tuple[str, str]:
    """(status, blocked reason) of a planloop run."""
    status = str(getattr(result, "status", "") or "")
    blocked = getattr(result, "blocked", None)
    reason = str(getattr(blocked, "reason", "") or "") if blocked else ""
    return status, reason


#: reasons that mean the run never got to exercise the acceptance.  Every other
#: blocked run is an ordinary outcome: the plan finished but the code was
#: wrong ("end-to-end acceptance failed"), or the node kept failing
#: ("execution failure persisted over N attempts").  Those are results, not
#: interruptions, and they must stay in the comparison.
INTERRUPT_MARKERS = (
    "needs approval", "refused by policy", "denied by policy",
    "policy blocked", "needs_approval", "denied_by_policy",
)


def is_interrupted(outcome: "Outcome") -> bool:
    """Was this run cut short before it measured the acceptance?"""
    if outcome.attempts == 0 and outcome.status == "blocked":
        return True
    reason = (outcome.blocked or "").lower()
    return any(marker in reason for marker in INTERRUPT_MARKERS)


#: an acceptance that always passes -- used by the B2C ablation
NEVER_FAILS = [{"id": "A1", "kind": "command", "spec": f'{PY} -c "pass"', "must": "exit=0"}]


def run_b2c(task: Task, *, p_correct: float, rng: random.Random) -> Outcome:
    """**Ablation**: the planloop executor with the verification switched off.

    Same context injection, same tools, same early stop, same goal -- but the
    node acceptance always passes, so the model never sees a failure and never
    gets a retry. The outcome is still judged by the held-out oracle, so the
    measurement stays honest; what changes is only whether the model is told.

    B0 vs B2C isolates the harness, B2C vs B2 isolates the feedback channel.
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "ws"
        root.mkdir(parents=True, exist_ok=True)
        task.build(root)
        task.prepare_oracle(base)
        model = _make(task, p_correct, rng)
        result = planloop_run(task.spec(node_acceptance=NEVER_FAILS), model=model, workspace=root)
        tokens = result.usage.get("tokens", 0)
        attempts = _node_executions(result)
        status, blocked = _run_status(result)
        success = task.passes(root)
    return Outcome("B2C", task.key, success, tokens, attempts, time.monotonic() - started,
                   scan_contamination(root, task.key), status, blocked)


def run_b2d(task: Task, *, p_correct: float, rng: random.Random) -> Outcome:
    """**Ablation**: the model is told it failed, but not *why*.

    The node acceptance is the quiet judge: it fails exactly like the held-out
    oracle, but its output carries no diagnostic. The node therefore fails,
    the scheduler retries, and the model gets a second attempt -- the only
    thing missing compared with B2 is the failure text.

    B2D vs B2 separates "another attempt" from "an attempt that knows what
    went wrong"; B2D vs B2C separates "another attempt" from "no attempt".
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "ws"
        root.mkdir(parents=True, exist_ok=True)
        task.build(root)
        task.prepare_oracle(base)
        model = _make(task, p_correct, rng)
        result = planloop_run(
            task.spec(node_acceptance=[{"id": "A1", "kind": "command",
                                        "spec": task.quiet_judge, "must": "exit=0"}]),
            model=model, workspace=root,
        )
        tokens = result.usage.get("tokens", 0)
        attempts = _node_executions(result)
        status, blocked = _run_status(result)
        success = task.passes(root)
    return Outcome("B2D", task.key, success, tokens, attempts, time.monotonic() - started,
                   scan_contamination(root, task.key), status, blocked)


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
@dataclass
class Report:
    reps: int
    p_correct: float
    seed: int
    outcomes: list[Outcome] = field(default_factory=list)
    #: sampling temperature of the real model (0.2 = nearly deterministic)
    temperature: float = 0.2

    def summary(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {}
        for config in ("B0", "B1", "B2", "B2C", "B2D"):
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
        order = ["B0", "B1", "B2C", "B2D", "B2"]
        present = [c for c in order if any(o.config == c for o in self.outcomes)]
        lines = [
            f"# Benchmark: {' / '.join(present) if present else '(no runs)'}",
            "",
            f"- repetitions per cell: **{self.reps}**",
            f"- per-attempt success probability of the scripted model: **{self.p_correct}**",
            f"- seed: {self.seed}",
            f"- sampling temperature: **{self.temperature}**",
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
        lines += ["", "## 按任务", "",
                  "| task | config | success | tokens | attempts | status |",
                  "|---|---|---|---|---|---|"]
        for outcome in self.outcomes:
            lines.append(
                f"| {outcome.task} | {outcome.config} | {'yes' if outcome.success else 'no'} | "
                f"{outcome.tokens} | {outcome.attempts} | {outcome.status or '-'} |"
            )
        interrupted = [o for o in self.outcomes if is_interrupted(o)]
        if interrupted:
            lines += [
                "",
                f"被中断的运行：**{len(interrupted)}** 次"
                "（这些格没有测到验收行为，不应计入成功率比较）：",
                "",
            ]
            for o in interrupted:
                lines.append(f"- {o.config} / {o.task}：{o.blocked}")
        dirty = [o for o in self.outcomes if o.contaminated]
        lines += [
            "",
            f"判据污染检查：{len(self.outcomes) - len(dirty)}/{len(self.outcomes)} 次运行"
            "未在工作区里发现 held-out 判据的副本（污染的运行不计入结论）。",
        ]
        return "\n".join(lines) + "\n"


def benchmark(*, reps: int = 5, p_correct: float = 0.6, seed: int = 0,
              tasks: list[Task] | None = None, n_best: int = 3,
              configs: tuple[str, ...] = ("B0", "B1", "B2"),
              temperature: float = 0.2) -> Report:
    rng = random.Random(seed)
    report = Report(reps=reps, p_correct=p_correct, seed=seed, temperature=temperature)
    for _ in range(reps):
        for task in tasks or TASKS:
            runners = []
            if "B0" in configs:
                runners.append(lambda: run_b0(task, p_correct=p_correct, rng=rng))
            if "B1" in configs:
                runners.append(lambda: run_b1(task, p_correct=p_correct, rng=rng, n=n_best))
            if "B2" in configs:
                runners.append(lambda: run_b2(task, p_correct=p_correct, rng=rng))
            if "B2C" in configs:
                runners.append(lambda: run_b2c(task, p_correct=p_correct, rng=rng))
            if "B2D" in configs:
                runners.append(lambda: run_b2d(task, p_correct=p_correct, rng=rng))
            for runner in runners:
                outcome = runner()
                report.outcomes.append(outcome)
                print(
                    f"[{outcome.config}] {outcome.task}: success={outcome.success} "
                    f"tokens={outcome.tokens} attempts={outcome.attempts} "
                    f"status={outcome.status or 'n/a'} {outcome.seconds:.0f}s"
                    + (f" blocked={outcome.blocked}" if outcome.blocked else "")
                    + ("  CONTAMINATED" if outcome.contaminated else ""),
                    file=sys.stderr,
                    flush=True,
                )
    return report


def make_rename_chain(index: int, *, callers: int) -> Task:
    """A rename that must propagate to N call sites, each covered by a test.

    Same failure mode every time: the model renames the definition and misses
    one caller. The number of call sites is the knob, so difficulty scales.
    """
    module = f"chain{index}"
    initial: dict[str, str] = {
        f"{module}/__init__.py": "",
        f"{module}/core.py": "def total(items):\n    return sum(items)\n",
        "tests/__init__.py": "",
        f"tests/test_core.py": (
            "import unittest\n"
            f"from {module} import total\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_total(self):\n"
            "        self.assertEqual(total([1, 2]), 3)\n"
        ),
    }
    correct = {
        f"{module}/core.py": "def compute_total(items):\n    return sum(items)\n",
        f"{module}/__init__.py": (
            "from .core import compute_total\n\ntotal = compute_total  # deprecated alias\n"
        ),
    }
    wrong = dict(correct)

    for i in range(callers):
        caller = f"{module}/c{i}.py"
        test = f"tests/test_c{i}.py"
        initial[caller] = (
            f"from {module}.core import total\n\n\ndef use{i}(items):\n    return total(items) + {i}\n"
        )
        initial[test] = (
            "import unittest\n"
            f"from {module}.c{i} import use{i}\n\n\n"
            "class T(unittest.TestCase):\n"
            f"    def test_use{i}(self):\n"
            f"        self.assertEqual(use{i}([1, 2]), {3 + i})\n"
        )
        correct[caller] = (
            f"from {module}.core import compute_total\n\n\ndef use{i}(items):\n"
            f"    return compute_total(items) + {i}\n"
        )

    # the trap: the last caller is left on the old name
    missed = f"{module}/c{callers - 1}.py"
    wrong[missed] = initial[missed]

    return Task(
        key=f"rename_chain_{callers}",
        goal=f"把 {module}.core.total 改名为 compute_total，更新全部 {callers} 个调用点，"
             f"兼容别名只放在 {module}/__init__.py",
        initial=initial,
        correct=correct,
        wrong=wrong,
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
        plan=[
            {
                "id": "T1",
                "goal": f"改名 {module}/core.py 并更新 {module}/__init__.py 的别名",
                "est_cost": 0.2,
                "acceptance": [{"id": "A1", "kind": "command",
                                "spec": f"{PY} -m unittest {module.replace('/', '.')} tests.test_core -q",
                                "must": "exit=0"}],
            },
            {
                "id": "T2",
                "deps": ["T1"],
                "goal": f"更新全部 {callers} 个调用点，让全量测试通过",
                "est_cost": 0.7,
                "acceptance": [{"id": "A2", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
            },
        ],
    )


TASKS.extend(make_rename_chain(i, callers=n) for i, n in enumerate((3, 4, 5, 6, 7, 8)))

"""Fault injection: how much of a *known* defect set does a criterion catch?

``bench.py`` measures whether **feedback** changes a model's success rate.
It cannot tell us whether the judge itself is sharp: a criterion that accepts
everything reports 100% success for entirely the wrong reasons.

This module measures the other half -- **criterion recall**:

    for each seeded defect (a mutant), does the criterion fail?

    recall = detected defects / seeded defects

Two criteria are compared for every defect:

* the **strong** criterion -- the held-out oracle that lives *outside* the
  workspace (the one ``bench.py`` uses). The model can neither read nor edit
  it.
* the **weak** criterion -- the test file the model itself could have written,
  and can still edit (the visible test inside the workspace).

A defect the strong criterion catches but the weak one misses is exactly the
case where "the model wrote its own test" is not enough. It is what the hidden
criterion buys, measured instead of asserted.

Three controls keep the number honest:

* the **correct** solution must pass both criteria -- no criterion may fail
  everything;
* a **neutral** rewrite (semantically identical, syntactically different)
  must also pass -- no criterion may kill correct work;
* every mutant is compared against the correct solution with a **behaviour
  probe**. A mutant that behaves identically is an *equivalent mutant*: the
  injected defect is not a defect at all, so it must not be counted as a
  criterion miss. Mutants are classified into three states --
  caught / missed / equivalent -- and only the first two enter the recall
  denominator.

Nothing here calls a model. This measures the *criterion*, not the agent.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from planloop.bench import FULL_SUITE, Task

#: defect families, grouped by what they break.
FAMILIES: dict[str, str] = {
    "missing_guard": "漏边界/缺守卫",
    "wrong_exception_type": "错异常类型",
    "swallowed_error": "吞掉异常",
    "unhelpful_message": "错误信息不可定位",
    "inverted_condition": "条件写反",
    "integer_division": "整除代替真除",
    "wrong_direction": "方向/符号错",
    "incomplete_computation": "公式只算了一半",
    "regression": "破坏既有功能",
    "missing_symbol": "符号缺失/命名不一致",
    "wrong_return_type": "返回类型错/精度截断",
    "swapped_args": "参数顺序颠倒",
    "mutates_input": "改写调用方数据",
    "state_leak": "副作用/全局状态污染",
    "partial_fix": "只修了一半",
}


@dataclass
class Mutant:
    """One seeded defect: the files that realise it."""

    key: str
    family: str
    files: dict[str, str]
    note: str = ""


@dataclass
class FaultCase:
    """A task plus the defects that can be injected into its solution."""

    key: str
    goal: str
    initial: dict[str, str]
    correct: dict[str, str]
    hidden_tests: dict[str, str]
    mutants: list[Mutant]
    #: script that prints the observable behaviour of the implementation; used
    #: to tell a *real* defect from an equivalent mutant.
    probe: str = ""
    neutral: list[Mutant] = field(default_factory=list)


# --------------------------------------------------------------------------
# probes: what the implementation observably does
# --------------------------------------------------------------------------
_CALC_PROBE = '''\
from calc.core import add, divide


def show(fn, *args):
    try:
        return repr(fn(*args))
    except Exception as exc:
        return type(exc).__name__ + ": " + str(exc)


print(show(divide, 6, 3))
print(show(divide, 1, 2))
print(show(divide, 1, 0))
print(show(divide, 0, 5))
print(show(add, 2, 2))
'''

_STATS_PROBE = '''\
from stats.core import mean, median


def show(fn, *args):
    try:
        return repr(fn(*args))
    except Exception as exc:
        return type(exc).__name__ + ": " + str(exc)


print(show(mean, [1, 2, 3]))
print(show(mean, []))
print(show(mean, [1, 2]))
print(show(mean, ["a", "b"]))
print(show(median, [1, 2, 3, 4]))
print(show(median, [1, 3, 2]))
data = [3, 1, 2]
show(mean, data)
print(data)
'''

_SHOP_PROBE = '''\
from app.service import checkout
from app.legacy_service import legacy_checkout


def show(fn, *args):
    try:
        return repr(fn(*args))
    except Exception as exc:
        return type(exc).__name__ + ": " + str(exc)


print(show(checkout, [100]))
print(show(checkout, [200]))
print(show(legacy_checkout, [100]))
print(show(checkout, [-1]))
data = [100, 50]
show(checkout, data)
print(data)
'''


# --------------------------------------------------------------------------
# case 1 -- calc.divide: guards, exception contract, regressions
# --------------------------------------------------------------------------
_CALC_INITIAL = {
    "calc/__init__.py": "",
    "calc/core.py": "def add(a, b):\n    return a + b\n",
    "tests/test_add.py": (
        "import unittest\nfrom calc.core import add\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 2), 4)\n"
    ),
    # the visible test: exactly what a model would write for "add a divide"
    "tests/test_divide.py": (
        "import unittest\nfrom calc.core import divide\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_divide(self):\n"
        "        self.assertEqual(divide(6, 3), 2)\n"
    ),
}

_CALC_CORRECT = {
    "calc/core.py": (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def divide(a, b):\n"
        "    if b == 0:\n"
        "        raise ValueError('division by zero')\n"
        "    return a / b\n"
    ),
}

_CALC_HIDDEN = {
    "test_divide_checked.py": (
        "import unittest\nfrom calc.core import add, divide\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_divide(self):\n"
        "        self.assertEqual(divide(6, 3), 2)\n"
        "    def test_divide_keeps_the_fraction(self):\n"
        "        self.assertAlmostEqual(divide(1, 2), 0.5)\n"
        "    def test_divide_by_zero_raises_value_error(self):\n"
        "        with self.assertRaises(ValueError):\n"
        "            divide(1, 0)\n"
        "    def test_divide_error_message_names_the_problem(self):\n"
        "        with self.assertRaises(ValueError) as ctx:\n"
        "            divide(1, 0)\n"
        "        self.assertIn('division by zero', str(ctx.exception))\n"
        "    def test_add_still_adds(self):\n"
        "        self.assertEqual(add(2, 2), 4)\n"
    ),
}


def _calc_core(body: str) -> dict[str, str]:
    return {"calc/core.py": body}


CALC_CASE = FaultCase(
    key="calc_divide",
    goal="给 calc 增加 divide(a, b)，返回 a / b，并补测试，不要破坏 add()",
    initial=_CALC_INITIAL,
    correct=_CALC_CORRECT,
    hidden_tests=_CALC_HIDDEN,
    probe=_CALC_PROBE,
    neutral=[
        Mutant(
            key="neutral_docstring",
            family="neutral",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    \"\"\"Divide a by b; a zero divisor is rejected.\"\"\"\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return a / b\n"
            ),
            note="只加文档字符串，语义不变",
        ),
    ],
    mutants=[
        Mutant(
            key="no_guard",
            family="missing_guard",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    return a / b\n"
            ),
            note="除零不处理，ZeroDivisionError 冒到调用方",
        ),
        Mutant(
            key="wrong_exception",
            family="wrong_exception_type",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise TypeError('division by zero')\n"
                "    return a / b\n"
            ),
            note="抛 TypeError 而不是约定的 ValueError",
        ),
        Mutant(
            key="empty_message",
            family="unhelpful_message",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('')\n"
                "    return a / b\n"
            ),
            note="错误信息为空，模型拿着报错也不知道错在哪",
        ),
        Mutant(
            key="swallowed",
            family="swallowed_error",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    try:\n"
                "        if b == 0:\n"
                "            raise ValueError('division by zero')\n"
                "        return a / b\n"
                "    except Exception:\n"
                "        return 0\n"
            ),
            note="吞掉异常并返回 0，错误被伪装成正常结果",
        ),
        Mutant(
            key="inverted",
            family="inverted_condition",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    if b != 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return a / b\n"
            ),
            note="条件写反：非零才报错",
        ),
        Mutant(
            key="int_division",
            family="integer_division",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return a // b\n"
            ),
            note="整除代替真除，小数被截断",
        ),
        Mutant(
            key="regress_add",
            family="regression",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a - b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return a / b\n"
            ),
            note="改 divide 时把 add 改坏了",
        ),
        Mutant(
            key="missing_symbol",
            family="missing_symbol",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def div(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return a / b\n"
            ),
            note="函数名与调用点不一致，导入即崩",
        ),
        Mutant(
            key="string_result",
            family="wrong_return_type",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return str(a / b)\n"
            ),
            note="返回字符串而不是数值",
        ),
        Mutant(
            key="swapped_args",
            family="swapped_args",
            files=_calc_core(
                "def add(a, b):\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise ValueError('division by zero')\n"
                "    return b / a\n"
            ),
            note="两个参数写反",
        ),
    ],
)


# --------------------------------------------------------------------------
# case 2 -- stats.mean: empty input, input mutation, side effects
# --------------------------------------------------------------------------
_STATS_INITIAL = {
    "stats/__init__.py": "",
    "stats/core.py": "def median(values):\n    return sorted(values)[len(values) // 2]\n",
    "tests/test_median.py": (
        "import unittest\nfrom stats.core import median\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_median(self):\n"
        "        self.assertEqual(median([1, 3, 2]), 2)\n"
    ),
    "tests/test_mean.py": (
        "import unittest\nfrom stats.core import mean\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_mean(self):\n"
        "        self.assertEqual(mean([1, 2, 3]), 2)\n"
    ),
}

_STATS_CORRECT = {
    "stats/core.py": (
        "def median(values):\n"
        "    return sorted(values)[len(values) // 2]\n"
        "\n"
        "\n"
        "def mean(values):\n"
        "    if not values:\n"
        "        return 0\n"
        "    return sum(values) / len(values)\n"
    ),
}

_STATS_HIDDEN = {
    "test_mean_checked.py": (
        "import unittest\nfrom stats.core import mean, median\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_mean(self):\n"
        "        self.assertEqual(mean([1, 2, 3]), 2)\n"
        "    def test_mean_of_empty_is_zero(self):\n"
        "        self.assertEqual(mean([]), 0)\n"
        "    def test_mean_keeps_the_fraction(self):\n"
        "        self.assertAlmostEqual(mean([1, 2]), 1.5)\n"
        "    def test_mean_does_not_mutate_its_input(self):\n"
        "        data = [3, 1, 2]\n"
        "        mean(data)\n"
        "        self.assertEqual(data, [3, 1, 2])\n"
        "    def test_mean_rejects_non_numbers(self):\n"
        "        with self.assertRaises(TypeError):\n"
        "            mean(['a', 'b'])\n"
        "    def test_median_of_even_length(self):\n"
        "        self.assertEqual(median([1, 2, 3, 4]), 3)\n"
        "    def test_median_still_works(self):\n"
        "        self.assertEqual(median([1, 3, 2]), 2)\n"
    ),
}


def _stats_core(body: str) -> dict[str, str]:
    return {"stats/core.py": body}


STATS_CASE = FaultCase(
    key="stats_mean",
    goal="给 stats 增加 mean(values)，返回所有数的平均值，并补测试",
    initial=_STATS_INITIAL,
    correct=_STATS_CORRECT,
    hidden_tests=_STATS_HIDDEN,
    probe=_STATS_PROBE,
    neutral=[
        Mutant(
            key="neutral_guard_style",
            family="neutral",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    if len(values) == 0:\n"
                "        return 0\n"
                "    return sum(values) / len(values)\n"
            ),
            note="空列表判断换成 len()，语义不变",
        ),
    ],
    mutants=[
        Mutant(
            key="no_guard",
            family="missing_guard",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    return sum(values) / len(values)\n"
            ),
            note="空列表直接抛 ZeroDivisionError",
        ),
        Mutant(
            key="none_for_empty",
            family="wrong_return_type",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    if not values:\n"
                "        return None\n"
                "    return sum(values) / len(values)\n"
            ),
            note="空列表返回 None 而不是约定的 0",
        ),
        Mutant(
            key="int_division",
            family="integer_division",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    if not values:\n"
                "        return 0\n"
                "    return sum(values) // len(values)\n"
            ),
            note="整除代替真除",
        ),
        Mutant(
            key="int_truncation",
            family="wrong_return_type",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    if not values:\n"
                "        return 0\n"
                "    return int(sum(values) / len(values))\n"
            ),
            note="小数被 int() 截断",
        ),
        Mutant(
            key="mutates_input",
            family="mutates_input",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    values.sort()\n"
                "    if not values:\n"
                "        return 0\n"
                "    return sum(values) / len(values)\n"
            ),
            note="就地排序，改写了调用方的列表",
        ),
        Mutant(
            key="swallowed",
            family="swallowed_error",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    try:\n"
                "        if not values:\n"
                "            return 0\n"
                "        return sum(values) / len(values)\n"
                "    except TypeError:\n"
                "        return 0\n"
            ),
            note="非数值输入被吞成 0，错误类型信息丢失",
        ),
        Mutant(
            key="regress_median",
            family="regression",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[0]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    if not values:\n"
                "        return 0\n"
                "    return sum(values) / len(values)\n"
            ),
            note="median 被改成最小值",
        ),
        Mutant(
            key="missing_symbol",
            family="missing_symbol",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def avg(values):\n"
                "    if not values:\n"
                "        return 0\n"
                "    return sum(values) / len(values)\n"
            ),
            note="mean 改名成 avg，调用点找不到",
        ),
        Mutant(
            key="inverted",
            family="inverted_condition",
            files=_stats_core(
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    if values:\n"
                "        return 0\n"
                "    return sum(values) / len(values)\n"
            ),
            note="条件写反：非空返回 0",
        ),
        Mutant(
            key="state_leak",
            family="state_leak",
            files=_stats_core(
                "_LAST = None\n"
                "\n"
                "\n"
                "def median(values):\n"
                "    return sorted(values)[len(values) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    global _LAST\n"
                "    if _LAST is not None:\n"
                "        return _LAST\n"
                "    if not values:\n"
                "        _LAST = 0\n"
                "        return 0\n"
                "    _LAST = sum(values) / len(values)\n"
                "    return _LAST\n"
            ),
            note="模块级缓存不区分入参，第二次调用返回上一次的结果",
        ),
        Mutant(
            key="partial_fix_median",
            family="partial_fix",
            files=_stats_core(
                "def median(values):\n"
                "    ordered = sorted(values)\n"
                "    if len(ordered) % 2 == 0:\n"
                "        return ordered[len(ordered) // 2 - 1]\n"
                "    return ordered[len(ordered) // 2]\n"
                "\n"
                "\n"
                "def mean(values):\n"
                "    if not values:\n"
                "        return 0\n"
                "    return sum(values) / len(values)\n"
            ),
            note="median 只修好了奇数长度，偶数长度没管",
        ),
    ],
)


# --------------------------------------------------------------------------
# case 3 -- shop billing: two copies of the same logic
# --------------------------------------------------------------------------
_SHOP_INITIAL = {
    "app/__init__.py": "",
    "app/pricing.py": "TAX_RATE = 0.2\n\n\ndef total(items):\n    return sum(items)\n",
    "app/legacy/__init__.py": "",
    "app/legacy/pricing.py": "TAX_RATE = 0.2\n\n\ndef total(items):\n    return sum(items)\n",
    "app/service.py": "from app.pricing import total\n\n\ndef checkout(items):\n    return total(items)\n",
    "app/legacy_service.py": (
        "from app.legacy.pricing import total\n\n\ndef legacy_checkout(items):\n    return total(items)\n"
    ),
    # the visible test only looks at the archived copy
    "tests/test_legacy_service.py": (
        "import unittest\nfrom app.legacy_service import legacy_checkout\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_legacy_checkout(self):\n"
        "        self.assertEqual(legacy_checkout([100]), 80)\n"
    ),
}

_SHOP_CORRECT = {
    "app/pricing.py": (
        "TAX_RATE = 0.2\n"
        "\n"
        "\n"
        "def total(items):\n"
        "    for item in items:\n"
        "        if item < 0:\n"
        "            raise ValueError('negative item')\n"
        "    subtotal = sum(items)\n"
        "    return subtotal - subtotal * TAX_RATE\n"
    ),
    "app/legacy/pricing.py": (
        "TAX_RATE = 0.2\n"
        "\n"
        "\n"
        "def total(items):\n"
        "    for item in items:\n"
        "        if item < 0:\n"
        "            raise ValueError('negative item')\n"
        "    subtotal = sum(items)\n"
        "    return subtotal - subtotal * TAX_RATE\n"
    ),
}

_SHOP_HIDDEN = {
    "test_billing_checked.py": (
        "import unittest\nfrom app.service import checkout\n"
        "from app.legacy_service import legacy_checkout\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_checkout(self):\n"
        "        self.assertEqual(checkout([100]), 80)\n"
        "    def test_legacy_checkout(self):\n"
        "        self.assertEqual(legacy_checkout([100]), 80)\n"
        "    def test_tax_rate_is_applied(self):\n"
        "        self.assertEqual(checkout([200]), 160)\n"
        "    def test_negative_item_is_rejected(self):\n"
        "        with self.assertRaises(ValueError) as ctx:\n"
        "            checkout([-1])\n"
        "        self.assertIn('negative item', str(ctx.exception))\n"
        "    def test_checkout_does_not_mutate_its_input(self):\n"
        "        data = [100, 50]\n"
        "        checkout(data)\n"
        "        self.assertEqual(data, [100, 50])\n"
    ),
}


def _shop_core(body: str) -> dict[str, str]:
    return {"app/pricing.py": body}


def _shop_legacy_core(body: str) -> dict[str, str]:
    return {"app/legacy/pricing.py": body}


_SHOP_BODY_TAX = (
    "    for item in items:\n"
    "        if item < 0:\n"
    "            raise ValueError('negative item')\n"
    "    subtotal = sum(items)\n"
)

SHOP_CASE = FaultCase(
    key="shop_billing",
    goal="把计费逻辑改成按 TAX_RATE 计税（结算金额 = 小计 − 税额），app/pricing.py 与 app/legacy/pricing.py 两处都要改",
    initial=_SHOP_INITIAL,
    correct=_SHOP_CORRECT,
    hidden_tests=_SHOP_HIDDEN,
    probe=_SHOP_PROBE,
    neutral=[
        Mutant(
            key="neutral_style",
            family="neutral",
            files=_shop_core(
                "TAX_RATE = 0.2\n"
                "\n"
                "\n"
                "def total(items):\n"
                + _SHOP_BODY_TAX
                + "    return sum(items) * (1 - TAX_RATE)\n"
            ),
            note="换一种等价写法，语义不变",
        ),
    ],
    mutants=[
        Mutant(
            key="only_legacy_fixed",
            family="partial_fix",
            files=_shop_core(
                "TAX_RATE = 0.2\n\n\ndef total(items):\n    return sum(items)\n"
            ),
            note="只改了归档副本，主路径没改",
        ),
        Mutant(
            key="only_main_fixed",
            family="partial_fix",
            files=_shop_legacy_core(
                "TAX_RATE = 0.2\n\n\ndef total(items):\n    return sum(items)\n"
            ),
            note="只改了主路径，归档副本没改",
        ),
        Mutant(
            key="tax_direction",
            family="wrong_direction",
            files={
                **_shop_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    + _SHOP_BODY_TAX
                    + "    return subtotal + subtotal * TAX_RATE\n"
                ),
                **_shop_legacy_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    + _SHOP_BODY_TAX
                    + "    return subtotal + subtotal * TAX_RATE\n"
                ),
            },
            note="税额加反了，变成加价",
        ),
        Mutant(
            key="tax_only",
            family="incomplete_computation",
            files={
                **_shop_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    + _SHOP_BODY_TAX
                    + "    return subtotal * TAX_RATE\n"
                ),
                **_shop_legacy_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    + _SHOP_BODY_TAX
                    + "    return subtotal * TAX_RATE\n"
                ),
            },
            note="只返回税额，忘了结算金额",
        ),
        Mutant(
            key="regress_legacy_rate",
            family="regression",
            files=_shop_legacy_core(
                "TAX_RATE = 0.1\n"
                "\n"
                "\n"
                "def total(items):\n"
                + _SHOP_BODY_TAX
                + "    return subtotal - subtotal * TAX_RATE\n"
            ),
            note="两份拷贝的税率不一致",
        ),
        Mutant(
            key="missing_symbol",
            family="missing_symbol",
            files={"app/service.py": "from app.pricing import amount\n\n\ndef checkout(items):\n    return amount(items)\n"},
            note="调用点找不到函数",
        ),
        Mutant(
            key="inverted",
            family="inverted_condition",
            files={
                **_shop_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    + _SHOP_BODY_TAX
                    + "    if TAX_RATE < 0:\n"
                    "        return subtotal - subtotal * TAX_RATE\n"
                    "    return subtotal\n"
                ),
                **_shop_legacy_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    + _SHOP_BODY_TAX
                    + "    if TAX_RATE < 0:\n"
                    "        return subtotal - subtotal * TAX_RATE\n"
                    "    return subtotal\n"
                ),
            },
            note="条件写反，计税分支永远不生效",
        ),
        Mutant(
            key="wrong_exception",
            family="wrong_exception_type",
            files={
                **_shop_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    "    for item in items:\n"
                    "        if item < 0:\n"
                    "            raise TypeError('negative item')\n"
                    "    subtotal = sum(items)\n"
                    "    return subtotal - subtotal * TAX_RATE\n"
                ),
                **_shop_legacy_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    "    for item in items:\n"
                    "        if item < 0:\n"
                    "            raise TypeError('negative item')\n"
                    "    subtotal = sum(items)\n"
                    "    return subtotal - subtotal * TAX_RATE\n"
                ),
            },
            note="负数抛 TypeError 而不是约定的 ValueError",
        ),
        Mutant(
            key="empty_message",
            family="unhelpful_message",
            files={
                **_shop_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    "    for item in items:\n"
                    "        if item < 0:\n"
                    "            raise ValueError('')\n"
                    "    subtotal = sum(items)\n"
                    "    return subtotal - subtotal * TAX_RATE\n"
                ),
                **_shop_legacy_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    "    for item in items:\n"
                    "        if item < 0:\n"
                    "            raise ValueError('')\n"
                    "    subtotal = sum(items)\n"
                    "    return subtotal - subtotal * TAX_RATE\n"
                ),
            },
            note="错误信息为空，模型拿着报错也不知道错在哪",
        ),
        Mutant(
            key="mutates_input",
            family="mutates_input",
            files={
                **_shop_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    "    items.sort()\n"
                    + _SHOP_BODY_TAX
                    + "    return subtotal - subtotal * TAX_RATE\n"
                ),
                **_shop_legacy_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    "    items.sort()\n"
                    + _SHOP_BODY_TAX
                    + "    return subtotal - subtotal * TAX_RATE\n"
                ),
            },
            note="就地排序，改写了调用方的列表",
        ),
        Mutant(
            key="state_leak",
            family="state_leak",
            files={
                **_shop_core(
                    "TAX_RATE = 0.2\n"
                    "_LAST = None\n"
                    "\n"
                    "\n"
                    "def total(items):\n"
                    "    global _LAST\n"
                    + _SHOP_BODY_TAX
                    + "    if _LAST is not None:\n"
                    "        return _LAST\n"
                    "    _LAST = subtotal - subtotal * TAX_RATE\n"
                    "    return _LAST\n"
                ),
                **_shop_legacy_core(
                    "TAX_RATE = 0.2\n"
                    "_LAST = None\n"
                    "\n"
                    "\n"
                    "def total(items):\n"
                    "    global _LAST\n"
                    + _SHOP_BODY_TAX
                    + "    if _LAST is not None:\n"
                    "        return _LAST\n"
                    "    _LAST = subtotal - subtotal * TAX_RATE\n"
                    "    return _LAST\n"
                ),
            },
            note="模块级缓存不区分入参，第二次调用返回上一次的结果",
        ),
        Mutant(
            key="swallowed",
            family="swallowed_error",
            files={
                **_shop_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    "    try:\n"
                    "        for item in items:\n"
                    "            if item < 0:\n"
                    "                raise ValueError('negative item')\n"
                    "        subtotal = sum(items)\n"
                    "        return subtotal - subtotal * TAX_RATE\n"
                    "    except Exception:\n"
                    "        return 0\n"
                ),
                **_shop_legacy_core(
                    "TAX_RATE = 0.2\n\n\ndef total(items):\n"
                    "    try:\n"
                    "        for item in items:\n"
                    "            if item < 0:\n"
                    "                raise ValueError('negative item')\n"
                    "        subtotal = sum(items)\n"
                    "        return subtotal - subtotal * TAX_RATE\n"
                    "    except Exception:\n"
                    "        return 0\n"
                ),
            },
            note="异常被吞成 0，非法输入看起来像免费",
        ),
    ],
)


CASES: list[FaultCase] = [CALC_CASE, STATS_CASE, SHOP_CASE]


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------
def _run(command: str, root: Path, timeout: int = 120) -> bool:
    """Run a judge command inside ``root``; True means it accepted the work."""
    proc = subprocess.run(
        command, shell=True, cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode == 0


def _materialise(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _probe(root: Path, source: str) -> str:
    """Run the behaviour probe and return its normalised output."""
    if not source:
        return ""
    script = root / "_probe.py"
    script.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [sys_executable(), str(script)], cwd=str(root), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    return (proc.stdout or "") + ("\n!!STDERR!!\n" + proc.stderr if proc.stderr else "")


def sys_executable() -> str:
    import sys

    return sys.executable


@dataclass
class MutantResult:
    mutant: Mutant
    strong_caught: bool
    weak_caught: bool
    #: the mutant behaves differently from the correct solution -> it *is* a defect
    is_real_defect: bool = True

    @property
    def state(self) -> str:
        if not self.is_real_defect:
            return "equivalent"
        return "caught" if self.strong_caught else "missed"


@dataclass
class CaseResult:
    case: FaultCase
    strong_passes_correct: bool
    weak_passes_correct: bool
    strong_passes_neutral: bool
    weak_passes_neutral: bool
    neutral_is_equivalent: bool = True
    results: list[MutantResult] = field(default_factory=list)


@dataclass
class SuiteResult:
    cases: list[CaseResult]

    # -- counters ---------------------------------------------------------
    def _counts(self, strong: bool = True) -> tuple[int, int]:
        """(caught, real defects) -- equivalent mutants are not defects."""
        caught = total = 0
        for case in self.cases:
            for res in case.results:
                if not res.is_real_defect:
                    continue
                total += 1
                hit = res.strong_caught if strong else res.weak_caught
                caught += 1 if hit else 0
        return caught, total

    def recall(self, strong: bool = True) -> float:
        caught, total = self._counts(strong)
        return caught / total if total else 0.0

    def equivalent(self) -> list[tuple[str, str]]:
        return [
            (case.case.key, res.mutant.key)
            for case in self.cases
            for res in case.results
            if not res.is_real_defect
        ]

    def false_positives(self, strong: bool = True) -> int:
        """How many *correct* variants the criterion rejected."""
        bad = 0
        for case in self.cases:
            ok = case.strong_passes_correct if strong else case.weak_passes_correct
            neutral_ok = case.strong_passes_neutral if strong else case.weak_passes_neutral
            bad += 0 if ok else 1
            bad += 0 if neutral_ok else 1
        return bad

    def only_strong(self) -> list[tuple[str, str, str]]:
        """Defects the strong criterion caught and the weak one missed."""
        return [
            (case.case.key, res.mutant.key, res.mutant.family)
            for case in self.cases
            for res in case.results
            if res.is_real_defect and res.strong_caught and not res.weak_caught
        ]

    def missed(self) -> list[tuple[str, str, str]]:
        """Real defects neither criterion caught."""
        return [
            (case.case.key, res.mutant.key, res.mutant.family)
            for case in self.cases
            for res in case.results
            if res.is_real_defect and not res.strong_caught
        ]

    def family_coverage(self) -> dict[str, dict[str, str]]:
        """family -> case -> 'caught/real' for the strong criterion."""
        out: dict[str, dict[str, str]] = {}
        for case in self.cases:
            for res in case.results:
                if not res.is_real_defect:
                    continue
                bucket = out.setdefault(res.mutant.family, {})
                caught, total = bucket.get(case.case.key, "0/0").split("/")
                bucket[case.case.key] = (
                    f"{int(caught) + (1 if res.strong_caught else 0)}/{int(total) + 1}"
                )
        return out

    # -- report -----------------------------------------------------------
    def to_markdown(self) -> str:
        strong_caught, strong_total = self._counts(True)
        weak_caught, weak_total = self._counts(False)
        equivalents = self.equivalent()
        misses = self.missed()
        only = self.only_strong()
        lines: list[str] = []
        add = lines.append
        add("# 判据召回率：故障注入集")
        add("")
        add("> 目的：测「验收层能不能抓住**已知**的失效」。这跟 [bench.md](bench.md) 问的问题不同——")
        add("> bench 问「有反馈能不能让模型改对」，这里问「判据本身够不够尖」。")
        add("> 判据若对什么都放行，bench 会给出 100% 的成功率，而且理由完全错误。")
        add(">")
        add("> 装置：对每个任务注入一批**已知**缺陷（mutant），分别用两个判据跑：")
        add("> - **强判据**：工作区外的 held-out oracle（模型读不到、改不到）；")
        add("> - **弱判据**：工作区里模型自己写的那份可见测试（模型能改）。")
        add(">")
        add("> 三道对照，防止判据「假装很尖」：正确实现必须通过；语义等价的改写（neutral）必须通过；")
        add("> 每个 mutant 还要和正确实现做**行为探针**对比——行为一样的 mutant 是**等价 mutant**")
        add("> （注入的其实不是缺陷），不计入召回率分母。")
        add("")
        add("## 一、结论")
        add("")
        if strong_total:
            add(f"- **强判据召回率 {strong_caught}/{strong_total} = {strong_caught / strong_total:.0%}**")
        else:
            add("- 强判据召回率：无样本")
        if weak_total:
            add(f"- 弱判据召回率 {weak_caught}/{weak_total} = {weak_caught / weak_total:.0%}")
        else:
            add("- 弱判据召回率：无样本")
        add(f"- 过杀（正确实现 / 等价改写被拒）：强判据 {self.false_positives(True)} 次，弱判据 {self.false_positives(False)} 次")
        add(f"- 等价 mutant（注入无效，已剔除）：{len(equivalents)} 个")
        add(f"- **只被强判据抓住的缺陷：{len(only)} 个**——这正是「模型自己写测试」换不来的部分")
        if misses:
            add(f"- **强判据真漏检：{len(misses)} 个** → {misses}")
        else:
            add("- 强判据真漏检：0 个")
        add("")
        add("## 二、逐案例")
        add("")
        add("| 案例 | 缺陷数 | 强判据抓到 | 弱判据抓到 | 正确实现通过（强/弱） | 等价改写通过（强/弱） |")
        add("|---|---|---|---|---|---|")
        for case in self.cases:
            res = case.results
            s = sum(1 for r in res if r.is_real_defect and r.strong_caught)
            w = sum(1 for r in res if r.is_real_defect and r.weak_caught)
            add(
                f"| `{case.case.key}` | {len(res)} | {s}/{len(res)} | {w}/{len(res)} | "
                f"{'✅' if case.strong_passes_correct else '❌'} / {'✅' if case.weak_passes_correct else '❌'} | "
                f"{'✅' if case.strong_passes_neutral else '❌'} / {'✅' if case.weak_passes_neutral else '❌'} |"
            )
        add("")
        add("## 三、逐缺陷明细")
        add("")
        add("| 案例 | 缺陷 | 类别 | 说明 | 强判据 | 弱判据 |")
        add("|---|---|---|---|---|---|")
        for case in self.cases:
            for res in case.results:
                if res.state == "equivalent":
                    strong = "等价 mutant（剔除）"
                    weak = "等价 mutant（剔除）"
                else:
                    strong = "抓住" if res.strong_caught else "**漏检**"
                    weak = "抓住" if res.weak_caught else "漏检"
                add(
                    f"| `{case.case.key}` | `{res.mutant.key}` | {FAMILIES.get(res.mutant.family, res.mutant.family)} "
                    f"| {res.mutant.note} | {strong} | {weak} |"
                )
        add("")
        add("## 四、按缺陷类别")
        add("")
        add("| 类别 | " + " | ".join(f"`{c.case.key}`" for c in self.cases) + " |")
        add("|---" * (len(self.cases) + 1) + "|")
        coverage = self.family_coverage()
        for family, buckets in coverage.items():
            cells = " | ".join(buckets.get(c.case.key, "—") for c in self.cases)
            add(f"| {FAMILIES.get(family, family)} | {cells} |")
        add("")
        add("## 五、这份数字不证明什么")
        add("")
        add("1. 召回率是**相对于我们自己定义的缺陷分类**。未知缺陷不在分类表里，")
        add("   所以高召回只说明「已知分类里没有盲区」，**不等于**真实任务上的召回率。")
        add("2. 强判据也不是无限的：它只能检查它写了的断言。判据写了什么、漏了什么，")
        add("   同样是天花板的一部分——本表测的是「注入的缺陷 vs 写下的断言」，不是「需求 vs 实现」。")
        add("3. 样本量小：每个案例 10~12 个缺陷、每缺陷 1 次运行，没有重复采样，也没有置信区间。")
        add("4. 本表**没有调用模型**：它测的是判据，不是 agent。模型侧的效果在 [bench-summary.md](bench-summary.md)。")
        add("5. **方法论备注**：第一轮跑出来有 1 条「漏检」，分诊后发现是**等价 mutant**——")
        add("   注入的缺陷本身语义没变（`if TAX_RATE < 0` 的分支永远不触发，但后面的正确计算还在）。")
        add("   判据没错，是注入无效。现在这三种状态由行为探针自动分诊，不再靠人眼。")
        return "\n".join(lines) + "\n"


def run_case(case: FaultCase, base: Path) -> CaseResult:
    """Materialise the case, inject each defect, and see what the judges do."""
    base.mkdir(parents=True, exist_ok=True)
    task = Task(
        key=case.key,
        goal=case.goal,
        initial=case.initial,
        correct=case.correct,
        wrong=case.correct,
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
        hidden_tests=case.hidden_tests,
        judge_visible=True,
    )
    task.prepare_oracle(base)
    strong = task.judge
    weak = FULL_SUITE
    counter = {"n": 0}

    def variant(files: dict[str, str]) -> tuple[bool, bool, str]:
        counter["n"] += 1
        root = base / "ws" / f"v{counter['n']}"
        root.mkdir(parents=True)
        _materialise(root, files)
        return _run(strong, root), _run(weak, root), _probe(root, case.probe)

    base_files = {**case.initial, **case.correct}
    strong_ok, weak_ok, correct_behaviour = variant(base_files)

    neutral_ok = (True, True)
    neutral_equivalent = True
    for neutral in case.neutral:
        files = {**base_files, **neutral.files}
        s, w, behaviour = variant(files)
        neutral_ok = (s, w)
        neutral_equivalent = neutral_equivalent and behaviour == correct_behaviour

    results: list[MutantResult] = []
    for mutant in case.mutants:
        files = {**base_files, **mutant.files}
        s, w, behaviour = variant(files)
        results.append(
            MutantResult(
                mutant=mutant,
                strong_caught=not s,
                weak_caught=not w,
                is_real_defect=behaviour != correct_behaviour,
            )
        )

    return CaseResult(
        case=case,
        strong_passes_correct=strong_ok,
        weak_passes_correct=weak_ok,
        strong_passes_neutral=neutral_ok[0],
        weak_passes_neutral=neutral_ok[1],
        neutral_is_equivalent=neutral_equivalent,
        results=results,
    )


def run_suite(cases: list[FaultCase] | None = None) -> SuiteResult:
    """Run the whole fault-injection suite in a throwaway directory."""
    selected = cases if cases is not None else CASES
    with tempfile.TemporaryDirectory(prefix="planloop-faults-") as tmp:
        base = Path(tmp)
        results = [run_case(case, base / case.key) for case in selected]
    return SuiteResult(cases=results)


def suite_for_keys(keys: list[str]) -> list[FaultCase]:
    return [c for c in CASES if c.key in keys]

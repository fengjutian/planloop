"""Does the memory system pay for itself? Measure the **second** run.

``docs/market-scan.md`` §四 lists the fifth revision that was never done:
"度量『第二次同类任务快多少』——没有这个，前四项都是猜".
This module is that measurement, on the path the agent actually takes
(lessons + guards, and verified facts), with the same discipline as
``bench.py``:

* the second task is the **same family** as the first -- both work on the same
  module, which is where the regression risk lives;
* the control arm has **no memory at all**, so any difference belongs to the
  memory;
* every arm is judged by the **same external criterion** (a held-out oracle
  that lives outside the workspace), never by the model's own report;
* the two memory subsystems are measured **separately**, so "memory" does not
  hide behind one working arm;
* **injection is measured, not assumed**: a spy wraps the model and counts
  how many prompts actually carried the lesson block or the facts block.
  A store that nobody reads is a store with no value, and the report has to
  be able to say so.

Two task families, because a scripted model and a real model cannot be
measured on the same footing:

``demo`` (scripted)
    The demo's teaching family: fix the discount, then add a feature to the
    same file.  The scripted personas play the roles a real model plays on
    its own.  Deterministic, zero variance -- it measures the **mechanism**.

``contract`` (real)
    A family built for a real model, because the demo family does not make a
    real model fail first -- and a lesson only exists when a task fails and
    is then fixed.  The repo ships a helper whose contract is *not* what its
    name suggests; the visible test does not pin the contract, a held-out
    oracle does.  So the model fails once, learns, and the second task can
    regress the very thing the lesson is about.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from planloop import demo
from planloop import heldout
from planloop.memdb import MemorySystem
from planloop.memory import MemoryStore
from planloop.scheduler import RunResult, run
from planloop.spec import ProblemSpec

FULL_SUITE = demo.FULL_SUITE

LESSON_MARKER = "# Lessons from your past mistakes"
FACT_MARKER = "# Repository facts (verified earlier)"

#: the fact the first task earns: "where the discount logic lives" -- and,
#: more importantly, **what it means**.  It states the knowledge instead of
#: pointing at the file: a fact that says "go look at helpers.py" sends the
#: model back to the file and the memory saves it nothing.
FACT_KEY = "shop/pricing.py.discount_dependency"
FACT_VALUE = (
    "shop/pricing.py 的 total() 用 shop/helpers.py 的 pct() 计算折扣；"
    "pct(value) 返回折扣后的剩余比例（pct(10) == 0.9），不是 value/100；"
    "所以 total([100, 200], 10) == 270"
)


# --------------------------------------------------------------------------
# task families
# --------------------------------------------------------------------------
@dataclass
class Family:
    """One task family: the repo it starts from and the two tasks.

    ``r1_node`` / ``r2_node`` are the **node-level** acceptance criteria --
    the ones re-checked after every action.  ``final`` is the run-level
    acceptance, checked once at the end.  The gap between them is where the
    interesting failures live: a node can pass while the repository is broken.

    ``r1_oracle`` / ``r2_oracle`` / ``final_oracle`` are **held-out** test
    sources.  They are written outside the workspace, so the model can
    neither read nor edit the thing it is judged by.
    """

    name: str
    initial: dict[str, str]
    r1_goal: str
    r2_goal: str
    #: the goal of the *node* -- the scripted personas key on it, so it cannot
    #: be derived from the task goal.
    r1_node_goal: str = ""
    r2_node_goal: str = ""
    r1_node: list[dict[str, Any]] = field(default_factory=list)
    r2_node: list[dict[str, Any]] = field(default_factory=list)
    r1_success: list[dict[str, Any]] = field(default_factory=list)
    r2_success: list[dict[str, Any]] = field(default_factory=list)
    r1_oracle: str = ""
    r2_oracle: str = ""
    final_oracle: str = ""
    #: what each held-out criterion checks, in words and **without numbers**.
    #: The model is told the requirement (the goal says it too); what it is
    #: never told is the answer.  A criterion that fails with a bare Python
    #: error -- "total() takes 2 positional arguments but 3 were given" --
    #: says nothing about what to fix, and a model that gets only that spins
    #: in place (measured: it re-read the same file sixteen times).
    r1_hint: str = ""
    r2_hint: str = ""
    final_hint: str = ""
    context_files: list[str] = field(default_factory=list)
    #: the verified fact the first task earns, and the key it is stored
    #: under.  A family that needs a different fact overrides them.
    fact_key: str = ""
    fact_value: str = ""
    #: substrings that identify "the model looked at the hidden thing".
    #: The observation of a ``read`` action is the file's content, so a
    #: marker like ``"def pct("`` shows up in the event log exactly when the
    #: model opened the file that holds the contract.  That is what makes the
    #: claim "the memory arm skipped the lookup" checkable instead of
    #: asserted.
    hidden_markers: list[str] = field(default_factory=list)
    #: per-task overrides: the files the prompt carries.  A task can leave a
    #: file **out** on purpose -- then the model has to open it to learn its
    #: contract, and a memory that already carries the contract saves it a
    #: step.  That is the only way "the second task is faster" can show up.
    r1_context_files: list[str] = field(default_factory=list)
    r2_context_files: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)

    def files_for(self, task: str) -> list[str]:
        override = self.r1_context_files if task == "r1" else self.r2_context_files
        return list(override) if override else list(self.context_files)

    def spec(self, goal: str, node_acceptance: list[dict[str, Any]],
             success: list[dict[str, Any]], node_goal: str = "",
             context_files: list[str] | None = None) -> dict[str, Any]:
        return {
            "goal": goal,
            "success_criteria": success,
            "context_files": list(context_files or self.context_files),
            "limits": dict(self.limits),
            "plan_template": {
                "nodes": [
                    {
                        "id": "T1",
                        "goal": node_goal or goal,
                        "deps": [],
                        "outputs": {"files": ", ".join(sorted(self.initial))},
                        "acceptance": list(node_acceptance),
                        "est_cost": 0.8,
                        "risk": "low",
                        "reversible": True,
                    }
                ]
            },
        }


#: the demo family -- unchanged, so the scripted numbers already published in
#: ``docs/memory-value.md`` stay valid.
DEMO_FAMILY = Family(
    name="demo",
    initial={
        "shop/__init__.py": "",
        "shop/pricing.py": demo.PRICING_BUGGY,
        "tests/test_pricing.py": demo.TEST_PRICING,
    },
    r1_goal=demo.TASK_LEARN["goal"],
    r2_goal=demo.TASK_EXPORT["goal"],
    r1_node_goal=demo.TASK_LEARN["plan_template"]["nodes"][0]["goal"],
    r2_node_goal=demo.TASK_EXPORT["plan_template"]["nodes"][0]["goal"],
    r1_node=demo.TASK_LEARN["plan_template"]["nodes"][0]["acceptance"],
    r2_node=demo.TASK_EXPORT["plan_template"]["nodes"][0]["acceptance"],
    r1_success=demo.TASK_LEARN["success_criteria"],
    r2_success=demo.TASK_EXPORT["success_criteria"],
)


# -- the contract family: built so that a *real* model fails first ---------

HELPERS_PCT = '''\
def pct(value):
    """Payable factor for a discount percentage, used by shop.pricing."""
    return value / 100
'''

PRICING_WITH_PCT = '''\
from shop.helpers import pct


def subtotal(prices):
    return sum(prices)


def total(prices, discount_pct):
    return subtotal(prices) * pct(discount_pct)
'''

TEST_PRICING_CONTRACT = '''\
import unittest

from shop.pricing import total


class PricingTest(unittest.TestCase):
    def test_total_applies_discount(self):
        self.assertEqual(total([100, 200], 10), 270)
'''

#: held out: the contract of the helper.  The visible test does not pin it,
#: so a model that fixes the symptom in the wrong layer fails here first.
ORACLE_PCT_CONTRACT = '''\
import unittest

from shop.helpers import pct


class PctContractTest(unittest.TestCase):
    def test_pct_returns_the_payable_factor(self):
        self.assertAlmostEqual(
            pct(10), 0.9,
            msg="pct(value) returns the factor to multiply a subtotal by: pct(10) == 0.9",
        )
'''

#: held out: the new feature of the second task.  It deliberately does **not**
#: check the old behaviour -- that is the whole point: the node passes while
#: the repository is broken.
ORACLE_COUPON = '''\
import unittest

from shop.pricing import total


class CouponTest(unittest.TestCase):
    def test_coupon_is_taken_after_the_discount(self):
        self.assertEqual(total([100, 200], 10, 20), 250)

    def test_total_never_goes_below_zero(self):
        self.assertEqual(total([100, 200], 10, 1000), 0)
'''

#: held out: the new feature of the second task.  It can only be satisfied by
#: knowing what ``pct()`` returns -- and the second task does **not** carry
#: ``shop/helpers.py`` in its prompt, so the model has to open the file to
#: learn the contract.  A memory that already carries the contract saves it
#: that step; that difference is what the cost measurement is about.
ORACLE_MEMBER = '''\
import unittest

from shop.pricing import total


class MemberTest(unittest.TestCase):
    def test_member_discount_is_applied_after_the_discount(self):
        # 300 * 0.9 * 0.8 -- pct() returns the factor to multiply by
        self.assertAlmostEqual(total([100, 200], 10, 20), 216.0, places=6)

    def test_no_member_discount_keeps_the_old_total(self):
        self.assertAlmostEqual(total([100, 200], 10, 0), 270.0, places=6)
'''

#: held out: the final verdict.  Both the new feature *and* the old
#: behaviour, because a run that breaks the old behaviour is a failed run.
ORACLE_FINAL = '''\
import unittest

from shop.pricing import total


class FinalTest(unittest.TestCase):
    def test_member_discount_is_applied_after_the_discount(self):
        self.assertAlmostEqual(total([100, 200], 10, 20), 216.0, places=6)

    def test_no_member_discount_keeps_the_old_total(self):
        self.assertAlmostEqual(total([100, 200], 10, 0), 270.0, places=6)

    def test_the_old_discount_still_works(self):
        self.assertEqual(total([100, 200], 10), 270)
'''

CONTRACT_FAMILY = Family(
    name="contract",
    initial={
        "shop/__init__.py": "",
        "shop/helpers.py": HELPERS_PCT,
        "shop/pricing.py": PRICING_WITH_PCT,
        "tests/test_pricing.py": TEST_PRICING_CONTRACT,
    },
    r1_goal=(
        "修正 shop/pricing.py 的 total()，让 tests/test_pricing.py 全部通过："
        "total(prices, discount_pct) 要按 discount_pct 打折。"
    ),
    r2_goal=(
        "给 shop/pricing.py 的 total() 增加第三个参数 member_pct（百分比）："
        "先按 discount_pct 打折，再乘上会员折扣因子。"
        "member_pct 的换算方式和 discount_pct 完全一样（用同一个换算函数），"
        "不要自己另写一套公式。"
    ),
    r1_node=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    r2_node=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    r1_success=[{"id": "SC1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    r2_success=[{"id": "SC1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    r1_oracle=ORACLE_PCT_CONTRACT,
    r2_oracle=ORACLE_MEMBER,
    final_oracle=ORACLE_FINAL,
    r1_hint=(
        "pct() must return the factor to multiply a subtotal by (what is left "
        "of it after the discount), not the percentage itself -- and it must "
        "hold for every percentage."
    ),
    r2_hint=(
        "total() must take a third argument member_pct and apply the member "
        "discount after the discount, through the same conversion helper as "
        "discount_pct; the total without a member discount must not change."
    ),
    final_hint=(
        "total() must apply the member discount after the discount, through "
        "the same conversion helper as discount_pct, and the behaviour without "
        "a member discount must be unchanged."
    ),
    context_files=["shop/pricing.py", "shop/helpers.py", "tests/test_pricing.py"],
    # the second task does **not** carry shop/helpers.py: the contract it
    # needs is only in that file, so a model that does not already know it
    # has to open the file -- and a memory that carries it does not.
    r2_context_files=["shop/pricing.py", "tests/test_pricing.py"],
    hidden_markers=["def pct("],
    limits={"max_attempts_per_node": 2, "max_steps_per_node": 12},
)


# -- the fee family: a contract that **cannot be guessed** ------------------
#
# The contract family (above) has a device flaw, and the 8-rep run exposed
# it: the visible test pins ``total([100, 200], 10) == 270``, so a model that
# guesses the obvious formula -- ``1 - p/100`` -- gets ``pct(10) == 0.9`` *and*
# ``pct(20) == 0.8`` right without ever opening ``shop/helpers.py``.
# The knowledge was never needed, so the measurement measured nothing: every
# arm was green, with or without memory.
#
# "Can be guessed" is the defect to remove.  Here the platform fee depends on
# the discount tier, so the visible test pins one row of a table and says
# nothing about the others: ``pct(10) == 0.88`` does not imply ``pct(20)``.
# The only way to learn the other row is to read the file (or to be told).
#
# Two further details keep the measurement honest:
#
# * the held-out oracles **do not print the expected numbers** -- a failure
#   message that says "expected 0.75" would hand the control arm the answer
#   it was supposed to have to look up;
# * the verified fact states the **rule** (the surcharge table), not the
#   answer, so the fact arm still has to apply it.

HELPERS_PCT_FEE = '''\
def pct(value):
    """Payable factor for a discount percentage, used by shop.pricing.

    A platform fee is deducted on top of the discount, and the fee depends on
    the discount tier -- so the factor for one percentage says nothing about
    the factor for another.  (Tier table: 0% -> 0.00, 10% -> 0.02,
    20% -> 0.05.)
    """
    return 1 - value / 100
'''

TEST_PRICING_FEE = '''\
import unittest

from shop.pricing import total


class PricingTest(unittest.TestCase):
    def test_total_applies_discount(self):
        self.assertEqual(total([100, 200], 10), 264)
'''

#: held out: the contract of the helper, for both tiers.  The messages are
#: fixed strings on purpose -- see the note above.
ORACLE_PCT_FEE = '''\
import unittest

from shop.helpers import pct


class PctContractTest(unittest.TestCase):
    def test_pct_returns_the_payable_factor(self):
        self.assertTrue(
            abs(pct(10) - 0.88) < 1e-7,
            msg="pct(10) is not the payable factor: a platform fee is "
                "deducted on top of the discount",
        )

    def test_pct_fee_depends_on_the_discount_tier(self):
        self.assertTrue(
            abs(pct(20) - 0.75) < 1e-7,
            msg="pct(20) is not the payable factor: the fee depends on the "
                "discount tier, so one tier does not tell you the next",
        )
'''

#: held out: the new feature.  300 * 0.88 * 0.75 == 198.
#:
#: The messages are **diagnostic but numeric-free**: they say *which* factor is
#: wrong, never *what it should be*.  A message that printed the expected
#: number would hand the control arm the answer it was supposed to have to
#: look up; a message with no diagnosis at all leaves the model flailing
#: (measured: the model re-ran the same check five times and never changed
#: the code).  The failure feedback has to say what to check, not what to
#: write.
ORACLE_MEMBER_FEE = '''\
import unittest

from shop.pricing import total


class MemberTest(unittest.TestCase):
    def test_member_discount_is_applied_after_the_discount(self):
        self.assertTrue(
            abs(total([100, 200], 10, 20) - 198.0) < 1e-7,
            msg="the member total is wrong: member_pct has to go through the "
                "same conversion helper as discount_pct",
        )
'''

#: held out: the final verdict -- the new feature *and* the old behaviour.
ORACLE_FINAL_FEE = '''\
import unittest

from shop.pricing import total


class FinalTest(unittest.TestCase):
    def test_member_discount_is_applied_after_the_discount(self):
        self.assertTrue(
            abs(total([100, 200], 10, 20) - 198.0) < 1e-7,
            msg="the member total is wrong: member_pct has to go through the "
                "same conversion helper as discount_pct",
        )

    def test_no_member_discount_keeps_the_old_total(self):
        self.assertTrue(
            abs(total([100, 200], 10, 0) - 264.0) < 1e-7,
            msg="the old total changed: the discount alone must still hold",
        )

    def test_the_old_discount_still_works(self):
        self.assertEqual(total([100, 200], 10), 264)
'''

FEE_FAMILY = Family(
    name="fee",
    initial={
        "shop/__init__.py": "",
        "shop/helpers.py": HELPERS_PCT_FEE,
        "shop/pricing.py": PRICING_WITH_PCT,
        "tests/test_pricing.py": TEST_PRICING_FEE,
    },
    r1_goal=(
        "修正 shop/pricing.py 的 total()，让 tests/test_pricing.py 全部通过："
        "total(prices, discount_pct) 要按 discount_pct 打折。"
        "pct() 的契约对所有折扣档位都成立。"
    ),
    r2_goal=(
        "给 shop/pricing.py 的 total() 增加第三个参数 member_pct（百分比）："
        "先按 discount_pct 打折，再乘上会员折扣因子。"
        "member_pct 的换算方式和 discount_pct 完全一样（用同一个换算函数），"
        "不要自己另写一套公式。"
    ),
    r1_node=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    r2_node=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    r1_success=[{"id": "SC1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    r2_success=[{"id": "SC1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    r1_oracle=ORACLE_PCT_FEE,
    r2_oracle=ORACLE_MEMBER_FEE,
    final_oracle=ORACLE_FINAL_FEE,
    r1_hint=(
        "pct() must return the payable factor for **every** discount tier: the "
        "platform fee of the tier is deducted on top of the discount, so one "
        "tier's factor says nothing about the next."
    ),
    r2_hint=(
        "total() must take a third argument member_pct and apply the member "
        "discount after the discount, through the same conversion helper as "
        "discount_pct; the total without a member discount must not change."
    ),
    final_hint=(
        "total() must apply the member discount after the discount, through "
        "the same conversion helper as discount_pct, and the behaviour without "
        "a member discount must be unchanged."
    ),
    context_files=["shop/pricing.py", "shop/helpers.py", "tests/test_pricing.py"],
    # The second task does **not** carry shop/helpers.py: the contract it
    # needs is only in that file, so a model that does not already know it
    # has to open the file -- and a memory that carries it does not.
    #
    # The first task *does* carry it.  Taking it away was tried (2026-11-11,
    # .membench-fee17p): the model then read the same file 24 times without
    # writing anything -- it spins instead of failing and fixing itself, so
    # the first task ends "blocked" and no lesson is produced at all.  The
    # cost of that: the first task is sometimes solved on the first write
    # (3 of 8 repetitions), and then there is no lesson either.  The lesson
    # yield is therefore decided by the model's behaviour, not by the
    # device; the device can only *report* it.
    r2_context_files=["shop/pricing.py", "tests/test_pricing.py"],
    hidden_markers=["def pct("],
    fact_key="shop/pricing.py.pct_surcharge",
    fact_value=(
        "shop/helpers.py 的 pct(value) = 1 - value/100 - surcharge(value)；"
        "平台费按折扣档位取值：0%→0.00、10%→0.02、20%→0.05；"
        "所以 pct(10)==0.88、pct(20)==0.75"
    ),
    limits={"max_attempts_per_node": 2, "max_steps_per_node": 12},
)


@dataclass
class ArmSpec:
    label: str
    arm: str
    #: scripted persona; ``None`` means "the real model, no persona"
    persona: str | None
    use_memory: bool
    use_facts: bool


SCRIPTED_ARMS: list[ArmSpec] = [
    ArmSpec("R2_none", "no_memory", "careless", False, False),
    ArmSpec("R2_guard", "guard_only", "careless", True, False),
    ArmSpec("R2_lesson", "lesson_and_guard", "attentive", True, False),
    ArmSpec("R2_fact", "fact_only", "factual", False, True),
]

#: with a real model there is no "careless" persona to force: the same model
#: runs in every arm and the only difference is what reaches its prompt.
REAL_ARMS: list[ArmSpec] = [
    ArmSpec("R2_none", "no_memory", None, False, False),
    ArmSpec("R2_memory", "lesson_and_guard", None, True, False),
    ArmSpec("R2_fact", "fact_only", None, False, True),
]


class FactAwareModel(demo.DemoModel):
    """A persona that reacts to injected **facts** rather than to lessons.

    ``demo.DemoModel`` keys on the lesson block. Here the same scripted
    script is reused, but the "you have been told before" signal comes from
    the facts block instead -- so one scenario can measure both subsystems
    against each other.
    """

    def __init__(self) -> None:
        super().__init__("attentive")

    def _act(self, prompt: str) -> str:
        if FACT_MARKER in prompt and LESSON_MARKER not in prompt:
            prompt = prompt + "\n" + LESSON_MARKER + "\n"
        return super()._act(prompt)


class Spy:
    """Counts how often the memory actually reached the model's prompt."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.prompts = 0
        self.with_lesson = 0
        self.with_facts = 0

    def complete(self, prompt: str, **kwargs: Any) -> Any:
        self.prompts += 1
        if LESSON_MARKER in prompt:
            self.with_lesson += 1
        if FACT_MARKER in prompt:
            self.with_facts += 1
        return self.inner.complete(prompt, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@dataclass
class EventStats:
    """What an arm actually *did*, read back from its event log."""

    actions: int = 0
    reads: int = 0
    runs: int = 0
    writes: int = 0
    #: steps spent on a reply the harness could not parse.  A reasoning model
    #: sometimes answers with its thinking instead of an action; those steps
    #: are wasted and they inflate the cost of whichever arm gets unlucky.
    invalid: int = 0
    first_action: str = ""
    first_target: str = ""
    #: how often the hidden contract showed up in an observation -- i.e. how
    #: often the model went looking for the thing the memory could have
    #: handed it.
    hidden_looks: int = 0

    @property
    def first_label(self) -> str:
        if not self.first_action:
            return ""
        if self.first_target:
            return f"{self.first_action} {self.first_target}"
        return self.first_action


def analyze_events(log_path: Path, markers: list[str] | None = None) -> EventStats:
    """Summarise an arm's behaviour from its event log.

    An action event carries the *observation* (what the command printed, what
    the file contains) and -- in newer logs -- the ``target`` it touched.
    A marker in the observation therefore answers "did the model look at the
    hidden contract?", which is the only way to tell "the memory changed the
    behaviour" from "the memory changed nothing".
    """
    import json

    stats = EventStats()
    path = Path(log_path)
    if not path.exists():
        return stats
    markers = markers or []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") != "action":
            if rec.get("kind") == "invalid_action":
                stats.invalid += 1
            continue
        data = rec.get("data") or {}
        kind = str(data.get("action", "")).lower()
        stats.actions += 1
        if kind == "read":
            stats.reads += 1
        elif kind == "run":
            stats.runs += 1
        elif kind in {"write", "write_many"}:
            stats.writes += 1
        if stats.actions == 1:
            stats.first_action = kind
            stats.first_target = str(data.get("target", "") or "")
        detail = str(data.get("detail", "") or "")
        if any(marker and marker in detail for marker in markers):
            stats.hidden_looks += 1
    return stats


@dataclass
class ArmResult:
    label: str
    arm: str
    rep: int
    status: str
    steps: int
    steps_to_pass: int
    tokens_to_pass: int
    model_calls: int
    tokens: int
    l1_failures: int
    guard_catches: int
    guard_injected: bool
    prompts: int
    prompts_with_lesson: int
    prompts_with_facts: int
    suite_green_after: bool
    verdict_green: bool
    tampered: bool
    duration_s: float
    #: the model never answered (the call failed): the harness broke, the task
    #: was not attempted.  Such a run must not be counted as a task failure.
    model_failed: bool = False
    #: behaviour, read back from the event log (see :func:`analyze_events`)
    actions: int = 0
    reads: int = 0
    runs: int = 0
    writes: int = 0
    invalid: int = 0
    first_action: str = ""
    hidden_looks: int = 0
    #: the model saw the held-out criterion (the canary was found in this
    #: run's event log): the repetition is contaminated and must not be
    #: published as a measurement.
    contaminated: bool = False


@dataclass
class MemBenchResult:
    mode: str
    reps: int
    family: str = "demo"
    setup: list[ArmResult] = field(default_factory=list)
    arms: list[ArmResult] = field(default_factory=list)
    lessons_after_setup: list[int] = field(default_factory=list)
    facts_stored: int = 0
    #: the lesson the setup run produced, with its trigger and its guard --
    #: they decide whether the second task is even *consulted*.
    lesson_detail: list[dict[str, Any]] = field(default_factory=list)
    guard_injected_reps: list[bool] = field(default_factory=list)
    #: the repetitions in which the model read the held-out criterion
    contaminated_reps: list[int] = field(default_factory=list)

    def by_arm(self, arm: str) -> list[ArmResult]:
        return [a for a in self.arms if a.arm == arm]

    def arm_names(self) -> list[str]:
        seen: list[str] = []
        for item in self.arms:
            if item.arm not in seen:
                seen.append(item.arm)
        return seen


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _reset_repo(repo: Path, family: Family) -> None:
    """Rebuild the code of the repository, keeping ``.planloop`` (the stores)."""
    for name in ("shop", "tests"):
        target = repo / name
        if target.exists():
            shutil.rmtree(target)
    for path, content in family.initial.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _snapshot(repo: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(repo, dest, ignore=shutil.ignore_patterns(".planloop"))


def _restore(snapshot: Path, repo: Path) -> None:
    for child in repo.iterdir():
        if child.name == ".planloop":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in snapshot.iterdir():
        target = repo / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _prepare_oracle(base: Path, name: str, source: str) -> str:
    """DEPRECATED -- and the reason this module exists is to stop using it.

    This wrote the criterion to a file and handed the model the command that
    reads it.  Measured on 2026-11-11: the model read that file three times
    in one campaign (``cd /d ... && type ...oracle\\r1_oracle_1.py``), and the
    criterion source contains the expected numbers -- so those runs were
    contaminated.  A criterion that lives on disk is not held out.

    Use :func:`planloop.heldout.run_held_out` instead: the criterion exists
    only while the check runs.
    """
    raise NotImplementedError(
        "the criterion must not be written to disk; use planloop.heldout.run_held_out"
    )


def _measure(result: RunResult, spy: Spy, repo: Path, *, guard_injected: bool,
             verdict_green: bool | None = None, tampered: bool) -> ArmResult:
    steps = sum(o.steps for o in result.outcomes)
    to_pass = sum(o.steps_to_pass for o in result.outcomes)
    tok_to_pass = sum(o.tokens_to_pass for o in result.outcomes)
    failures = sum(o.l1_failures for o in result.outcomes)
    catches = sum(1 for o in result.outcomes for f in o.failures if f.get("guard_only"))
    usage = result.usage or {}
    suite_green = demo.suite_green(repo)
    total_tokens = int(usage.get("tokens", 0) or 0)
    return ArmResult(
        label="",
        arm="",
        rep=0,
        status=result.status,
        steps=steps,
        steps_to_pass=to_pass,
        tokens_to_pass=tok_to_pass,
        model_calls=int(usage.get("calls", 0) or 0) or spy.prompts,
        tokens=total_tokens,
        l1_failures=failures,
        guard_catches=catches,
        guard_injected=guard_injected,
        prompts=spy.prompts,
        prompts_with_lesson=spy.with_lesson,
        prompts_with_facts=spy.with_facts,
        suite_green_after=suite_green,
        # with no held-out oracle the repository's own suite is the verdict
        verdict_green=suite_green if verdict_green is None else verdict_green,
        tampered=tampered,
        duration_s=result.duration_s,
        # 0 tokens means the model never answered: the call failed.  That is
        # a broken harness, not a task the model failed -- measured on a real
        # 8-rep run, three reps died that way (the provider rate-limited us)
        # and would have been counted as "the model got it wrong".
        model_failed=(total_tokens == 0 and spy.prompts <= 2),
    )


def _run_task(repo: Path, spec_dict: dict, model: Any, *, memory: MemoryStore | None,
              memory_system: MemorySystem | None, log_path: Path) -> tuple[RunResult, Spy]:
    spy = Spy(model)
    spec = ProblemSpec.from_dict(spec_dict)
    result = run(
        spec,
        model=spy,
        workspace=repo,
        memory=memory,
        memory_system=memory_system,
        log_path=log_path,
    )
    return result, spy


def scripted_model_for(arm: ArmSpec | None) -> Any:
    """The scripted model for a given arm (``None`` = the setup run)."""
    persona = (arm.persona if arm is not None else None) or "careless"
    if persona == "factual":
        return FactAwareModel()
    return demo.DemoModel(persona)


# --------------------------------------------------------------------------
# the benchmark
# --------------------------------------------------------------------------
def _build_specs(base: Path, fam: Family, rep: int, token: str = "") -> tuple[dict, dict, dict]:
    """Build the two task specs of a family, with their held-out criteria.

    Returned as plain dicts so a test can assert on them without running a
    model: the *shape* of the acceptance criteria is the measurement.

    The held-out criteria are ``kind="held_out"``: their payload is the
    answer key, so it is materialised only while the check runs and it is
    never rendered into a prompt (:mod:`planloop.heldout`).  ``token`` is the
    canary of this repetition -- it is put **into** the criterion, so a hit in
    the event log means the model read the answer key and the repetition is
    contaminated.

    ``base`` is the run's directory; the criteria no longer live there (that
    was how the model found them).
    """
    def _criterion(cid: str, source: str, hint: str) -> dict[str, Any]:
        return {
            "id": cid,
            "kind": "held_out",
            "spec": hint or "a held-out criterion the verifier runs",
            "source": heldout.with_canary(source, token) if token else source,
            "must": "exit=0",
        }

    r1_source = fam.r1_oracle
    r2_source = fam.r2_oracle
    final_source = fam.final_oracle

    r1_node = list(fam.r1_node)
    if r1_source:
        r1_node = r1_node + [_criterion("A2", r1_source, fam.r1_hint)]
    # The node of the second task is judged by the *new* feature only.
    # That gap is the point: a node can pass while the repository is
    # broken, and what closes the gap is what this benchmark measures.
    r2_node = list(fam.r2_node)
    if r2_source:
        r2_node = [_criterion("A1", r2_source, fam.r2_hint)]
    r1_success = list(fam.r1_success) or list(r1_node)
    if r1_source:
        r1_success = r1_success + [_criterion("SC2", r1_source, fam.r1_hint)]
    r2_success = list(fam.r2_success) or list(r2_node)
    if final_source:
        r2_success = [_criterion("SC1", final_source, fam.final_hint)]

    r1_spec = fam.spec(fam.r1_goal, r1_node, r1_success, fam.r1_node_goal, fam.files_for("r1"))
    r2_spec = fam.spec(fam.r2_goal, r2_node, r2_success, fam.r2_node_goal, fam.files_for("r2"))
    oracles = {"r1": r1_source, "r2": r2_source, "final": final_source}
    return r1_spec, r2_spec, oracles


def run_membench(
    base: Path,
    *,
    reps: int = 1,
    model_for: Callable[[ArmSpec | None], Any] | None = None,
    arms: list[ArmSpec] | None = None,
    mode: str = "scripted",
    family: Family | None = None,
    progress: Callable[["MemBenchResult"], None] | None = None,
) -> MemBenchResult:
    """Run the first task, then measure the second task under every arm."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    repo = base / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    fam = family or DEMO_FAMILY
    _reset_repo(repo, fam)

    factory = model_for or scripted_model_for
    arm_plan = arms if arms is not None else SCRIPTED_ARMS

    setup_results: list[ArmResult] = []
    arm_results: list[ArmResult] = []
    lessons_seen: list[int] = []
    lesson_detail: list[dict[str, Any]] = []
    guard_injected_reps: list[bool] = []
    contaminated_reps: list[int] = []
    facts_stored = 0

    for rep in range(1, reps + 1):
        _reset_repo(repo, fam)

        # -- the held-out criteria -----------------------------------------
        # the canary of this repetition: it goes *into* the criterion, so a
        # hit in the event log means the model read the answer key and the
        # repetition is contaminated.  "The criterion was held out" has to
        # be a check, not a claim.
        token = heldout.canary_token()
        r1_spec, r2_spec, oracles = _build_specs(base, fam, rep, token)
        r1_oracle_cmd = oracles["r1"]
        r2_oracle_cmd = oracles["r2"]
        final_oracle_cmd = oracles["final"]
        _write_canaries(base, rep, token)

        memory_path = base / f"memory_setup_{rep}.jsonl"
        if memory_path.exists():
            memory_path.unlink()
        store = MemoryStore(memory_path)

        # -- setup run: the first task of the family ------------------------
        setup_result, setup_spy = _run_task(
            repo, r1_spec, factory(None),
            memory=store, memory_system=None, log_path=base / f"events_setup_{rep}.jsonl",
        )
        setup = _measure(setup_result, setup_spy, repo, guard_injected=False,
                         verdict_green=(_oracle_green(r1_oracle_cmd, repo)
                                       if r1_oracle_cmd else demo.suite_green(repo)),
                         tampered=False)
        setup.label, setup.arm, setup.rep = "R1_learn", "setup", rep
        setup_stats = analyze_events(base / f"events_setup_{rep}.jsonl", fam.hidden_markers)
        setup.actions = setup_stats.actions
        setup.reads = setup_stats.reads
        setup.runs = setup_stats.runs
        setup.writes = setup_stats.writes
        setup.first_action = setup_stats.first_label
        setup.hidden_looks = setup_stats.hidden_looks
        setup_results.append(setup)
        lessons_seen.append(len(store.active()))
        for lesson in store.active():
            lesson_detail.append({
                "rep": rep,
                "summary": lesson.summary[:400],
                "cause": lesson.cause[:400],
                "fix": lesson.fix[:400],
                "strength": lesson.strength,
                "source": lesson.source,
                "notes": [
                    e for e in (lesson.evidence or [])
                    if "lesson model" in e or "second attempt" in e
                ],
                "trigger_files": list(lesson.trigger.files),
                "trigger_goals": list(lesson.trigger.goals),
                "trigger_errors": list(lesson.trigger.error_patterns),
                "guard": lesson.guard,
            })

        # the state the first task leaves behind
        snapshot = base / f"snapshot_after_r1_{rep}"
        _snapshot(repo, snapshot)
        memory_snapshot = base / f"memory_r1_{rep}.jsonl"
        shutil.copyfile(memory_path, memory_snapshot)
        # the visible tests are part of the state the second task starts from:
        # if the model rewrote them, the memory guard would be guarding a
        # test the model wrote, so tampering has to be visible in the report.
        guarded_tests = {
            path: (repo / path).read_text(encoding="utf-8")
            for path in ("tests/test_pricing.py",) if (repo / path).exists()
        }

        # the verified fact the first task earns
        system = MemorySystem(repo)
        fact_key = fam.fact_key or FACT_KEY
        fact_value = fam.fact_value or FACT_VALUE
        system.remember(
            "fact",
            fact_key,
            fact_value,
            evidence={"type": "command", "spec": FULL_SUITE, "exit": 0},
            confidence=0.9,
            source="observation",
            reason="verified by the full suite after the first task",
        )
        fact_row = system.private.get("fact", fact_key)
        if fact_row is not None:
            facts_stored += 1

        # -- measured arms: the second task of the family ------------------
        rep_guard_injected = False
        rep_arms: list[ArmResult] = []
        for spec in arm_plan:
            _restore(snapshot, repo)
            arm_memory_path = base / f"memory_{spec.label}_{rep}.jsonl"
            shutil.copyfile(memory_snapshot, arm_memory_path)
            arm_memory = MemoryStore(arm_memory_path) if spec.use_memory else None
            arm_system = system if spec.use_facts else None

            result, spy = _run_task(
                repo, r2_spec, factory(spec),
                memory=arm_memory, memory_system=arm_system,
                log_path=base / f"events_{spec.label}_{rep}.jsonl",
            )
            guard_injected = bool(result.compile_report and result.compile_report.injections)
            rep_guard_injected = rep_guard_injected or guard_injected
            verdict_green = None
            if final_oracle_cmd:
                verdict_green = _oracle_green(final_oracle_cmd, repo)
            tampered = any(
                (repo / path).exists() and (repo / path).read_text(encoding="utf-8") != content
                for path, content in guarded_tests.items()
            )
            measured = _measure(result, spy, repo, guard_injected=guard_injected,
                                verdict_green=verdict_green, tampered=tampered)
            measured.label, measured.arm, measured.rep = spec.label, spec.arm, rep
            # what the arm actually did, read back from its own event log:
            # without this the report can say the arms differed in cost, but
            # not whether the memory changed anything at all.
            stats = analyze_events(base / f"events_{spec.label}_{rep}.jsonl",
                                   fam.hidden_markers)
            measured.actions = stats.actions
            measured.reads = stats.reads
            measured.runs = stats.runs
            measured.writes = stats.writes
            measured.invalid = stats.invalid
            measured.first_action = stats.first_label
            measured.hidden_looks = stats.hidden_looks
            arm_results.append(measured)
            rep_arms.append(measured)

        # -- was the criterion really held out in this repetition? ---------
        # the canary is inside the criterion source; the source only exists
        # while a check runs, so a hit means the model read the answer key.
        rep_logs = [base / f"events_setup_{rep}.jsonl"] + [
            base / f"events_{spec.label}_{rep}.jsonl" for spec in arm_plan
        ]
        leaks = heldout.find_canary(rep_logs, token)
        leaks += heldout.find_canary([p for p in repo.rglob("*") if p.is_file()], token)
        if leaks:
            contaminated_reps.append(rep)
            setup.contaminated = True
            for measured in rep_arms:
                measured.contaminated = True

        guard_injected_reps.append(rep_guard_injected)

        # the sqlite handles are still open here; on Windows that blocks the
        # temporary directory from being cleaned up, so close them explicitly
        system.public.close()
        system.private.close()

        if progress is not None:
            # write the partial report after every rep: a run that dies at
            # rep 7 must not take reps 1-6 with it
            progress(MemBenchResult(
                mode=mode,
                reps=reps,
                family=fam.name,
                setup=list(setup_results),
                arms=list(arm_results),
                lessons_after_setup=list(lessons_seen),
                facts_stored=facts_stored,
                lesson_detail=list(lesson_detail),
                guard_injected_reps=list(guard_injected_reps),
                contaminated_reps=list(contaminated_reps),
            ))

    return MemBenchResult(
        mode=mode,
        reps=reps,
        family=fam.name,
        setup=setup_results,
        arms=arm_results,
        lessons_after_setup=lessons_seen,
        facts_stored=facts_stored,
        lesson_detail=lesson_detail,
        guard_injected_reps=guard_injected_reps,
        contaminated_reps=contaminated_reps,
    )


def _write_canaries(base: Path, rep: int, token: str) -> None:
    """Record the canary of a repetition next to its artifacts.

    The verification tool needs it to re-check the raw event logs, so it is
    written per repetition (a run that dies at rep 7 keeps reps 1-6).
    """
    path = base / "canaries.json"
    data: dict[str, str] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data[str(rep)] = token
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _oracle_green(source: str, repo: Path) -> bool:
    """Run a held-out criterion against the workspace and report pass/fail.

    The criterion is materialised for the duration of the run and deleted
    again: while the model acts, the answer key is not on disk.
    """
    if not source:
        return False
    return heldout.run_held_out(source, repo, name="oracle").ok


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _arm_label(arm: str) -> str:
    return {
        "setup": "R1 第一道任务（建立记忆）",
        "no_memory": "R2 无记忆（对照）",
        "guard_only": "R2 只有护栏",
        "lesson_and_guard": "R2 教训+护栏",
        "fact_only": "R2 只有已验证事实",
    }.get(arm, arm)


def to_markdown(result: MemBenchResult) -> str:
    lines: list[str] = []
    add = lines.append
    mode_note = (
        "模型是**脚本模型**（确定性，可复现），所以这份数字量的是**机制**，不是真实模型。"
        if result.mode == "scripted"
        else "模型是**真实模型**（同一模型、同一任务，只有记忆不同），所以这份数字量的是**真实模型下的机制收益**。"
    )
    add("# 记忆值不值：第二次同类任务快多少")
    add("")
    add("> 这份文档回答 [market-scan.md](market-scan.md) §四 第 5 条那个一直没做的度量：")
    add("> **记忆到底值不值那些代码**。装置：同一个仓库、同一个任务族的两道任务——")
    add("> 第一道任务修折扣逻辑（并因此踩坑），第二道任务改同一个文件（回归风险最高的那种）。")
    add("> 对照臂**完全无记忆**，其余臂分别只开一个记忆子系统，判分统一用**仓外判据**。")
    add(">")
    add(f"> {mode_note}")
    add(f"> 模式：**{result.mode}**，任务族：**{result.family}**，重复 **{result.reps}** 次。")
    add("")
    add("## 一、结论")
    add("")
    control = [a for a in result.by_arm("no_memory") if not a.model_failed and not a.contaminated]
    lessons = [a for a in result.by_arm("lesson_and_guard") if not a.model_failed and not a.contaminated]
    facts = [a for a in result.by_arm("fact_only") if not a.model_failed and not a.contaminated]
    if result.contaminated_reps:
        add(f"- **被污染的重复：{len(result.contaminated_reps)} 次"
            f"（rep {', '.join(str(r) for r in result.contaminated_reps)}）**——"
            "模型读到了仓外判据的源码（事件流里扫到了判据里的 canary），"
            "这些重复的数字**不进下面的统计**：判据一旦泄题，测的就不是记忆了。")
        add("")
    if control:
        green = sum(1 for a in control if a.verdict_green)
        add(f"- 第一次任务（R1）留下：教训 {result.lessons_after_setup} 条、已验证事实 {result.facts_stored} 条")
        if result.lesson_detail:
            model_written = sum(1 for d in result.lesson_detail if d.get("source") == "model")
            total = len(result.lesson_detail)
            if model_written < total:
                add(f"- **其中 {model_written}/{total} 条是模型自己写的**；"
                    f"其余 {total - model_written} 条**退化成了兜底模板**"
                    f"（模型只交了思考过程、没交答案）——"
                    f"这种教训没有触发条件也没有护栏，等于没有。")
        add(f"- 第二次任务，**无记忆**：外部判定为绿 **{green}/{len(control)}**，"
            f"平均步数 {_mean([a.steps for a in control]):.1f}")
        if lessons:
            green_l = sum(1 for a in lessons if a.verdict_green)
            injected = sum(a.prompts_with_lesson for a in lessons)
            prompts = sum(a.prompts for a in lessons)
            add(f"- 第二次任务，**教训+护栏**：外部判定为绿 **{green_l}/{len(lessons)}**，"
                f"平均步数 {_mean([a.steps for a in lessons]):.1f}，"
                f"教训出现在 **{injected}/{prompts}** 个 prompt 里")
        if facts:
            green_f = sum(1 for a in facts if a.verdict_green)
            injected_f = sum(a.prompts_with_facts for a in facts)
            prompts_f = sum(a.prompts for a in facts)
            add(f"- 第二次任务，**只有已验证事实**：外部判定为绿 **{green_f}/{len(facts)}**，"
                f"平均步数 {_mean([a.steps for a in facts]):.1f}，"
                f"事实出现在 **{injected_f}/{prompts_f}** 个 prompt 里")
    if control and lessons:
        c_tokens = _mean([a.tokens for a in control])
        l_tokens = _mean([a.tokens for a in lessons])
        c_green = sum(1 for a in control if a.verdict_green)
        l_green = sum(1 for a in lessons if a.verdict_green)
        delta = (l_tokens - c_tokens) / c_tokens if c_tokens else 0.0
        if c_green == l_green:
            verdict = "**结果没有差异**"
        else:
            verdict = f"结果差异 {c_green}/{len(control)} vs {l_green}/{len(lessons)}"
        if not c_tokens:
            cost = "（对照臂还没有可用数据）"
        else:
            cost = f"少花 {-delta:.0%} tokens" if delta < 0 else f"多花 {delta:+.0%} tokens"
        add(f"- **一句话**：教训确实进了 prompt，但{verdict}，"
            f"代价是**{cost}**（{c_tokens:.0f} → {l_tokens:.0f}）。")
    if control and facts:
        c_work = _mean([a.steps_to_pass for a in control])
        f_work = _mean([a.steps_to_pass for a in facts])
        c_tok = _mean([a.tokens_to_pass for a in control])
        f_tok = _mean([a.tokens_to_pass for a in facts])
        # The report is written after *every* rep, so it must survive a run
        # whose first rep produced nothing: a zero denominator is a normal
        # state here, not an error.  Measured on 2026-11-11, a division by
        # zero in this line killed a whole 8-rep campaign **after the model
        # calls had already been paid for**.
        delta = f"{(f_tok - c_tok) / c_tok:+.0%}" if c_tok else "（对照臂还没有可用数据）"
        add(f"- **成本口径（对照 vs 事实，两臂验收集合相同）**："
            f"做到验收通过要 **{c_work:.1f} → {f_work:.1f} 步**，"
            f"**{c_tok:.0f} → {f_tok:.0f} tokens**"
            f"（{delta}）。")
    if control and lessons:
        c_green = sum(1 for a in control if a.verdict_green)
        l_green = sum(1 for a in lessons if a.verdict_green)
        add(f"- **正确性口径（对照 vs 教训+护栏，护栏改的就是验收集合）**："
            f"外部判定为绿 **{c_green}/{len(control)} vs {l_green}/{len(lessons)}**，"
            f"护栏拦截 {sum(a.guard_catches for a in lessons)} 次。")
    # the behaviour question: did the memory change what the model *did*?
    if control and (lessons or facts):
        c_looks = sum(a.hidden_looks for a in control)
        other = lessons or facts
        o_looks = sum(a.hidden_looks for a in other)
        label = "教训+护栏" if lessons else "只有已验证事实"
        if c_looks or o_looks:
            if o_looks < c_looks:
                add(f"- **行为口径（对照 vs {label}）**：隐藏约定被打开 **{c_looks} → {o_looks} 次**——"
                    f"记忆**替掉了一次核对**。")
            else:
                add(f"- **行为口径（对照 vs {label}）**：隐藏约定被打开 **{c_looks} → {o_looks} 次**——"
                    f"记忆**没有替掉核对**（模型照样去核对），"
                    f"所以成本口径上的差异不能算作记忆的收益。")
    # "never finished" and "finished but failed" are different failures --
    # bench.md 17.3: conflating them turns a budget exhaustion into a
    # correctness verdict.
    unfinished = sum(1 for a in result.arms if a.steps_to_pass == 0)
    red_but_finished = sum(1 for a in result.arms if a.steps_to_pass > 0 and not a.verdict_green)
    if result.arms and (unfinished or red_but_finished):
        add(f"- **失败要分开看**：{unfinished} 次**没跑完**（验收一次都没过，步数用尽），"
            f"{red_but_finished} 次**跑完了但外部判定为红**。"
            f"前者是预算问题，不是答案错——合并统计会把预算问题说成正确性问题。")
    # "the model never answered" is a broken harness, not a task outcome:
    # those runs must leave the denominators, or a provider hiccup is
    # reported as "the model got it wrong".
    broken = [a for a in result.arms if a.model_failed]
    valid = [a for a in result.arms if not a.model_failed]
    broken_setups = [s for s in result.setup if s.model_failed]
    if broken:
        add(f"- **模型调用失败的运行：{len(broken)} 次**（0 tokens，模型根本没回答）——"
            f"那是装置坏了，不是任务结果，**不计入下面的绿/红统计**。"
            + (f"其中 {len(broken_setups)} 次连第一道任务都没跑起来。"
               if broken_setups else ""))
    add("")
    add("## 二、逐臂汇总")
    add("")
    add("| 臂 | n（有效/总） | 外部判定为绿 | 仓库全量测试为绿 | 到验收通过：步数 | 到验收通过：tokens | 总步数 | 总 tokens | 平均 L1 失败 | 护栏拦截 | 带教训的 prompt | 带事实的 prompt | 动了隐藏约定 | 无效回复 | 第一步 |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for arm in result.arm_names():
        items = result.by_arm(arm)
        ok = [a for a in items if not a.model_failed and not a.contaminated]
        green = sum(1 for a in ok if a.verdict_green)
        suite = sum(1 for a in ok if a.suite_green_after)
        prompts = sum(a.prompts for a in ok)
        looks = sum(a.hidden_looks for a in ok)
        firsts = sorted({a.first_action for a in ok if a.first_action})
        add(
            f"| {_arm_label(arm)} | {len(ok)}/{len(items)} | **{green}/{len(ok)}** | {suite}/{len(ok)} | "
            f"{_mean([a.steps_to_pass for a in ok]):.1f} | "
            f"{_mean([a.tokens_to_pass for a in ok]):.0f} | "
            f"{_mean([a.steps for a in ok]):.1f} | {_mean([a.tokens for a in ok]):.0f} | "
            f"{_mean([a.l1_failures for a in ok]):.1f} | "
            f"{sum(a.guard_catches for a in ok)} | "
            f"{sum(a.prompts_with_lesson for a in ok)}/{prompts} | "
            f"{sum(a.prompts_with_facts for a in ok)}/{prompts} | "
            f"{'是' if any(a.tampered for a in ok) else '否'} | "
            f"{looks} 次 | {sum(a.invalid for a in ok)} | {'、'.join(firsts) or '—'} |"
        )
    add("")
    add("> **「到验收通过」那两列才是工作量**：执行器要等模型「自己宣布完成」才停，")
    add("> 真实模型做完之后还会反复跑同一套测试——那是模型的确认习惯，不是工作量。")
    add("> 总步数 / 总 tokens 因此会被顶满，不该当成本口径。")
    add("")
    add("> **「动了隐藏约定」是从事件流回读出来的行为**，不是自述：")
    add("> 动作事件的 observation 里出现了隐藏约定的源码，就说明模型又去打开它了。")
    add("> 这一列是「记忆到底有没有改变行为」的唯一可查证据——")
    add("> 成本没差、结果没差，但**行为也没差**，那结论就不是「记忆没用」，")
    add("> 而是「这类任务上记忆没有可测收益」。")
    add("")
    add("## 三、逐次明细")
    add("")
    add("| rep | 臂 | 状态 | 到验收通过：步数 | 到验收通过：tokens | 总步数 | 模型调用 | 总 tokens | L1 失败 | 护栏拦截 | 无效回复 | 带教训 | 带事实 | 外部判定 | 全量测试 |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for item in result.setup + result.arms:
        status = item.status + (" **污染**" if item.contaminated else "")
        add(
            f"| {item.rep} | {item.label} | {status} | {item.steps_to_pass} | {item.tokens_to_pass} | "
            f"{item.steps} | {item.model_calls} | "
            f"{item.tokens} | {item.l1_failures} | {item.guard_catches} | {item.invalid} | "
            f"{item.prompts_with_lesson}/{item.prompts} | {item.prompts_with_facts}/{item.prompts} | "
            f"{'绿' if item.verdict_green else '**红**'} | "
            f"{'绿' if item.suite_green_after else '**红**'} |"
        )
    add("")
    add("## 四、两个记忆子系统分开算")
    add("")
    add("- **教训 + 护栏**（`memory.py`）：模型读到教训 → 少走弯路；模型不听 → 护栏拦住错误写入。")
    add("- **已验证事实**（`memdb.py`）：只有事实真的进了 prompt 才有收益；"
        "「带事实的 prompt」那一列是 0/N 的话，它就是库里躺着没人看的记录。")
    add("")
    add(f"- 护栏注入：{sum(result.guard_injected_reps)}/{result.reps} 次重复里，"
        "教训被编译成一条硬检查（`I_MEMORY_GUARD`）并加进节点的验收项。")
    add("")
    if result.family == "contract":
        add("### 装置说明（为什么任务族长这样）")
        add("")
        add("脚本模型的任务族对真实模型**不成立**：真实模型一次就把折扣改对了，")
        add("没有「失败 → 修复」，也就**没有教训可学**（教训只在失败后被修复时才产生）。")
        add("所以真实模式换了一个任务族，把坑放进**仓库**而不是放进模型的心不在焉：")
        add("")
        add("- `shop/helpers.py` 的 `pct()` 契约与名字相反（`pct(10) == 0.9`，不是 `0.1`）；")
        add("- 仓库里的可见测试**不**钉这个契约，**仓外判据**钉——所以第一遍修复必然失败一次；")
        add("- 第二道任务要求用 `pct()` 换算会员折扣，而它的 prompt **不带** `shop/helpers.py`：")
        add("  想知道契约只能自己打开那个文件——**或者从记忆里直接拿到**。")
        add("")
        add("这条缝是「第二次快多少」唯一能露出来的地方：")
        add("记忆臂**不用去读**那个文件，对照臂必须读。")
        add("")
        add("> **两个信号要分开读**，因为两组的验收集合故意不同：")
        add(">")
        add("> - **成本**：对照 vs **只有事实**。事实臂不注入护栏，验收集合与对照臂完全相同，")
        add(">   所以「到验收通过的步数」和 tokens 是可比的成本口径。")
        add("> - **正确性**：对照 vs **教训+护栏**。护栏**就是**把全套测试加进节点验收，")
        add(">   所以这里的差异归因于护栏，不是别的。")
        add("")
    if result.family == "fee":
        add("### 装置说明（为什么任务族长这样）")
        add("")
        add("这一族是为了修掉 `contract` 族的**装置缺陷**：")
        add("那里的契约（`pct(10) == 0.9`）是**猜得出来的**——")
        add("可见测试钉住 `total([100,200],10) == 270`，模型推出 0.9 就顺手推出 0.8，")
        add("于是那次「核对」是**自愿的**，记忆没有东西可省（见 [bench.md](bench.md) §二十一）。")
        add("")
        add("这一族改了三处，全在装置侧：")
        add("")
        add("- **平台费按折扣档位取值**（0%→0.00、10%→0.02、20%→0.05），")
        add("  所以 `pct(10) == 0.88` **推不出** `pct(20) == 0.75`——")
        add("  可见测试只钉 10% 那一行，20% 那一行**只有读文件才知道**；")
        add("- **仓外判据的断言消息是固定字符串**，不打印期望值，")
        add("  免得失败反馈把答案直接送给对照臂；")
        add("- 第二道任务的 prompt **不带** `shop/helpers.py`，")
        add("  于是「查表」是**必需动作**而不是「可选确认」。")
        add("")
        add("> 守卫测试 `tests/test_membench.py::FeeFamilyTest` 里有一条是**反证**：")
        add("> 把「显然公式」写进仓库，仓外判据必须判它**不通过**——")
        add("> 如果它通过了，装置又变回可测不出东西的样子。")
        add("")
    if result.lesson_detail:
        add("## 五、R1 留下的那条教训")
        add("")
        for detail in result.lesson_detail:
            add(f"- **rep {detail['rep']}**｜强度 `{detail['strength']}`｜来源 `{detail['source']}`")
            add(f"  - 教训：{detail['summary']}")
            if detail["cause"]:
                add(f"  - 原因：{detail['cause']}")
            if detail["fix"]:
                add(f"  - 做法：{detail['fix']}")
            add(f"  - 触发条件：files={detail['trigger_files']} goals={detail['trigger_goals']} "
                f"errors={detail['trigger_errors']}")
            add(f"  - 护栏：`{detail['guard']}`")
            for note in detail.get("notes") or []:
                add(f"  - ⚠️ {note[:200]}")
        add("")
        add("> 触发条件决定第二道任务**会不会真的去查这条教训**；护栏决定教训能不能变成一条硬检查。")
        add("> 两者都由模型自己写，所以「记忆存在」不等于「记忆被用上」。")
        add("")
    add("## 六、这份数字不证明什么")
    add("")
    items: list[str] = []
    if result.mode == "scripted":
        items.append("**脚本模型**：数字是确定性的，方差为 0；它量的是机制，不是真实模型的表现。")
    else:
        items.append("**真实模型但样本小**：重复次数少，比例型指标（绿/红）的置信区间很宽。")
        items.append(
            "**步数被打满**：真实模型很少发 `finish`，执行器要等模型「自己宣布完成」才停，"
            "所以步数常常顶到上限——**真实模式下步数这一列饱和，不能当成本口径**，成本要看 tokens。"
        )
    items.append("**一个任务族**：只有「先修折扣、后改同一个文件」这一类。别的族（性能、并发、依赖升级）没测。")
    items.append("**步数不是全部**：省步数要换算成 token 与时间才是成本口径。")
    items.append(
        "**没有对照组「模型更强」**：真正该比的不是「有记忆 vs 没记忆」，而是「有记忆 vs 换个更强的模型」。"
    )
    items.append(
        "**任务太容易时测不出差异**：如果模型一次就做对（先修折扣、后加参数），三条臂都会是绿的——"
        "这说明的是「**这类任务上**记忆没有可测收益」，**不是**「记忆没有收益」。"
        "要测收益，得找模型真的会犯错的场景。"
    )
    if result.family == "contract":
        items.append(
            "**这一族的契约是猜得出来的**：可见测试钉住 `total([100,200],10) == 270`，"
            "模型推出 `pct(10) == 0.9` 就顺手推出 `pct(20) == 0.8`——"
            "所以那次「核对」是**自愿的**（不核对也能过），"
            "**这份数字测的是「核对」，不是「依赖」**（见 [bench.md](bench.md) §二十一）。"
        )
    if result.family == "fee":
        items.append(
            "**知识仍然在仓库里**：这一族只是把契约从「可猜」变成「要查」，"
            "没有把它搬到模型够不着的地方——所以测的是「记忆能不能替掉一次必需的查表」，"
            "**不是**「记忆能不能在知识不可得时救场」。后者要另设装置。"
        )
        items.append(
            "**档位表本身是有先例的**：模型若熟悉这类费率表结构，"
            "可能自己推出「10% 是 0.02、20% 就不是」——装置只保证「表里没写的就推不出」，"
            "不保证「任何模型都推不出」。"
        )
    for index, item in enumerate(items, start=1):
        add(f"{index}. {item}")
    add("")
    add("## 七、决策含义")
    add("")
    add("- 这一轮在真实模型上证明的是：**记忆这条路是通的**（教训进 prompt、护栏注入），")
    add("  而修之前它是**死的**。机制活不活，和机制值不值，是两件事。")
    c_green = sum(1 for a in control if a.verdict_green) if control else 0
    l_green = sum(1 for a in lessons if a.verdict_green) if lessons else 0
    f_green = sum(1 for a in facts if a.verdict_green) if facts else 0
    all_green = control and lessons and facts and c_green == len(control) \
        and l_green == len(lessons) and f_green == len(facts)
    if all_green:
        add("- 这一轮**没有**证明记忆有价值：三条臂全部为绿，记忆只是多花 tokens。")
        add("  所以**不能说**\"记忆没用\"，也**不能说**\"记忆有用\"；只能说：")
        add("  **「在这类任务上，注入记忆没有改变结果」**。")
    elif control and lessons:
        add(f"- 这一轮**测到了正确性差异**：对照 {c_green}/{len(control)} vs "
            f"教训+护栏 {l_green}/{len(lessons)}（事实臂 {f_green}/{len(facts)}）。")
        add("  差异归因于记忆本身，不是装置——但**样本量小**，")
        add("  必须换场景/换模型再验一次才敢写进结论。")
    add("- `memory.py`（教训 + 护栏）与 `memdb.py`（事实）两条路径：脚本模型上收益重合，")
    add("  真实模型上**都只是成本**。合并/取舍不得在真实模型数据之前做决定，")
    add("  否则又是拿脚本模型替真实模型做决定。")
    if result.family == "fee":
        add("- 这一族的装置是**有效的**，所以这里的差异不是装置制造的：")
        add("  契约**不可猜**（`pct(10)==0.88` 推不出 `pct(20)==0.75`）、")
        add("  仓外判据**不泄题**（断言消息里没有任何小数）、")
        add("  「显然公式」会被判据判不通过（守卫测试 `FeeFamilyTest`）。")
        add("- `memory.py`（教训 + 护栏）与 `memdb.py`（事实）两条路径：**差别不只在收益，还在保真度**。")
        add("  已验证事实是**外部观测**本身；模型写的教训是模型对观测的**转述**——")
        add("  转述会掉信息、会编（[bench.md](bench.md) §十九.7 实测到一条编错的教训）。")
        add("  **合并/取舍不得只按收益决定，还要按保真度决定。**")
    return "\n".join(lines) + "\n"

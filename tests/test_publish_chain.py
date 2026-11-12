"""The publish chain, end to end -- and **every link must be able to say no**.

The chain is: the run's event logs -> the machine report -> the published
document.  Four gates stand on it:

* ``membench --verify`` -- the report against the event logs (exit code);
* ``membench_publish check`` -- the document's per-rep table against a rebuild
  from the logs;
* ``membench_publish check-behaviour`` -- the document's lookup counts;
* ``membench_publish check-floor`` -- the document's effects against the
  recomputed noise floor.

A gate nobody has ever seen go red is a gate nobody reads.  So this test
walks the whole chain with a **real report** (the real generator) and then
breaks one number at a time: each break has to turn exactly the gate that
owns it red.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from planloop.cli import main
from planloop.membench import ArmResult, MemBenchResult, to_markdown
from planloop.membench_publish import check_behaviour, check_floor, check_published
from planloop.membench_verify import recompute

#: per arm, per repetition: the steps, the tokens, the lookups, the failures.
#: Repetition 1 is the **silent** one (the lesson arm carried no lesson, so
#: both arms ran the same prompt) -- that is where the noise floor comes from.
ARMS = {
    "R2_none": {"steps": [4, 3], "tokens": [100, 100], "looks": [3, 2], "failures": [1, 1]},
    "R2_memory": {"steps": [6, 5], "tokens": [140, 130], "looks": [5, 4], "failures": [1, 1]},
    "R2_fact": {"steps": [2, 2], "tokens": [70, 70], "looks": [2, 2], "failures": [1, 1]},
}
#: the setup phase (the first task), same shape
SETUP = {"steps": [12, 12], "tokens": [100, 100], "looks": [1, 1], "failures": [1, 1]}
#: the repetitions in which the lesson arm actually carried a lesson
LESSON_REPS = {1: 0, 2: 5}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _pct(delta: float, base: float) -> int:
    return round(delta / base * 100)


class PublishChainTest(unittest.TestCase):
    def _log(self, actions: int, looks: int, failures: int) -> list[dict]:
        events: list[dict] = [{"kind": "node_start", "data": {"attempt": 1}}]
        for _ in range(failures):
            events.append({"kind": "failure", "data": {"reason": "boom"}})
        for index in range(actions):
            events.append({"kind": "action", "data": {
                "action": "read", "target": "shop/helpers.py",
                "detail": "def pct(value): ..." if index < looks else "nothing here"}})
        events.append({"kind": "node_end", "data": {"status": "done", "steps": actions}})
        return events

    def _arm_result(self, label: str, arm: str, rep: int, values: dict,
                    lesson: int = 0) -> ArmResult:
        return ArmResult(
            label=label, arm=arm, rep=rep, status="succeeded",
            steps=values["steps"][rep - 1], steps_to_pass=values["steps"][rep - 1],
            tokens_to_pass=values["tokens"][rep - 1], model_calls=12, tokens=1000,
            l1_failures=values["failures"][rep - 1], guard_catches=0, guard_injected=False,
            prompts=12, prompts_with_lesson=lesson,
            prompts_with_facts=12 if label == "R2_fact" else 0,
            suite_green_after=True, verdict_green=True, tampered=False, duration_s=1.0,
            invalid=0, hidden_looks=values["looks"][rep - 1],
        )

    def _fixture(self, tmp: Path):
        """A run directory, the machine report the real generator writes, and a document."""
        run_dir = tmp / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        setup: list[ArmResult] = []
        arms: list[ArmResult] = []

        for rep in (1, 2):
            setup.append(self._arm_result("R1_learn", "setup", rep, SETUP))
            (run_dir / f"events_setup_{rep}.jsonl").write_text(
                "\n".join(json.dumps(e) for e in self._log(
                    SETUP["steps"][rep - 1], SETUP["looks"][rep - 1],
                    SETUP["failures"][rep - 1])) + "\n", encoding="utf-8")
            for label, values in ARMS.items():
                arm = {"R2_none": "no_memory", "R2_memory": "lesson_and_guard",
                       "R2_fact": "fact_only"}[label]
                lesson = LESSON_REPS[rep] if label == "R2_memory" else 0
                arms.append(self._arm_result(label, arm, rep, values, lesson))
                (run_dir / f"events_{label}_{rep}.jsonl").write_text(
                    "\n".join(json.dumps(e) for e in self._log(
                        values["steps"][rep - 1], values["looks"][rep - 1],
                        values["failures"][rep - 1])) + "\n", encoding="utf-8")

        report = tmp / "report.md"
        report.write_text(
            to_markdown(MemBenchResult(mode="real", reps=2, family="fee",
                                       setup=setup, arms=arms)),
            encoding="utf-8")
        doc = tmp / "doc.md"
        doc.write_text(self._doc(setup + arms), encoding="utf-8")
        return run_dir, report, doc

    def _doc(self, items: list[ArmResult]) -> str:
        """A document with the three tables the publish checks read."""
        per_rep = ["| rep | 臂 | 状态 | 到验收通过：步数 | 到验收通过：tokens | 总步数 | 总 tokens | "
                   "L1 失败 | 带教训 | 带事实 | 外部判定 | 全量测试 |"]
        for item in items:
            per_rep.append(
                f"| {item.rep} | {item.label} | {item.status} | {item.steps_to_pass} | "
                f"{item.tokens_to_pass} | {item.steps} | {item.tokens} | {item.l1_failures} | "
                f"{item.prompts_with_lesson}/{item.prompts} | "
                f"{item.prompts_with_facts}/{item.prompts} | 绿 | 绿 |")

        def looks(label: str, rep: int) -> int:
            return ARMS[label]["looks"][rep - 1]

        behaviour = ["| rep | 对照臂 查表/第一步 | 教训臂 查表/第一步 | 事实臂 查表/第一步 |",
                     "|---|---|---|---|"]
        for rep in (1, 2):
            behaviour.append(
                f"| {rep} | {looks('R2_none', rep)} | {looks('R2_memory', rep)} | "
                f"{looks('R2_fact', rep)} |")
        behaviour.append("| **合计** | "
                         + " | ".join(str(sum(ARMS[a]["looks"])) for a in ARMS) + " |")

        # the floor comes from repetition 1 (the silent one) and the effects
        # from both repetitions -- the same arithmetic the gate does
        def mean_of(label: str, key: str, reps=(1, 2)) -> float:
            return _mean([ARMS[label][key][r - 1] for r in reps])

        floors = {
            "steps": _pct(mean_of("R2_memory", "steps", (1,))
                          - mean_of("R2_none", "steps", (1,)), mean_of("R2_none", "steps", (1,))),
            "tokens": _pct(mean_of("R2_memory", "tokens", (1,))
                           - mean_of("R2_none", "tokens", (1,)), mean_of("R2_none", "tokens", (1,))),
            "looks": _pct(mean_of("R2_memory", "looks", (1,))
                          - mean_of("R2_none", "looks", (1,)), mean_of("R2_none", "looks", (1,))),
        }
        effects = {
            "steps": _pct(mean_of("R2_fact", "steps") - mean_of("R2_none", "steps"),
                          mean_of("R2_none", "steps")),
            "tokens": _pct(mean_of("R2_fact", "tokens") - mean_of("R2_none", "tokens"),
                           mean_of("R2_none", "tokens")),
            "looks": _pct(mean_of("R2_fact", "looks") - mean_of("R2_none", "looks"),
                          mean_of("R2_none", "looks")),
        }

        def verdict(effect: int, floor: int) -> str:
            return ("✅ **在噪声之上**" if abs(effect) >= floor
                    else f"❌ 落在地板（±{floor}%）内")

        effect = ["| 口径 | 对照 → 事实臂 | 相对噪声地板 |", "|---|---|---|",
                  f"| 到验收通过的步数 | 3.5 → 2.0（{effects['steps']}%） | "
                  f"{verdict(effects['steps'], floors['steps'])} |",
                  f"| 到验收通过的 tokens | 100 → 70（{effects['tokens']}%） | "
                  f"{verdict(effects['tokens'], floors['tokens'])} |",
                  f"| 查档位表次数 | 5 → 3（{effects['looks']}%） | "
                  f"{verdict(effects['looks'], floors['looks'])} |",
                  "",
                  f"同 prompt 的两臂相差：步数 ±{floors['steps']}%、tokens ±{floors['tokens']}%、"
                  f"查表 ±{floors['looks']}%。"]
        return ("\n".join(per_rep) + "\n\n" + "\n".join(behaviour) + "\n\n"
                + "\n".join(effect) + "\n")

    # -- the chain --------------------------------------------------------

    def _verify_code(self, run_dir: Path, report: Path) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["membench", "--verify", "--family", "fee",
                         "--dir", str(run_dir), "--report", str(report)])

    def test_the_whole_chain_is_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report, doc = self._fixture(tmp)
            self.assertEqual(self._verify_code(run_dir, report), 0)
            self.assertEqual(check_published(doc, run_dir, report), [])
            self.assertEqual(check_behaviour(doc, run_dir), [])
            self.assertEqual(check_floor(doc, run_dir, report), [])

    def test_a_broken_report_turns_the_gate_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report, doc = self._fixture(tmp)
            text = report.read_text(encoding="utf-8")
            # the L1 failure cell of the first per-rep row
            broken = text.replace("| 1 | R2_none | succeeded | 4 | 100 | 4 | 12 | 1000 | 1 |",
                                  "| 1 | R2_none | succeeded | 4 | 100 | 4 | 12 | 1000 | 7 |", 1)
            self.assertNotEqual(text, broken, "the fixture's report row changed shape")
            report.write_text(broken, encoding="utf-8")
            self.assertEqual(self._verify_code(run_dir, report), 1)

    def test_a_broken_cell_in_the_document_turns_its_own_gate_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report, doc = self._fixture(tmp)
            text = doc.read_text(encoding="utf-8")
            doc.write_text(text.replace("| 1 | R2_none | succeeded | 4 |",
                                        "| 1 | R2_none | succeeded | 9 |", 1), encoding="utf-8")
            self.assertTrue(check_published(doc, run_dir, report),
                            "a wrong cell in the per-rep table passed the check")

    def test_a_wrong_lookup_count_turns_the_behaviour_gate_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report, doc = self._fixture(tmp)
            text = doc.read_text(encoding="utf-8")
            row = (f"| 1 | {ARMS['R2_none']['looks'][0]} | {ARMS['R2_memory']['looks'][0]} | "
                   f"{ARMS['R2_fact']['looks'][0]} |")
            broken = text.replace(row, f"| 1 | {ARMS['R2_none']['looks'][0] + 4} | "
                                       f"{ARMS['R2_memory']['looks'][0]} | "
                                       f"{ARMS['R2_fact']['looks'][0]} |", 1)
            self.assertNotEqual(text, broken, "the fixture's behaviour row changed shape")
            doc.write_text(broken, encoding="utf-8")
            self.assertTrue(check_behaviour(doc, run_dir),
                            "a wrong lookup count passed the check")

    def test_an_effect_below_the_floor_cannot_be_published_as_a_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report, doc = self._fixture(tmp)
            text = doc.read_text(encoding="utf-8")
            doc.write_text(text.replace("落在地板（±50%）内", "✅ **在噪声之上**", 1),
                           encoding="utf-8")
            problems = check_floor(doc, run_dir, report)
            self.assertTrue(problems, "an effect inside the noise floor passed the check")

    def test_the_gate_recomputes_the_same_numbers_the_document_quotes(self):
        """The chain must not drift: the gate's aggregate is the document's aggregate."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report, doc = self._fixture(tmp)
            result = recompute(run_dir, report, MEM_FAMILY)
            summary = result.summaries["R2_none"]
            self.assertEqual(summary.l1_failures, 1.0)
            self.assertEqual(summary.hidden_looks, 5)


from planloop.membench import FEE_FAMILY as MEM_FAMILY  # noqa: E402


if __name__ == "__main__":
    unittest.main()

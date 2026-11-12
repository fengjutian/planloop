"""The gate has to be able to **fail**.

2026-11-12: the publish gate (`membench --verify`) recomputes the published
numbers from the raw artifacts and cross-checks them against the machine
report.  Its exit code is the only thing that stops a broken run from being
published -- and until now **nothing tested that exit code**: the gate had
never been observed to say "no".

So: the gate is green on a consistent run, red on a disagreement, and still
green on a note (a note is "worth saying", not "wrong").
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from planloop.cli import main
from planloop.membench import Spy, _measure
from planloop.scheduler import RunResult, all_attempts


def _rows(report_rows: list[dict]) -> str:
    header = ("| rep | 臂 | 状态 | steps2pass | tok2pass | 总步数 | 模型调用 | 总 tokens | "
              "L1 失败 | 护栏拦截 | 无效回复 | 带教训 | 带事实 | 外部判定 | 全量测试 |")
    lines = [header, "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in report_rows:
        cells = [str(row[c]) for c in (
            "rep", "arm", "status", "steps_to_pass", "tokens_to_pass", "total_steps",
            "model_calls", "tokens", "l1_failures", "guard_catches")]
        cells.append(str(row.get("invalid", 0)))
        cells += [str(row[c]) for c in (
            "prompts_lesson", "prompts_facts", "verdict", "suite")]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


class GateExitCodeTest(unittest.TestCase):
    """`membench --verify` must be able to say no."""

    def _fixture(self, tmp: Path, *, report_l1: int, log_failures: int):
        run_dir = tmp / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {"kind": "node_start", "data": {"attempt": 1}},
            {"kind": "failure", "data": {"reason": "boom"}},
            {"kind": "node_end", "data": {"status": "failed", "steps": 12}},
        ]
        for _ in range(log_failures - 1):
            events.append({"kind": "failure", "data": {"reason": "boom again"}})
        events.append({"kind": "action", "data": {"action": "read", "target": "a.py"}})
        (run_dir / "events_R2_none_1.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
            encoding="utf-8",
        )
        report = tmp / "report.md"
        report.write_text(_rows([{
            "rep": 1, "arm": "R2_none", "status": "succeeded", "steps_to_pass": 2,
            "tokens_to_pass": 100, "total_steps": 12, "model_calls": 12, "tokens": 1000,
            "l1_failures": report_l1, "guard_catches": 0, "prompts_lesson": "0/12",
            "prompts_facts": "0/12", "verdict": "绿", "suite": "绿",
        }]), encoding="utf-8")
        return run_dir, report

    def _verify(self, run_dir: Path, report: Path) -> int:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["membench", "--verify", "--family", "fee",
                         "--dir", str(run_dir), "--report", str(report)])
        self.last_output = buffer.getvalue()
        return code

    def test_the_gate_is_green_when_the_report_matches_the_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, report_l1=1, log_failures=1)
            self.assertEqual(self._verify(run_dir, report), 0)

    def test_the_gate_is_red_when_the_report_disagrees_with_the_logs(self):
        """This is the whole point of the gate."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, report_l1=0, log_failures=2)
            self.assertEqual(self._verify(run_dir, report), 1)

    def test_a_missing_report_is_a_usage_error(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp, report_l1=1, log_failures=1)
            with contextlib.redirect_stderr(io.StringIO()):
                without = main(["membench", "--verify", "--family", "fee",
                                "--dir", str(run_dir)])
                missing = main(["membench", "--verify", "--family", "fee", "--dir", str(run_dir),
                                "--report", str(tmp / "nope.md")])
        self.assertEqual(without, 2)
        self.assertEqual(missing, 2)


class InterruptedAttemptsAreCountedTest(unittest.TestCase):
    """The measurement must count the attempts a policy check cut short.

    Otherwise the report says fewer steps and fewer failures than the event
    log records -- which is exactly what happened in the real run.
    """

    def _make_outcome(self, steps: int, l1: int):
        from planloop.execute import NodeOutcome

        outcome = NodeOutcome(node_id="T1")
        outcome.steps = steps
        outcome.l1_failures = l1
        return outcome

    def test_all_attempts_includes_both_lists(self):
        result = RunResult(status="blocked")
        result.outcomes.append(self._make_outcome(3, 1))
        result.interrupted.append(self._make_outcome(5, 2))
        attempts = all_attempts(result)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum(o.steps for o in attempts), 8)
        self.assertEqual(sum(o.l1_failures for o in attempts), 3)

    def test_the_measurement_counts_the_interrupted_attempt(self):
        result = RunResult(status="blocked")
        result.outcomes.append(self._make_outcome(3, 1))
        result.interrupted.append(self._make_outcome(5, 2))
        result.usage = {"calls": 12, "tokens": 1000}
        with tempfile.TemporaryDirectory() as tmp:
            measured = _measure(result, Spy(object()), Path(tmp),
                                guard_injected=False, tampered=False)
        self.assertEqual(measured.steps, 8, "the interrupted attempt's steps are missing")
        self.assertEqual(measured.l1_failures, 3,
                         "the interrupted attempt's failures are missing")


if __name__ == "__main__":
    unittest.main()

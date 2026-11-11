"""The attribution pass must not lie about where the numbers come from.

What is guarded here is the *reading* of finished runs, not the runs:

* every log encoding a real campaign can produce is read (UTF-16LE with BOM,
  UTF-8 with BOM, plain UTF-8) -- reading one of them as UTF-8 silently drops
  a whole log, and a missing log biases the attribution;
* PowerShell's duplicated echo of a cell is counted once, not twice;
* the ``_hidden`` suffix is what separates "the model was never told" from
  "the model cannot do it";
* one-sided Fisher is the same statistic the reports quote;
* the event-stream reader counts failure texts and zero-token runs correctly,
  and does not count a setup stream as a run.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from planloop.diagnose import (
    Cell,
    build_report,
    collect_cells,
    fisher_greater,
    parse_cells,
    read_events,
)

CELL = "[{cfg}] {task}: success={ok} tokens={tok}{extra} {sec}s"


def _write(path: Path, text: str, encoding: str) -> None:
    path.write_bytes(text.encode(encoding))


class ParsingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_every_encoding_powershell_produces(self):
        line = CELL.format(cfg="B2", task="divide_zero_hidden", ok=True, tok=8000, extra="", sec=40)
        _write(self.root / "utf16.log", line + "\n", "utf-16")  # writes a BOM
        _write(self.root / "utf8bom.log", line + "\n", "utf-8-sig")
        _write(self.root / "plain.log", line + "\n", "utf-8")
        for name in ("utf16.log", "utf8bom.log", "plain.log"):
            cells = parse_cells(self.root / name)
            self.assertEqual(len(cells), 1, name)
            self.assertEqual(cells[0].task, "divide_zero_hidden")

    def test_powershell_echo_is_counted_once(self):
        line = CELL.format(cfg="B0", task="mean_empty_hidden", ok=False, tok=14000, extra="", sec=90)
        text = f"python : {line}\n{line}\n"
        _write(self.root / "echo.log", text, "utf-8")
        self.assertEqual(len(parse_cells(self.root / "echo.log")), 1)

    def test_attempts_and_blocked_reason_are_parsed(self):
        line = CELL.format(
            cfg="B2D", task="divide_zero_hidden", ok=False, tok=15000,
            extra=" attempts=2 status=blocked blocked=execution failure persisted over 2 attempts",
            sec=70,
        )
        _write(self.root / "cell.log", line + "\n", "utf-8")
        cell = parse_cells(self.root / "cell.log")[0]
        self.assertEqual(cell.attempts, 2)
        self.assertIn("persisted", cell.blocked)
        self.assertEqual(cell.seconds, 70)

    def test_old_lines_without_suffix_still_parse(self):
        line = CELL.format(cfg="B1", task="fix_discount", ok=True, tok=4000, extra="", sec=30)
        _write(self.root / "old.log", line + "\n", "utf-8")
        cell = parse_cells(self.root / "old.log")[0]
        self.assertIsNone(cell.attempts)
        self.assertTrue(cell.success)

    def test_since_cutoff_skips_older_logs(self):
        line = CELL.format(cfg="B0", task="fix_discount", ok=True, tok=4000, extra="", sec=30)
        old = self.root / "old.log"
        new = self.root / "new.log"
        _write(old, line + "\n", "utf-8")
        _write(new, line + "\n", "utf-8")
        os.utime(old, (1_700_000_000, 1_700_000_000))
        cells, used, skipped = collect_cells(self.root, since=datetime(2026, 11, 10, 17, 5))
        self.assertEqual([cell.log for cell in cells], ["new.log"])
        self.assertEqual(used, ["new.log"])
        self.assertEqual(skipped, ["old.log"])

    def test_hidden_suffix_marks_the_task(self):
        line = CELL.format(cfg="B0", task="divide_zero_hidden", ok=False, tok=100, extra="", sec=5)
        _write(self.root / "hidden.log", line + "\n", "utf-8")
        self.assertTrue(parse_cells(self.root / "hidden.log")[0].hidden)


class StatisticsTest(unittest.TestCase):
    def test_fisher_matches_the_hand_computed_tail(self):
        # [[3,0],[0,3]] -> only the observed table is at least as extreme: 1/20
        self.assertAlmostEqual(fisher_greater(3, 0, 0, 3), 0.05, places=12)

    def test_fisher_is_one_when_there_is_no_evidence(self):
        # [[1,2],[1,2]]: every table except X=0 is at least as extreme -> 0.8
        self.assertAlmostEqual(fisher_greater(1, 2, 1, 2), 0.8, places=12)

    def test_fisher_separates_a_clean_split(self):
        self.assertLess(fisher_greater(10, 0, 0, 10), 1e-5)


class ReportTest(unittest.TestCase):
    @staticmethod
    def _cells() -> list[Cell]:
        cells = []
        for i in range(3):
            cells.append(Cell("a.log", "B0", "visible_task", True, 1000))
            cells.append(Cell("a.log", "B2", "task_hidden", True, 1000, attempts=1))
        for i in range(2):
            cells.append(Cell("a.log", "B0", "task_hidden", False, 9999))
            cells.append(Cell("a.log", "B2D", "task_hidden", False, 15000, attempts=2))
            cells.append(Cell("a.log", "B2C", "task_hidden", False, 7000, attempts=1))
        return cells

    def test_report_separates_visible_from_hidden(self):
        text = build_report(self._cells(), ["a.log"], [])
        self.assertIn("| B0 | 3/3 | 0/2 |", text)
        self.assertIn("| B2C | 0/2 | 0/2 |", text)

    def test_report_quotes_a_p_value_for_the_ablation(self):
        text = build_report(self._cells(), ["a.log"], [])
        self.assertIn("B2 vs B2D", text)
        self.assertIn("B2 vs B2C", text)

    def test_report_says_the_retry_units_differ(self):
        text = build_report(self._cells(), ["a.log"], [])
        self.assertIn("模型调用次数", text)
        self.assertIn("执行器被调用的次数", text)


class EventReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _stream(self, name: str, records: list[dict]) -> None:
        body = "\n".join(json.dumps(record) for record in records)
        (self.root / name).write_text(body + "\n", encoding="utf-8")

    def test_counts_failure_texts_and_zero_token_runs(self):
        self._stream("events_T1_1.jsonl", [
            {"kind": "node_start", "data": {"attempt": 1}},
            {"kind": "failure", "data": {"reason": "abc: TypeError: total() takes 2 positional arguments"}},
            {"kind": "failure", "data": {"reason": "exit=1"}},
            {"kind": "node_end", "data": {"status": "failed", "tokens": 0}},
            {"kind": "run_end", "data": {"status": "blocked"}},
        ])
        self._stream("events_T1_2.jsonl", [
            {"kind": "node_start", "data": {"attempt": 1}},
            {"kind": "node_end", "data": {"status": "succeeded", "tokens": 500}},
            {"kind": "run_end", "data": {"status": "succeeded"}},
        ])
        self._stream("events_setup_1.jsonl", [
            {"kind": "artifact", "data": {"path": "shop/helpers.py"}},
        ])
        facts = read_events(self.root)
        self.assertEqual(facts.runs, 2)
        self.assertEqual(facts.failures, 2)
        self.assertEqual(facts.with_error, 1)
        self.assertEqual(facts.zero_token_runs, 1)
        self.assertEqual(facts.statuses, {"blocked": 1, "succeeded": 1})

    def test_approval_request_marks_the_run_interrupted(self):
        self._stream("events_T1_1.jsonl", [
            {"kind": "node_start", "data": {"attempt": 1}},
            {"kind": "approval_requested", "data": {"reason": "subshell"}},
            {"kind": "run_end", "data": {"status": "blocked"}},
        ])
        self.assertEqual(read_events(self.root).interrupted, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

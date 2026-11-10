import tempfile
import unittest
from pathlib import Path

from planloop.audit import build_report, write_report
from planloop.model import ScriptedModel
from planloop.persist import runs_root
from planloop.scheduler import run
from planloop.spec import ProblemSpec


def blocked_spec(rubric: bool = False) -> ProblemSpec:
    acceptance = (
        [{"id": "A1", "kind": "rubric", "spec": "the output reads well"}]
        if rubric
        else [{"id": "A1", "kind": "file_exists", "spec": "c.txt"}]
    )
    return ProblemSpec.from_dict(
        {
            "goal": "make something that ends up missing",
            "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "c.txt"}],
            "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 2},
            "plan_template": {
                "nodes": [
                    {"id": "T1", "goal": "make a", "acceptance": [
                        {"id": "B1", "kind": "file_exists", "spec": "a.txt"}]},
                    {"id": "T2", "goal": "make c", "deps": ["T1"], "acceptance": acceptance},
                ]
            },
        }
    )


def handler(prompt: str) -> str:
    if "# Current node T1" in prompt:
        return '{"action": "write", "path": "a.txt", "content": "ok"}'
    return '{"action": "write", "path": "b.txt", "content": "wrong file"}'


class AuditTest(unittest.TestCase):
    def run_once(self, ws: Path, spec: ProblemSpec, name: str = "r-audit"):
        return run(spec, model=ScriptedModel(handler), workspace=ws, run_dir=runs_root(ws) / name)

    def test_report_lists_progress_and_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            result = self.run_once(ws, blocked_spec())
            self.assertEqual(result.status, "blocked")

            run_dir = runs_root(ws) / "r-audit"
            report = build_report(run_dir)
            self.assertEqual(report.run_id, "r-audit")
            self.assertEqual([n.id for n in report.nodes], ["T1", "T2"])
            self.assertEqual(report.nodes[0].status, "done")
            self.assertEqual(report.nodes[1].status, "failed")
            self.assertTrue(report.gaps)
            self.assertIn("T2 未完成", report.gaps[0])

            text = report.to_markdown()
            self.assertIn("## 进度", text)
            self.assertIn("## 缺口", text)
            self.assertIn("| T2 | failed |", text)
            self.assertIn("## 时间线", text)

    def test_audit_is_written_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.run_once(ws, blocked_spec())
            audit_path = runs_root(ws) / "r-audit" / "AUDIT.md"
            self.assertTrue(audit_path.exists())
            self.assertIn("# Run r-audit", audit_path.read_text(encoding="utf-8"))

    def test_grade_is_the_weakest_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            # T1 is binary-verifiable (B), T2 is rubric-only (C) and never finishes
            self.run_once(ws, blocked_spec(rubric=True), name="r-grade")
            report = build_report(runs_root(ws) / "r-grade")
            grades = {n.id: n.grade for n in report.nodes}
            self.assertEqual(grades["T1"], "B")
            self.assertEqual(grades["T2"], "C")
            # only finished nodes count towards the run grade
            self.assertEqual(report.grade, "B")

    def test_write_report_to_custom_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.run_once(ws, blocked_spec(), name="r-out")
            target = write_report(runs_root(ws) / "r-out", out=Path(tmp) / "report.md")
            self.assertTrue(target.exists())
            self.assertIn("# Run r-out", target.read_text(encoding="utf-8"))

    def test_report_without_events_still_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.run_once(ws, blocked_spec(), name="r-noevents")
            run_dir = runs_root(ws) / "r-noevents"
            (run_dir / "events.jsonl").unlink()
            report = build_report(run_dir)
            self.assertEqual(report.run_id, "r-noevents")
            self.assertEqual(report.timeline, [])


if __name__ == "__main__":
    unittest.main()

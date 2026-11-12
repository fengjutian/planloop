"""The noise floor needs an error bar -- and the sample size needs a number.

2026-11-12: the fee run published a floor ("same prompt, the arms differ by
+-48% in steps") and used it to refuse a -27% effect.  But the floor came
from **three** repetitions, and the paired differences behind it were
+5, -1 and 0 steps: the floor's own error bar is wider than the effect it
was used to judge.  And "how many repetitions would it take" was a guess
("dozens per arm").

These tests guard the two numbers that make that honest: the floor with its
interval, and the repetitions a claim of a given size needs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from planloop.membench_power import (
    estimate_floor,
    estimate_floors,
    t_quantile,
)


class FloorWithErrorBarTest(unittest.TestCase):
    """The floor is a measurement, so it has to carry an interval."""

    def test_the_floor_is_the_mean_paired_difference(self):
        est = estimate_floor("steps", [3.0, 3.0, 2.0], [8.0, 2.0, 2.0])
        self.assertEqual(est.n_pairs, 3)
        self.assertAlmostEqual(est.mean_diff, (5 - 1 + 0) / 3)
        self.assertAlmostEqual(est.floor_pct, est.mean_diff / est.control_mean * 100)
        self.assertEqual(est.diffs, [5.0, -1.0, 0.0])

    def test_the_interval_widens_when_there_are_fewer_pairs(self):
        few = estimate_floor("steps", [3.0, 3.0, 2.0], [8.0, 2.0, 2.0])
        many = estimate_floor("steps", [3.0] * 10, [8.0, 2.0] + [2.0] * 8)
        self.assertGreater(few.width_pct, many.width_pct)

    def test_a_single_pair_has_no_error_bar_to_report(self):
        """One pair cannot say how sure it is -- and must not pretend to."""
        est = estimate_floor("steps", [3.0], [8.0])
        self.assertEqual(est.ci_low_pct, est.ci_high_pct)
        self.assertEqual(est.width_pct, 0.0)

    def test_the_interval_contains_the_point_estimate(self):
        est = estimate_floor("tokens", [100.0, 100.0, 100.0], [140.0, 130.0, 100.0])
        self.assertLessEqual(est.ci_low_pct, est.floor_pct)
        self.assertGreaterEqual(est.ci_high_pct, est.floor_pct)

    def test_the_t_quantile_is_sane(self):
        self.assertGreater(t_quantile(2), t_quantile(5))
        self.assertGreater(t_quantile(5), t_quantile(30))
        self.assertAlmostEqual(t_quantile(10_000), 1.96, places=2)


class RepetitionsNeededTest(unittest.TestCase):
    """How many repetitions does a claim of a given size need?"""

    def test_a_smaller_effect_needs_more_repetitions(self):
        est = estimate_floor("steps", [3.0, 3.0, 2.0], [8.0, 2.0, 2.0])
        self.assertGreater(est.reps_for(10), est.reps_for(20))
        self.assertGreater(est.reps_for(20), est.reps_for(40))

    def test_more_noise_needs_more_repetitions(self):
        quiet = estimate_floor("steps", [3.0, 3.0, 2.0], [4.0, 2.0, 2.0])
        noisy = estimate_floor("steps", [3.0, 3.0, 2.0], [8.0, 2.0, 2.0])
        self.assertGreater(noisy.reps_for(20), quiet.reps_for(20))

    def test_the_sample_size_formula(self):
        """n = ((z + z) * sd / delta)^2 -- the standard paired formula."""
        est = estimate_floor("steps", [10.0, 10.0], [12.0, 8.0])  # sd = 2.83
        delta = 0.2 * est.control_mean
        expected = ((1.959964 + 0.8416) * est.sd_diff / delta) ** 2
        self.assertAlmostEqual(est.reps_for(20), round(expected), delta=1)

    def test_a_zero_effect_needs_nothing(self):
        est = estimate_floor("steps", [3.0, 3.0, 2.0], [8.0, 2.0, 2.0])
        self.assertEqual(est.reps_for(0), 0)


class SilentRepetitionsTest(unittest.TestCase):
    """The floor comes from the repetitions where both arms ran the same prompt."""

    def _fixture(self, tmp: Path):
        """rep 1 carries no lesson (the same prompt on both sides), rep 2 does."""
        rows: list[dict] = []
        events: dict[str, list[dict]] = {}
        values = {
            "R2_none": {"steps": [4, 3], "tokens": [100, 100]},
            "R2_memory": {"steps": [6, 5], "tokens": [140, 130]},
            "R2_fact": {"steps": [2, 2], "tokens": [70, 70]},
        }
        for rep in (1, 2):
            for arm, vals in values.items():
                rows.append({
                    "rep": rep, "arm": arm, "status": "succeeded",
                    "steps_to_pass": vals["steps"][rep - 1],
                    "tokens_to_pass": vals["tokens"][rep - 1],
                    "total_steps": 12, "model_calls": 12, "tokens": 1000,
                    "l1_failures": 1, "guard_catches": 0,
                    "prompts_lesson": ("5/12" if arm == "R2_memory" and rep == 2 else "0/12"),
                    "prompts_facts": "12/12" if arm == "R2_fact" else "0/12",
                    "verdict": "绿", "suite": "绿",
                })
                events[f"{arm}_{rep}"] = []
        run_dir = tmp / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        header = ("| rep | 臂 | 状态 | steps2pass | tok2pass | 总步数 | 模型调用 | 总 tokens | "
                  "L1 失败 | 护栏拦截 | 无效回复 | 带教训 | 带事实 | 外部判定 | 全量测试 |")
        lines = [header, "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for row in rows:
            cells = [str(row[c]) for c in (
                "rep", "arm", "status", "steps_to_pass", "tokens_to_pass", "total_steps",
                "model_calls", "tokens", "l1_failures", "guard_catches")]
            cells.append(str(row.get("invalid", 0)))
            cells += [str(row[c]) for c in (
                "prompts_lesson", "prompts_facts", "verdict", "suite")]
            lines.append("| " + " | ".join(cells) + " |")
        report = tmp / "report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for name, records in events.items():
            (run_dir / f"events_{name}.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                encoding="utf-8",
            )
        return run_dir, report

    def test_only_the_silent_repetition_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp)
            floors = estimate_floors(run_dir, report)
        steps = floors["steps_to_pass"]
        self.assertEqual(steps.n_pairs, 1, "rep 2 carried a lesson -- it is not a floor")
        self.assertAlmostEqual(steps.floor_pct, (6 - 4) / 4 * 100)

    def test_a_run_without_a_silent_repetition_has_no_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run_dir, report = self._fixture(tmp)
            # make the lesson arm carry a lesson in every repetition
            text = report.read_text(encoding="utf-8")
            report.write_text(text.replace("| 0/12 | 0/12 |", "| 5/12 | 0/12 |"), encoding="utf-8")
            floors = estimate_floors(run_dir, report)
        self.assertEqual(floors, {})


if __name__ == "__main__":
    unittest.main()

"""How solid is the noise floor -- and how many repetitions does a claim need?

The fee run published a floor ("same prompt, the two arms differ by +-48% in
steps") and then refused to report a -27% effect because it was inside that
floor.  Two questions follow, and neither was answered anywhere:

1. **How solid is the floor itself?**  It came from three repetitions, and
   the paired differences behind it were +5, -1 and 0 steps -- so the floor's
   own error bar is wider than the effect it was used to judge.  A floor
   without an error bar is a number, not a measurement.
2. **How many repetitions would it take?**  "Dozens per arm" was a guess.
   The answer is computable: the spread of the paired differences is the
   noise of the *difference*, and the sample size follows from it.

So: this module recomputes the floor with an error bar, and says how many
repetitions each metric needs to resolve an effect of a given size.

The t-quantile table is here because the project is pure standard library.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path

from .membench import FEE_FAMILY
from .membench_verify import parse_report, recompute

#: two-sided 95% t quantiles (df = n - 1)
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131,
        20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000,
        120: 1.980}

#: one-sided quantiles for the common powers
_Z = {0.80: 0.8416, 0.90: 1.2816, 0.95: 1.6449}


def t_quantile(df: int) -> float:
    """The two-sided 95% t quantile for ``df`` degrees of freedom."""
    if df <= 0:
        return float("nan")
    if df in _T95:
        return _T95[df]
    keys = sorted(_T95)
    if df > keys[-1]:
        return 1.960
    for lo, hi in zip(keys, keys[1:]):
        if lo < df <= hi:
            span = hi - lo
            return _T95[lo] + (_T95[hi] - _T95[lo]) * (df - lo) / span
    return 1.960


@dataclass
class FloorEstimate:
    """One metric's floor, with the error bar that was missing."""

    metric: str
    n_pairs: int
    control_mean: float
    mean_diff: float
    sd_diff: float
    floor_pct: float
    ci_low_pct: float
    ci_high_pct: float
    #: the paired differences themselves -- the reader should see the outliers
    diffs: list[float] = field(default_factory=list)

    @property
    def width_pct(self) -> float:
        """How wide the floor's own error bar is, in percent."""
        return self.ci_high_pct - self.ci_low_pct

    def reps_for(self, effect_pct: float, *, power: float = 0.80) -> int:
        """Repetitions needed to resolve an effect of ``effect_pct``.

        The estimator is the mean of the paired differences, so the sample
        size follows from their spread: ``n = ((z + z) * sd / delta)^2``.
        """
        if effect_pct <= 0:
            return 0
        delta = abs(effect_pct) / 100.0 * abs(self.control_mean)
        if delta <= 0:
            return 0
        z = 1.959964 + _Z.get(power, 0.8416)
        return int(math.ceil((z * self.sd_diff / delta) ** 2))


def _sd(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def estimate_floor(metric: str, control: list[float], memory: list[float]) -> FloorEstimate:
    """The floor of one metric, from the pairs where both arms ran the same prompt."""
    diffs = [m - c for c, m in zip(control, memory)]
    control_mean = sum(control) / len(control) if control else 0.0
    mean_diff = sum(diffs) / len(diffs) if diffs else 0.0
    sd = _sd(diffs)
    floor_pct = (mean_diff / control_mean * 100) if control_mean else 0.0
    if len(diffs) < 2 or sd == 0 or control_mean == 0:
        return FloorEstimate(metric, len(diffs), control_mean, mean_diff, sd,
                             floor_pct, floor_pct, floor_pct, list(diffs))
    half = t_quantile(len(diffs) - 1) * sd / math.sqrt(len(diffs))
    return FloorEstimate(
        metric, len(diffs), control_mean, mean_diff, sd, floor_pct,
        (mean_diff - half) / control_mean * 100,
        (mean_diff + half) / control_mean * 100,
        list(diffs),
    )


def _metric_value(row, metric: str, hidden) -> float:
    if metric == "steps_to_pass":
        return float(row.steps_to_pass)
    if metric == "tokens_to_pass":
        return float(row.tokens_to_pass)
    if metric == "total_tokens":
        return float(row.tokens)
    if metric == "hidden_looks":
        return float(hidden)
    return 0.0


def estimate_floors(run_dir: Path, report_path: Path, family=None) -> dict[str, FloorEstimate]:
    """Every metric's floor, from the repetitions where both arms ran the same prompt."""
    from .membench import analyze_events

    family = family or FEE_FAMILY
    result = recompute(run_dir, report_path, family)
    reps = list((result.noise_floor or {}).get("reps") or [])
    rows = parse_report(report_path)
    by = {(row.arm, row.rep): row for row in rows}

    out: dict[str, FloorEstimate] = {}
    for metric in ("steps_to_pass", "tokens_to_pass", "total_tokens", "hidden_looks"):
        control: list[float] = []
        memory: list[float] = []
        for rep in reps:
            c_row = by.get(("R2_none", rep))
            m_row = by.get(("R2_memory", rep))
            if c_row is None or m_row is None:
                continue
            control.append(_metric_value(c_row, metric, 0))
            memory.append(_metric_value(m_row, metric, 0))
            if metric == "hidden_looks":
                control[-1] = float(analyze_events(
                    run_dir / f"events_R2_none_{rep}.jsonl", family.hidden_markers).hidden_looks)
                memory[-1] = float(analyze_events(
                    run_dir / f"events_R2_memory_{rep}.jsonl", family.hidden_markers).hidden_looks)
        if control:
            out[metric] = estimate_floor(metric, control, memory)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="planloop.membench_power",
        description="the noise floor with its error bar, and the repetitions a claim needs")
    ap.add_argument("--dir", required=True, help="the run directory")
    ap.add_argument("--report", required=True, help="the machine report")
    ap.add_argument("--effect", type=float, default=20.0,
                    help="the effect to resolve, in percent (default: 20)")
    ap.add_argument("--power", type=float, default=0.80, help="the power to reach")
    args = ap.parse_args(argv)

    floors = estimate_floors(Path(args.dir), Path(args.report))
    if not floors:
        print("this run has no repetition where both arms ran the same prompt -- "
              "it cannot say anything about its own noise")
        return 1

    print(f"effect to resolve: {args.effect:.0f}%   power: {args.power:.0%}\n")
    print(f"{'metric':<16}{'pairs':>6}{'floor':>9}{'95% CI':>18}{'sd(d)':>10}"
          f"{'n needed':>10}")
    for metric, est in floors.items():
        ci = f"[{est.ci_low_pct:+.0f}%, {est.ci_high_pct:+.0f}%]"
        n = est.reps_for(args.effect, power=args.power)
        print(f"{metric:<16}{est.n_pairs:>6}{est.floor_pct:>+8.0f}%{ci:>18}"
              f"{est.sd_diff:>10.2f}{n:>10}")
        print(f"{'':<16}the paired differences: "
              + ", ".join(f"{d:+g}" for d in est.diffs))
    print("\n(the floor is the difference between two arms that ran the *same* prompt; "
          "the CI is\n what that number is worth at this sample size -- compare it with "
          "the effect it\n is supposed to judge.)")
    print("\nCAVEAT: the spread behind these numbers comes from "
          f"{min(e.n_pairs for e in floors.values())} pairs, so it is itself unstable "
          "(one outlier\n doubles it) and the 'n needed' column is an order-of-magnitude "
          "guide, not a budget.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

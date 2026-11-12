"""Rebuild the published per-rep table from the raw event logs -- and check it.

Why this module exists
----------------------
The machine report of a run is written by the harness, from what the executor
had in memory.  The event log is what actually happened.  When the two
disagree, the log wins -- and the published document must quote the log.

2026-11-12 exposed the case: an attempt that the risk policy cut short was
dropped by the executor, so the report counted fewer steps and fewer L1
failures than the log recorded (12 steps / 1 failure vs 20 steps / 2 failures
in one repetition of ``.membench-fee16``).  The document quoted the report.
⇒ Now the table is **rebuilt from the log** by this module, and
:func:`check_published` compares the document against the rebuild, cell for
cell -- so a hand-edited number cannot survive.

Usage::

    python -m planloop.membench_publish rebuild --dir .membench-fee16 \\
        --report .demo/membench-fee16.md
    python -m planloop.membench_publish check --doc docs/memory-value-fee.md \\
        --dir .membench-fee16 --report .demo/membench-fee16.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .membench import FEE_FAMILY
from .membench_verify import count_events, log_steps, log_stem, parse_report, recompute

#: a row of the published per-rep table
_ROW = re.compile(r"^\|\s*\d+\s*\|\s*R[12]_")

#: the columns of the published per-rep table, in order
COLUMNS = ("rep", "arm", "status", "steps_to_pass", "tokens_to_pass", "total_steps",
           "tokens", "l1_failures", "lesson", "facts", "verdict", "suite")


def rebuild(run_dir: Path, report_path: Path) -> tuple[list[str], list[str]]:
    """The per-rep table, rebuilt from the event logs.

    Returns ``(table, changed)``: the markdown rows, and the cells the
    machine report got wrong (as ``"arm rep N: column old -> new"``).
    """
    rows = parse_report(report_path)
    table: list[str] = []
    changed: list[str] = []
    for row in rows:
        log = run_dir / f"events_{log_stem(row.arm)}_{row.rep}.jsonl"
        counts = count_events(log)
        steps = log_steps(counts)
        failures = counts["failure"]
        if row.total_steps != steps:
            changed.append(f"{row.arm} rep {row.rep}: total_steps {row.total_steps} -> {steps}")
        if row.l1_failures != failures:
            changed.append(f"{row.arm} rep {row.rep}: l1_failures {row.l1_failures} -> {failures}")
        cells = (row.rep, row.arm, row.status, row.steps_to_pass, row.tokens_to_pass,
                 steps, row.tokens, failures,
                 f"{row.prompts_with_lesson}/{row.prompts_total_lesson}",
                 f"{row.prompts_with_facts}/{row.prompts_total_facts}",
                 row.verdict, row.suite)
        table.append("| " + " | ".join(str(c) for c in cells) + " |")
    return table, changed


def check_published(doc_path: Path, run_dir: Path, report_path: Path) -> list[str]:
    """The rows of the document that do not match the rebuild."""
    table, _ = rebuild(run_dir, report_path)
    doc_rows = [line.strip() for line in doc_path.read_text(encoding="utf-8").splitlines()
                if _ROW.match(line.strip())]
    problems: list[str] = []
    if len(doc_rows) != len(table):
        problems.append(f"the document has {len(doc_rows)} rows, the rebuild has {len(table)}")
        return problems
    for published, rebuilt in zip(doc_rows, table):
        if published != rebuilt:
            problems.append(f"published: {published}\n     rebuilt: {rebuilt}")
    return problems


def _arm_means(run_dir: Path, report_path: Path) -> list[str]:
    """The per-arm mean L1 failures over the paired reps, from the logs."""
    result = recompute(run_dir, report_path, FEE_FAMILY)
    rows = parse_report(report_path)
    paired = set(result.paired_reps)
    out: list[str] = []
    for arm in ("R2_none", "R2_memory", "R2_fact"):
        vals = []
        for row in rows:
            if row.arm != arm or row.rep not in paired:
                continue
            log = run_dir / f"events_{log_stem(row.arm)}_{row.rep}.jsonl"
            vals.append(count_events(log)["failure"])
        if vals:
            out.append(f"  {arm}: n={len(vals)} sum={sum(vals)} mean={round(sum(vals) / len(vals), 1)}")
    out.append(f"  (paired reps: {sorted(paired)})")
    return out


def _behaviour_rows(doc_path: Path) -> list[tuple[str, list[int]]]:
    """The rows of the published behaviour table: ``(label, [one number per arm])``."""
    rows: list[tuple[str, list[int]]] = []
    in_table = False
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("|") and "查表" in line and "第一步" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if set(cells[0]) <= {"-", ":"}:
            continue
        numbers = []
        for cell in cells[1:]:
            match = re.search(r"(\d+)", cell.replace("**", ""))
            if not match:
                numbers = []
                break
            numbers.append(int(match.group(1)))
        if numbers:
            rows.append((cells[0], numbers))
    return rows


def check_behaviour(doc_path: Path, run_dir: Path, arms: list[str] | None = None,
                    reps: int = 8) -> list[str]:
    """The published behaviour table must match the event logs.

    The headline effect of a run ("the arm with the fact looks the price
    table half as often") is read back from the event logs -- but the
    document quotes the *per-repetition* counts, and nothing checked them.
    """
    arms = arms or ["R2_none", "R2_memory", "R2_fact"]
    rows = _behaviour_rows(doc_path)
    if not rows:
        return ["no behaviour table found in the document"]
    problems: list[str] = []
    for label, numbers in rows:
        if label == "合计":
            want = [sum(_hidden_looks(run_dir, arm, rep) for rep in range(1, reps + 1))
                    for arm in arms]
            label_txt = "合计"
        elif label.isdigit():
            rep = int(label)
            want = [_hidden_looks(run_dir, arm, rep) for arm in arms]
            label_txt = f"rep {rep}"
        else:
            continue
        if numbers != want:
            problems.append(f"{label_txt}: published {numbers}, the log says {want}")
    return problems


def _hidden_looks(run_dir: Path, arm: str, rep: int) -> int:
    from .membench import FEE_FAMILY, analyze_events

    log = run_dir / f"events_{log_stem(arm)}_{rep}.jsonl"
    return analyze_events(log, FEE_FAMILY.hidden_markers).hidden_looks


def _pct(delta: float, base: float) -> float | None:
    if not base:
        return None
    return round(delta / base * 100)


def _metric_of(label: str) -> str:
    """Which metric a row of the effect table is about."""
    label = label.replace("**", "").strip()
    if "查" in label:
        return "hidden_looks"
    if "步数" in label:
        return "total_steps" if "总" in label else "steps_to_pass"
    if "tokens" in label.lower():
        return "total_tokens" if "总" in label else "tokens_to_pass"
    return ""


def _effect_rows(doc_path: Path) -> list[list[str]]:
    """The rows of the published effect table (the one that names the floor)."""
    rows: list[list[str]] = []
    in_table = False
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("|") and "噪声地板" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if set(cells[0]) <= {"-", ":"}:
            continue
        if cells[0].replace("**", "").strip() in ("口径", "口径 / 方向"):
            continue
        if len(cells) >= 3:
            rows.append(cells)
    return rows


def check_floor(doc_path: Path, run_dir: Path, report_path: Path,
                family=None) -> list[str]:
    """The published effect table must agree with the recomputed noise floor.

    2026-11-11: a run claimed "-45% tokens" and nobody had measured how far
    two arms running **the same prompt** already differ -- the effect was
    inside the noise, and the whole number had to be withdrawn.  The gate
    computes the floor; this checks that the **document** uses it the same
    way: an effect smaller than the floor must not be published as a win, and
    a published effect must be the number the gate recomputed.
    """
    from .membench import FEE_FAMILY

    family = family or FEE_FAMILY
    result = recompute(run_dir, report_path, family)
    problems: list[str] = []

    floor = result.noise_floor or {}
    if not floor.get("reps"):
        return ["this run has no noise floor -- the effect table cannot be checked "
                "(and no effect from it may be published as a win)"]
    floors = {
        "steps_to_pass": _floor_pct(floor.get("steps_delta_pct")),
        "tokens_to_pass": _floor_pct(floor.get("tokens_delta_pct")),
        "hidden_looks": _floor_pct(floor.get("hidden_delta_pct")),
    }
    control = result.summaries.get("R2_none")
    facts = result.summaries.get("R2_fact")
    if not control or not facts:
        return ["the run has no control/fact arms -- nothing to compare"]
    effects = {
        "steps_to_pass": _pct(facts.steps_to_pass - control.steps_to_pass,
                              control.steps_to_pass),
        "tokens_to_pass": _pct(facts.tokens_to_pass - control.tokens_to_pass,
                               control.tokens_to_pass),
        "total_tokens": _pct(facts.total_tokens - control.total_tokens,
                             control.total_tokens),
        "hidden_looks": _pct(facts.hidden_looks - control.hidden_looks,
                             control.hidden_looks),
    }

    text = doc_path.read_text(encoding="utf-8")
    for metric, value in floors.items():
        if value is not None and f"{value}%" not in text:
            problems.append(f"the document never states the noise floor for {metric} "
                            f"({value}%)")

    rows = _effect_rows(doc_path)
    if not rows:
        return problems + ["no effect table found (a table that names the noise floor)"]
    for cells in rows:
        label = cells[0]
        metric = _metric_of(label)
        if not metric:
            problems.append(f"cannot tell which metric the row {label!r} is about")
            continue
        published = _published_pct(cells[1])
        computed = effects.get(metric)
        if published is None:
            problems.append(f"{label}: no percentage in the effect cell {cells[1]!r}")
            continue
        if computed is None:
            problems.append(f"{label}: the gate cannot compute this effect")
            continue
        if published != computed:
            problems.append(f"{label}: the document says {published}%, "
                            f"the gate recomputes {computed}%")
        verdict = cells[2]
        above = "在噪声之上" in verdict
        mentions_floor = "地板" in verdict
        limit = floors.get(metric)
        if limit is None:
            if above:
                problems.append(f"{label}: the document calls it 'above the noise' but "
                                f"this metric has no floor -- that claim cannot be made")
            continue
        if abs(computed) >= limit and not above:
            problems.append(f"{label}: {computed}% is above the noise floor ({limit}%), "
                            f"but the document does not say so")
        if abs(computed) < limit and above:
            problems.append(f"{label}: {computed}% is **inside** the noise floor "
                            f"({limit}%), but the document publishes it as a win")
        if abs(computed) < limit and not mentions_floor:
            problems.append(f"{label}: {computed}% is inside the noise floor ({limit}%), "
                            f"but the document never says so")
    return problems


def _floor_pct(value) -> int | None:
    """``"48%"`` -> ``48``."""
    if value is None:
        return None
    text = str(value).strip().rstrip("%")
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def _published_pct(cell: str) -> int | None:
    """The percentage the document published, e.g. ``"-50%"`` -> ``-50``."""
    match = re.search(r"(-?\d+)\s*%", cell.replace("**", ""))
    if not match:
        return None
    return int(match.group(1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="planloop.membench_publish",
                                 description="rebuild the published table from the event logs")
    ap.add_argument("command", choices=("rebuild", "check", "check-behaviour", "check-floor"))
    ap.add_argument("--dir", required=True, help="the run directory")
    ap.add_argument("--report", required=True, help="the machine report")
    ap.add_argument("--doc", default="", help="the published document")
    args = ap.parse_args(argv)

    run_dir = Path(args.dir)
    report_path = Path(args.report)

    if args.command == "rebuild":
        table, changed = rebuild(run_dir, report_path)
        print("== cells the machine report got wrong (report -> log) ==")
        print("\n".join(changed) if changed else "  (none)")
        print("\n== the per-rep table, rebuilt from the logs ==")
        print("| rep | 臂 | 状态 | 到验收通过：步数 | 到验收通过：tokens | 总步数 | 总 tokens | "
              "L1 失败 | 带教训 | 带事实 | 外部判定 | 全量测试 |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|")
        print("\n".join(table))
        print("\n== the per-arm mean L1 failures, over the paired reps ==")
        print("\n".join(_arm_means(run_dir, report_path)))
        return 0

    if not args.doc:
        print(f"{args.command} needs --doc", file=sys.stderr)
        return 2
    if args.command == "check":
        table, _ = rebuild(run_dir, report_path)
        problems = check_published(Path(args.doc), run_dir, report_path)
        if problems:
            print(f"FAIL: {len(problems)} problem(s)")
            for line in problems:
                print(f"  {line}")
            return 1
        print(f"OK: all {len(table)} published rows match the rebuild from the event logs")
        return 0

    if args.command == "check-behaviour":
        problems = check_behaviour(Path(args.doc), run_dir)
        if problems:
            print(f"FAIL: {len(problems)} problem(s) in the published behaviour table")
            for line in problems:
                print(f"  {line}")
            return 1
        print("OK: the published behaviour table matches the event logs")
        return 0

    problems = check_floor(Path(args.doc), run_dir, report_path)
    if problems:
        print(f"FAIL: {len(problems)} problem(s) with the published effect table")
        for line in problems:
            print(f"  {line}")
        return 1
    print("OK: the published effects agree with the recomputed noise floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

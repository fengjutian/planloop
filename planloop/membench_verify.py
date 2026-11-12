"""Recompute a membench run from its raw artifacts -- and cross-check it.

Why this module exists
----------------------
The published numbers of a membench run used to be **typed into the report by
hand**.  On 2026-11-11 that produced four wrong numbers in one document at
once:

* "rep 6/7/8 died" -- the failures are **not** aligned across arms: the
  control arm's rep 6 ran fine and was judged green, so hand-dropping
  "rep 6/7/8" threw away a valid control run;
* the behaviour total mixed denominators (control over 6 reps vs facts over
  5 reps), so the percentage meant nothing;
* the lesson-injection count was off by a factor of two.

All four were invisible: the machine report and the hand-written document
were two independent copies of the same numbers, and they drifted apart.
**A number that is typed twice is a number that will be wrong.**

So: the run's own event logs are the source of truth, this module re-derives
every published aggregate from them, and :func:`crosscheck` compares that
against the machine report and fails loudly on any disagreement.

What it can and cannot recompute
---------------------------------
``steps_to_pass`` / ``tokens_to_pass`` / the verdicts live in the run's
report (the executor is the only thing that knows them), so this module
reads the machine report for those and re-derives **everything else** from
the event logs -- behaviour, failure counts, and which runs are usable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .heldout import find_canary
from .membench import EventStats, Family, analyze_events

#: the per-rep row of a machine report, e.g.
#: ``| 1 | R2_none | succeeded | 4 | 4908 | 12 | 12 | 13669 | 1 | 0 | 0/12 | 0/12 | 绿 | 绿 |``
_REPORT_ROW = re.compile(r"^\d+$")

#: the arms of a run: the arm name -> the label its event logs carry
ARM_LABELS = {
    "no_memory": "R2_none",
    "lesson_and_guard": "R2_memory",
    "fact_only": "R2_fact",
}

#: the report's arm label -> the stem of its event log.  The setup phase is
#: labelled ``R1_learn`` in the report but its log is called ``events_setup_N``
#: -- and the setup phase is exactly where the model is most likely to go
#: looking for the held-out criterion (in the contaminated run the model read
#: the criterion source in ``setup_1`` and ``setup_4``).  A wrong stem here
#: means the canary scan never looks at that log: the self-check would report
#: "clean" for the very phase the leak happened in.
LOG_STEMS = {
    "R1_learn": "setup",
    "R2_none": "R2_none",
    "R2_memory": "R2_memory",
    "R2_fact": "R2_fact",
}

#: the setup arm: it learns, it is not measured.
SETUP_LABEL = "R1_learn"


def log_stem(arm: str) -> str:
    """The event log's file stem for a report arm label."""
    return LOG_STEMS.get(arm, arm)


@dataclass
class RunRow:
    """One row of the machine report, parsed."""

    rep: int
    arm: str
    status: str
    steps_to_pass: int
    tokens_to_pass: int
    total_steps: int
    model_calls: int
    tokens: int
    l1_failures: int
    guard_catches: int
    prompts_with_lesson: int
    prompts_total_lesson: int
    prompts_with_facts: int
    prompts_total_facts: int
    verdict: str
    suite: str
    #: the machine report marked this run as contaminated
    contaminated: bool = False
    #: how many replies the harness could not parse (wasted steps).  ``-1``
    #: means the report does not carry the column (older reports).
    invalid: int = -1

    @property
    def model_failed(self) -> bool:
        """The model never answered: the harness broke, the task was not attempted.

        Such a run must **not** be counted as the model getting it wrong -- and
        it must not be counted as a valid sample either.
        """
        return self.tokens == 0 and self.model_calls <= 2


@dataclass
class ArmSummary:
    """The aggregate of one arm over a set of reps."""

    arm: str
    n: int = 0
    green: int = 0
    suite_green: int = 0
    steps_to_pass: float = 0.0
    tokens_to_pass: int = 0
    total_tokens: int = 0
    l1_failures: float = 0.0
    hidden_looks: int = 0
    prompts: int = 0
    prompts_with_lesson: int = 0
    prompts_with_facts: int = 0
    reps: list[int] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "n": self.n,
            "green": f"{self.green}/{self.n}",
            "suite": f"{self.suite_green}/{self.n}",
            "steps_to_pass": self.steps_to_pass,
            "tokens_to_pass": self.tokens_to_pass,
            "total_tokens": self.total_tokens,
            "l1_failures": self.l1_failures,
            "hidden_looks": self.hidden_looks,
            "prompts": self.prompts,
            "prompts_with_lesson": self.prompts_with_lesson,
            "prompts_with_facts": self.prompts_with_facts,
            "reps": list(self.reps),
        }


def _split_ratio(cell: str) -> tuple[int, int]:
    """``"12/13"`` -> ``(12, 13)``."""
    text = cell.strip()
    if "/" not in text:
        return 0, 0
    left, right = text.split("/", 1)
    try:
        return int(left.strip()), int(right.strip())
    except ValueError:
        return 0, 0


def parse_report(report_path: Path) -> list[RunRow]:
    """Parse the per-rep table out of a machine report.

    The table grew a column (``无效回复`` -- the replies the harness could not
    parse) so that the wasted steps are published per repetition and can be
    cross-checked against the event log.  Both widths are accepted: a report
    from an older run has 14 cells, a current one has 15.

    Only the **per-rep table** is read -- it is the one whose header starts
    with ``rep``.  The report has another table (the per-arm summary) of the
    same width, so without that anchor a summary row whose first cell happens
    to be a number would be read as a repetition -- and the gate would then
    compare the wrong numbers and go red for the wrong reason.  A report with
    no per-rep table yields no rows, and :func:`recompute` says so loudly.
    """
    rows: list[RunRow] = []
    in_table = False
    for line in report_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if in_table:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "rep":
            in_table = True
            continue
        if not in_table:
            continue
        if len(cells) not in (14, 15) or not _REPORT_ROW.match(cells[0]):
            continue
        if len(cells) == 15:
            invalid_cell = cells[10]
            lesson_with, lesson_total = _split_ratio(cells[11])
            facts_with, facts_total = _split_ratio(cells[12])
            verdict_cell, suite_cell = cells[13], cells[14]
        else:
            invalid_cell = None
            lesson_with, lesson_total = _split_ratio(cells[10])
            facts_with, facts_total = _split_ratio(cells[11])
            verdict_cell, suite_cell = cells[12], cells[13]
        try:
            rows.append(RunRow(
                rep=int(cells[0]),
                arm=cells[1],
                status=cells[2],
                steps_to_pass=int(cells[3]),
                tokens_to_pass=int(cells[4]),
                total_steps=int(cells[5]),
                model_calls=int(cells[6]),
                tokens=int(cells[7]),
                l1_failures=int(cells[8]),
                guard_catches=int(cells[9]),
                invalid=int(invalid_cell) if invalid_cell is not None else -1,
                prompts_with_lesson=lesson_with,
                prompts_total_lesson=lesson_total,
                prompts_with_facts=facts_with,
                prompts_total_facts=facts_total,
                verdict=verdict_cell,
                suite=suite_cell,
                contaminated="污染" in cells[2],
            ))
        except ValueError:
            continue
    return rows


def count_events(log_path: Path) -> dict[str, int]:
    """Count the event kinds the report's columns are derived from."""
    counts = {"action": 0, "failure": 0, "invalid_action": 0, "node_end": 0, "run_end": 0,
              "node_start": 0, "interrupted": 0}
    if not log_path.exists():
        return counts
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        kind = rec.get("kind")
        if kind in counts:
            counts[kind] += 1
        if kind == "node_end" and str((rec.get("data") or {}).get("status")) == "interrupted":
            # an attempt that a policy check cut short.  It spent steps and it
            # recorded failures, and it never returned an outcome -- so a
            # report that dropped it counts fewer steps and fewer failures
            # than the log holds.
            counts["interrupted"] += 1
    return counts


def log_steps(counts: dict[str, int]) -> int:
    """The number of steps the event log accounts for.

    Every step of the act loop is one model call, and it ends in exactly one
    of three ways: an ``action``, an ``invalid_action`` (a reply the harness
    could not parse), or a policy check that cut the attempt short.  So the
    step count is re-derivable from the log -- which is how a **dropped
    attempt** shows up: the report counts fewer steps than the log holds.
    """
    cut_short = counts["interrupted"] + max(0, counts["node_start"] - counts["node_end"])
    return counts["action"] + counts["invalid_action"] + cut_short


@dataclass
class Recomputed:
    """Everything this module re-derives from a run's raw artifacts."""

    #: per arm, the usable reps (the model answered)
    usable: dict[str, list[int]] = field(default_factory=dict)
    #: per arm, the reps that died because the model never answered
    broken: dict[str, list[int]] = field(default_factory=dict)
    #: the reps every arm has -- the only ones a cross-arm comparison may use
    paired_reps: list[int] = field(default_factory=list)
    #: the arms the report actually contains
    measured_arms: list[str] = field(default_factory=list)
    #: per arm aggregate over the paired reps
    summaries: dict[str, ArmSummary] = field(default_factory=dict)
    #: the behaviour read back from the event logs
    stats: dict[tuple[str, int], EventStats] = field(default_factory=dict)
    #: the event counts per (arm, rep), or ``None`` when the log is missing
    log_counts: dict[tuple[str, int], dict[str, int] | None] = field(default_factory=dict)
    #: disagreements between the machine report and the event logs
    mismatches: list[str] = field(default_factory=list)
    #: things that are worth saying but are not a disagreement (e.g. a column
    #: whose definition changed between report versions)
    notes: list[str] = field(default_factory=list)
    #: the reps in which the model read the held-out criterion -> rep: logs
    contaminated: dict[int, list[str]] = field(default_factory=dict)
    #: the paired reps in which the lesson was actually injected (the only
    #: reps where "with a lesson" is a different arm from "without one")
    lesson_paired_reps: list[int] = field(default_factory=list)
    #: the noise floor: in the paired reps where the lesson arm had **no**
    #: lesson to carry, both arms ran the *same* prompt (no memory block at
    #: all), so the difference between them is nothing but the model's own
    #: variance.  Any claimed effect smaller than this is noise.
    noise_floor: dict[str, Any] = field(default_factory=dict)


def find_contaminated(run_dir: Path, rows: list[RunRow]) -> dict[int, list[str]]:
    """The repetitions in which the model read the held-out criterion.

    The criterion carries a canary token, and the criterion only exists on
    disk while a check runs -- so a token in an event log means the model
    read the answer key.  Such a repetition measures nothing about memory and
    must not be published.
    """
    path = run_dir / "canaries.json"
    if not path.exists():
        return {}
    try:
        canaries = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    if not isinstance(canaries, dict):
        return {}
    logs_by_rep: dict[int, list[Path]] = {}
    for row in rows:
        logs_by_rep.setdefault(row.rep, []).append(
            run_dir / f"events_{log_stem(row.arm)}_{row.rep}.jsonl"
        )
    out: dict[int, list[str]] = {}
    for rep, token in canaries.items():
        try:
            rep_no = int(rep)
        except (TypeError, ValueError):
            continue
        hits = find_canary(logs_by_rep.get(rep_no, []), str(token))
        if hits:
            out[rep_no] = hits
    return out


def recompute(run_dir: Path, report_path: Path, family: Family) -> Recomputed:
    """Re-derive a run's published aggregates from its raw artifacts."""
    rows = parse_report(report_path)
    out = Recomputed()
    if not rows:
        # a report the gate cannot read is a report it cannot check -- and a
        # gate that silently checks nothing is the "gate that always says
        # pass" this project already paid for once
        out.mismatches.append(
            "the machine report has no per-rep table (a header row whose first "
            "cell is 'rep') -- there is nothing to cross-check"
        )

    # -- contamination: did the model read the held-out criterion? ---------
    out.contaminated = find_contaminated(run_dir, rows)

    by_arm: dict[str, list[RunRow]] = {}
    for row in rows:
        by_arm.setdefault(row.arm, []).append(row)

    # -- per run: the behaviour comes from the event log -----------------
    # Every published row is checked, including the setup phase: its numbers
    # are in the report too, and the setup phase is where the model is most
    # likely to go looking for the held-out criterion.
    for label, arm_rows in by_arm.items():
        for row in arm_rows:
            log = run_dir / f"events_{log_stem(row.arm)}_{row.rep}.jsonl"
            stats = analyze_events(log, family.hidden_markers)
            out.stats[(row.arm, row.rep)] = stats
            counts = count_events(log)
            out.log_counts[(row.arm, row.rep)] = counts if log.exists() else None

            # an attempt that a policy check cut short: it started, it spent
            # steps, it recorded failures -- and it never returned an
            # outcome, so a report made before the executor recorded it
            # counts fewer steps and fewer failures than the log holds
            cut_short = counts["interrupted"] + max(0, counts["node_start"] - counts["node_end"])
            steps = log_steps(counts)
            if cut_short:
                out.notes.append(
                    f"{row.arm} rep {row.rep}: {cut_short} attempt(s) were cut short by a "
                    f"policy check -- the log accounts for {steps} step(s) and "
                    f"{counts['failure']} failure(s) for this repetition"
                )

            # the event log is the truth about what happened
            if row.l1_failures != counts["failure"]:
                if row.invalid >= 0:
                    # a current report counts every failing step, which is
                    # exactly what the log records
                    out.mismatches.append(
                        f"{row.arm} rep {row.rep}: event log has {counts['failure']} failure(s), "
                        f"report says {row.l1_failures}"
                    )
                else:
                    # an older report counted *distinct* failures per attempt;
                    # that number cannot be re-derived exactly (the attempt
                    # bookkeeping and the "finished while red" case do not
                    # line up), so it is reported as a note, not a mismatch
                    out.notes.append(
                        f"{row.arm} rep {row.rep}: report says {row.l1_failures} failure(s) "
                        f"(old, deduplicated count), the event log has {counts['failure']} "
                        f"failing check(s) -- the column is not re-derivable for old reports"
                    )
            if cut_short and row.total_steps != steps:
                # the report cannot have counted the cut-short attempt
                detail = (
                    f"{counts['action']} action(s) + {counts['invalid_action']} invalid "
                    f"+ {cut_short} cut short"
                )
                if row.invalid >= 0:
                    out.mismatches.append(
                        f"{row.arm} rep {row.rep}: event log accounts for {steps} step(s) "
                        f"({detail}), report says {row.total_steps}"
                    )
                else:
                    out.notes.append(
                        f"{row.arm} rep {row.rep}: report says {row.total_steps} step(s), the "
                        f"event log accounts for {steps} ({detail}) -- the attempt that a "
                        "policy check cut short is in the log but not in the report"
                    )
            if row.invalid >= 0 and counts["invalid_action"] != row.invalid:
                out.mismatches.append(
                    f"{row.arm} rep {row.rep}: event log has {counts['invalid_action']} "
                    f"invalid_action(s), report says {row.invalid}"
                )
            if not row.model_failed and stats.actions == 0:
                out.mismatches.append(
                    f"{row.arm} rep {row.rep}: reported as a real run but the event log has no action"
                )
            if row.model_failed and stats.actions != 0:
                out.mismatches.append(
                    f"{row.arm} rep {row.rep}: reported as a broken run but the event log has actions"
                )
            # a contaminated repetition leaves the sample: the criterion was
            # not held out, so the run measured the model's reading skill
            if row.contaminated != (row.rep in out.contaminated):
                out.mismatches.append(
                    f"{row.arm} rep {row.rep}: report marks contamination "
                    f"{row.contaminated}, the event log says {row.rep in out.contaminated}"
                )

    for arm, label in ARM_LABELS.items():
        arm_rows = by_arm.get(label, [])
        out.usable[arm] = []
        out.broken[arm] = []
        for row in arm_rows:
            if row.model_failed:
                out.broken[arm].append(row.rep)
            else:
                out.usable[arm].append(row.rep)

    # a contaminated repetition is not a usable sample either
    for arm in ARM_LABELS:
        out.usable[arm] = [r for r in out.usable[arm] if r not in out.contaminated]

    # -- the paired sample: the reps every arm has -----------------------
    # Only the arms that actually ran take part: an arm that is absent from
    # the report was not compared, and including it would empty the sample.
    measured = [arm for arm in ARM_LABELS if by_arm.get(ARM_LABELS[arm])]
    out.measured_arms = measured
    if measured:
        out.paired_reps = sorted(
            set.intersection(*(set(out.usable[arm]) for arm in measured))
        )
        # The lesson arm is only comparable in the reps where the lesson was
        # actually **injected**: a rep whose first task never failed has no
        # lesson to carry, so that arm ran without memory and belongs to the
        # control, not to the lesson comparison.
        out.lesson_paired_reps = [
            rep for rep in out.paired_reps
            if any(
                row.prompts_with_lesson > 0
                for row in by_arm.get(ARM_LABELS["lesson_and_guard"], [])
                if row.rep == rep
            )
        ]

    # -- aggregate over the paired reps -----------------------------------
    for arm, label in ARM_LABELS.items():
        summary = ArmSummary(arm=label)
        for row in by_arm.get(label, []):
            if row.rep not in out.paired_reps:
                continue
            stats = out.stats.get((label, row.rep), EventStats())
            summary.n += 1
            summary.reps.append(row.rep)
            summary.green += 1 if row.verdict.strip() == "绿" else 0
            summary.suite_green += 1 if row.suite.strip() == "绿" else 0
            summary.steps_to_pass += row.steps_to_pass
            summary.tokens_to_pass += row.tokens_to_pass
            summary.total_tokens += row.tokens
            # the failure count comes from the log, not from the report: the
            # log is where the failures happened, and a report that dropped
            # an attempt counts fewer of them than the log records
            counts = out.log_counts.get((label, row.rep))
            summary.l1_failures += counts["failure"] if counts else row.l1_failures
            summary.hidden_looks += stats.hidden_looks
            summary.prompts += row.prompts_total_lesson or row.prompts_total_facts
            summary.prompts_with_lesson += row.prompts_with_lesson
            summary.prompts_with_facts += row.prompts_with_facts
        if summary.n:
            summary.steps_to_pass = round(summary.steps_to_pass / summary.n, 1)
            summary.tokens_to_pass = round(summary.tokens_to_pass / summary.n)
            summary.total_tokens = round(summary.total_tokens / summary.n)
            summary.l1_failures = round(summary.l1_failures / summary.n, 1)
        out.summaries[label] = summary

    # -- the noise floor --------------------------------------------------
    # In the paired reps where the lesson arm had no lesson to carry, both
    # arms ran the *same* prompt: the memory block was empty, so the two
    # arms differ only by the model's own variance.  That difference is the
    # resolution limit of this run: an effect smaller than it cannot be
    # told apart from noise.
    silent_reps = [r for r in out.paired_reps if r not in out.lesson_paired_reps]
    if silent_reps:
        floor: dict[str, Any] = {"reps": silent_reps}
        #: the **unrounded** means.  The percentage below has to be computed
        #: from them: rounding first (2.667 -> 2.7) turned a 50% floor into
        #: 48% on the real run, and the floor is a headline number.
        raw: dict[str, dict[str, float]] = {}
        for arm, label in (("no_memory", "R2_none"), ("lesson_and_guard", "R2_memory")):
            rows_here = [r for r in by_arm.get(label, []) if r.rep in silent_reps]
            if not rows_here:
                continue
            steps = sum(r.steps_to_pass for r in rows_here) / len(rows_here)
            tokens = sum(r.tokens_to_pass for r in rows_here) / len(rows_here)
            looks = sum(
                out.stats.get((label, r.rep), EventStats()).hidden_looks for r in rows_here
            )
            raw[arm] = {"steps": steps, "tokens": tokens, "hidden": float(looks)}
            floor[arm] = {
                "n": len(rows_here),
                "steps_to_pass": round(steps, 1),
                "tokens_to_pass": round(tokens),
                "hidden_looks": looks,
            }
        if "no_memory" in floor and "lesson_and_guard" in floor:
            c, m = raw["no_memory"], raw["lesson_and_guard"]
            floor["steps_delta_pct"] = _pct(m["steps"] - c["steps"], c["steps"])
            floor["tokens_delta_pct"] = _pct(m["tokens"] - c["tokens"], c["tokens"])
            floor["hidden_delta_pct"] = _pct(m["hidden"] - c["hidden"], c["hidden"])
        out.noise_floor = floor

    return out


def format_report(result: Recomputed) -> str:
    """Render the recomputed numbers as the table a document should quote."""
    lines: list[str] = []
    add = lines.append
    add(f"paired reps (every arm valid): {result.paired_reps}")
    if result.lesson_paired_reps != result.paired_reps:
        add(f"paired reps with the lesson actually injected: {result.lesson_paired_reps}"
            "  <- only these are a 'with a lesson vs without one' comparison")
    if result.contaminated:
        add("CONTAMINATED reps (the model read the held-out criterion): "
            + ", ".join(
                f"rep {rep} ({len(paths)} log(s))" for rep, paths in sorted(result.contaminated.items())
            ))
        add("  -> excluded from the sample; a repetition whose criterion leaked "
            "measures the model's reading skill, not the memory")
    for arm, label in ARM_LABELS.items():
        usable = result.usable.get(arm, [])
        broken = result.broken.get(arm, [])
        add(f"  {label:<12} usable={usable} broken={broken}")
    add("")
    header = (f"{'arm':<12}{'n':>3}{'绿':>7}{'steps2pass':>11}{'tok2pass':>10}"
              f"{'tok':>8}{'L1':>6}{'hidden':>8}{'prompts':>9}{'lesson':>8}{'facts':>7}")
    add(header)
    for arm in ARM_LABELS.values():
        s = result.summaries.get(arm)
        if not s or not s.n:
            continue
        add(f"{s.arm:<12}{s.n:>3}{s.green:>4}/{s.n:<2}{s.steps_to_pass:>11}"
            f"{s.tokens_to_pass:>10}{s.total_tokens:>8}{s.l1_failures:>6}"
            f"{s.hidden_looks:>8}{s.prompts:>9}{s.prompts_with_lesson:>8}"
            f"{s.prompts_with_facts:>7}")
    add("")
    control = result.summaries.get("R2_none")
    facts = result.summaries.get("R2_fact")
    memory = result.summaries.get("R2_memory")
    if control and control.n and facts and facts.n:
        add(f"cost  control -> fact   : steps {control.steps_to_pass} -> {facts.steps_to_pass}"
            f"  ({_pct(facts.steps_to_pass - control.steps_to_pass, control.steps_to_pass)})")
        add(f"cost  control -> fact   : tokens {control.tokens_to_pass} -> {facts.tokens_to_pass}"
            f"  ({_pct(facts.tokens_to_pass - control.tokens_to_pass, control.tokens_to_pass)})")
        add(f"behav control -> fact   : hidden {control.hidden_looks} -> {facts.hidden_looks}"
            f"  ({_pct(facts.hidden_looks - control.hidden_looks, control.hidden_looks)})")
    if control and memory and control.n and memory.n:
        add(f"cost  control -> memory : steps {control.steps_to_pass} -> {memory.steps_to_pass}"
            f"  tokens {control.tokens_to_pass} -> {memory.tokens_to_pass}"
            f"  ({_pct(memory.tokens_to_pass - control.tokens_to_pass, control.tokens_to_pass)})")
        add(f"behav control -> memory : hidden {control.hidden_looks} -> {memory.hidden_looks}")
    floor = result.noise_floor
    if floor.get("reps"):
        add("")
        add(f"NOISE FLOOR (reps {floor['reps']}: the lesson arm had no lesson to carry, "
            "so both arms ran the same prompt):")
        for arm in ("no_memory", "lesson_and_guard"):
            if arm in floor:
                f = floor[arm]
                add(f"  {arm:<18} n={f['n']} steps2pass={f['steps_to_pass']} "
                    f"tok2pass={f['tokens_to_pass']} hidden={f['hidden_looks']}")
        if "steps_delta_pct" in floor:
            add(f"  => the two arms differ by {floor['steps_delta_pct']} in steps, "
                f"{floor['tokens_delta_pct']} in tokens, {floor['hidden_delta_pct']} in lookups"
                " -- that is the model's own variance, so an effect below it is noise")
    else:
        # No floor is not the same as a floor of zero.  A run in which every
        # paired repetition carried a lesson has no repetition where both
        # arms ran the same prompt -- so it cannot tell an effect apart from
        # the model's own variance.  Say so, or the effect lines above it get
        # read as a result.
        add("")
        add("NO NOISE FLOOR: this run has no repetition in which both arms ran the "
            "same prompt, so it cannot tell an effect apart from the model's own "
            "variance -- an effect size from this run is not reportable.")
    add("")
    if result.mismatches:
        add(f"{len(result.mismatches)} DISAGREEMENT(S) between the report and the event logs:")
        for line in result.mismatches:
            add(f"  {line}")
    else:
        add("no disagreement between the machine report and the raw event logs")
    if result.notes:
        add("")
        add(f"{len(result.notes)} NOTE(S):")
        for line in result.notes:
            add(f"  {line}")
    return "\n".join(lines)


def _pct(delta: float, base: float) -> str:
    if not base:
        return "n/a"
    return f"{round(delta / base * 100)}%"

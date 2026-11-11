"""What bounds the success numbers: an attribution pass over finished runs.

The reports answer "did it work".  This module answers a different question:
**which factor is the number sensitive to** -- is it the model's ability, the
sampling, the retry budget, the acceptance signal, or the diagnostic inside the
failure output?  It never calls a model; it reads what a real campaign leaves
behind:

* **bench stdout** (``[B2] task: success=True tokens=...``): one line per cell,
  with the arm, the task, the token cost and -- in newer runs -- how many times
  the executor was invoked and why the run stopped.
* **event streams** (``events_*.jsonl``): the failure text the model actually
  received, whether the run was cut short by a policy gate, and whether the
  model call produced anything at all.

Two attribution rules matter more than the tables:

1. A cell is **not** evidence about capability unless the requirement was
   reachable from the workspace.  ``*_hidden`` tasks put the requirement only in
   the out-of-workspace judge, so a blind arm has no way to know it.  Comparing
   the blind arm across the two classes separates "the model cannot do it" from
   "the model was never told".
2. The retry budget is only a suspect if runs actually hit it.  Counting the
   executor invocations says whether the cap was binding, instead of assuming
   it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: A cell printed by ``planloop bench``.  The ``attempts=``/``status=`` suffix
#: only exists in runs from 2026-11-10 on; older logs stop after the duration.
CELL_RE = re.compile(
    r"\[(?P<config>B[0-9A-Z]{1,3})\] (?P<task>[A-Za-z0-9_]+): "
    r"success=(?P<ok>True|False) tokens=(?P<tokens>\d+)(?P<rest>.*)$"
)
ATTEMPTS_RE = re.compile(r"attempts=(\d+)")
BLOCKED_RE = re.compile(r"blocked=(.+?)\s+\d+s\s*$")
SECONDS_RE = re.compile(r"(\d+)s\s*$")

#: PowerShell echoes a failing child's stderr with this prefix, so one cell can
#: appear twice in a log.  The prefix is stripped, then exact duplicates are
#: collapsed per file.
PS_PREFIX = "python : "

#: "Text before the answer" is the failure mode this project is about: the
#: requirement lives only in the judge.  The name is the contract.
HIDDEN_SUFFIX = "_hidden"

ERROR_RE = re.compile(r"\b\w*(?:Error|Exception)\b|assert", re.IGNORECASE)


@dataclass(frozen=True)
class Cell:
    """One benchmark cell, parsed back out of a stdout log."""

    log: str
    config: str
    task: str
    success: bool
    tokens: int
    attempts: int | None = None
    blocked: str = ""
    seconds: int = 0

    @property
    def hidden(self) -> bool:
        return self.task.endswith(HIDDEN_SUFFIX)


@dataclass
class EventFacts:
    """What the failure texts in one campaign show."""

    files: int = 0
    runs: int = 0
    failures: int = 0
    with_error: int = 0
    lengths: list[int] = field(default_factory=list)
    zero_token_runs: int = 0
    interrupted: int = 0
    statuses: dict[str, int] = field(default_factory=dict)

    @property
    def error_share(self) -> float:
        return self.with_error / self.failures if self.failures else 0.0

    @property
    def median_length(self) -> int:
        if not self.lengths:
            return 0
        ordered = sorted(self.lengths)
        return ordered[len(ordered) // 2]


# --------------------------------------------------------------------------
# parsing


def read_text(path: Path) -> str:
    """Decode a log regardless of how PowerShell wrote it.

    Windows redirection produces UTF-16LE with a BOM for some streams and
    UTF-8 with a BOM for others.  Reading everything as UTF-8 silently drops
    whole logs -- which is exactly the kind of missing data that flatters a
    diagnosis, so the encoding is detected instead of assumed.
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def parse_cells(path: Path) -> list[Cell]:
    """All cells in one log, with PowerShell's duplicate echo collapsed."""
    cells: list[Cell] = []
    seen: set[tuple[str, str, bool, int, int]] = set()
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if line.startswith(PS_PREFIX):
            line = line[len(PS_PREFIX):].strip()
        match = CELL_RE.search(line)
        if not match:
            continue
        rest = match.group("rest")
        seconds_match = SECONDS_RE.search(rest)
        seconds = int(seconds_match.group(1)) if seconds_match else 0
        attempts_match = ATTEMPTS_RE.search(rest)
        blocked_match = BLOCKED_RE.search(rest)
        key = (
            match.group("config"),
            match.group("task"),
            match.group("ok") == "True",
            int(match.group("tokens")),
            seconds,
        )
        if key in seen:
            continue
        seen.add(key)
        cells.append(
            Cell(
                log=path.name,
                config=key[0],
                task=key[1],
                success=key[2],
                tokens=key[3],
                attempts=int(attempts_match.group(1)) if attempts_match else None,
                blocked=blocked_match.group(1).strip() if blocked_match else "",
                seconds=seconds,
            )
        )
    return cells


def collect_cells(directory: Path, *, since: datetime | None = None) -> tuple[list[Cell], list[str], list[str]]:
    """(cells, used log names, skipped log names) under ``directory``."""
    cells: list[Cell] = []
    used: list[str] = []
    skipped: list[str] = []
    for path in sorted(directory.glob("*.log")):
        if since is not None:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
            if modified < since:
                skipped.append(path.name)
                continue
        parsed = parse_cells(path)
        if not parsed:
            skipped.append(path.name)
            continue
        used.append(path.name)
        cells.extend(parsed)
    return cells, used, skipped


def read_events(directory: Path) -> EventFacts:
    """Failure-text and run-level facts from one campaign's event streams."""
    facts = EventFacts()
    for path in sorted(directory.glob("events_*.jsonl")):
        facts.files += 1
        status = ""
        zero_tokens = False
        interrupted = False
        executed = False
        for line in read_text(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = str(record.get("kind") or "")
            data = record.get("data") or {}
            if kind == "failure":
                reason = str(data.get("reason") or "")
                facts.failures += 1
                facts.lengths.append(len(reason))
                if ERROR_RE.search(reason):
                    facts.with_error += 1
            elif kind == "node_start":
                executed = True
            elif kind == "node_end":
                if int(data.get("tokens") or 0) == 0 and str(data.get("status")) == "failed":
                    zero_tokens = True
            elif kind in {"approval_requested", "approval_denied", "action_denied"}:
                interrupted = True
            elif kind == "run_end":
                status = str(data.get("status") or "?")
        # ``events_setup_*.jsonl`` only stages the repo; it is not a run.
        if not executed:
            continue
        facts.runs += 1
        if zero_tokens:
            facts.zero_token_runs += 1
        if interrupted:
            facts.interrupted += 1
        facts.statuses[status or "?"] = facts.statuses.get(status or "?", 0) + 1
    return facts


# --------------------------------------------------------------------------
# statistics


def fisher_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact p for the 2x2 table [[a, b], [c, d]].

    P(X >= a) under the hypergeometric null -- the same test the reports quote,
    reimplemented here so the diagnosis can be recomputed without a dependency.
    """
    n = a + b + c + d
    row1 = a + b
    col1 = a + c
    if n == 0 or row1 == 0 or col1 == 0:
        return 1.0
    denom = math.comb(n, col1)
    tail = 0.0
    for x in range(a, min(row1, col1) + 1):
        tail += math.comb(row1, x) * math.comb(n - row1, col1 - x)
    return min(1.0, tail / denom)


# --------------------------------------------------------------------------
# report


def _rate(rows: list[Cell]) -> str:
    if not rows:
        return "—"
    wins = sum(1 for row in rows if row.success)
    return f"{wins}/{len(rows)}"


def _mean_tokens(rows: list[Cell]) -> str:
    if not rows:
        return "—"
    return f"{sum(row.tokens for row in rows) // len(rows):,}"


def build_report(cells: list[Cell], used: list[str], skipped: list[str],
                 events: EventFacts | None = None, *, since: datetime | None = None,
                 events_label: str = "") -> str:
    lines: list[str] = []
    add = lines.append

    configs = sorted({cell.config for cell in cells})
    tasks = sorted({cell.task for cell in cells})
    hidden = [cell for cell in cells if cell.hidden]
    visible = [cell for cell in cells if not cell.hidden]

    add("# 成功率被什么限制：真实运行归因诊断")
    add("")
    add("> 这份报告**不测模型**，它读已经跑完的真实运行留下的两样东西——")
    add("> `bench` 的标准输出（每个格子一行）与事件流（模型实际收到的失败文本）——")
    add("> 回答一个问题：**现有的成功率数字，对哪个因素敏感**。")
    add(">")
    add("> 复现：`python -m planloop diagnose --demo .demo --events .membench-fee9 --out docs/limit-diagnosis.md`")
    add("")
    add("## 一、样本与口径")
    add("")
    add(f"- 纳入 **{len(used)}** 个日志、**{len(cells)}** 个真实格子；"
        f"配置 {', '.join(configs)}；任务 {len(tasks)} 个。")
    if since is not None:
        add(f"- 只统计 **{since:%Y-%m-%d %H:%M}** 之后的日志：`bench.py` 的任务定义在此之前改过，"
            "更早的格子与当前题目不是同一批（这一条不写清，池化就会把旧题目的失败算到新结论上）。")
    if skipped:
        shown = ", ".join("`" + name + "`" for name in skipped[:6])
        more = "" if len(skipped) <= 6 else f" 等 {len(skipped)} 个"
        add(f"- 跳过 {len(skipped)} 个日志（更早或没有格子行）：{shown}{more}。")
    add("- 编码按 BOM 检测（UTF-16LE / UTF-8-BOM 都有），PowerShell 回显的重复行按"
        "（配置, 任务, 结果, tokens, 时长）去重。**按 UTF-8 硬读会静默丢掉整份日志**。")
    add("- 格子的 `_hidden` 后缀表示要求**只存在于仓外判据**：盲发臂无论如何都看不到它。")
    add("")

    add("## 二、按任务（池化）")
    add("")
    header = "| 任务 | 可见性 | " + " | ".join(configs) + " |"
    add(header)
    add("|---" * (len(configs) + 2) + "|")
    for task in tasks:
        row = [task, "隐藏" if task.endswith(HIDDEN_SUFFIX) else "可见"]
        for config in configs:
            row.append(_rate([cell for cell in cells if cell.task == task and cell.config == config]))
        add("| " + " | ".join(row) + " |")
    add("")

    add("## 三、限制项一：需求可见性（不是模型能力，也不是采样）")
    add("")
    add("| 配置 | 可见任务 | 隐藏任务 | 单侧 Fisher p | 平均 tokens（可见 / 隐藏） |")
    add("|---|---|---|---|---|")
    for config in configs:
        vis = [cell for cell in visible if cell.config == config]
        hid = [cell for cell in hidden if cell.config == config]
        if not vis or not hid:
            continue
        a = sum(1 for cell in vis if cell.success)
        c = sum(1 for cell in hid if cell.success)
        p = fisher_greater(a, len(vis) - a, c, len(hid) - c)
        add(f"| {config} | {a}/{len(vis)} | {c}/{len(hid)} | {p:.2e} | "
            f"{_mean_tokens(vis)} / {_mean_tokens(hid)} |")
    add("")
    add("读法：**同一个模型、同一个 harness、同一套判据**，只在\"要求能不能从工作区里读到\"这一点上分组。")
    add("盲发臂在可见任务上接近全过、在隐藏任务上全挂 —— 那么 0% 说明的是\"它没被告知\"，")
    add("而不是\"它不会\"；投票臂（B1）在隐藏任务上同样全挂，说明**再采样也补不出信息**。")
    add("")

    add("## 四、限制项二：信号与诊断（不是\"重试次数\"）")
    add("")
    add("| 配置 | 全部 | 隐藏任务 | 说明 |")
    add("|---|---|---|---|")
    notes = {
        "B2": "验收真、失败带诊断、可重试",
        "B2C": "执行器在、验收永远通过（没有信号）",
        "B2D": "验收真、告知失败、**不带诊断**",
        "B0": "盲发（没有验收）",
        "B1": "多次盲发投票（没有验收）",
    }
    for config in configs:
        rows = [cell for cell in cells if cell.config == config]
        hid = [cell for cell in rows if cell.hidden]
        add(f"| {config} | {_rate(rows)} | {_rate(hid) if hid else '—'} | {notes.get(config, '')} |")
    add("")
    add("> \"全部\"列把两类任务混在一起，比例本身没有意义；要看的是它右边两列的差。")
    add("")
    b2 = [cell for cell in hidden if cell.config == "B2"]
    b2d = [cell for cell in hidden if cell.config == "B2D"]
    b2c = [cell for cell in hidden if cell.config == "B2C"]
    if b2 and b2d:
        p = fisher_greater(sum(1 for cell in b2 if cell.success), len(b2) - sum(1 for cell in b2 if cell.success),
                           sum(1 for cell in b2d if cell.success), len(b2d) - sum(1 for cell in b2d if cell.success))
        add(f"- B2 vs B2D（隐藏任务）：`p={p:.2e}` —— 只差\"失败输出里有没有诊断\"。")
    if b2 and b2c:
        p = fisher_greater(sum(1 for cell in b2 if cell.success), len(b2) - sum(1 for cell in b2 if cell.success),
                           0, len(b2c))
        add(f"- B2 vs B2C（隐藏任务）：`p={p:.2e}` —— 只差\"有没有验收信号\"。")
    add("")

    add("## 五、限制项三：重试上限是否真的绑定")
    add("")
    add("| 配置 | 记录到的执行器调用次数分布 |")
    add("|---|---|")
    for config in configs:
        counts: dict[int, int] = {}
        for cell in cells:
            if cell.config != config or cell.attempts is None:
                continue
            counts[cell.attempts] = counts.get(cell.attempts, 0) + 1
        if not counts:
            continue
        dist = ", ".join(f"{k} 次 ×{v}" for k, v in sorted(counts.items()))
        add(f"| {config} | {dist} |")
    add("")
    add("读法：B2 的格子**全部一次执行就完成**（纠错发生在同一次节点执行内，用不上调度器的重试），")
    add("而 B2D 的格子**用满了重试**仍然全挂。所以“上限太低”不是瓶颈——")
    add("**瓶颈是那一次重试里有没有带诊断**。")
    add("")
    add("> 口径：B0 的数字是**单次 episode 里的模型调用次数**，不是重试次数；")
    add("> B2/B2C/B2D 的数字是**执行器被调用的次数**（1 + 验收失败后的重试）。两者不可直接比较。")
    add("")

    if events is not None and events.failures:
        add("## 六、限制项四：诊断载荷（事件流实测）")
        add("")
        add(f"- 事件流 {events.files} 份、{events.runs} 次运行、{events.failures} 条失败记录"
            + (f"（目录 `{events_label}`）" if events_label else "") + "。")
        add(f"- **{events.with_error}/{events.failures}** 条失败文本带异常类型/断言文本"
            f"（{events.error_share:.0%}），中位长度 **{events.median_length}** 字符。")
        add(f"- 零 token 运行（模型调用直接失败、仓库没被碰过）：**{events.zero_token_runs}** 次；"
            f"策略闸门拦截：**{events.interrupted}** 次。")
        if events.statuses:
            dist = ", ".join(f"{k} ×{v}" for k, v in sorted(events.statuses.items()))
            add(f"- 运行终态：{dist}。")
            blocked = events.statuses.get("blocked", 0)
            other = blocked - events.zero_token_runs - events.interrupted
            if other > 0:
                add(f"- 其余 `blocked`（**验收/执行失败**，是结果不是装置故障）：**{other}** 次。")
        add("")
        add("读法：这批任务里失败输出**几乎总是等于那条缺失的要求**（异常类型 + 一句消息），")
        add("B2 的 100% 正是靠这一点成立；换成\"实现方向错\"或\"多要求只满足一半\"的任务，")
        add("同样的 traceback 不会把要求告诉模型——**这就是 100% 不可外推的地方**。")
        if events.zero_token_runs:
            add("")
            add(f"> 那 {events.zero_token_runs} 次零 token 运行没有产生失败记录，"
                "它们不是\"答案错\"，是\"模型没回答\"——两种必须分开统计，否则装置故障会被算成正确性问题。")
        add("")

    add("## 七、限制清单")
    add("")
    add("| 限制项 | 现状 | 是否当前瓶颈 | 依据 |")
    add("|---|---|---|---|")
    add("| 需求可见性 | 隐藏任务要求只在判据里 | **是** | 盲发在可见任务上接近全过，在隐藏任务上全挂 |")
    add("| 验收信号 | B2C 关掉验收后与盲发无差别 | 是（前提） | 没有信号就没有失败可反馈 |")
    add("| 诊断质量 | 失败输出带 traceback；B2D 去掉后归零 | **是** | B2 vs B2D 的 p 值 |")
    add("| 重试上限 | B2 从未用满；B2D 用满仍失败 | 否 | 执行器调用次数分布 |")
    add("| 模型能力 | 单模型、可见任务上已接近饱和 | 未测（天花板不在这里） | 可见任务的全过率 |")
    add("| 任务形态 | 每题一条缺失要求、一次修补可完成 | **是**（不可外推的原因） | 任务集只有 2 类隐藏题 |")
    add("| 判据召回 | 已知缺陷分类内 33/33，真实任务未测 | 未知 | [fault-recall.md](fault-recall.md) |")
    add("| 装置损耗 | 零 token 运行与批准拦截会吃掉有效格子 | 否，但会缩小有效样本 | §六的运行终态 |")
    add("")

    add("## 八、这份诊断不证明什么")
    add("")
    add("1. **它没有调用模型**：所有数字来自已完成的运行，不能替代新一轮真实模型实验。")
    add("2. **隐藏任务只有 2 道**，所以\"需求可见性\"这个分组只有 2 个样本点；")
    add("   p 值再小，也不能把它外推到其它失效形态（性能、并发、跨模块契约）。")
    add("3. **可见任务普遍更短**：分组同时混着\"要求可见\"和\"改动更小\"两个变量，")
    add("   本报告只能说\"0% 不是能力下限\"，不能说\"可见性单独解释了全部差异\"。")
    add("4. **判据召回率仍只有分类内的数**：真实任务上抓不抓得住，仍然没测——")
    add("   这是下一步最该补的一个数，而不是继续加功能。")
    add("5. **格子来自不同批次的日志**：已按 `bench.py` 的变更时间截断，但仍混着不同任务族与运行时段。")
    add("")
    return "\n".join(lines) + "\n"


def diagnose(demo: Path, events: Path | None = None, *, since: datetime | None = None) -> str:
    cells, used, skipped = collect_cells(demo, since=since)
    facts = read_events(events) if events is not None else None
    return build_report(cells, used, skipped, facts, since=since,
                        events_label=(events.name if events is not None else ""))


def parse_since(text: str) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="attribute the success numbers to a limiting factor")
    parser.add_argument("--demo", default=".demo", help="directory with bench stdout logs")
    parser.add_argument("--events", default="", help="directory with events_*.jsonl (optional)")
    parser.add_argument("--since", default="", help="only logs modified at/after this ISO time")
    parser.add_argument("--out", default="", help="write markdown here instead of stdout")
    args = parser.parse_args(argv)

    demo = Path(args.demo)
    if not demo.is_dir():
        print(f"no such directory: {demo}", file=sys.stderr)
        return 2
    events = Path(args.events) if args.events else None
    text = diagnose(demo, events, since=parse_since(args.since))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

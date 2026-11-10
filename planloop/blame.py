"""Blame: which node changed this file, and why.

Two sources, used together:

* **git** -- authoritative for *what changed*, and the commit message carries the
  ``planloop-node: <id>`` trailer written by the scheduler, so a commit maps back
  to the node that produced it;
* **events.jsonl** -- the fallback when there is no repository, and the only
  source that explains *attempts that were rolled back* (their commits no longer
  exist, but the actions are still in the log).

Line-level attribution uses ``git blame -L n,n``, which yields the commit that
last touched the line; that commit is then mapped to a node.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from planloop.persist import RunState


@dataclass
class BlameEntry:
    kind: str          # commit | action
    node_id: str
    detail: str
    sha: str = ""
    when: float = 0.0
    attempt: int = 0

    def render(self) -> str:
        stamp = ""
        if self.when:
            import time

            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.when))
        node = self.node_id or "unknown"
        sha = f" {self.sha[:8]}" if self.sha else ""
        return f"[{self.kind}] {node}{sha} {stamp} — {self.detail}"


@dataclass
class BlameReport:
    path: str
    line: int | None
    entries: list[BlameEntry] = field(default_factory=list)
    note: str = ""

    @property
    def nodes(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries:
            if entry.node_id and entry.node_id not in seen:
                seen.append(entry.node_id)
        return seen

    def to_markdown(self) -> str:
        target = f"{self.path}:{self.line}" if self.line else self.path
        lines = [f"# Why {target} looks like this", ""]
        if not self.entries:
            lines.append("_(no commit and no recorded action touched this path)_")
            if self.note:
                lines.append("")
                lines.append(self.note)
            return "\n".join(lines) + "\n"
        for entry in self.entries:
            lines.append(f"- {entry.render()}")
        if self.note:
            lines += ["", self.note]
        return "\n".join(lines) + "\n"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )


def _node_from_commit(root: Path, sha: str) -> tuple[str, str]:
    body = _git(root, "show", "-s", "--format=%B", sha).stdout
    node = ""
    for line in body.splitlines():
        if line.lower().startswith("planloop-node:"):
            node = line.split(":", 1)[1].strip()
            break
    subject = body.splitlines()[0] if body.splitlines() else ""
    return node, subject


def _git_entries(root: Path, path: str, line: int | None) -> list[BlameEntry]:
    if line is not None:
        proc = _git(root, "blame", "-L", f"{line},{line}", "--porcelain", "--", path)
        if proc.returncode != 0:
            return []
        sha = proc.stdout.split(" ", 1)[0].strip() if proc.stdout.strip() else ""
        if not sha or set(sha) == {"0"}:
            return [BlameEntry("commit", "", "line not yet committed", sha=sha)]
        node, subject = _node_from_commit(root, sha)
        when = float(_git(root, "show", "-s", "--format=%at", sha).stdout.strip() or 0)
        return [BlameEntry("commit", node, subject, sha=sha, when=when)]

    proc = _git(root, "log", "--format=%H%x09%at%x09%s", "--", path)
    if proc.returncode != 0:
        return []
    entries = []
    for raw in proc.stdout.splitlines():
        sha, at, subject = (raw.split("\t") + ["", "", ""])[:3]
        node, full_subject = _node_from_commit(root, sha)
        entries.append(
            BlameEntry("commit", node, full_subject or subject, sha=sha, when=float(at or 0))
        )
    return entries


def _event_entries(run_dir: Path, path: str) -> list[BlameEntry]:
    state_path = Path(run_dir) / "state.json"
    if not state_path.exists():
        return []
    state = RunState.load(state_path)
    events_path = Path(state.events_path) if state.events_path else Path(run_dir) / "events.jsonl"
    if not events_path.exists():
        return []
    entries = []
    normalised = path.replace("\\", "/")
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("kind") != "action":
            continue
        detail = str((event.get("data") or {}).get("detail", ""))
        if normalised in detail.replace("\\", "/"):
            entries.append(
                BlameEntry(
                    "action",
                    str(event.get("node_id", "")),
                    detail.splitlines()[0][:160],
                    when=float(event.get("ts", 0)),
                )
            )
    return entries


def blame(
    workspace: Path | str,
    path: str,
    *,
    line: int | None = None,
    run_dir: Path | str | None = None,
) -> BlameReport:
    root = Path(workspace).resolve()
    report = BlameReport(path=path, line=line)

    in_repo = _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    if in_repo:
        report.entries.extend(_git_entries(root, path, line))

    if not report.entries and run_dir is not None:
        report.entries.extend(_event_entries(Path(run_dir), path))
    elif run_dir is not None:
        # append rolled-back attempts: they never reached a commit
        rolled_back = [e for e in _event_entries(Path(run_dir), path) if e.node_id not in report.nodes]
        report.entries.extend(rolled_back)

    if not report.entries:
        notes = []
        if not in_repo:
            notes.append("not a git repository, so attribution can only come from the event log;")
        notes.append(
            "no commit and no recorded action touched this path: check that the path is "
            "relative to the workspace root and that the run was executed with --git"
        )
        report.note = " ".join(notes)
    return report


def find_run_dir(workspace: Path | str, run_id: str | None = None) -> Path | None:
    from planloop.persist import find_state

    try:
        return find_state(workspace, run_id).parent
    except FileNotFoundError:
        return None

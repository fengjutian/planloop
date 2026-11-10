"""Git checkpoints: a long run must be able to go back.

Design decisions, and why:

* **A dedicated branch per run** (``planloop/<run_id>``) keeps the user's history
  clean; a run that goes sideways can be thrown away with one branch delete.
* **One commit per finished node**, carrying the node id and the acceptance hash.
  That makes ``git log`` the progress log, and makes each node's work a
  separate, revertable unit.
* **Stash before reset** -- resetting on failure would otherwise destroy the
  evidence of *why* it failed.  The stash keeps it retrievable for audit.
* **Refuse to start on a dirty tree.**  Silently committing someone's unrelated
  work-in-progress is worse than refusing.

The module is intentionally thin: it wraps the git CLI and reports failures as
:class:`GitError`, so the scheduler can decide whether a missing repository is
fatal or merely disables checkpoints.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

BOT_NAME = "planloop"
BOT_EMAIL = "planloop@localhost"


class GitError(RuntimeError):
    pass


@dataclass
class Commit:
    sha: str
    node_id: str
    label: str


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "run"


class GitWorkspace:
    def __init__(self, root: Path | str, *, timeout: float = 60.0):
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.branch: str | None = None

    # -- plumbing ---------------------------------------------------------
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
        return proc

    @staticmethod
    def available() -> bool:
        try:
            proc = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # -- state ------------------------------------------------------------
    def is_repo(self) -> bool:
        proc = self._git("rev-parse", "--is-inside-work-tree", check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def init(self) -> None:
        if self.is_repo():
            return
        self._git("init")
        self._git("add", "-A")
        self._git("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
                  "commit", "-m", "planloop: initial state", "--allow-empty")

    def dirty_files(self) -> list[str]:
        """Uncommitted paths, excluding planloop's own bookkeeping directory."""
        if not self.is_repo():
            return []
        proc = self._git("status", "--porcelain")
        entries = [line[3:] for line in proc.stdout.splitlines() if line.strip()]
        return [e for e in entries if not e.startswith(".planloop/")]

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def _exclude_state_dir(self) -> None:
        """Keep ``.planloop/`` out of git, locally.

        ``git stash -u`` and ``git clean -fd`` would otherwise move or delete the
        run's own state and event log -- destroying exactly the audit trail we
        need after a failure.  We write to ``.git/info/exclude`` rather than the
        user's ``.gitignore`` so their repository is not modified.
        """
        git_dir = self.root / ".git"
        if not git_dir.is_dir():  # worktrees / submodules store .git as a file
            proc = self._git("rev-parse", "--git-dir", check=False)
            if proc.returncode != 0:
                return
            candidate = (self.root / proc.stdout.strip()).resolve()
            if not candidate.is_dir():
                return
            git_dir = candidate
        info = git_dir / "info" / "exclude"
        entry = ".planloop/"
        existing = info.read_text(encoding="utf-8") if info.exists() else ""
        if entry in {line.strip() for line in existing.splitlines()}:
            return
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + entry + "\n",
                        encoding="utf-8")

    # -- lifecycle --------------------------------------------------------
    def start(self, run_id: str, *, allow_dirty: bool = False) -> None:
        """Create the run branch.  Fails loudly rather than mixing in stray work."""
        if not self.is_repo():
            self.init()
        self._exclude_state_dir()
        dirty = self.dirty_files()
        if dirty and not allow_dirty:
            raise GitError(
                "workspace has uncommitted changes: "
                + ", ".join(dirty[:5])
                + ("..." if len(dirty) > 5 else "")
                + " -- commit them, or pass allow_dirty to stash them first"
            )
        if dirty:
            self.stash(f"pre-run dirty state for {run_id}")
        self.branch = f"planloop/{_slug(run_id)}"
        self._git("checkout", "-B", self.branch)

    def checkout(self, branch: str) -> None:
        self._git("checkout", branch)
        self.branch = branch

    def branch_exists(self, branch: str) -> bool:
        proc = self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        return proc.returncode == 0

    def commit(self, node_id: str, label: str, *, detail: str = "") -> Commit:
        self._git("add", "-A")
        message = f"planloop: {node_id} {label}\n\nplanloop-node: {node_id}"
        if detail:
            message += f"\n{detail}"
        self._git(
            "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
            "commit", "--allow-empty", "-m", message,
        )
        return Commit(sha=self.head(), node_id=node_id, label=label)

    def stash(self, label: str) -> str | None:
        """Park failed work somewhere retrievable instead of deleting it."""
        if not self.dirty_files():
            return None
        proc = self._git("stash", "push", "--include-untracked", "-m", label, check=False)
        if proc.returncode != 0:
            raise GitError(f"git stash failed: {proc.stderr.strip()[:300]}")
        return label

    def reset(self, ref: str) -> None:
        self._git("reset", "--hard", ref)
        self._git("clean", "-fd")

    def changed_files(self, ref: str) -> list[str]:
        proc = self._git("diff", "--name-only", ref, "HEAD")
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def log(self, *, limit: int = 20) -> list[str]:
        proc = self._git("log", "--oneline", f"-{limit}")
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def stashes(self) -> list[str]:
        proc = self._git("stash", "list")
        return [line for line in proc.stdout.splitlines() if line.strip()]

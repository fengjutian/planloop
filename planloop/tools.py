"""Tool layer: the only place that touches the outside world.

Every call is charged to the ledger and every path is confined to the
workspace root, so an executor cannot wander off and a run cannot spend
silently.

Since P5 every action also passes a risk policy *before* it runs: ordinary
commands are allowed, risky ones stop the run for a one-shot human approval,
and a small set is refused outright.  The check lives here because this is the
single chokepoint -- guarding anywhere else would leave a door open.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from planloop.budget import Ledger
from planloop.policy import ALLOW, DENY, DeniedAction, NeedsApproval, default_policy

MAX_OUTPUT = 2200


class ToolError(RuntimeError):
    pass


@dataclass
class Observation:
    action: str
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    def render(self) -> str:
        bits = [f"exit_code={self.exit_code}", f"ok={str(self.ok).lower()}"]
        if self.stdout.strip():
            bits.append("stdout:\n" + self.stdout.strip()[-MAX_OUTPUT:])
        if self.stderr.strip():
            bits.append("stderr:\n" + self.stderr.strip()[-MAX_OUTPUT:])
        return "\n".join(bits)


class Tools:
    def __init__(
        self,
        root: Path | str,
        *,
        ledger: Ledger | None = None,
        phase: str = "execute",
        timeout: float = 90.0,
        policy=None,
        approvals=None,
        run_id: str = "",
        node_id: str = "",
        sandbox=None,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger
        self.phase = phase
        self.timeout = timeout
        self.policy = policy if policy is not None else default_policy()
        self.approvals = approvals
        self.run_id = run_id
        self.node_id = node_id
        self.sandbox = sandbox

    # -- policy -----------------------------------------------------------
    def _guard(self, action: str, target: str) -> None:
        """Refuse, or stop for approval, *before* anything touches the machine."""
        assessment = self.policy.assess(action, target)
        if assessment.level == ALLOW:
            return
        if assessment.level == DENY:
            raise DeniedAction(assessment)
        if self.approvals is None:
            # no place to record consent -> fail closed
            raise DeniedAction(assessment)
        usable = self.approvals.find_usable(action, assessment.target, self.run_id)
        if usable is not None:
            if self.approvals.consume(usable.token):
                return
        pending = self.approvals.find_pending(action, assessment.target, self.run_id)
        if pending is None:
            pending = self.approvals.request(
                action=action,
                target=assessment.target,
                reason=assessment.reason,
                rule=assessment.rule,
                run_id=self.run_id,
                node_id=self.node_id,
            )
        raise NeedsApproval(assessment, pending.token)

    # -- helpers ----------------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        candidate = (self.root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError(f"path escapes workspace: {rel}") from exc
        return candidate

    def _charge(self) -> None:
        if self.ledger is not None:
            self.ledger.charge(self.phase, tool_calls=1)

    # -- actions ----------------------------------------------------------
    def run(self, cmd: str, *, cwd: str | None = None) -> Observation:
        self._guard("run", cmd)
        self._charge()
        workdir = self._resolve(cwd) if cwd else self.root
        if self.sandbox is None:
            from planloop.sandbox import HostSandbox

            self.sandbox = HostSandbox(timeout=self.timeout)
        return self.sandbox.run(cmd, cwd=workdir, timeout=self.timeout)

    def run_python(self, script: str, *args: str, cwd: str | None = None) -> Observation:
        return self.run(f'"{sys.executable}" -c {_q(script)} ' + " ".join(_q(a) for a in args), cwd=cwd)

    def write(self, path: str, content: str) -> Observation:
        self._guard("write", path)
        self._charge()
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return Observation("write", True, 0, f"wrote {len(content)} chars to {path}")

    def read(self, path: str) -> Observation:
        self._charge()
        target = self._resolve(path)
        if not target.exists():
            return Observation("read", False, 1, "", f"no such file: {path}")
        if target.is_dir():
            entries = sorted(p.name for p in target.iterdir())
            return Observation("read", True, 0, "\n".join(entries))
        text = target.read_text(encoding="utf-8", errors="replace")
        return Observation("read", True, 0, text[:MAX_OUTPUT])

    def exists(self, path: str) -> bool:
        try:
            return self._resolve(path).exists()
        except ToolError:
            return False

    def snapshot(self) -> dict[str, float]:
        """Cheap fingerprints, used to detect environment drift."""
        out: dict[str, float] = {}
        for p in self.root.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                try:
                    out[str(p.relative_to(self.root))] = p.stat().st_mtime
                except OSError:
                    continue
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"root": str(self.root)}


def _q(text: str) -> str:
    return '"' + text.replace('"', '\\"') + '"'

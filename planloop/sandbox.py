"""Sandboxing: the simple version, plus where the stronger options live.

What this file is **not**: an isolation boundary.  It cannot stop a command
from deleting files outside the workspace, and it is not a substitute for a
container or a microVM.

What it does do, cheaply and honestly:

* **environment scrubbing** -- API keys, tokens and cloud credentials are
  removed from the child's environment, so `env` / `print(os.environ)` cannot
  leak them.  This is the single most common exfiltration path in agent runs
  and it costs nothing to close;
* **execution disable** -- ``NoExecSandbox`` refuses every command, for
  review-only or verification-only runs;
* **docker passthrough** -- when explicitly requested and available, commands
  run in a throwaway container with no network.

Everything else (KVM microVMs, eBPF network policy, credential proxies) is
deliberately out of scope for now; see docs/sandbox.md for the survey.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from planloop.tools import Observation

#: environment variables that must never reach a child process
SECRET_NAME = re.compile(
    r"(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY|"
    r"ACCESS[_-]?KEY|SESSION[_-]?KEY|AUTH[_-]?TOKEN|COOKIE|"
    r"^(AWS|AZURE|GCP|GOOGLE|OPENAI|ANTHROPIC|DEEPSEEK|GITHUB|GITLAB|HF)_)",
    re.IGNORECASE,
)

#: variables that are safe even though their names match nothing above
KEEP = {
    "PATH", "HOME", "USER", "USERNAME", "TEMP", "TMP", "TMPDIR", "SHELL",
    "LANG", "LC_ALL", "TERM", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMDATA", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
}


class SandboxError(RuntimeError):
    pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a command and everything it started."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
            )
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


class Sandbox(Protocol):
    name: str

    def run(self, cmd: str, *, cwd: Path, timeout: float) -> Observation: ...


def scrub_env(environ: dict[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Return (clean environment, names removed)."""
    source = dict(os.environ if environ is None else environ)
    clean: dict[str, str] = {}
    removed: list[str] = []
    for name, value in source.items():
        upper = name.upper()
        if upper in KEEP:
            clean[name] = value
            continue
        if SECRET_NAME.search(upper):
            removed.append(name)
            continue
        clean[name] = value
    return clean, removed


@dataclass
class HostSandbox:
    """Runs on the host with a scrubbed environment.  Not an isolation boundary."""

    name: str = "host"
    root: Path | None = None
    timeout: float = 90.0
    _removed: set[str] = field(default_factory=set)

    def run(self, cmd: str, *, cwd: Path, timeout: float | None = None) -> Observation:
        env, removed = scrub_env()
        self._removed.update(removed)
        limit = timeout or self.timeout
        kwargs: dict[str, Any] = dict(
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            encoding="utf-8",
            errors="replace",
            # Windows: cmd.exe waits for *more input* when a quote is
            # unbalanced -- a model that writes `python -c "print(1` hangs the
            # whole run forever.  Giving stdin EOF ends that wait at once.
            stdin=subprocess.DEVNULL,
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            return Observation("run", False, 126, "", f"could not start command: {exc}")
        try:
            stdout, stderr = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            # Killing the shell is not enough: a grandchild that still holds
            # the stdout pipe makes ``communicate()`` block forever, so the
            # timeout would never fire.  Kill the tree, then stop waiting.
            _kill_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            note = f"timed out after {limit}s"
            return Observation("run", False, 124, stdout or "", (stderr or "") + f"\n{note}")
        return Observation("run", proc.returncode == 0, proc.returncode, stdout or "", stderr or "")

    @property
    def redacted_variables(self) -> list[str]:
        return sorted(self._removed)


@dataclass
class NoExecSandbox:
    """Refuses to run anything; useful for review-only runs."""

    name: str = "none"

    def run(self, cmd: str, *, cwd: Path, timeout: float | None = None) -> Observation:
        return Observation(
            "run", False, 126, "", "sandbox: command execution is disabled (--sandbox none)"
        )


@dataclass
class DockerSandbox:
    """Throwaway container, no network, only the workspace mounted."""

    image: str = "python:3.11-slim"
    network: str = "none"
    memory: str = "1g"
    name: str = "docker"

    @staticmethod
    def available() -> bool:
        return shutil.which("docker") is not None

    def run(self, cmd: str, *, cwd: Path, timeout: float = 120.0) -> Observation:
        if not self.available():
            raise SandboxError("docker was requested but is not on PATH")
        args = [
            "docker", "run", "--rm",
            "--network", self.network,
            "--memory", self.memory,
            "--pids-limit", "256",
            "-v", f"{Path(cwd).resolve()}:/work",
            "-w", "/work",
            self.image,
            "sh", "-lc", cmd,
        ]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return Observation("run", False, 124, "", f"sandbox timed out after {timeout}s")
        return Observation("run", proc.returncode == 0, proc.returncode, proc.stdout, proc.stderr)


def build(name: str = "host") -> Sandbox:
    name = (name or "host").lower()
    if name == "host":
        return HostSandbox()
    if name in ("none", "noexec"):
        return NoExecSandbox()
    if name == "docker":
        if not DockerSandbox.available():
            raise SandboxError("docker is not available; use --sandbox host or install Docker")
        return DockerSandbox()
    raise SandboxError(f"unknown sandbox {name!r}; expected host | none | docker")

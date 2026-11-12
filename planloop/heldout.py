"""Held-out acceptance: a criterion the model can neither read nor edit.

Why this module exists
----------------------
A measurement device needs a ground truth the model cannot look at.  A file
that merely lives *outside the workspace* is not that: the ``run`` action goes
through a shell, and ``type <file>`` reads anything the process can reach.
The workspace lock stops the ``read`` tool, not the shell.

Measured on 2026-11-11 (fee family, 8 reps, one real model): the model read
the held-out criterion **three times** with ``cd /d ... && type ...oracle\\
r1_oracle_1.py``, and the criterion source contains the expected numbers, so
those runs were contaminated and the numbers measured in them cannot be
published.

How the criterion stays hidden
------------------------------
It is fed to python **on stdin** (``python -``) and never written to disk:

* there is no file for the model to find -- not outside the workspace, not
  inside it, not for a moment;
* the traceback of a program that came from stdin shows **no source line**,
  and that matters: the failing line is the one that holds the expected
  number, so a criterion that ran from a file handed the answer to the model
  through the acceptance feedback itself (measured: the failure output
  contained ``198.0``, the answer of the second task).

The failure text still reaches the model -- it has to, or the model cannot
act on it -- and the assertion messages are numeric-free by contract
(``docs/memory-value-fee.md``).  Its location is never rendered into a
prompt either: a model that is told where the answer lives spends its step
budget looking for it (measured: 6 of the 12 steps of one run went to a
fruitless hunt for the criterion).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: appended to the criterion so that it can run itself.  It discovers the
#: tests defined in ``__main__`` and exits non-zero when one of them fails.
RUNNER = '''

if __name__ == "__main__":
    import sys as _sys
    import unittest as _unittest
    _suite = _unittest.defaultTestLoader.loadTestsFromModule(_sys.modules["__main__"])
    _result = _unittest.TextTestRunner(verbosity=0).run(_suite)
    _sys.exit(0 if _result.wasSuccessful() else 1)
'''


@dataclass
class HeldOutResult:
    """The outcome of one held-out check."""

    exit_code: int
    stdout: str
    stderr: str
    #: where the criterion ran (``<stdin>``: there is no file)
    location: str = "<stdin>"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


def run_held_out(
    source: str,
    workspace: Path,
    *,
    name: str = "criterion",
    timeout: float = 120.0,
) -> HeldOutResult:
    """Run the criterion against the workspace without ever writing it out.

    ``name`` only shows up in the test ids of the output.
    """
    workspace = Path(workspace).resolve()
    program = source + RUNNER
    env = dict(os.environ)
    # the criterion is read from a pipe: say so, or python decodes it with
    # the console code page and a non-ascii message comes out mangled
    env["PYTHONIOENCODING"] = "utf-8"
    # ... and it must not leave a __pycache__ behind: the check runs against
    # the workspace, and the workspace is what the next arm starts from
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            f'"{sys.executable}" -',
            shell=True,
            cwd=str(workspace),
            input=program,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        code, out, err = 124, "", "the held-out criterion timed out"
    return HeldOutResult(code, out, err)


def canary_token() -> str:
    """A token that identifies one criterion instance.

    Put it in the criterion source and search the event log for it: if the
    model ever saw the criterion, the token is in the log, and the run is
    contaminated.  That turns "the criterion was held out" from a claim into
    a check.
    """
    import uuid

    return "planloop-canary-" + uuid.uuid4().hex[:16]


def with_canary(source: str, token: str) -> str:
    """Return the criterion source with the canary token appended."""
    if not token:
        return source
    return source.rstrip("\n") + f"\n\n# {token}\n"


def find_canary(paths: list[Path], token: str) -> list[str]:
    """Return the files whose contents contain the canary token."""
    hits: list[str] = []
    if not token:
        return hits
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if token in text:
            hits.append(str(path))
    return hits

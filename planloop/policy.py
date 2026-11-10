"""Risk policy: what the agent is allowed to do without a human.

Three levels:

* ``allow``   -- ordinary commands inside the workspace
* ``confirm`` -- reversible-but-costly or irreversible; needs a one-shot approval
* ``deny``    -- never, not even with approval

The decision is deterministic and explainable: every verdict names the rule that
fired.  A model's opinion about its own safety is not part of this file, on
purpose -- it would be unauditable and it would drift between runs.

Fail-closed: an unrecognised shell construct (``$(...)``, backticks, ``bash -c``)
is ``confirm`` rather than ``allow``.  That produces noise, and the noise is the
honest option.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Pattern

ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"

RANK = {ALLOW: 0, CONFIRM: 1, DENY: 2}


class PolicyError(RuntimeError):
    pass


class DeniedAction(PolicyError):
    def __init__(self, assessment: "Assessment"):
        super().__init__(f"denied by policy ({assessment.rule}): {assessment.reason}")
        self.assessment = assessment


class NeedsApproval(PolicyError):
    def __init__(self, assessment: "Assessment", token: str):
        super().__init__(f"needs approval ({assessment.rule}): {assessment.reason}")
        self.assessment = assessment
        self.token = token


@dataclass(frozen=True)
class Assessment:
    level: str
    rule: str
    reason: str
    action: str  # "run" or "write"
    target: str  # command or path

    @property
    def blocked(self) -> bool:
        return self.level != ALLOW

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "rule": self.rule,
            "reason": self.reason,
            "action": self.action,
            "target": self.target,
        }


@dataclass(frozen=True)
class Rule:
    id: str
    level: str
    pattern: Pattern[str]
    reason: str


def _rule(rule_id: str, level: str, pattern: str, reason: str) -> Rule:
    return Rule(rule_id, level, re.compile(pattern, re.IGNORECASE), reason)


#: deny rules are checked first; a deny can never be approved away
DENY_RULES: list[Rule] = [
    _rule("rm_root", DENY,
          r"\brm\s+(?:-[a-z]*\s+)*(-[a-z]*[rf][a-z]*\s+)*(?:/|/\*|~|\$HOME|\.\.)(?:\s|$)",
          "deletes a root or home directory"),
    _rule("disk_format", DENY, r"\b(mkfs(\.\w+)?|fdisk|diskpart)\b",
          "formats or repartitions a disk"),
    _rule("dd_device", DENY, r"\bdd\b[^\n]*\bof=/dev/",
          "writes raw bytes to a device"),
    _rule("shutdown", DENY, r"\b(shutdown|reboot|halt|poweroff)\b",
          "takes the machine down"),
    _rule("win_format", DENY, r"\bformat\s+[a-z]:",
          "formats a Windows volume"),
    _rule("drop_database", DENY, r"\bdrop\s+database\b",
          "drops an entire database"),
    _rule("fork_bomb", DENY, r":\s*\(\s*\)\s*\{[^}]*\|[^}]*&",
          "fork bomb"),
    _rule("credential_exfil", DENY,
          r"(\.ssh/|\.aws/credentials|\.env\b)[^\n]*\|\s*(curl|wget|nc|ncat)\b",
          "pipes credentials to the network"),
]

CONFIRM_RULES: list[Rule] = [
    _rule("rm_recursive", CONFIRM, r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+",
          "recursive or forced delete"),
    _rule("win_delete", CONFIRM, r"\b(rd|rmdir)\s+/s\b|\bdel\s+/[a-z]*[fq]",
          "forced recursive delete on Windows"),
    _rule("git_force_push", CONFIRM, r"\bgit\s+push\b[^\n]*(\s-f\b|--force)",
          "rewrites remote history"),
    _rule("git_reset_hard", CONFIRM, r"\bgit\s+reset\s+--hard\b",
          "discards uncommitted work"),
    _rule("git_clean", CONFIRM, r"\bgit\s+clean\b[^\n]*-[a-z]*[fdx]",
          "deletes untracked or ignored files"),
    _rule("git_branch_delete", CONFIRM, r"\bgit\s+branch\s+-D\b",
          "force-deletes a branch"),
    _rule("publish", CONFIRM,
          r"\b(npm|cargo|twine|docker|helm|poetry)\s+(publish|push|upload|deploy)\b",
          "publishes an artefact outside this machine"),
    _rule("infra", CONFIRM, r"\b(kubectl\s+(delete|apply)|terraform\s+(apply|destroy)|ansible-playbook)\b",
          "changes live infrastructure"),
    _rule("privilege", CONFIRM, r"\b(sudo|runas|chmod\s+-R\s+777)\b",
          "escalates privileges"),
    _rule("pipe_to_shell", CONFIRM,
          r"\b(curl|wget|iwr|invoke-webrequest)\b[^\n]*\|\s*(sh|bash|zsh|python3?|powershell)\b",
          "executes code downloaded from the network"),
    _rule("package_install", CONFIRM,
          r"\b(pip3?|npm|yarn|pnpm|apt|apt-get|brew|choco|winget)\s+(install|add)\b",
          "installs third-party code"),
    _rule("shell_eval", CONFIRM, r"\b(eval|bash\s+-c|sh\s+-c|powershell\s+-enc)\b",
          "evaluates a nested shell command"),
    _rule("subshell", CONFIRM, r"\$\(|`",
          "embeds a nested command whose effect cannot be read statically"),
    _rule("inline_code", CONFIRM,
          r"\bpython[0-9.]*\s+-c\b[^\n]*(os\.system|os\.remove|os\.rmdir|shutil\.|subprocess|popen|socket|urllib|requests)",
          "runs inline code that can touch the system"),
]

#: written paths that can change what happens on the *next* run
SENSITIVE_WRITES: list[Rule] = [
    _rule("git_hooks", CONFIRM, r"(^|/)\.git/hooks/",
          "installs a git hook that runs on future commits"),
    _rule("ci_config", CONFIRM, r"(^|/)\.github/workflows/",
          "changes CI, which runs code on push"),
    _rule("git_config", CONFIRM, r"(^|/)\.git/config$",
          "changes repository configuration"),
]


class CommandPolicy:
    def __init__(
        self,
        *,
        allow_exact: Iterable[str] = (),
        allow_prefix: Iterable[str] = (),
        deny_rules: list[Rule] | None = None,
        confirm_rules: list[Rule] | None = None,
        sensitive_writes: list[Rule] | None = None,
    ):
        self.deny_rules = deny_rules if deny_rules is not None else DENY_RULES
        self.confirm_rules = confirm_rules if confirm_rules is not None else CONFIRM_RULES
        self.sensitive_writes = sensitive_writes if sensitive_writes is not None else SENSITIVE_WRITES
        self.allow_exact = {c.strip() for c in allow_exact if c.strip()}
        self.allow_prefix = tuple(p for p in allow_prefix if p)

    # -- decisions --------------------------------------------------------
    def assess_command(self, command: str) -> Assessment:
        cmd = " ".join(command.split())
        if cmd in self.allow_exact or any(cmd.startswith(p) for p in self.allow_prefix):
            return Assessment(ALLOW, "allowlist", "explicitly allowed by policy", "run", cmd)
        for rule in self.deny_rules:
            if rule.pattern.search(cmd):
                return Assessment(DENY, rule.id, rule.reason, "run", cmd)
        for rule in self.confirm_rules:
            if rule.pattern.search(cmd):
                return Assessment(CONFIRM, rule.id, rule.reason, "run", cmd)
        return Assessment(ALLOW, "default", "ordinary command", "run", cmd)

    def assess_write(self, path: str) -> Assessment:
        normalised = path.replace("\\", "/")
        while normalised.startswith("./"):
            normalised = normalised[2:]
        for rule in self.sensitive_writes:
            if rule.pattern.search(normalised):
                return Assessment(CONFIRM, rule.id, rule.reason, "write", normalised)
        return Assessment(ALLOW, "default", "ordinary file write", "write", normalised)

    def assess(self, action: str, target: str) -> Assessment:
        if action == "run":
            return self.assess_command(target)
        if action == "write":
            return self.assess_write(target)
        return Assessment(ALLOW, "unknown_action", "not a guarded action", action, target)


_DEFAULT: CommandPolicy | None = None


def default_policy() -> CommandPolicy:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = CommandPolicy()
    return _DEFAULT

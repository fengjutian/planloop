import json
import tempfile
import unittest
from pathlib import Path

from planloop.approvals import ApprovalStore, digest
from planloop.model import ScriptedModel
from planloop.persist import runs_root
from planloop.policy import ALLOW, CONFIRM, DENY, CommandPolicy
from planloop.scheduler import run
from planloop.spec import ProblemSpec


class PolicyRulesTest(unittest.TestCase):
    def setUp(self):
        self.policy = CommandPolicy()

    def level(self, cmd: str) -> str:
        return self.policy.assess_command(cmd).level

    def test_ordinary_commands_are_allowed(self):
        for cmd in (
            "python -m unittest discover -s tests -q",
            "python -V",
            "pytest -q",
            'python -c "raise SystemExit(3)"',
            "git status",
            "npm test",
            "cargo build",
        ):
            self.assertEqual(self.level(cmd), ALLOW, cmd)

    def test_destructive_commands_need_approval(self):
        for cmd in (
            "rm -rf build",
            "git push --force origin main",
            "git reset --hard HEAD~3",
            "npm publish",
            "pip install requests",
            "chmod -R 777 .",
        ):
            self.assertEqual(self.level(cmd), CONFIRM, cmd)

    def test_denied_commands_can_never_run(self):
        for cmd in ("rm -rf /", "shutdown -h now", "mkfs.ext4 /dev/sda", "DROP DATABASE prod"):
            self.assertEqual(self.level(cmd), DENY, cmd)

    def test_unreadable_shell_is_confirm_not_allow(self):
        self.assertEqual(self.level("echo $(cat /etc/passwd)"), CONFIRM)
        self.assertEqual(self.level("bash -c 'make deploy'"), CONFIRM)

    def test_allowlist_bypasses_confirm_rules(self):
        policy = CommandPolicy(allow_exact=["rm -rf build"])
        self.assertEqual(policy.assess_command("rm -rf build").level, ALLOW)
        self.assertEqual(policy.assess_command("rm -rf dist").level, CONFIRM)

    def test_sensitive_writes_need_approval(self):
        self.assertEqual(self.policy.assess_write(".git/hooks/pre-commit").level, CONFIRM)
        self.assertEqual(self.policy.assess_write(".github/workflows/ci.yml").level, CONFIRM)
        self.assertEqual(self.policy.assess_write("src/app.py").level, ALLOW)

    def test_reason_names_the_rule(self):
        assessment = self.policy.assess_command("git push --force")
        self.assertEqual(assessment.rule, "git_force_push")
        self.assertTrue(assessment.blocked)


class ApprovalStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ApprovalStore(Path(self.tmp.name) / "approvals.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_then_approved_then_consumed_once(self):
        approval = self.store.request(action="run", target="rm -rf build", reason="delete", rule="rm_recursive")
        self.assertEqual(approval.status, "pending")
        self.assertEqual(len(self.store.pending()), 1)

        self.store.approve(approval.token)
        self.assertTrue(self.store.find_usable("run", "rm -rf build"))
        self.assertTrue(self.store.consume(approval.token))
        self.assertFalse(self.store.consume(approval.token), "approvals are one-shot")
        self.assertIsNone(self.store.find_usable("run", "rm -rf build"))

    def test_approval_is_bound_to_the_exact_action(self):
        approval = self.store.request(action="run", target="rm -rf build", reason="r", rule="x")
        self.store.approve(approval.token)
        self.assertIsNotNone(self.store.find_usable("run", "rm -rf build"))
        self.assertIsNone(self.store.find_usable("run", "rm -rf /"), "must not transfer to another command")

    def test_denied_approval_is_not_usable(self):
        approval = self.store.request(action="run", target="npm publish", reason="r", rule="publish")
        self.store.deny(approval.token, by="reviewer")
        self.assertFalse(self.store.get(approval.token).usable)

    def test_expired_approval_is_not_usable(self):
        approval = self.store.request(action="run", target="x", reason="r", rule="x")
        self.store.approve(approval.token, ttl_s=-1)
        self.assertFalse(self.store.get(approval.token).usable)

    def test_events_replay_after_reopen(self):
        approval = self.store.request(action="run", target="rm -rf build", reason="r", rule="x")
        self.store.approve(approval.token, by="charles")
        reopened = ApprovalStore(self.store.path)
        loaded = reopened.get(approval.token)
        self.assertEqual(loaded.status, "approved")
        self.assertEqual(loaded.approved_by, "charles")
        self.assertEqual(loaded.target_hash, digest("run", "rm -rf build"))


def spec() -> ProblemSpec:
    return ProblemSpec.from_dict(
        {
            "goal": "delete a folder then write a file",
            "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "done.txt"}],
            "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 4},
            "plan_template": {
                "nodes": [
                    {"id": "T1", "goal": "clean and finish", "acceptance": [
                        {"id": "A1", "kind": "file_exists", "spec": "done.txt"}]}
                ]
            },
        }
    )


STEPS = [
    '{"action": "run", "cmd": "python -c \\"import shutil; shutil.rmtree(\'scratch\')\\""}',
    '{"action": "write", "path": "done.txt", "content": "ok"}',
    '{"action": "finish", "summary": "cleaned and wrote"}',
]


def scripted() -> ScriptedModel:
    calls = {"n": 0}

    def handler(prompt: str) -> str:
        idx = min(calls["n"], len(STEPS) - 1)
        calls["n"] += 1
        return STEPS[idx]

    return ScriptedModel(handler)


class EndToEndApprovalTest(unittest.TestCase):
    def test_high_risk_command_stops_then_runs_after_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "scratch").mkdir(parents=True)
            (ws / "scratch" / "junk.txt").write_text("x", encoding="utf-8")
            run_dir = runs_root(ws) / "r-approval"

            first = run(spec(), model=scripted(), workspace=ws, run_dir=run_dir)
            self.assertEqual(first.status, "blocked")
            self.assertIn("needs approval", first.blocked.reason)
            self.assertTrue((ws / "scratch").exists(), "nothing may run before approval")

            store = ApprovalStore(ws / ".planloop" / "approvals.jsonl")
            pending = store.pending()
            self.assertEqual(len(pending), 1)
            self.assertIn("rmtree", pending[0].target)
            token = pending[0].token
            self.assertIn(token, first.blocked.needed)

            store.approve(token)

            second = run(None, model=scripted(), workspace=ws, run_dir=run_dir, resume=True)
            self.assertEqual(second.status, "succeeded")
            self.assertFalse((ws / "scratch").exists(), "approved command should have run")
            self.assertTrue((ws / "done.txt").exists())

    def test_resume_preserves_the_earlier_event_log(self):
        """Regression: Blackboard used to truncate events.jsonl on every resume."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "scratch").mkdir(parents=True)
            run_dir = runs_root(ws) / "r-log"
            run(spec(), model=scripted(), workspace=ws, run_dir=run_dir)

            def kinds() -> list[str]:
                path = run_dir / "events.jsonl"
                return [
                    json.loads(line)["kind"]
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]

            before = kinds()
            self.assertIn("approval_requested", before)

            store = ApprovalStore(ws / ".planloop" / "approvals.jsonl")
            store.approve(store.pending()[0].token)
            run(None, model=scripted(), workspace=ws, run_dir=run_dir, resume=True)

            after = kinds()
            self.assertEqual(after[: len(before)], before, "resume must append, not truncate")
            self.assertIn("resume", after)

    def test_denied_command_stops_without_an_approval_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            steps = [
                '{"action": "run", "cmd": "shutdown -h now"}',
                '{"action": "finish", "summary": "done"}',
            ]
            calls = {"n": 0}

            def handler(prompt: str) -> str:
                idx = min(calls["n"], len(steps) - 1)
                calls["n"] += 1
                return steps[idx]

            result = run(spec(), model=ScriptedModel(handler), workspace=ws, run_dir=runs_root(ws) / "r-deny")
            self.assertEqual(result.status, "blocked")
            self.assertIn("refused by policy", result.blocked.reason)
            store = ApprovalStore(ws / ".planloop" / "approvals.jsonl")
            self.assertEqual(store.pending(), [], "denied actions must not create approvals")

    def test_sensitive_write_stops_for_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / ".git" / "hooks").mkdir(parents=True)
            steps = [
                '{"action": "write", "path": ".git/hooks/pre-commit", "content": "#!/bin/sh\\nexit 0\\n"}',
                '{"action": "write", "path": "done.txt", "content": "ok"}',
                '{"action": "finish", "summary": "done"}',
            ]
            calls = {"n": 0}

            def handler(prompt: str) -> str:
                idx = min(calls["n"], len(steps) - 1)
                calls["n"] += 1
                return steps[idx]

            result = run(spec(), model=ScriptedModel(handler), workspace=ws, run_dir=runs_root(ws) / "r-hook")
            self.assertEqual(result.status, "blocked")
            self.assertIn("git hook", result.blocked.reason)
            self.assertFalse((ws / ".git" / "hooks" / "pre-commit").exists())


if __name__ == "__main__":
    unittest.main()

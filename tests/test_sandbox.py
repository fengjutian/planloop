import os
import tempfile
import unittest
from pathlib import Path

from planloop.sandbox import DockerSandbox, HostSandbox, NoExecSandbox, SandboxError, build, scrub_env
from planloop.scheduler import run
from planloop.model import ScriptedModel
from planloop.spec import ProblemSpec
from planloop.tools import Tools


class ScrubEnvTest(unittest.TestCase):
    def test_secrets_are_removed_and_basics_are_kept(self):
        source = {
            "PATH": "/usr/bin",
            "HOME": "/home/dev",
            "OPENAI_API_KEY": "sk-secret",
            "GITHUB_TOKEN": "ghp_x",
            "DB_PASSWORD": "hunter2",
            "MY_API_KEY": "k",
            "AWS_SECRET_ACCESS_KEY": "a",
            "NORMAL_SETTING": "keep-me",
        }
        clean, removed = scrub_env(source)
        self.assertIn("PATH", clean)
        self.assertIn("HOME", clean)
        self.assertEqual(clean["NORMAL_SETTING"], "keep-me")
        for name in ("OPENAI_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "MY_API_KEY", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(name, clean)
            self.assertIn(name, removed)


class SandboxTest(unittest.TestCase):
    def test_host_sandbox_hides_secrets_from_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["PLANLOOP_TEST_TOKEN"] = "super-secret"
            try:
                sandbox = HostSandbox()
                obs = sandbox.run(
                    'python -c "import os; print(os.environ.get(\'PLANLOOP_TEST_TOKEN\', \'<absent>\'))"',
                    cwd=Path(tmp),
                )
                self.assertTrue(obs.ok, obs.render())
                self.assertIn("<absent>", obs.stdout)
                self.assertNotIn("super-secret", obs.stdout)
                self.assertIn("PLANLOOP_TEST_TOKEN", sandbox.redacted_variables)
            finally:
                os.environ.pop("PLANLOOP_TEST_TOKEN", None)

    def test_noexec_refuses_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = NoExecSandbox().run("echo hi", cwd=Path(tmp))
            self.assertFalse(obs.ok)
            self.assertEqual(obs.exit_code, 126)
            self.assertIn("disabled", obs.stderr)

    def test_build_rejects_unknown_names(self):
        with self.assertRaises(SandboxError):
            build("chroot")

    def test_docker_is_optional_and_fails_loudly(self):
        if DockerSandbox.available():
            self.assertEqual(build("docker").name, "docker")
        else:
            with self.assertRaises(SandboxError):
                build("docker")

    def test_tools_use_the_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Tools(tmp, sandbox=NoExecSandbox())
            obs = tools.run("echo hi")
            self.assertFalse(obs.ok)
            self.assertIn("disabled", obs.stderr)


class SandboxRunTest(unittest.TestCase):
    def spec(self) -> ProblemSpec:
        return ProblemSpec.from_dict(
            {
                "goal": "run a command",
                "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "out.txt"}],
                "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 2},
                "plan_template": {
                    "nodes": [
                        {"id": "T1", "goal": "produce out.txt", "acceptance": [
                            {"id": "A1", "kind": "file_exists", "spec": "out.txt"}]}
                    ]
                },
            }
        )

    def test_run_refuses_commands_when_execution_is_disabled(self):
        steps = [
            '{"action": "run", "cmd": "echo hi > out.txt"}',
            '{"action": "finish", "summary": "done"}',
        ]
        calls = {"n": 0}

        def handler(prompt: str) -> str:
            idx = min(calls["n"], len(steps) - 1)
            calls["n"] += 1
            return steps[idx]

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            result = run(self.spec(), model=ScriptedModel(handler), workspace=ws,
                         sandbox=NoExecSandbox())
            self.assertNotEqual(result.status, "succeeded")
            self.assertFalse((ws / "out.txt").exists())


if __name__ == "__main__":
    unittest.main()

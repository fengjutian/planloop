import tempfile
import time
import unittest
from pathlib import Path

from planloop.embed import HashingEmbedder, cosine
from planloop.memdb import MemoryDB, MemorySystem, check_evidence, repo_scope


class EmbedderTest(unittest.TestCase):
    def test_deterministic_and_normalised(self):
        emb = HashingEmbedder(dim=128)
        a = emb.embed("run the test suite")
        b = emb.embed("run the test suite")
        self.assertEqual(a, b)
        self.assertAlmostEqual(cosine(a, a), 1.0, places=6)

    def test_related_text_scores_higher(self):
        emb = HashingEmbedder()
        q = emb.embed("how do I run the tests")
        near = emb.embed("test command: python -m pytest -q")
        far = emb.embed("helm upgrade production cluster")
        self.assertGreater(cosine(q, near), cosine(q, far))


class MemoryDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = MemoryDB(self.root / "m.db", scope="repo:x", root=self.root)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_upsert_and_get(self):
        self.db.upsert("fact", "test_command", "python -m pytest -q",
                       evidence={"type": "command", "spec": "python -V"})
        row = self.db.get("fact", "test_command")
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], "python -m pytest -q")
        self.db.upsert("fact", "test_command", "python -m pytest -x")
        self.assertEqual(self.db.get("fact", "test_command")["value"], "python -m pytest -x")
        self.assertEqual(len(self.db.rows()), 1)

    def test_missing_evidence_caps_confidence(self):
        self.db.upsert("fact", "guess", "maybe this works", confidence=0.99)
        self.assertLessEqual(self.db.get("fact", "guess")["confidence"], 0.35)

    def test_search_ranks_relevant_first(self):
        self.db.upsert("fact", "test_command", "python -m pytest -q", evidence={"type": "manual"})
        self.db.upsert("fact", "deploy", "helm upgrade production", evidence={"type": "manual"})
        self.db.upsert("fact", "lint", "ruff check .", evidence={"type": "manual"})
        hits = self.db.search("how do I run the test suite")
        self.assertTrue(hits)
        self.assertEqual(hits[0].key, "test_command")

    def test_ttl_expiry(self):
        self.db.upsert("fact", "old", "stale knowledge", evidence={"type": "manual"}, ttl_days=-1)
        hits = self.db.search("stale knowledge")
        self.assertEqual(hits, [])
        self.assertEqual(self.db.get("fact", "old")["status"], "stale")

    def test_evidence_verification_marks_stale(self):
        self.db.upsert("fact", "broken", "was true once",
                       evidence={"type": "command", "spec": 'python -c "import sys; sys.exit(3)"'})
        results = self.db.verify()
        self.assertTrue(results)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(self.db.get("fact", "broken")["status"], "stale")

    def test_file_evidence_outside_repo_is_rejected(self):
        ok, detail = check_evidence({"type": "file", "path": "../../etc/passwd"}, self.root)
        self.assertFalse(ok)
        self.assertIn("escapes", detail)

    def test_context_block_respects_budget(self):
        for i in range(20):
            self.db.upsert("fact", f"k{i}", "x" * 200, evidence={"type": "manual"})
        block, ids = self.db.context_block("k1 k2", limit=10, max_chars=300)
        self.assertLessEqual(len(block), 400)
        self.assertTrue(ids)

    def test_persistence_across_connections(self):
        self.db.upsert("fact", "k", "v", evidence={"type": "manual"})
        self.db.close()
        again = MemoryDB(self.root / "m.db", scope="repo:x", root=self.root)
        try:
            self.assertEqual(again.get("fact", "k")["value"], "v")
            self.assertEqual(len(again.search("v")), 1)  # fts survives reopen
        finally:
            again.close()
            self.db = again  # avoid double close in teardown


class MemoryIntegrationTest(unittest.TestCase):
    """A stored fact must actually reach the executor's context, not just the DB."""

    def test_fact_reaches_the_executor_prompt(self):
        from planloop.model import ScriptedModel
        from planloop.scheduler import run
        from planloop.spec import ProblemSpec

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "ws"
            workspace.mkdir()
            mem = MemorySystem(base)
            mem.remember(
                "fact",
                "test_command",
                "python -m unittest discover -s tests -q",
                evidence={"type": "manual"},
                confidence=0.9,
            )
            prompts: list[str] = []

            def handler(prompt: str) -> str:
                prompts.append(prompt)
                return '{"action": "finish", "summary": "looked at the tests"}'

            spec = ProblemSpec.from_dict(
                {
                    "goal": "run the tests and report the suite command",
                    "success_criteria": [{"id": "S1", "kind": "file_exists", "spec": "ok.txt"}],
                    "plan_template": {
                        "nodes": [
                            {
                                "id": "T1",
                                "goal": "run the test suite",
                                "acceptance": [{"id": "A1", "kind": "file_exists", "spec": "ok.txt"}],
                            }
                        ]
                    },
                }
            )
            run(spec, model=ScriptedModel(handler), workspace=workspace, memory_system=mem)
            mem.close()

            joined = "\n".join(prompts)
            self.assertIn("Repository facts", joined)
            self.assertIn("python -m unittest discover", joined)


class WriteDecisionTest(unittest.TestCase):
    """ADD / UPDATE / NOOP / DELETE, and the history that makes them auditable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = MemoryDB(self.root / "m.db", scope="repo:x", root=self.root)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_add_then_update_then_noop(self):
        first = self.db.write("fact", "k", "v1", evidence={"type": "manual"}, reason="initial")
        self.assertEqual(first.op, "ADD")
        second = self.db.write("fact", "k", "v2", evidence={"type": "manual"}, reason="changed my mind")
        self.assertEqual(second.op, "UPDATE")
        self.assertEqual(second.previous, "v1")
        third = self.db.write("fact", "k", "v2", evidence={"type": "manual"})
        self.assertEqual(third.op, "NOOP")

        history = self.db.history(first.id)
        self.assertEqual([h["op"] for h in history], ["ADD", "UPDATE", "NOOP"])
        self.assertEqual(history[1]["old_value"], "v1")
        self.assertEqual(history[1]["new_value"], "v2")
        self.assertEqual(history[1]["reason"], "changed my mind")

    def test_delete_closes_the_validity_window(self):
        mid = self.db.write("fact", "k", "v", evidence={"type": "manual"}).id
        self.assertTrue(self.db.forget(mid, reason="no longer true"))
        self.assertEqual(self.db.search("v"), [])
        ops = [h["op"] for h in self.db.history(mid)]
        self.assertEqual(ops[-1], "DELETE")

    def test_valid_to_hides_the_fact_before_status_changes(self):
        self.db.write("fact", "k", "v", evidence={"type": "manual"}, ttl_days=-1)
        self.assertEqual(self.db.search("v"), [])

    def test_history_survives_reopen(self):
        mid = self.db.write("fact", "k", "v", evidence={"type": "manual"}).id
        self.db.write("fact", "k", "v2", evidence={"type": "manual"})
        self.db.close()
        again = MemoryDB(self.root / "m.db", scope="repo:x", root=self.root)
        try:
            self.assertEqual(len(again.history(mid)), 2)
        finally:
            again.close()
            self.db = again

    def test_procedure_is_executable(self):
        self.db.write(
            "procedure", "hello", "print something",
            evidence={"type": "manual"}, exec_spec='python -c "print(42)"',
        )
        ok, output = self.db.run_procedure("hello")
        self.assertTrue(ok)
        self.assertIn("42", output)
        self.assertGreater(self.db.get("procedure", "hello")["confidence"], 0.35)

    def test_failed_procedure_loses_confidence(self):
        self.db.write("procedure", "bad", "always fails", evidence={"type": "manual"},
                      exec_spec='python -c "import sys; sys.exit(7)"')
        ok, _ = self.db.run_procedure("bad")
        self.assertFalse(ok)
        self.assertLess(self.db.get("procedure", "bad")["confidence"], 0.35)

    def test_export_is_agents_md_shaped(self):
        self.db.write("fact", "test_command", "python -m pytest -q",
                      evidence={"type": "command", "spec": "python -V"})
        self.db.write("procedure", "build", "build it",
                      evidence={"type": "file", "path": "."}, exec_spec="python -m build")
        text = self.db.export_markdown()
        self.assertIn("### Facts", text)
        self.assertIn("**test_command**: python -m pytest -q", text)
        self.assertIn("### Procedures", text)
        self.assertIn("`python -m build`", text)
        self.assertIn("generated by planloop", text)

    def test_export_gate_excludes_unverified_by_default(self):
        """Exported memory becomes instructions, so only machine-verified rows leave."""
        self.db.write("fact", "verified", "true thing", evidence={"type": "command", "spec": "python -V"})
        self.db.write("fact", "attested", "human said so", evidence={"type": "manual"})
        self.db.write("fact", "guessed", "nobody checked")
        default = self.db.export_markdown()
        self.assertIn("true thing", default)
        self.assertNotIn("human said so", default)
        self.assertNotIn("nobody checked", default)

        widened = self.db.export_markdown(tiers=("A", "B", "C", "D"))
        self.assertIn("human said so", widened)
        self.assertIn("nobody checked", widened)


class MemorySystemTest(unittest.TestCase):
    def test_public_and_private_are_separate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mem = MemorySystem(root)
            mem.remember("fact", "secret_path", "/home/me/private", evidence={"type": "manual"})
            mem.remember("fact", "test_command", "python -m pytest -q", public=True,
                         evidence={"type": "manual"})
            public_hits = mem.public.search("secret private path test", limit=5)
            self.assertEqual([h.key for h in public_hits], ["test_command"])
            self.assertTrue((root / ".planloop" / "memory" / "public.db").exists())
            self.assertTrue((root / ".planloop" / "memory" / "private.db").exists())
            mem.close()

    def test_scope_is_repo_specific(self):
        a = repo_scope(Path("C:/a"))
        b = repo_scope(Path("C:/b"))
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("repo:"))


if __name__ == "__main__":
    unittest.main()

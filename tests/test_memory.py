import json
import tempfile
import unittest
from pathlib import Path

from planloop.memory import (
    Lesson,
    MemoryContext,
    MemoryStore,
    Trigger,
    normalize,
    propose_lesson,
    salient_line,
    signature,
)
from planloop.model import Completion, ScriptedModel
from planloop.spec import Criterion

TRACEBACK = """\
Traceback (most recent call last):
  File "C:\\Users\\dev\\repo\\tests\\test_pricing.py", line 7, in test_total
    self.assertEqual(total([100, 200], 10), 270)
AssertionError: 290 != 270

----------------------------------------------------------------------
Ran 1 test in 0.001s

FAILED (failures=1)
"""

IMPORT_ERROR = """\
Traceback (most recent call last):
  File "C:\\Users\\dev\\repo\\shop\\pricing.py", line 1, in <module>
    from shop.helpers import pct
ModuleNotFoundError: No module named 'shop.helpers'
"""


def _ws(tmp: str) -> Path:
    root = Path(tmp) / "ws"
    (root / "shop").mkdir(parents=True)
    (root / "shop" / "pricing.py").write_text("x = 1\n", encoding="utf-8")
    return root


class NormalisationTest(unittest.TestCase):
    def test_paths_and_line_numbers_are_volatile(self):
        a = "File C:\\Users\\a\\repo\\x.py, line 7: boom"
        b = "File C:\\Users\\b\\other.py, line 99: boom"
        na, nb = normalize(a), normalize(b)
        self.assertNotIn("repo", na)
        self.assertEqual(na, nb)

    def test_salient_line_prefers_the_cause_over_the_summary(self):
        self.assertIn("AssertionError", salient_line(TRACEBACK))
        self.assertIn("ModuleNotFoundError", salient_line(IMPORT_ERROR))

    def test_generic_summary_line_is_not_used_as_identity(self):
        generic = "Ran 1 test in 0.001s\n\nFAILED (failures=1)"
        self.assertNotEqual(signature(generic)[0], signature(TRACEBACK)[0])

    def test_same_failure_same_fingerprint(self):
        a = signature(TRACEBACK)[0]
        b = signature(TRACEBACK.replace("0.001s", "0.040s"))[0]
        self.assertEqual(a, b)


class TriggerTest(unittest.TestCase):
    def test_all_sections_must_match(self):
        trigger = Trigger(files=["shop/*.py"], goals=["pricing"])
        ctx = MemoryContext(goal="write the pricing docs", files=["docs/pricing.md"])
        self.assertFalse(trigger.matches(ctx)[0])
        ctx2 = MemoryContext(goal="add pricing export", files=["shop/pricing.py"])
        self.assertTrue(trigger.matches(ctx2)[0])

    def test_error_patterns_only_match_when_error_present(self):
        trigger = Trigger(error_patterns=["ModuleNotFoundError"])
        self.assertFalse(trigger.matches(MemoryContext(goal="g", files=["a.py"]))[0])


class StoreTest(unittest.TestCase):
    def make_lesson(self, **kw) -> Lesson:
        base = dict(
            id="e1",
            signature="ModuleNotFoundError: No module named 'shop.helpers'",
            summary="imported before creating",
            trigger=Trigger(files=["shop/*.py"]),
        )
        base.update(kw)
        return Lesson(**base)

    def test_duplicate_signature_is_a_recurrence_not_a_new_lesson(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.jsonl")
            store.add(self.make_lesson())
            store.add(self.make_lesson())
            self.assertEqual(len(store.lessons), 1)
            self.assertEqual(store.lessons["e1"].recurrences, 1)
            self.assertGreater(store.lessons["e1"].trust, 0.5)

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            store = MemoryStore(path)
            store.add(self.make_lesson(guard={"kind": "command", "spec": "pytest -q"}, strength="guard"))
            again = MemoryStore(path)
            self.assertIn("e1", again.lessons)
            self.assertEqual(again.lessons["e1"].guard["spec"], "pytest -q")
            self.assertTrue(again.lessons["e1"].hard)

    def test_repeat_failure_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.jsonl")
            text = IMPORT_ERROR
            lesson = self.make_lesson(id=signature(text)[0], signature=normalize(salient_line(text)))
            store.add(lesson)
            self.assertTrue(store.note_failure(text))
            self.assertEqual(store.summary()["repeat_failure_rate"], 1.0)
            self.assertFalse(store.note_failure("something entirely different happened"))

    def test_false_positive_decays_trust_until_refuted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.jsonl")
            store.add(self.make_lesson(guard={"kind": "command", "spec": "pytest -q"}, strength="guard", trust=0.7))
            for _ in range(3):
                store.record_guard_false_positive("e1")
            lesson = store.lessons["e1"]
            self.assertEqual(lesson.status, "refuted")
            self.assertFalse(lesson.hard)

    def test_match_is_deterministic_not_fuzzy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.jsonl")
            store.add(self.make_lesson())
            hit = store.match(MemoryContext(goal="touch shop/pricing.py", files=["shop/pricing.py"]))
            miss = store.match(MemoryContext(goal="touch docs", files=["docs/a.md"]))
            self.assertEqual(len(hit), 1)
            self.assertEqual(len(miss), 0)


class LessonRetryTest(unittest.TestCase):
    """A lesson must not degrade to the fallback template because the model
    thought out loud.

    2026-11-11: on a real reasoning model the lesson call sometimes came back
    with **no content** -- the client fell back to the reasoning channel, so
    the reply was prose ("We need answer JSON only. Need infer lesson...")
    with no ``{`` in it.  The JSON parser rejected it and the lesson silently
    became the fallback template: no advice, no trigger, no guard.  The fix is
    one strict retry; the point of this test is that the retry actually
    recovers, and that the failure is still recorded.
    """

    def test_a_prose_first_reply_is_retried_and_the_lesson_survives(self):
        calls = {"n": 0}

        def handler(prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                return "We need answer JSON only. Need infer lesson."
            return json.dumps(
                {
                    "summary": "the tier table was applied with the wrong fee",
                    "cause": "the fee depends on the tier",
                    "fix": "use pct() for every discount",
                    "trigger": {"files": ["shop/*.py"]},
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            model = ScriptedModel(handler, name="scripted")
            lesson = propose_lesson(
                model,
                node_goal="g",
                failure_text=IMPORT_ERROR,
                fix_text="wrote shop/helpers.py",
                workspace=ws,
            )
        assert lesson is not None
        self.assertEqual(calls["n"], 2)
        self.assertEqual(lesson.source, "model")
        self.assertIn("tier table", lesson.summary)
        self.assertEqual(lesson.trigger.files, ["shop/*.py"])
        self.assertTrue(
            any("second attempt" in e for e in lesson.evidence),
            f"the first failure was not recorded: {lesson.evidence}",
        )

    def test_two_prose_replies_fall_back_and_say_so(self):
        def handler(prompt):
            return "still thinking, no json here"

        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            model = ScriptedModel(handler, name="scripted")
            lesson = propose_lesson(
                model,
                node_goal="g",
                failure_text=IMPORT_ERROR,
                fix_text="wrote shop/helpers.py",
                workspace=ws,
            )
        assert lesson is not None
        self.assertEqual(lesson.source, "heuristic")
        self.assertTrue(
            any("lesson model unusable" in e for e in lesson.evidence),
            f"the failure was swallowed: {lesson.evidence}",
        )


class FixExcerptTest(unittest.TestCase):
    """The lesson proposer must see **what the fix looks like**.

    2026-11-11: the proposer used to get "wrote 239 chars to
    shop/helpers.py" and the model's own one-line summary.  With nothing but
    that it **invented** the cause and the fix -- a lesson claimed the fix
    was ``1 - p/100`` for every tier while the file it had just written
    contained the tier table.  Such a lesson is injected into the next task
    with a matching trigger, so a wrong one is worse than none.
    """

    def test_the_written_file_content_is_shown(self):
        from planloop.scheduler import _fix_excerpt

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "shop").mkdir()
            (ws / "shop" / "helpers.py").write_text(
                "def pct(value):\n    fee = 0.05 if value >= 20 else 0.02\n",
                encoding="utf-8",
            )
            text = _fix_excerpt(ws, ["shop/helpers.py"])
        self.assertIn("def pct(value):", text)
        self.assertIn("0.05 if value >= 20", text)
        self.assertIn("after the fix", text)

    def test_no_files_means_no_excerpt(self):
        from planloop.scheduler import _fix_excerpt

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_fix_excerpt(Path(tmp), []), "")
            self.assertEqual(_fix_excerpt(Path(tmp), ["missing.py"]), "")


class GuardValidationTest(unittest.TestCase):
    """A lesson may only carry a guard that already passes in the workspace."""

    def test_failing_guard_is_downgraded_to_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            model = ScriptedModel(
                lambda p: json.dumps(
                    {
                        "summary": "s",
                        "cause": "c",
                        "fix": "f",
                        "trigger": {"files": ["shop/*.py"]},
                        "guard": {"kind": "command", "spec": "exit 3", "must": "exit=0"},
                    }
                ),
                name="scripted",
            )
            lesson = propose_lesson(
                model,
                node_goal="g",
                failure_text=IMPORT_ERROR,
                fix_text="created helpers.py",
                workspace=ws,
            )
            assert lesson is not None
            self.assertEqual(lesson.strength, "advisory")
            self.assertIsNone(lesson.guard)
            self.assertLess(lesson.trust, 0.5)

    def test_passing_guard_is_promoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            model = ScriptedModel(
                lambda p: json.dumps(
                    {
                        "summary": "s",
                        "trigger": {"files": ["shop/*.py"]},
                        "guard": {"kind": "file_exists", "spec": "shop/pricing.py"},
                    }
                )
            )
            lesson = propose_lesson(model, node_goal="g", failure_text=IMPORT_ERROR,
                                    fix_text="f", workspace=ws)
            assert lesson is not None
            self.assertEqual(lesson.strength, "guard")
            self.assertEqual(lesson.criterion().kind, "file_exists")

    def test_no_model_falls_back_to_heuristic_lesson(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _ws(tmp)
            lesson = propose_lesson(None, node_goal="g", failure_text=IMPORT_ERROR,
                                    fix_text="f", workspace=ws)
            assert lesson is not None
            self.assertEqual(lesson.source, "heuristic")
            self.assertFalse(lesson.trigger.empty())


if __name__ == "__main__":
    unittest.main()

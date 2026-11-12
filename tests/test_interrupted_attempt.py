"""An attempt that a policy check cuts short must not vanish from the books.

2026-11-12: in a real 8-rep benchmark run, the second attempt of one
repetition was stopped by the risk policy -- the model ran a command with a
nested command in it.  The exception left the executor, the scheduler blocked
the run, and the attempt's outcome was **never appended** to
``result.outcomes``.  The report counts L1 failures out of that list, so it
published 1 failure and 12 steps while the event log recorded 2 failures and
19 actions.

The two numbers are supposed to be the same number.  So the attempt is now
closed in the log (``node_end`` with the status ``interrupted``) and its
partial outcome is recorded -- the run is still blocked exactly as before,
only the *measurement* stops losing the attempt.
"""

import json
import tempfile
import unittest
from pathlib import Path

from planloop.model import ScriptedModel
from planloop.scheduler import run
from planloop.spec import ProblemSpec


def _spec() -> ProblemSpec:
    return ProblemSpec.from_dict(
        {
            "goal": "g",
            "success_criteria": [{"id": "S1", "kind": "command", "spec": "true"}],
            "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 4},
            "plan_template": {
                "nodes": [
                    {
                        "id": "T1",
                        "goal": "g",
                        "est_cost": 0.2,
                        # never satisfied -> every step records an L1 failure
                        "acceptance": [{"id": "A1", "kind": "file_exists", "spec": "never.txt"}],
                    }
                ]
            },
        }
    )


def _model(replies: list[str]) -> ScriptedModel:
    state = {"i": 0}

    def handler(_prompt: str) -> str:
        text = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return text

    return ScriptedModel(handler, name="cut-short")


class InterruptedAttemptTest(unittest.TestCase):
    """The run is still blocked -- but the attempt is still on the books."""

    def _run(self):
        replies = [
            # passes the risk policy, fails the acceptance -> one L1 failure
            json.dumps({"action": "run", "cmd": "echo one"}),
            # a nested command: the policy asks for approval -> cut short
            json.dumps({"action": "run", "cmd": "echo `nested`"}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "events.jsonl")
            result = run(_spec(), model=_model(replies), workspace=tmp, log_path=log)
            raw = log.read_text(encoding="utf-8")
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return result, events

    def test_the_run_is_still_blocked(self):
        """The fix changes the books, not the outcome of the run."""
        result, _ = self._run()
        self.assertEqual(result.status, "blocked")
        self.assertIsNotNone(result.blocked)

    def test_the_cut_short_attempt_is_recorded(self):
        result, _ = self._run()
        interrupted = [o for o in result.interrupted if o.status == "interrupted"]
        self.assertEqual(
            len(interrupted), 1,
            f"the attempt was dropped: {[(o.status, o.steps) for o in result.interrupted]}",
        )
        self.assertEqual(interrupted[0].steps, 2, "both model turns were spent")
        self.assertEqual(interrupted[0].l1_failures, 1, "the failure it recorded")

    def test_it_is_not_counted_as_a_completed_attempt(self):
        """``outcomes`` means "ran to a verdict" -- the bench counts its length.

        The mutation bench counts the executor runs that completed; an attempt
        the policy cut short never measured the acceptance, so counting it
        there would bias every arm that gets interrupted.
        """
        result, _ = self._run()
        self.assertEqual([o for o in result.outcomes], [],
                         "an interrupted attempt must not look like a finished one")

    def test_the_log_closes_the_attempt(self):
        """A ``node_start`` that no ``node_end`` answers is a hole in the log."""
        _, events = self._run()
        starts = [e for e in events if e["kind"] == "node_start"]
        ends = [e for e in events if e["kind"] == "node_end"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1, "the interrupted attempt was never closed")
        self.assertEqual(ends[0]["data"]["status"], "interrupted")

    def test_the_executor_counts_the_failures_the_log_records(self):
        """``l1_failures`` and the log's ``failure`` events are one number."""
        result, events = self._run()
        failures = [e for e in events if e["kind"] == "failure"]
        counted = sum(o.l1_failures for o in result.outcomes + result.interrupted)
        self.assertEqual(len(failures), counted)
        self.assertGreater(counted, 0, "the test would prove nothing if nothing failed")


if __name__ == "__main__":
    unittest.main()

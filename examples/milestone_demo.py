"""Why milestones exist: every node passes, the product is broken.

    python examples/milestone_demo.py

T1 adds a function with its own targeted test.  T2 "just adds a docstring" --
its targeted check still passes, but it silently changes behaviour.  Only the
milestone's full-suite check catches it.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planloop.model import ScriptedModel
from planloop.scheduler import run
from planloop.spec import ProblemSpec

CALC = "def add(a, b):\n    return a + b\n"
TEST_ADD = (
    "import unittest\n\nfrom calc import add\n\n\n"
    "class AddTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 2), 4)\n"
)
CALC_WITH_MUL = CALC + "\n\ndef mul(a, b):\n    return a * b\n"
TEST_MUL = (
    "import unittest\n\nfrom calc import mul\n\n\n"
    "class MulTest(unittest.TestCase):\n"
    "    def test_mul(self):\n"
    "        self.assertEqual(mul(3, 4), 12)\n"
)
# the "docstring" edit that quietly changes behaviour
CALC_DOCSTRING_BUG = (
    "def add(a, b):\n"
    '    """Return the sum of a and b."""\n'
    "    return a - b\n"
    "\n\ndef mul(a, b):\n"
    "    return a * b\n"
)

SPEC = {
    "goal": "polish calc.py without breaking addition",
    "success_criteria": [{"id": "S1", "kind": "file_contains", "spec": "calc.py", "must": "def mul"}],
    "limits": {"max_attempts_per_node": 1, "max_steps_per_node": 3},
    "plan_template": {
        "milestones": [
            {
                "id": "M1",
                "goal": "the whole suite still passes",
                "acceptance": [{
                    "id": "MA1", "kind": "command",
                    "spec": "python -m unittest discover -s tests -q", "must": "exit=0",
                }],
            }
        ],
        "nodes": [
            {
                "id": "T1", "milestone": "M1", "goal": "add mul()",
                "acceptance": [{
                    "id": "A1", "kind": "command",
                    "spec": 'python -m unittest discover -s tests -p "test_mul.py" -q', "must": "exit=0",
                }],
            },
            {
                "id": "T2", "milestone": "M1", "deps": ["T1"], "goal": "add a docstring to add()",
                "acceptance": [{"id": "A2", "kind": "file_contains", "spec": "calc.py", "must": '"""'}],
            },
        ],
    },
}


def handler(prompt: str) -> str:
    key = "T1" if "# Current node T1" in prompt else "T2"
    n = handler.calls.get(key, 0)
    handler.calls[key] = n + 1
    if key == "T1":
        if n == 0:
            return json.dumps({"action": "write", "path": "calc.py", "content": CALC_WITH_MUL})
        return json.dumps({"action": "write", "path": "tests/test_mul.py", "content": TEST_MUL})
    if n == 0:
        return json.dumps({"action": "write", "path": "calc.py", "content": CALC_DOCSTRING_BUG})
    return '{"action": "finish", "summary": "added a docstring"}'


handler.calls = {}

with tempfile.TemporaryDirectory() as tmp:
    ws = Path(tmp) / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "calc.py").write_text(CALC, encoding="utf-8")
    (ws / "tests" / "test_add.py").write_text(TEST_ADD, encoding="utf-8")

    result = run(ProblemSpec.from_dict(SPEC), model=ScriptedModel(handler), workspace=ws, auto_approve=True)

    print("== result ==")
    print(result.render())
    print("\n== what the milestone saw ==")
    if result.blocked:
        print(result.blocked.needed)

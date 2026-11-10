"""Generate a family of rename-chain tasks (same shape, increasing call-site count)."""

import pathlib

p = pathlib.Path("planloop/bench.py")
s = p.read_text(encoding="utf-8")

GENERATOR = '''

def make_rename_chain(index: int, *, callers: int) -> Task:
    """A rename that must propagate to N call sites, each covered by a test.

    Same failure mode every time: the model renames the definition and misses
    one caller. The number of call sites is the knob, so difficulty scales.
    """
    module = f"chain{index}"
    initial: dict[str, str] = {
        f"{module}/__init__.py": "",
        f"{module}/core.py": "def total(items):\\n    return sum(items)\\n",
        "tests/__init__.py": "",
        f"tests/test_core.py": (
            "import unittest\\n"
            f"from {module} import total\\n\\n\\n"
            "class T(unittest.TestCase):\\n"
            "    def test_total(self):\\n"
            "        self.assertEqual(total([1, 2]), 3)\\n"
        ),
    }
    correct = {
        f"{module}/core.py": "def compute_total(items):\\n    return sum(items)\\n",
        f"{module}/__init__.py": (
            "from .core import compute_total\\n\\ntotal = compute_total  # deprecated alias\\n"
        ),
    }
    wrong = dict(correct)

    for i in range(callers):
        caller = f"{module}/c{i}.py"
        test = f"tests/test_c{i}.py"
        initial[caller] = (
            f"from {module}.core import total\\n\\n\\ndef use{i}(items):\\n    return total(items) + {i}\\n"
        )
        initial[test] = (
            "import unittest\\n"
            f"from {module}.c{i} import use{i}\\n\\n\\n"
            "class T(unittest.TestCase):\\n"
            f"    def test_use{i}(self):\\n"
            f"        self.assertEqual(use{i}([1, 2]), {3 + i})\\n"
        )
        correct[caller] = (
            f"from {module}.core import compute_total\\n\\n\\ndef use{i}(items):\\n"
            f"    return compute_total(items) + {i}\\n"
        )

    # the trap: the last caller is left on the old name
    missed = f"{module}/c{callers - 1}.py"
    wrong[missed] = initial[missed]

    return Task(
        key=f"rename_chain_{callers}",
        goal=f"把 {module}.core.total 改名为 compute_total，更新全部 {callers} 个调用点，"
             f"兼容别名只放在 {module}/__init__.py",
        initial=initial,
        correct=correct,
        wrong=wrong,
        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
        plan=[
            {
                "id": "T1",
                "goal": f"改名 {module}/core.py 并更新 {module}/__init__.py 的别名",
                "est_cost": 0.2,
                "context_files": [
                    f"{module}/core.py",
                    f"{module}/__init__.py",
                    "tests/test_core.py",
                ],
                "acceptance": [{"id": "A1", "kind": "command",
                                "spec": f"{PY} -m unittest {module.replace('/', '.')} tests.test_core -q",
                                "must": "exit=0"}],
            },
            {
                "id": "T2",
                "deps": ["T1"],
                "goal": f"更新全部 {callers} 个调用点，让全量测试通过",
                "est_cost": 0.7,
                "context_files": [f"{module}/c{i}.py" for i in range(callers)]
                + [f"{module}/core.py", f"{module}/__init__.py"],
                "acceptance": [{"id": "A2", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
            },
        ],
    )


TASKS.extend(make_rename_chain(i, callers=n) for i, n in enumerate((3, 4, 5, 6, 7, 8)))
'''

s = s.rstrip() + "\n" + GENERATOR
p.write_text(s, encoding="utf-8")
print("generator appended")

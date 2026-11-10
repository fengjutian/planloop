import pathlib

from planloop.bench import FULL_SUITE

FULL_SUITE = '"python" -m unittest discover -s tests -q'

p = pathlib.Path("planloop/bench.py")
s = p.read_text(encoding="utf-8")

CALLERS = {
    "ledger/cart.py": (
        "from ledger.core import total\n\n\ndef cart_total(items):\n    return total(items)\n"
    ),
    "ledger/checkout.py": (
        "from ledger.core import total\n\n\ndef checkout(items, pct):\n"
        "    return total(items) * (100 - pct) / 100\n"
    ),
    "ledger/report.py": (
        "from ledger.core import total\n\n\ndef summary(items):\n"
        '    return f"total={total(items)}"\n'
    ),
    "ledger/admin.py": (
        "from ledger.core import total\n\n\ndef audit(items):\n    return total(items) > 0\n"
    ),
    "ledger/export.py": (
        "from ledger.core import total\n\n\ndef as_row(items):\n    return [str(total(items))]\n"
    ),
}

CALLERS_FIXED = {
    path: body.replace("from ledger.core import total", "from ledger.core import compute_total")
                .replace("total(items)", "compute_total(items)")
    for path, body in CALLERS.items()
}

initial = {"ledger/__init__.py": ""}
initial["ledger/core.py"] = "def total(items):\n    return sum(items)\n"
initial.update(CALLERS)
initial["tests/__init__.py"] = ""
initial["tests/test_core.py"] = (
    "import unittest\nfrom ledger.core import total\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_total(self):\n"
    "        self.assertEqual(total([1, 2]), 3)\n"
)
initial["tests/test_checkout.py"] = (
    "import unittest\nfrom ledger.checkout import checkout\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_checkout(self):\n"
    "        self.assertEqual(checkout([100, 200], 10), 270)\n"
)
initial["tests/test_report.py"] = (
    "import unittest\nfrom ledger.export import as_row\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_export(self):\n"
    "        self.assertEqual(as_row([5]), ['5'])\n"
)

correct = dict(CALLERS_FIXED)
correct["ledger/core.py"] = (
    "def compute_total(items):\n    return sum(items)\n\n\n"
    "total = compute_total  # deprecated alias, kept for compatibility\n"
)

# the plausible mistake: update four of five call sites
wrong = dict(CALLERS_FIXED)
wrong.pop("ledger/admin.py")
wrong["ledger/admin.py"] = CALLERS["ledger/admin.py"]
wrong["ledger/core.py"] = correct["ledger/core.py"]

PLAN = [
    {
        "id": "T1",
        "goal": "把 ledger/core.py 的 total 改名为 compute_total，并保留 total 别名",
        "est_cost": 0.3,
        "acceptance": [
            {"id": "A1", "kind": "command",
             "spec": f'{FULL_SUITE} tests.test_core', "must": "exit=0"},
        ],
    },
    {
        "id": "T2",
        "deps": ["T1"],
        "goal": "更新所有引用 total 的调用点，让全量测试通过",
        "est_cost": 0.6,
        "acceptance": [{"id": "A2", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],
    },
]

TASK = (
    '    Task(\n'
    '        key="long_rename_chain",\n'
    '        goal="把 ledger.core.total 改名为 compute_total，更新全部 5 个调用点，并保留 total 兼容别名",\n'
    f"        initial={initial!r},\n"
    f"        correct={correct!r},\n"
    f"        wrong={wrong!r},\n"
    '        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],\n'
    f"        plan={PLAN!r},\n"
    '    ),\n'
)

marker = "]"
idx = s.rindex(marker)
s = s[:idx] + TASK + s[idx:]
p.write_text(s, encoding="utf-8")
print("task appended")

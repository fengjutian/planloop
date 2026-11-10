"""Rewrite the long-chain task so a missed call site actually fails.

Previous version kept `total` as an alias inside core.py, so a stale
`from ledger.core import total` kept working and the mistake was invisible.
Now the alias lives in `ledger/__init__.py` only, so a missed call site raises
ImportError.
"""

import pathlib

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

FIXED = {
    "ledger/cart.py": "from ledger.core import compute_total\n\n\ndef cart_total(items):\n    return compute_total(items)\n",
    "ledger/checkout.py": "from ledger.core import compute_total\n\n\ndef checkout(items, pct):\n    return compute_total(items) * (100 - pct) / 100\n",
    "ledger/report.py": "from ledger.core import compute_total\n\n\ndef summary(items):\n    return f\"total={compute_total(items)}\"\n",
    "ledger/admin.py": "from ledger.core import compute_total\n\n\ndef audit(items):\n    return compute_total(items) > 0\n",
    "ledger/export.py": "from ledger.core import compute_total\n\n\ndef as_row(items):\n    return [str(compute_total(items))]\n",
}

initial = {
    "ledger/__init__.py": "",
    "ledger/core.py": "def total(items):\n    return sum(items)\n",
    **CALLERS,
    "tests/__init__.py": "",
    "tests/test_core.py": (
        "import unittest\nfrom ledger import total\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_total(self):\n"
        "        self.assertEqual(total([1, 2]), 3)\n"
    ),
    "tests/test_checkout.py": (
        "import unittest\nfrom ledger.checkout import checkout\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_checkout(self):\n"
        "        self.assertEqual(checkout([100, 200], 10), 270)\n"
    ),
    "tests/test_report.py": (
        "import unittest\nfrom ledger.export import as_row\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_export(self):\n"
        "        self.assertEqual(as_row([5]), ['5'])\n"
    ),
    "tests/test_admin.py": (
        "import unittest\nfrom ledger.admin import audit\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_audit(self):\n"
        "        self.assertTrue(audit([5]))\n"
    ),
}

CORE_FIXED = "def compute_total(items):\n    return sum(items)\n"
INIT_FIXED = "from ledger.core import compute_total\n\ntotal = compute_total  # deprecated alias\n"

correct = dict(FIXED, **{"ledger/core.py": CORE_FIXED, "ledger/__init__.py": INIT_FIXED})
wrong = dict(correct)
wrong["ledger/admin.py"] = CALLERS["ledger/admin.py"]  # stale import -> ImportError

TASK_TEXT = (
    "    Task(\n"
    '        key="long_rename_chain",\n'
    '        goal="把 ledger.core.total 改名为 compute_total，更新全部 5 个调用点；'
    '兼容别名只放在 ledger/__init__.py",\n'
    f"        initial={initial!r},\n"
    f"        correct={correct!r},\n"
    f"        wrong={wrong!r},\n"
    '        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],\n'
    "        plan=[\n"
    '            {"id": "T1", "goal": "改名 core 并把别名放进 ledger/__init__.py", "est_cost": 0.3,\n'
    '             "acceptance": [{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}]},\n'
    '            {"id": "T2", "deps": ["T1"], "goal": "更新全部 5 个调用点，让全量测试通过",\n'
    '             "est_cost": 0.6,\n'
    '             "acceptance": [{"id": "A2", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}]},\n'
    "        ],\n"
    "    ),\n"
)

start = s.index('    Task(\n        key="long_rename_chain"')
end = s.index("\n    ),\n", start) + len("\n    ),\n")
s = s[:start] + TASK_TEXT + s[end:]
p.write_text(s, encoding="utf-8")
print("long task rewritten")

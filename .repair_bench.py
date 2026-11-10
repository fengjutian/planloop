"""Repair bench.py after a bad textual insertion, and add the long-chain task."""

import pathlib
import re

p = pathlib.Path("planloop/bench.py")
s = p.read_text(encoding="utf-8")

# 1) remove the task text that was spliced into the print statement
start = s.index('f"[{outcome.config}')
end_marker = '] {outcome.task}: success={outcome.success} "'
end = s.index(end_marker) + len(end_marker)
s = s[:start] + 'f"[{outcome.config}] {outcome.task}: success={outcome.success} "' + s[end:]

# 2) build the long-chain task and insert it into TASKS
CALLERS = {
    "ledger/cart.py": "from ledger.core import total\n\n\ndef cart_total(items):\n    return total(items)\n",
    "ledger/checkout.py": "from ledger.core import total\n\n\ndef checkout(items, pct):\n    return total(items) * (100 - pct) / 100\n",
    "ledger/report.py": "from ledger.core import total\n\n\ndef summary(items):\n    return f\"total={total(items)}\"\n",
    "ledger/admin.py": "from ledger.core import total\n\n\ndef audit(items):\n    return total(items) > 0\n",
    "ledger/export.py": "from ledger.core import total\n\n\ndef as_row(items):\n    return [str(total(items))]\n",
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
        "import unittest\nfrom ledger.core import total\n\n\n"
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
}

CORE_FIXED = (
    "def compute_total(items):\n    return sum(items)\n\n\n"
    "total = compute_total  # deprecated alias, kept for compatibility\n"
)

correct = dict(FIXED, **{"ledger/core.py": CORE_FIXED})
wrong = dict(FIXED, **{"ledger/core.py": CORE_FIXED})
wrong["ledger/admin.py"] = CALLERS["ledger/admin.py"]  # missed one call site

TASK_TEXT = (
    "    Task(\n"
    '        key="long_rename_chain",\n'
    '        goal="把 ledger.core.total 改名为 compute_total，更新全部 5 个调用点，并保留 total 兼容别名",\n'
    f"        initial={initial!r},\n"
    f"        correct={correct!r},\n"
    f"        wrong={wrong!r},\n"
    '        acceptance=[{"id": "A1", "kind": "command", "spec": FULL_SUITE, "must": "exit=0"}],\n'
    "        plan=[\n"
    "            {\"id\": \"T1\", \"goal\": \"把 core.py 的 total 改名为 compute_total 并保留别名\",\n"
    "             \"est_cost\": 0.3,\n"
    "             \"acceptance\": [{\"id\": \"A1\", \"kind\": \"command\",\n"
    "                             \"spec\": FULL_SUITE, \"must\": \"exit=0\"}]},\n"
    "            {\"id\": \"T2\", \"deps\": [\"T1\"], \"goal\": \"更新全部调用点，让全量测试通过\",\n"
    "             \"est_cost\": 0.6,\n"
    "             \"acceptance\": [{\"id\": \"A2\", \"kind\": \"command\",\n"
    "                             \"spec\": FULL_SUITE, \"must\": \"exit=0\"}]},\n"
    "        ],\n"
    "    ),\n"
)

# insert right before the TASKS list closes
marker = "\n]\n\n\n# --------------------------------------------------------------------------\n# the scripted model"
if marker not in s:
    raise SystemExit("cannot find TASKS closing marker")
s = s.replace(marker, "\n" + TASK_TEXT + "]\n\n\n# --------------------------------------------------------------------------\n# the scripted model", 1)

p.write_text(s, encoding="utf-8")
print("repaired and task added")

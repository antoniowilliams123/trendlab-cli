"""Benchmark fixtures A–E (build plan §47): small repositories with known defects, materialized
into a temp directory on demand so the package ships no loose files."""

from __future__ import annotations

from pathlib import Path

FIXTURES: dict[str, dict[str, str]] = {
    "A": {  # single-file arithmetic bug
        "_prompt": "Run the tests, find the bug in the order total calculation, fix it, and make the tests pass.",
        "orders.py": "def order_total(items):\n    return sum(i['price'] * i['qty'] for i in items) - 1\n",
        "test_orders.py": "from orders import order_total\n\ndef test_total():\n    assert order_total([{'price': 2.0, 'qty': 3}]) == 6.0\n",
    },
    "B": {  # multi-file API bug
        "_prompt": "The API handler returns the wrong status for missing users. Fix it and make the tests pass.",
        "store.py": "USERS = {1: 'ann'}\n\ndef get_user(uid):\n    return USERS.get(uid)\n",
        "api.py": "from store import get_user\n\ndef handle(uid):\n    user = get_user(uid)\n    if user is None:\n        return 200, 'not found'\n    return 200, user\n",
        "test_api.py": "from api import handle\n\ndef test_found():\n    assert handle(1) == (200, 'ann')\n\ndef test_missing():\n    assert handle(9) == (404, 'not found')\n",
    },
    "C": {  # broken import
        "_prompt": "The tests fail with an import error. Fix the import without changing behaviour.",
        "utils/__init__.py": "",
        "utils/text.py": "def shout(s):\n    return s.upper() + '!'\n",
        "main.py": "from utils.txt import shout\n\ndef run():\n    return shout('hi')\n",
        "test_main.py": "from main import run\n\ndef test_run():\n    assert run() == 'HI!'\n",
    },
    "D": {  # incorrect edge case requiring a new test
        "_prompt": "safe_div crashes on zero divisors; it should return None. Fix it and add a regression test.",
        "mathx.py": "def safe_div(a, b):\n    return a / b\n",
        "test_mathx.py": "from mathx import safe_div\n\ndef test_basic():\n    assert safe_div(6, 3) == 2\n",
    },
    "E": {  # refactor with tests
        "_prompt": "Refactor greet() to use an f-string and keep all tests passing.",
        "greet.py": "def greet(name):\n    return 'Hello, ' + name + '!'\n",
        "test_greet.py": "from greet import greet\n\ndef test_greet():\n    assert greet('Tony') == 'Hello, Tony!'\n",
    },
}

EXPECTED_CHANGED = {
    "A": {"orders.py"},
    "B": {"api.py"},
    "C": {"main.py"},
    "D": {"mathx.py", "test_mathx.py"},
    "E": {"greet.py"},
}


def materialize(name: str, dest: Path) -> str:
    spec = FIXTURES[name]
    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in spec.items():
        if rel.startswith("_"):
            continue
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (dest / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8"
    )
    return spec["_prompt"]

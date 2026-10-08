"""Merge docs/ratings/part*.py into docs/ratings/ledger.json."""

import importlib.util
import json
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "docs" / "ratings"
ledger = {}
for p in sorted(D.glob("part*.py")):
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for n, r, ev in mod.R:
        assert str(n) not in ledger, f"duplicate {n}"
        ledger[str(n)] = {"rating": r, "evidence": ev}
(D / "ledger.json").write_text(json.dumps(ledger, indent=1, ensure_ascii=False))
print(len(ledger), "entries")

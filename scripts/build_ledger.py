"""Merge docs/ratings/part*.py into docs/ratings/ledger.json.

Parts are applied in order; a later part re-rates a term from an earlier one (the evidence
changed). A term may appear only once within a part."""

import importlib.util
import json
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "docs" / "ratings"
ledger = {}
# numeric order: part10 comes after part9 (a plain sort would put it after part1)
for p in sorted(D.glob("part*.py"), key=lambda f: int(f.stem.removeprefix("part"))):
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = set()
    for n, r, ev in mod.R:
        assert n not in seen, f"duplicate {n} in {p.name}"
        seen.add(n)
        ledger[str(n)] = {"rating": r, "evidence": ev}
(D / "ledger.json").write_text(json.dumps(ledger, indent=1, ensure_ascii=False))
print(len(ledger), "entries")

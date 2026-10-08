"""Memory eval (uplift U30): does learning keep what lasts and drop what does not?

Each case is a run transcript holding durable facts (how to test, conventions, things the user
insisted on, pitfalls) and ephemeral ones (this run's narration, a one-off error, a timestamp).
The learning prompt extracts facts; a durable fact counts as kept when a learned line matches
its pattern, an ephemeral one counts as leaked when a learned line matches its pattern.
"""

from __future__ import annotations

import json
import re
from typing import Any

from trendlab.context.project_memory import LEARN_PROMPT, parse_facts

CASES = [
    {
        "transcript": [
            ("user", "fix the discount rounding"),
            (
                "assistant",
                "Ran `make test` — this repo has no pytest config; tests only run via make test.",
            ),
            ("assistant", "At 14:02 the first run failed with 3 errors."),
            ("user", "money must always be Decimal, never float — remember that"),
            ("assistant", "Changed apply_discount to Decimal; make test now passes."),
        ],
        "durable": {"make test": r"make test", "Decimal": r"decimal"},
        "ephemeral": {"14:02": r"14:02|3 errors"},
    },
    {
        "transcript": [
            ("user", "add the export endpoint"),
            (
                "assistant",
                "Found that generated/ is produced by `npm run codegen` and must not be edited by hand.",
            ),
            ("assistant", "I first edited generated/api.ts by mistake and reverted it."),
            ("user", "always run `npm run lint -- --fix` before you finish"),
            ("assistant", "Endpoint added; lint and tests pass."),
        ],
        "durable": {"codegen": r"codegen|generated", "lint fix": r"lint"},
        "ephemeral": {"mistake": r"by mistake|reverted"},
    },
    {
        "transcript": [
            ("user", "why is CI red?"),
            (
                "assistant",
                "CI uses Python 3.11; the local venv is 3.12, so the walrus-in-comprehension issue only shows in CI.",
            ),
            ("assistant", "The failing job id was 88123."),
            (
                "assistant",
                "Database tests need `docker compose up db` first or they hang for 10 minutes.",
            ),
        ],
        "durable": {"python 3.11": r"3\.11", "docker db": r"docker compose up db|docker"},
        "ephemeral": {"job id": r"88123"},
    },
    {
        "transcript": [
            ("user", "rename the config loader"),
            ("assistant", "Settings live in config/settings.toml; loader is app/config.py."),
            ("assistant", "Renamed load() to load_settings() in 4 files today."),
            ("user", "don't touch the migrations folder, ever"),
        ],
        "durable": {"settings path": r"settings\.toml|app/config\.py", "migrations": r"migration"},
        "ephemeral": {"renamed today": r"renamed|4 files|today"},
    },
]


async def run(call) -> dict[str, Any]:
    kept = durable = leaked = ephemeral = 0
    rows = []
    for case in CASES:
        transcript = "\n".join(f"{r}: {t}" for r, t in case["transcript"])
        evidence = json.dumps(
            {"changed_files": ["x"], "validation_runs": [], "status": "COMPLETED"}
        )
        prompt = LEARN_PROMPT.format(existing="(empty)", evidence=evidence, transcript=transcript)
        facts = parse_facts(await call([{"role": "user", "content": prompt}]))
        text = "\n".join(facts).lower()
        k = [n for n, pat in case["durable"].items() if re.search(pat, text, re.I)]
        lk = [n for n, pat in case["ephemeral"].items() if re.search(pat, text, re.I)]
        kept += len(k)
        durable += len(case["durable"])
        leaked += len(lk)
        ephemeral += len(case["ephemeral"])
        rows.append({"facts": facts, "kept": k, "leaked": lk})
    return {
        "durable_recall": round(kept / durable, 3),
        "ephemeral_leak": round(leaked / ephemeral, 3),
        "rows": rows,
    }

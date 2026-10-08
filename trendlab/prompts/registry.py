"""Registry of the harness's own prompt templates (uplift U28).

Every prompt the harness sends on its own behalf is listed with the eval that measures it.
``docs/prompts.lock.json`` records each template's hash: editing a prompt changes the hash,
``trendlab prompts --check`` (and the test suite) report it, and the change is accepted with
``trendlab prompts --lock`` after the named eval has been re-run.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

# name -> (module.attribute, the eval that measures it)
PROMPTS: dict[str, tuple[str, str]] = {
    "router": (
        "trendlab.agent.router.PROMPT",
        "router held-out set (tests/data/router_holdout.jsonl)",
    ),
    "planner": ("trendlab.agent.planner.PLANNER_PROMPT", "bench --planning-eval"),
    "verifier": (
        "trendlab.agent.verifier.PROMPT",
        "bench --suite (judge precision/recall), determinism_eval",
    ),
    "judge_pairwise": ("trendlab.agent.judge.PAIRWISE_PROMPT", "trendlab judge-bias"),
    "review_lens": ("trendlab.agent.review.PROMPT", "bench --review-eval --review-mode deep"),
    "review_quick": ("trendlab.agent.review.QUICK", "bench --review-eval --review-mode quick"),
    "review_confirm": (
        "trendlab.agent.review.CONFIRM",
        "bench --review-eval (noise per correct fix)",
    ),
    "review_recheck": ("trendlab.agent.review.RECHECK", "tests/test_review.py"),
    "critic": ("trendlab.agent.critique.CRITIC", "trendlab critique --eval [--hard]"),
    "adversary": ("trendlab.agent.critique.ADVERSARY", "trendlab critique --eval [--hard]"),
    "spec_extract": ("trendlab.agent.spec.EXTRACT", "trendlab spec --eval"),
    "spec_check": ("trendlab.agent.spec.CHECK", "trendlab spec --eval"),
    "screener": ("trendlab.tools.views.screener.PROMPT", "tests/test_tiered_output.py"),
    "memory_learn": (
        "trendlab.context.project_memory.LEARN_PROMPT",
        "tests/test_project_memory.py",
    ),
    "sleeptime": ("trendlab.engine.sleep.PROMPT", "tests/test_engine.py"),
    "drafter": ("trendlab.engine.drafter.PROMPT", "tests/test_engine.py"),
    "redteam": ("trendlab.benchmarks.redteam.PROMPT", "trendlab redteam (scanner recall)"),
}
LOCK = Path(__file__).resolve().parent.parent.parent / "docs" / "prompts.lock.json"


def text_of(ref: str) -> str:
    module, _, attr = ref.rpartition(".")
    return str(getattr(importlib.import_module(module), attr))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def table() -> list[dict[str, Any]]:
    locked = json.loads(LOCK.read_text()) if LOCK.is_file() else {}
    out = []
    for name, (ref, evaluation) in PROMPTS.items():
        text = text_of(ref)
        h = digest(text)
        out.append(
            {
                "name": name,
                "ref": ref,
                "hash": h,
                "tokens": len(text) // 4,
                "eval": evaluation,
                "changed": name in locked and locked[name] != h,
                "new": name not in locked,
            }
        )
    return out


def lock() -> dict[str, str]:
    hashes = {row["name"]: row["hash"] for row in table()}
    LOCK.write_text(json.dumps(hashes, indent=1, sort_keys=True) + "\n")
    return hashes

"""Compaction eval (uplift U22): do the facts an agent needs survive context compaction?

Each synthetic transcript plants facts of the kinds the compaction prompt promises to keep —
a user decision, an exact error, a failed approach, a validation result, a file finding, the
next action — and buries them in long tool output. The transcript is compacted (model
summarizer, or the deterministic fallback), then a model answers one question per fact from
the compacted context alone (summary + the last messages). Retention = answers that contain the
fact; the full-context answer rate is the ceiling.
"""

from __future__ import annotations

import json
import random
from typing import Any

from trendlab.context.compaction import compaction_prompt, deterministic_summary

KEEP_LAST = 4

SCENARIOS = [
    {
        "task": "Fix the checkout total that ignores quantities.",
        "facts": [
            (
                "decision",
                "The user said never edit files under legacy/.",
                "Which directory must never be edited?",
                ["legacy"],
            ),
            (
                "error",
                "Test failure: AssertionError: 30.0 != 90.0 in test_order_total.",
                "What exact assertion failed?",
                ["30.0", "90.0"],
            ),
            (
                "failed",
                "Tried rounding in with_tax(); it did not fix the total.",
                "What approach was tried and did not work?",
                ["with_tax", "round"],
            ),
            (
                "validation",
                "Last run: 3 failed, 41 passed.",
                "What was the last test result?",
                ["3 failed", "41 passed"],
            ),
            (
                "finding",
                "order_subtotal in shop/orders.py line 12 sums price without qty.",
                "Which function and file hold the bug?",
                ["order_subtotal", "orders.py"],
            ),
            (
                "next",
                "Next: multiply by item qty in order_subtotal, then rerun pytest.",
                "What is the next action?",
                ["qty", "order_subtotal"],
            ),
        ],
    },
    {
        "task": "Move the API server to the new port and keep TLS.",
        "facts": [
            (
                "decision",
                "User decision: the new port is 8443 and TLS stays on.",
                "Which port was chosen?",
                ["8443"],
            ),
            (
                "error",
                "Startup error: OSError: [Errno 98] Address already in use.",
                "What error did the server raise at startup?",
                ["Errno 98", "already in use"],
            ),
            (
                "failed",
                "Restarting nginx did not free the port.",
                "What was tried that did not help?",
                ["nginx"],
            ),
            (
                "validation",
                "curl https://localhost:8443/health returned 200 after the change.",
                "What did the health check return?",
                ["200"],
            ),
            (
                "finding",
                "The port is hard-coded in config/server.toml under [listen].",
                "Where is the port configured?",
                ["server.toml"],
            ),
            (
                "next",
                "Next: update the systemd unit api.service and reload it.",
                "What is the next action?",
                ["api.service"],
            ),
        ],
    },
    {
        "task": "Speed up the nightly report job.",
        "facts": [
            (
                "decision",
                "The user said the report must stay in CSV, not Parquet.",
                "Which output format must the report keep?",
                ["csv"],
            ),
            (
                "error",
                "Profiler: build_rows() takes 41.2 s of the 44 s run.",
                "Which function dominates the runtime and how long does it take?",
                ["build_rows", "41.2"],
            ),
            (
                "failed",
                "Adding an index on orders.created_at made no difference.",
                "Which optimisation made no difference?",
                ["index", "created_at"],
            ),
            (
                "validation",
                "After batching, the job runs in 6.8 s.",
                "How long does the job take after the fix?",
                ["6.8"],
            ),
            (
                "finding",
                "build_rows issues one SQL query per customer (N+1) in reports/nightly.py.",
                "What causes the slowness?",
                ["n+1", "one sql query per customer", "per customer"],
            ),
            (
                "next",
                "Next: add a test that the CSV columns did not change.",
                "What is the next action?",
                ["test", "column"],
            ),
        ],
    },
    {
        "task": "Make login lock accounts after repeated failures.",
        "facts": [
            (
                "decision",
                "User decision: lock after 5 failed attempts for 15 minutes.",
                "What are the lockout rules?",
                ["5", "15"],
            ),
            (
                "error",
                "KeyError: 'failed_attempts' in auth/session.py.",
                "What exact error occurred?",
                ["keyerror", "failed_attempts"],
            ),
            (
                "failed",
                "Storing the counter in a module-level dict failed across workers.",
                "What storage approach failed and why?",
                ["module-level", "dict", "worker"],
            ),
            (
                "validation",
                "test_lockout passes; test_unlock_after_timeout still fails.",
                "Which test still fails?",
                ["test_unlock_after_timeout"],
            ),
            (
                "finding",
                "Counters must live in Redis key login:fail:<user>.",
                "Where must the counters be stored?",
                ["redis"],
            ),
            (
                "next",
                "Next: implement the unlock timer with a Redis TTL.",
                "What is the next action?",
                ["ttl"],
            ),
        ],
    },
    {
        "task": "Upgrade the payments SDK to v5.",
        "facts": [
            (
                "decision",
                "The user said do not upgrade Python itself; stay on 3.11.",
                "Which Python version must be kept?",
                ["3.11"],
            ),
            (
                "error",
                "ImportError: cannot import name 'Charge' from 'payments'.",
                "What import error appeared?",
                ["charge"],
            ),
            (
                "failed",
                "Pinning payments==5.0.0 still raised the ImportError.",
                "What pin was tried without success?",
                ["5.0.0"],
            ),
            (
                "validation",
                "Unit tests: 120 passed, 0 failed after the rename.",
                "What was the final test result?",
                ["120"],
            ),
            (
                "finding",
                "v5 renamed Charge to PaymentIntent in billing/pay.py.",
                "What was renamed in v5?",
                ["paymentintent"],
            ),
            (
                "next",
                "Next: update the webhook handler to the v5 event names.",
                "What is the next action?",
                ["webhook"],
            ),
        ],
    },
]


def transcript(scenario: dict[str, Any], *, filler_blocks: int = 14, seed: int = 0) -> list[dict]:
    """A plausible agent transcript: the facts appear once, in order, among long tool output."""
    rng = random.Random(seed)
    msgs: list[dict[str, Any]] = [{"role": "user", "content": scenario["task"]}]
    facts = list(scenario["facts"])
    for i in range(filler_blocks):
        call_id = f"c{i}"
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": f"src/m{i}.py"}),
                        },
                    }
                ],
            }
        )
        noise = "\n".join(
            f"{n}: x{rng.randint(0, 9999)} = compute(y{n}, z{n})  # unrelated" for n in range(60)
        )
        msgs.append(
            {"role": "tool", "tool_call_id": call_id, "name": "read_file", "content": noise}
        )
        if facts and i % 2 == 1:
            kind, text, _q, _k = facts.pop(0)
            role = "user" if kind == "decision" else "assistant"
            msgs.append({"role": role, "content": text})
    for _kind, text, _q, _k in facts:  # anything left goes near the end
        msgs.append({"role": "assistant", "content": text})
    msgs.append({"role": "assistant", "content": "Working on it."})
    msgs += [
        {"role": "user", "content": "ok, continue"},
        {"role": "assistant", "content": "Continuing."},
    ]
    return msgs


def _render(msgs: list[dict]) -> str:
    out = []
    for m in msgs:
        c = m.get("content") or ""
        if m.get("tool_calls"):
            c += " CALL " + m["tool_calls"][0]["function"]["name"]
        out.append(f"[{m['role']}] {c}")
    return "\n".join(out)


QA = """Answer the question from the context only. Quote exact names, numbers and error text.
If the context does not contain the answer, reply exactly: unknown.

## Context
{context}

## Question
{question}
"""


async def run(summarize, answer, *, method: str = "model") -> dict[str, Any]:
    """``summarize(messages) -> str`` and ``answer(messages) -> str`` are model callers."""
    rows = []
    tokens_before = tokens_after = 0
    for i, sc in enumerate(SCENARIOS):
        msgs = transcript(sc, seed=i)
        old, recent = msgs[:-KEEP_LAST], msgs[-KEEP_LAST:]
        full_ctx = _render(msgs)
        if method == "full":
            ctx = full_ctx
        else:
            structured = {"task": sc["task"]}
            if method == "model":
                summary = await summarize(compaction_prompt(old, structured))
            else:
                summary = deterministic_summary(old, structured)
            ctx = f"SUMMARY OF EARLIER WORK:\n{summary}\n\nRECENT:\n{_render(recent)}"
        tokens_before += len(full_ctx) // 4
        tokens_after += len(ctx) // 4
        for kind, _text, question, keys in sc["facts"]:
            reply = await answer(
                [{"role": "user", "content": QA.format(context=ctx[:120_000], question=question)}]
            )
            low = reply.lower()
            kept = any(k.lower() in low for k in keys)
            rows.append({"scenario": i, "kind": kind, "kept": kept, "reply": reply[:200]})
    by_kind: dict[str, list[bool]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r["kept"])
    return {
        "method": method,
        "facts": len(rows),
        "retention": round(sum(r["kept"] for r in rows) / len(rows), 3),
        "by_kind": {k: round(sum(v) / len(v), 2) for k, v in by_kind.items()},
        "compression": round(tokens_after / tokens_before, 3) if tokens_before else None,
        "lost": [f"s{r['scenario']}:{r['kind']}" for r in rows if not r["kept"]],
    }

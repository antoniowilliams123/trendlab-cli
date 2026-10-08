"""Planner call (cheap-model spec §3.3) and step plans (§4.1).

A non-trivial task gets one call to the ``planning`` role that returns 2–6 verifiable steps,
each with the files it touches, a ``done_when`` condition and (when possible) a validation
command. The lead then executes the steps; the loop validates each one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from trendlab.agent.tasks import Plan, Task, TaskStatus

PLANNER_PROMPT = """You are the planner for an autonomous coding agent that will execute your
plan step by step with file and shell tools. Produce the smallest plan that gets the task done
and proven.

Answer with ONE JSON object and nothing else:
{{"steps": [{{"title": "imperative, one line", "files": ["path", "..."],
            "done_when": "a condition another model can check",
            "validation": "a shell command that proves this step, or null",
            "depends_on": [numbers of earlier steps this step needs, e.g. 1]}}]}}

Rules: 2 to 6 steps; each step changes at most a few files; the last step is always the full
validation (tests/lint) when the project has one; never invent files that do not exist unless
the step creates them; prefer the project's own test command when known. depends_on lists only
earlier steps; leave it empty when a step does not need another (independent steps can run in
any order).

## Task
{task}

## Repository context
{context}

## Known validation commands
{validation}
"""

_JSON = re.compile(r"\{.*\}", re.S)
_FILE = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|cs|md|toml|yaml|yml|json|html|css|sh|sql)\b"
)


def needs_planner(task_text: str, *, min_prompt_chars: int = 400) -> bool:
    """Heuristic from §3.3: long prompt, or two or more files mentioned."""
    text = task_text or ""
    if len(text) > min_prompt_chars:
        return True
    return len(set(_FILE.findall(text))) >= 2


def build_messages(task: str, context: str, validation: dict[str, str] | None) -> list[dict]:
    val = ", ".join(f"{k}: `{v}`" for k, v in (validation or {}).items()) or "(none known)"
    content = PLANNER_PROMPT.format(
        task=task.strip()[:6000] or "(no task)",
        context=context.strip()[:6000] or "(none)",
        validation=val,
    )
    return [{"role": "user", "content": content}]


def parse_steps(text: str) -> list[dict[str, Any]] | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    steps = []
    for raw in obj.get("steps") or []:
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            continue
        validation = raw.get("validation")
        if isinstance(validation, str):
            validation = validation.strip() or None
            if validation and validation.lower() in {"null", "none", "n/a"}:
                validation = None
        else:
            validation = None
        files = raw.get("files") or []
        n = len(steps) + 1
        deps = raw.get("depends_on") or []
        deps = sorted(
            {int(d) for d in deps if isinstance(d, int | float) or str(d).strip().isdigit()}
        )
        steps.append(
            {
                "title": str(raw["title"]).strip()[:160],
                "files": [str(f)[:200] for f in files if isinstance(f, str)][:12],
                "done_when": str(raw.get("done_when") or "").strip()[:400],
                "validation": validation[:400] if validation else None,
                # 1-based indices of earlier steps; forward or self references kept for lint
                "depends_on": [d for d in deps if d >= 1][:6],
                "_index": n,
            }
        )
    return steps[:6] or None


def apply_steps(plan: Plan, steps: list[dict[str, Any]]) -> list[Task]:
    """Replace the open part of the plan with the planner's steps; first becomes ACTIVE."""
    plan.replace([s["title"] for s in steps])
    added = [t for t in plan.tasks if t.status != TaskStatus.COMPLETED][-len(steps) :]
    ids = {i + 1: t.id for i, t in enumerate(added)}
    for task, step in zip(added, steps, strict=False):
        me = step.get("_index") or 0
        task.dependencies = [ids[d] for d in step.get("depends_on") or [] if d in ids and d < me]
        task.files = step.get("files") or []
        task.done_when = step.get("done_when") or ""
        task.validation = step.get("validation")
    if added:
        plan.update(added[0].id, status=TaskStatus.ACTIVE)
    plan.revision += 1
    return added


def step_brief(task: Task) -> str:
    lines = [f"{task.id} — {task.title}"]
    if task.files:
        lines.append("  files: " + ", ".join(task.files))
    if task.done_when:
        lines.append("  done when: " + task.done_when)
    if task.validation:
        lines.append(
            f"  validate with: `{task.validation}` (the harness runs it when you complete the step)"
        )
    return "\n".join(lines)


def plan_message(tasks: list[Task]) -> str:
    body = "\n".join(step_brief(t) for t in tasks)
    first = tasks[0].id if tasks else ""
    return (
        "A step plan was prepared for this task. Work through it in order with the tools; mark "
        "each step done with task(action='complete', task_id=..., evidence=...) and the harness "
        "will run that step's validation. Do not re-plan unless a step is impossible.\n\n"
        f"{body}\n\nStart with {first}."
    )


_CREATE = re.compile(r"\b(add|create|new|write|introduce|scaffold)\b", re.I)


def lint_plan(
    steps: list[dict[str, Any]],
    *,
    exists: Any = None,
    validation: dict[str, str] | None = None,
) -> list[str]:
    """Deterministic pre-implementation review of a plan (no model call).

    ``exists(path) -> bool`` checks the repository. Issues are short sentences the planner can
    act on in one repair round."""
    issues: list[str] = []
    if not steps:
        return ["the plan has no steps"]
    if len(steps) > 6:
        issues.append(f"{len(steps)} steps; use at most 6")
    for i, st in enumerate(steps, 1):
        for d in st.get("depends_on") or []:
            if d >= i:
                issues.append(f"step {i} depends on step {d}, which is not earlier")
        if exists is not None and not _CREATE.search(st.get("title", "")):
            missing = [f for f in st.get("files") or [] if not exists(f)]
            if missing:
                issues.append(
                    f"step {i} edits {', '.join(missing[:3])} which does not exist; "
                    "say the step creates it or use the real path"
                )
        if not st.get("done_when"):
            issues.append(f"step {i} has no done_when condition")
        if len(st.get("files") or []) > 5:
            issues.append(f"step {i} touches {len(st['files'])} files; split it")
    if validation and not steps[-1].get("validation"):
        issues.append("the last step must run the project's validation")
    return issues


def repair_messages(messages: list[dict], previous: str, issues: list[str]) -> list[dict]:
    return [
        *messages,
        {"role": "assistant", "content": previous},
        {
            "role": "user",
            "content": "Review of your plan found these problems:\n- "
            + "\n- ".join(issues)
            + "\nReturn the corrected plan as the same JSON object, nothing else.",
        },
    ]

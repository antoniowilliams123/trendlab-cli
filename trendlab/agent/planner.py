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
            "validation": "a shell command that proves this step, or null"}}]}}

Rules: 2 to 6 steps; each step changes at most a few files; the last step is always the full
validation (tests/lint) when the project has one; never invent files that do not exist unless
the step creates them; prefer the project's own test command when known.

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


def needs_planner(task_text: str, *, min_prompt_chars: int = 200) -> bool:
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
        steps.append(
            {
                "title": str(raw["title"]).strip()[:160],
                "files": [str(f)[:200] for f in files if isinstance(f, str)][:12],
                "done_when": str(raw.get("done_when") or "").strip()[:400],
                "validation": validation[:400] if validation else None,
            }
        )
    return steps[:6] or None


def apply_steps(plan: Plan, steps: list[dict[str, Any]]) -> list[Task]:
    """Replace the open part of the plan with the planner's steps; first becomes ACTIVE."""
    plan.replace([s["title"] for s in steps])
    added = [t for t in plan.tasks if t.status != TaskStatus.COMPLETED][-len(steps) :]
    for task, step in zip(added, steps, strict=False):
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

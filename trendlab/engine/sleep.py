"""Sleeptime pass (cheap-model spec §7.3): consolidate what the day's sessions taught.

Reads the day's sessions (prompts, denials, undos, steering, inbox feedback, memory
additions), asks the summarizer role to rewrite ``.trendlab/memory.md`` to at most 40 facts
and to propose amendments to ``TRENDLAB.md``, skills and the per-model notes, then opens
branch ``trendlab/memory-YYYY-MM-DD`` with the diff for review. Nothing lands on the working
branch by itself.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trendlab.context.project_memory import HEADER, ProjectMemory
from trendlab.tools.git import run_git

MAX_FACTS = 40
PROMPT = """You maintain the long-term memory of a software project for a coding agent.
Below are the current memory facts and a digest of today's sessions (user prompts, what was
denied or undone, steering messages, inbox feedback, facts learned).

Produce ONE JSON object:
{{"facts": ["≤ {max_facts} short, durable, verifiable facts about this project (merge
duplicates, drop stale or one-off ones, keep the most useful)"],
 "instructions": "0-6 lines to add to TRENDLAB.md (project instructions), or empty",
 "model_notes": "0-4 lines of guidance for the model '{model}' learned today, or empty",
 "skills": [{{"name": "kebab-name", "instructions": "..."}}]  // only when a repeatable
 procedure was learned, else []
}}

## Current memory
{memory}

## Today
{today}
"""
_JSON = re.compile(r"\{.*\}", re.S)


def day_digest(store, project: str, day: datetime | None = None, max_chars: int = 16_000) -> str:
    """Text digest of the day's sessions for one project."""
    day = day or datetime.now(UTC)
    start = (day - timedelta(days=1)).isoformat()
    parts: list[str] = []
    for sess in store.sessions(project, limit=50):
        if sess.get("updated_at", "") < start:
            continue
        sid = sess["id"]
        parts.append(f"### session {sid} ({sess.get('model')})")
        for m in store.messages(sid):
            if m.get("role") != "user":
                continue
            c = m.get("content")
            text = (
                c
                if isinstance(c, str)
                else " ".join(str(p.get("text") or "") for p in (c or []) if isinstance(p, dict))
            )
            if text:
                parts.append("user: " + text[:300].replace("\n", " "))
        for ev in store.events(sid):
            t = ev.get("type", "")
            d = ev.get("data") or {}
            if t in {"approval.decided", "permission.decided"} and d.get("decision") == "deny":
                parts.append(f"denied: {d.get('tool')} {str(d.get('summary') or '')[:120]}")
            elif t.startswith("checkpoint.undo") or t == "session.restored":
                parts.append(f"undo: {str(d)[:120]}")
            elif t == "run.steered":
                parts.append(f"steer: {str(d.get('text') or '')[:200]}")
            elif t == "memory.updated":
                parts.append(f"learned: {', '.join(d.get('facts') or [])[:300]}")
            elif t == "run.failed":
                parts.append(f"failed: {str(d.get('stop_reason') or '')[:200]}")
    text = "\n".join(parts)
    return text[:max_chars] if text else "(no sessions today)"


def parse_plan(text: str) -> dict[str, Any] | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    facts = [str(f).strip() for f in (obj.get("facts") or []) if str(f).strip()][:MAX_FACTS]
    return {
        "facts": facts,
        "instructions": str(obj.get("instructions") or "").strip()[:2000],
        "model_notes": str(obj.get("model_notes") or "").strip()[:1500],
        "skills": [
            {
                "name": re.sub(r"[^a-z0-9-]+", "-", str(s.get("name") or "").lower()).strip("-")[
                    :40
                ],
                "instructions": str(s.get("instructions") or "")[:4000],
            }
            for s in (obj.get("skills") or [])
            if isinstance(s, dict) and s.get("name") and s.get("instructions")
        ][:3],
    }


async def sleeptime(
    call: Callable[[list[dict[str, Any]]], Awaitable[str]],
    *,
    store,
    project_root: Path,
    model_ref: str,
    inbox_feedback: list[str] | None = None,
    day: datetime | None = None,
    branch: bool = True,
) -> dict[str, Any]:
    root = project_root.resolve()
    memory = ProjectMemory(root)
    today = day_digest(store, str(root), day)
    if inbox_feedback:
        today += "\n### inbox feedback\n" + "\n".join(inbox_feedback)
    prompt = PROMPT.format(
        max_facts=MAX_FACTS,
        model=model_ref,
        memory="\n".join(f"- {f}" for f in memory.facts) or "(empty)",
        today=today,
    )
    plan = parse_plan(await call([{"role": "user", "content": prompt}]))
    if plan is None:
        return {"ok": False, "reason": "summarizer returned no plan"}
    stamp = (day or datetime.now(UTC)).strftime("%Y-%m-%d")
    written: list[str] = []
    # memory.md: rewrite to the consolidated facts (dates kept when the fact survived)
    if plan["facts"]:
        old = {f: d for d, f in memory.entries}
        body = "".join(f"- [{old.get(f) or stamp}] {f}\n" for f in plan["facts"])
        (root / ".trendlab").mkdir(exist_ok=True)
        (root / ".trendlab" / "memory.md").write_text(HEADER + body, encoding="utf-8")
        written.append(".trendlab/memory.md")
    if plan["instructions"]:
        p = root / "TRENDLAB.md"
        existing = (
            p.read_text(encoding="utf-8") if p.is_file() else "# TrendLab project instructions\n"
        )
        p.write_text(
            existing.rstrip("\n") + f"\n\n## Learned {stamp}\n{plan['instructions']}\n",
            encoding="utf-8",
        )
        written.append("TRENDLAB.md")
    if plan["model_notes"]:
        from trendlab.prompts.drivers import model_slug

        p = root / ".trendlab" / "skills" / "_model" / f"{model_slug(model_ref)}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text(encoding="utf-8") if p.is_file() else ""
        p.write_text(
            existing.rstrip("\n") + f"\n\n<!-- {stamp} -->\n{plan['model_notes']}\n",
            encoding="utf-8",
        )
        written.append(str(p.relative_to(root)))
    for sk in plan["skills"]:
        d = root / ".trendlab" / "skills" / sk["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {sk['name']}\n{sk['instructions']}\n", encoding="utf-8")
        written.append(f".trendlab/skills/{sk['name']}/SKILL.md")
    out: dict[str, Any] = {
        "ok": True,
        "facts": len(plan["facts"]),
        "written": written,
        "branch": None,
    }
    if branch and written and (root / ".git").exists():
        out["branch"] = await _commit_to_branch(root, f"trendlab/memory-{stamp}", written, stamp)
    return out


async def _commit_to_branch(root: Path, branch: str, files: list[str], stamp: str) -> str | None:
    """Commit the written files on a review branch without touching the current branch's
    index: a temporary worktree of HEAD receives the files and commits."""
    import shutil
    import tempfile

    from trendlab.orchestration.gitflow import narrow_exclude

    narrow_exclude(root)
    tmp = Path(tempfile.mkdtemp(prefix="trendlab-sleep-"))
    try:
        code, out = await run_git(root, "worktree", "add", "-B", branch, str(tmp), "HEAD")
        if code != 0:
            return None
        for rel in files:
            src, dst = root / rel, tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        await run_git(tmp, "add", "-f", *files)
        code, _ = await run_git(
            tmp,
            "-c",
            "user.name=TrendLab sleeptime",
            "-c",
            "user.email=trendlab@localhost",
            "commit",
            "-q",
            "-m",
            f"chore: sleeptime memory consolidation {stamp}",
        )
        return branch if code == 0 else None
    finally:
        await run_git(root, "worktree", "remove", "--force", str(tmp))
        shutil.rmtree(tmp, ignore_errors=True)

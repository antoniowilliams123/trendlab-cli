"""Git and GitHub workflow (spec §26): generated commit messages, pull requests through ``gh``,
issue context, and throwaway worktrees.

Commits and pushes are *user* commands, never model-callable tools, and they execute through
the shell tool so the permission engine, approvals and audit log apply.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from trendlab.providers.base import ToolCall
from trendlab.providers.registry import resolve_role
from trendlab.tools.git import git_branch, run_git

_COMMIT_PROMPT = (
    "Write a git commit message for the diff below. Rules: first line is a conventional-commit "
    "subject (feat:, fix:, docs:, refactor:, test:, chore:) under 72 characters; then a blank "
    "line; "
    "then 1-4 short bullets explaining why, only if they add information. No code fences, no "
    "quotes, nothing else.\n\n"
)
_PR_PROMPT = (
    "Write a pull request description in Markdown for the commits and diff below. Sections: a "
    "one-paragraph Summary, a 'Changes' bullet list, and 'Testing' (what was run). Be concrete and "
    "brief. No code fences around the whole answer.\n\n"
)


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "task"


async def _ask_model(app, prompt: str, role: str = "summarizer") -> str:
    ref = resolve_role(app.config, role, app.model_ref)
    response, used = await app.gateway.complete(ref, [{"role": "user", "content": prompt}], None)
    app.costs.record(
        used, response.usage, 0, role=role, local=app.gateway.provider(used).capabilities().local
    )
    return response.text.strip().strip("`").strip()


async def _shell(app, command: str) -> Any:
    """Run a git/gh command through the tool runtime (permissions + audit apply)."""
    call = ToolCall(id="gitflow", name="shell", arguments={"command": command})
    return await app.tools.execute(call)


# -- commit --------------------------------------------------------------------------------------
def ensure_excluded(root: Path) -> None:
    """Keep TrendLab's own state (.trendlab/) out of git without touching the user's .gitignore."""
    git_path = root / ".git"
    info_dir: Path | None = None
    if git_path.is_dir():
        info_dir = git_path / "info"
    elif git_path.is_file():  # a worktree: .git is a file pointing at the main repo's gitdir
        target = Path(git_path.read_text().strip().removeprefix("gitdir: "))
        common = target / "commondir"
        if common.is_file():
            info_dir = (target / common.read_text().strip()).resolve() / "info"
    if info_dir is None or not info_dir.parent.is_dir():
        return
    info_dir.mkdir(exist_ok=True)
    exclude = info_dir / "exclude"
    existing = exclude.read_text() if exclude.exists() else ""
    if ".trendlab/" not in existing.splitlines():
        exclude.write_text(existing.rstrip("\n") + ("\n" if existing else "") + ".trendlab/\n")


NARROW = [
    "# TrendLab: state stays private, memory and skills are reviewable (sleeptime, §7.3)",
    ".trendlab/*",
    "!.trendlab/memory.md",
    "!.trendlab/skills/",
    "!.trendlab/permissions.toml",
]


def narrow_exclude(root: Path) -> None:
    """Replace the blanket ``.trendlab/`` exclude with one that lets memory.md, skills and
    the project permission rules be committed (the sleeptime branch needs them tracked)."""
    git_path = root / ".git"
    info_dir = git_path / "info" if git_path.is_dir() else None
    if info_dir is None:
        return
    info_dir.mkdir(exist_ok=True)
    exclude = info_dir / "exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if NARROW[1] in lines:
        return
    lines = [ln for ln in lines if ln.strip() != ".trendlab/"]
    exclude.write_text("\n".join([*lines, *NARROW]) + "\n")


async def current_diff(app) -> tuple[str, str]:
    """(stat, patch) of all uncommitted changes including untracked files."""
    ensure_excluded(app.project_root)
    await run_git(app.project_root, "add", "-N", ".")  # intent-to-add so new files appear in diff
    _, stat = await run_git(app.project_root, "diff", "--stat")
    _, patch = await run_git(app.project_root, "diff")
    return stat.strip(), patch


async def generate_commit_message(app, patch: str, stat: str) -> str:
    if not patch.strip():
        return ""
    try:
        text = await _ask_model(app, _COMMIT_PROMPT + stat + "\n\n" + patch[:24_000])
    except Exception:  # noqa: BLE001 — fall back to a deterministic message
        text = ""
    subject = text.splitlines()[0].strip() if text else ""
    if not subject or len(subject) > 100:
        files = [ln.split("|")[0].strip() for ln in stat.splitlines() if "|" in ln][:3]
        subject = "chore: update " + ", ".join(files) if files else "chore: update files"
        text = subject
    return text


async def commit(app, message: str | None = None) -> dict[str, Any]:
    stat, patch = await current_diff(app)
    if not patch.strip():
        return {"ok": False, "error": "nothing to commit"}
    message = message or await generate_commit_message(app, patch, stat)
    # -m with shlex quoting: safe against backticks/$() and works inside the sandbox, where the
    # host's /tmp (and hence any temp message file) is not visible.
    res = await _shell(app, f"git add -A && git commit -m {shlex.quote(message.rstrip())}")
    if not res.ok:
        return {"ok": False, "error": res.output, "message": message}
    _, head = await run_git(app.project_root, "rev-parse", "--short", "HEAD")
    return {"ok": True, "message": message, "head": head.strip(), "stat": stat}


# -- pull requests ---------------------------------------------------------------------------------
async def default_base(app) -> str:
    code, out = await run_git(app.project_root, "symbolic-ref", "refs/remotes/origin/HEAD")
    if code == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        code, _ = await run_git(app.project_root, "rev-parse", "--verify", f"origin/{cand}")
        if code == 0:
            return cand
    return "main"


async def ensure_feature_branch(app, hint: str = "") -> str:
    branch = await git_branch(app.project_root) or "HEAD"
    base = await default_base(app)
    if branch not in {base, "HEAD", "main", "master"}:
        return branch
    name = f"trendlab/{_slug(hint or app.session_id)}"
    code, out = await run_git(app.project_root, "checkout", "-b", name)
    if code != 0:
        raise RuntimeError(f"could not create branch {name}: {out.strip()}")
    return name


async def create_pr(app, title: str | None = None, *, draft: bool = False) -> dict[str, Any]:
    if shutil.which("gh") is None:
        return {"ok": False, "error": "the GitHub CLI (gh) is not installed"}
    stat, patch = await current_diff(app)
    if patch.strip():
        return {"ok": False, "error": "uncommitted changes; run /commit first"}
    base = await default_base(app)
    branch = await ensure_feature_branch(app, title or "")
    _, log = await run_git(app.project_root, "log", f"origin/{base}..HEAD", "--format=%s%n%b")
    _, diffstat = await run_git(app.project_root, "diff", f"origin/{base}...HEAD", "--stat")
    if not log.strip():
        return {"ok": False, "error": f"no commits on {branch} ahead of origin/{base}"}
    push = await _shell(app, f"git push -u origin {shlex.quote(branch)}")
    if not push.ok:
        return {"ok": False, "error": push.output}
    title = title or log.strip().splitlines()[0][:72]
    try:
        body = await _ask_model(
            app, _PR_PROMPT + "Commits:\n" + log + "\n\nDiff stat:\n" + diffstat
        )
    except Exception:  # noqa: BLE001
        body = "## Summary\n\n" + log.strip()
    issue = getattr(app, "current_issue", None)
    if issue:
        body += f"\n\nCloses #{issue['number']}"
    cmd = (
        f"gh pr create --base {shlex.quote(base)} --head {shlex.quote(branch)} "
        f"--title {shlex.quote(title)} --body {shlex.quote(body)}" + (" --draft" if draft else "")
    )
    res = await _shell(app, cmd)
    if not res.ok:
        return {"ok": False, "error": res.output}
    tokens = res.output.split()
    url = next((tok for tok in tokens if tok.startswith("https://")), res.output.strip())
    return {"ok": True, "url": url, "branch": branch, "base": base, "title": title, "body": body}


# -- issues ----------------------------------------------------------------------------------------
async def load_issue(app, number: int) -> dict[str, Any]:
    if shutil.which("gh") is None:
        return {"ok": False, "error": "the GitHub CLI (gh) is not installed"}
    fields = "number,title,body,labels,url,state"
    res = await _shell(app, f"gh issue view {int(number)} --json {fields}")
    if not res.ok:
        return {"ok": False, "error": res.output}
    try:
        data = json.loads(res.output)
    except ValueError:
        return {"ok": False, "error": f"unexpected gh output: {res.output[:200]}"}
    labels = ", ".join(lb.get("name", "") for lb in data.get("labels") or []) or "none"
    body_text = (data.get("body") or "").strip()[:6000]
    note = (
        f"GitHub issue #{data['number']}: {data.get('title', '')}\n"
        f"State: {data.get('state', '?')} · Labels: {labels}\n"
        f"URL: {data.get('url', '')}\n\n{body_text}"
    )
    app.current_issue = data
    app.context.messages.append({"role": "user", "content": f"[context] {note}"})
    if app.store:
        app.store.append_message(app.session_id, {"role": "user", "content": f"[context] {note}"})
    return {"ok": True, "issue": data, "note": note}


# -- worktrees -------------------------------------------------------------------------------------
class WorktreeManager:
    def __init__(self, project_root: Path) -> None:
        self.main_root = project_root
        self.dir = project_root / ".trendlab" / "worktrees"

    def create_sync(self, name: str) -> Path:
        """Create the worktree with plain subprocess calls (usable before an event loop exists)."""
        import subprocess

        name = _slug(name)
        path = self.dir / name
        if path.exists():
            raise RuntimeError(f"worktree {name} already exists at {path}")
        self.dir.mkdir(parents=True, exist_ok=True)
        ensure_excluded(self.main_root)
        branch = f"trendlab/{name}"
        base = ["git", "-C", str(self.main_root), "worktree", "add"]
        proc = subprocess.run(
            [*base, "-b", branch, str(path)], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                [*base, str(path), branch], capture_output=True, text=True, check=False
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout).strip())
        return path

    async def create(self, name: str) -> Path:
        return self.create_sync(name)

    async def list(self) -> list[dict[str, str]]:
        code, out = await run_git(self.main_root, "worktree", "list", "--porcelain")
        if code != 0:
            return []
        items: list[dict[str, str]] = []
        cur: dict[str, str] = {}
        for line in out.splitlines():
            if line.startswith("worktree "):
                cur = {"path": line[9:]}
                items.append(cur)
            elif line.startswith("branch "):
                cur["branch"] = line[7:].replace("refs/heads/", "")
        return items

    async def remove(self, name: str, *, force: bool = False) -> str:
        path = self.dir / _slug(name)
        args = ["worktree", "remove", str(path)] + (["--force"] if force else [])
        code, out = await run_git(self.main_root, *args)
        if code != 0:
            raise RuntimeError(out.strip())
        return str(path)


_JUNK = (
    "__pycache__/",
    ".trendlab/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
)


async def worktree_patch(wt: Path, rules=None) -> tuple[str, list[str]]:
    """Stage everything in ``wt`` and return (binary patch vs HEAD, files), skipping build junk
    and anything the project's ignore rules exclude (worktrees have no .gitignore of their own
    when the project never committed one)."""
    await run_git(wt, "add", "-A")
    code, names = await run_git(wt, "diff", "--cached", "--name-only", "HEAD")
    if code != 0:
        return "", []
    keep: list[str] = []
    for rel in (ln.strip() for ln in names.splitlines() if ln.strip()):
        if any(part in rel for part in _JUNK) or rel.endswith((".pyc", ".pyo")):
            continue
        if rules is not None and rules.ignored(rel):
            continue
        keep.append(rel)
    if not keep:
        return "", []
    code, patch = await run_git(wt, "diff", "--cached", "--binary", "HEAD", "--", *keep)
    return (patch if code == 0 else ""), keep

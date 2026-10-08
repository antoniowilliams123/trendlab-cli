"""``trendlab watch``: run each project's test command and file failures in the inbox."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from trendlab.context.validation import validation_commands
from trendlab.engine.inbox import Inbox
from trendlab.tools.views import tier_output

_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


async def run_tests(root: Path, command: str, timeout: int = 900) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def watch_project(inbox: Inbox, root: Path, config) -> list[dict[str, Any]]:
    cmds = validation_commands(config, root)
    command = cmds.get("test")
    if not command:
        return []
    code, output = await run_tests(root, command)
    if code == 0:
        return []
    view = tier_output(output, {"exit_code": code, "command": command})
    failing = _FAILED.findall(output)
    issues = []
    for test_id in failing[:10] or ["(suite failed)"]:
        issues.append(
            inbox.record(
                project=str(root),
                title=f"test failing: {test_id}"[:200],
                signature=test_id if test_id != "(suite failed)" else view.tier1,
                source="watch",
                root_cause=view.tier1[:400],
                impacted_files=[test_id.split("::")[0]] if "::" in test_id else [],
                evidence=[f"{command} → exit {code}"],
                severity="high",
            )
        )
    return issues


async def watch_projects(inbox: Inbox, projects: list[Path], config) -> int:
    n = 0
    for root in projects:
        if root.is_dir():
            n += len(await watch_project(inbox, root, config))
    return n

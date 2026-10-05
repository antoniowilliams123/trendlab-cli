from __future__ import annotations

from typing import Any

from trendlab.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def restricted(self, allowed: list[str]) -> ToolRegistry:
        """A view containing only ``allowed`` tools (used for sub-agents)."""
        sub = ToolRegistry()
        for name in allowed:
            tool = self._tools.get(name)
            if tool is not None:
                sub.register(tool)
        return sub

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]


READ_ONLY_TOOLS = [
    "read_file",
    "list_directory",
    "glob",
    "search_text",
    "git_status",
    "git_diff",
    "git_log",
]


def default_registry() -> ToolRegistry:
    from trendlab.tools.apply_patch import ApplyPatchTool
    from trendlab.tools.files import (
        DeleteFileTool,
        GlobTool,
        ListDirectoryTool,
        PatchFileTool,
        ReadFileTool,
        SearchTextTool,
        WriteFileTool,
    )
    from trendlab.tools.git import GitDiffTool, GitLogTool, GitStatusTool
    from trendlab.tools.shell import ShellTool
    from trendlab.tools.tests import RunTestsTool

    reg = ToolRegistry()
    for tool in (
        ReadFileTool(),
        ListDirectoryTool(),
        GlobTool(),
        SearchTextTool(),
        WriteFileTool(),
        PatchFileTool(),
        ApplyPatchTool(),
        DeleteFileTool(),
        ShellTool(),
        GitStatusTool(),
        GitDiffTool(),
        GitLogTool(),
        RunTestsTool(),
    ):
        reg.register(tool)
    return reg

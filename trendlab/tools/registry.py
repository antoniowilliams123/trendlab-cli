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

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]


def default_registry() -> ToolRegistry:
    from trendlab.tools.files import (
        DeleteFileTool,
        ListDirectoryTool,
        ReadFileTool,
        SearchTextTool,
        WriteFileTool,
    )
    from trendlab.tools.shell import ShellTool

    reg = ToolRegistry()
    for tool in (
        ReadFileTool(),
        ListDirectoryTool(),
        SearchTextTool(),
        WriteFileTool(),
        DeleteFileTool(),
        ShellTool(),
    ):
        reg.register(tool)
    return reg

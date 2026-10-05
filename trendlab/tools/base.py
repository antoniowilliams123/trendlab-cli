"""Tool contract. Every tool declares its input schema and describes the permission
it needs for a given call; it never checks permissions itself."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from trendlab.permissions.engine import PermissionRequest


class ToolResult(BaseModel):
    ok: bool
    output: str
    data: dict[str, Any] = Field(default_factory=dict)


class PathOutsideProjectError(Exception):
    pass


@dataclass
class ToolContext:
    project_root: Path
    session_id: str
    task_id: str | None = None

    def resolve(self, raw: str) -> Path:
        """Resolve ``raw`` inside the project root, following symlinks; reject escapes."""
        candidate = (
            (self.project_root / raw).resolve()
            if not Path(raw).is_absolute()
            else Path(raw).resolve()
        )
        root = self.project_root.resolve()
        if candidate != root and root not in candidate.parents:
            raise PathOutsideProjectError(f"path outside project boundary: {raw}")
        return candidate


class Tool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }

    def parse(self, raw: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(raw)

    @abstractmethod
    def permission(self, args: BaseModel, ctx: ToolContext) -> PermissionRequest: ...

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...

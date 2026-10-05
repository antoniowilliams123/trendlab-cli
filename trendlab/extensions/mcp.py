"""Minimal MCP (Model Context Protocol) stdio client (spec §37).

Each configured server is spawned as a subprocess speaking JSON-RPC 2.0 over
stdin/stdout. Its tools are registered in the TrendLab registry as ordinary
tools with the server's configured permission category, so they pass through
the permission engine, approvals, redaction and the audit log like any other.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from pydantic import BaseModel, Field, create_model

from trendlab.config.schema import McpServerConfig
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.security.redaction import redact_text
from trendlab.telemetry.events import EventBus, EventType
from trendlab.tools.base import Tool, ToolContext, ToolResult
from trendlab.tools.registry import ToolRegistry

PROTOCOL_VERSION = "2024-11-05"


class McpError(Exception):
    pass


class McpClient:
    def __init__(self, name: str, cfg: McpServerConfig) -> None:
        self.name = name
        self.cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self.tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        env = {**os.environ, **self.cfg.env}
        self._proc = await asyncio.create_subprocess_exec(
            self.cfg.command,
            *self.cfg.args,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader = asyncio.create_task(self._read_loop())
        await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "trendlab", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized", {})
        result = await self.request("tools/list", {})
        self.tools = result.get("tools", [])

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            fut = self._pending.pop(msg.get("id"), None) if "id" in msg else None
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(McpError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result", {}))
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpError(f"{self.name}: server exited"))

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=self.cfg.timeout_seconds)
        except TimeoutError as exc:
            self._pending.pop(rid, None)
            raise McpError(f"{self.name}: {method} timed out") from exc

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(f"[{item.get('type')} content omitted]")
        text = "\n".join(parts) or json.dumps(result)[:2000]
        if result.get("isError"):
            raise McpError(text)
        return text

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.stdin:
            proc.stdin.close()
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (TimeoutError, ProcessLookupError):
                proc.kill()
                await proc.wait()
        if self._reader:
            try:
                await asyncio.wait_for(self._reader, timeout=1)
            except (TimeoutError, asyncio.CancelledError):
                self._reader.cancel()
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()


def _input_model(schema: dict[str, Any], name: str) -> type[BaseModel]:
    """Permissive Pydantic model from a JSON schema: required keys must exist, extras allowed."""
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    fields: dict[str, Any] = {}
    for key, spec in props.items():
        typ = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }.get(spec.get("type"), Any)
        fields[key] = (typ, ...) if key in required else (typ | None, Field(default=None))
    model = create_model(f"Mcp_{name}_Input", **fields)  # type: ignore[call-overload]
    model.model_config["extra"] = "allow"
    return model


class McpTool(Tool):
    def __init__(
        self, client: McpClient, spec: dict[str, Any], category: OperationCategory
    ) -> None:
        self.client = client
        self.remote_name = spec["name"]
        self.name = f"mcp_{client.name}_{spec['name']}"
        self.description = f"[MCP {client.name}] {spec.get('description', '')}"[:500]
        self.input_model = _input_model(spec.get("inputSchema", {}), self.name)
        self.category = category

    def permission(self, args: BaseModel, ctx: ToolContext) -> PermissionRequest:
        return PermissionRequest(
            tool=self.name,
            category=self.category,
            summary=f"MCP {self.client.name}: {self.remote_name}",
            command=f"{self.remote_name} "
            f"{json.dumps(args.model_dump(exclude_none=True), default=str)[:200]}",
            cwd=str(ctx.project_root),
            args=args.model_dump(exclude_none=True),
            task_id=ctx.task_id,
        )

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        try:
            text = await self.client.call_tool(self.remote_name, args.model_dump(exclude_none=True))
        except McpError as exc:
            return ToolResult(ok=False, output=f"MCP error: {redact_text(str(exc))[:2000]}")
        return ToolResult(ok=True, output=redact_text(text)[:20_000])


class McpManager:
    def __init__(
        self, servers: dict[str, McpServerConfig], events: EventBus, session_id: str
    ) -> None:
        self.servers = servers
        self.events = events
        self.session_id = session_id
        self.clients: dict[str, McpClient] = {}
        self.errors: dict[str, str] = {}

    async def start(self, registry: ToolRegistry) -> None:
        for name, cfg in self.servers.items():
            client = McpClient(name, cfg)
            try:
                category = OperationCategory(cfg.category)
            except ValueError:
                category = OperationCategory.NETWORK
            try:
                await client.start()
            except (OSError, McpError, ValueError) as exc:
                self.errors[name] = str(exc)[:200]
                self.events.emit(
                    EventType.MCP_SERVER_FAILED,
                    session_id=self.session_id,
                    server=name,
                    error=str(exc)[:200],
                )
                continue
            self.clients[name] = client
            for spec in client.tools:
                registry.register(McpTool(client, spec, category))
            self.events.emit(
                EventType.MCP_SERVER_STARTED,
                session_id=self.session_id,
                server=name,
                tools=[t["name"] for t in client.tools],
                category=category.value,
            )

    def status(self) -> dict[str, dict[str, Any]]:
        out = {
            name: {"status": "running", "tools": [t["name"] for t in c.tools]}
            for name, c in self.clients.items()
        }
        for name, err in self.errors.items():
            out[name] = {"status": f"failed: {err}", "tools": []}
        return out

    async def stop(self) -> None:
        for client in self.clients.values():
            await client.stop()

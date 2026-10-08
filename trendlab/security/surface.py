"""Attack-surface inventory and dependency audit (uplift U27).

``inventory`` lists everything the agent can reach under the current configuration, with the
permission decision for each category: tools, the network, MCP servers (external processes),
hooks (commands the harness runs), remote control channels and where they listen, and which
secrets are referenced (by name, never value). ``risks`` turns that into findings.

``audit`` checks the packages installed in the project's virtualenv against the public OSV
vulnerability database (one batched request with package names and versions).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx


def inventory(config, root: Path) -> dict[str, Any]:
    from trendlab.permissions.engine import PermissionEngine
    from trendlab.permissions.models import OperationCategory
    from trendlab.tools.registry import default_registry

    engine = PermissionEngine(config.defaults.permission_mode)
    table = {c.value: d.value for c, d in engine.policy_table().items()}
    tools = sorted(default_registry().names())
    allow, deny = set(config.tools.allow), set(config.tools.deny)
    if allow:
        tools = [t for t in tools if t in allow or t in {"task", "ask_user"}]
    tools = [t for t in tools if t not in deny]
    ra = config.remote_approval
    loopback = ra.host in {"127.0.0.1", "localhost", "::1"}
    return {
        "mode": config.defaults.permission_mode.value,
        "policy": table,
        "tools": tools,
        "network": {
            "decision": table.get(OperationCategory.NETWORK.value),
            "sandbox_network": getattr(config.sandbox, "allow_network", False),
            "web_tools": [t for t in tools if t.startswith("web_")],
        },
        "mcp_servers": [
            {"name": name, "command": srv.command, "category": srv.category}
            for group in config.mcp.values()
            for name, srv in group.items()
        ],
        "hooks": [
            {"event": h.event, "command": h.command, "blocking": h.blocking} for h in config.hooks
        ],
        "remote": {
            "web_approvals": ra.enabled,
            "listen": f"{ra.host}:{ra.port}" if ra.enabled else None,
            "loopback_only": loopback,
            "tls": bool(ra.tls_cert and ra.tls_key),
            "high_risk_remote": ra.allow_high_risk,
            "telegram_approvals": ra.telegram,
            "telegram_bridge": config.telegram_bridge.enabled,
        },
        "secrets_referenced": sorted(
            {
                *(p.api_key_env for p in config.providers.values() if p.api_key_env),
                *(
                    [config.notifications.telegram.bot_token_env]
                    if config.notifications.telegram.chat_id
                    else []
                ),
            }
        ),
        "change_scope": list(config.governance.change_allow),
        "engine_jobs": [
            j
            for j, on in (
                ("canary", config.engine.canary),
                ("review_commits", config.engine.review_commits),
            )
            if on
        ],
    }


def risks(inv: dict[str, Any]) -> list[str]:
    out = []
    if inv["mode"] == "auto":
        out.append(
            "AUTO mode: edits, shell, installs and deletes run without asking "
            "(irreversible commands still ask; privileged and outside-project are denied)"
        )
    if inv["policy"].get("irreversible") == "allow":
        out.append("irreversible commands run without asking")
    if inv["network"]["decision"] == "allow":
        out.append("network calls run without asking (secret reads still force approval)")
    if (
        inv["remote"]["web_approvals"]
        and not inv["remote"]["loopback_only"]
        and not inv["remote"]["tls"]
    ):
        out.append(f"web approvals listen on {inv['remote']['listen']} without TLS")
    if inv["remote"]["high_risk_remote"]:
        out.append("high-risk operations can be approved remotely")
    for m in inv["mcp_servers"]:
        out.append(f"MCP server {m['name']} runs `{m['command']}` with {m['category']} rights")
    for h in inv["hooks"]:
        out.append(f"hook on {h['event']} runs `{h['command']}`")
    if not inv["change_scope"]:
        out.append("no change scope: edits allowed anywhere in the project")
    return out


def installed_packages(root: Path) -> list[tuple[str, str]]:
    pkgs = []
    for venv in (root / ".venv", root / "venv"):
        for sp in venv.glob("lib/python*/site-packages"):
            for d in sp.glob("*.dist-info"):
                meta = d / "METADATA"
                name = version = None
                try:
                    for line in meta.read_text(errors="replace").splitlines()[:30]:
                        if line.startswith("Name: "):
                            name = line[6:].strip()
                        elif line.startswith("Version: "):
                            version = line[9:].strip()
                except OSError:
                    continue
                if name and version:
                    pkgs.append((name, version))
    return sorted(set(pkgs))


def audit(root: Path, *, client: httpx.Client | None = None) -> dict[str, Any]:
    pkgs = installed_packages(root)
    if not pkgs:
        return {"packages": 0, "vulnerable": [], "note": "no virtualenv found"}
    queries = [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in pkgs]
    c = client or httpx.Client(timeout=60)
    vulnerable = []
    try:
        for i in range(0, len(queries), 500):
            r = c.post("https://api.osv.dev/v1/querybatch", json={"queries": queries[i : i + 500]})
            r.raise_for_status()
            for (name, version), res in zip(
                pkgs[i : i + 500], r.json().get("results", []), strict=False
            ):
                ids = [v["id"] for v in res.get("vulns") or []]
                if ids:
                    vulnerable.append({"package": name, "version": version, "advisories": ids[:8]})
    finally:
        if client is None:
            c.close()
    return {"packages": len(pkgs), "vulnerable": vulnerable}


def to_text(inv: dict[str, Any]) -> str:
    return json.dumps(inv, indent=2)

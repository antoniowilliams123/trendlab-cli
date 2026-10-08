"""Uplift U27: attack-surface inventory and dependency audit."""

from pathlib import Path

import httpx

from trendlab.config.schema import AppConfig, HookConfig, McpServerConfig, PermissionMode
from trendlab.security.surface import audit, installed_packages, inventory, risks


def test_inventory_and_risks(tmp_path: Path):
    cfg = AppConfig()
    cfg.defaults.permission_mode = PermissionMode.UNSAFE
    cfg.hooks = [HookConfig(event="after_edit", command="ruff format")]
    cfg.mcp = {"user": {"browser": McpServerConfig(command="npx browser-mcp")}}
    cfg.remote_approval.enabled = True
    cfg.remote_approval.host = "0.0.0.0"
    cfg.tools.deny = ["web_search"]
    inv = inventory(cfg, tmp_path)
    assert "web_search" not in inv["tools"] and inv["mcp_servers"][0]["name"] == "browser"
    assert inv["remote"]["loopback_only"] is False
    text = " ".join(risks(inv))
    assert "AUTO mode" in text and "without TLS" in text and "browser-mcp" in text
    assert "ruff format" in text and "no change scope" in text
    assert all("=" not in s for s in inv["secrets_referenced"])  # names only


def test_audit_reports_advisories(tmp_path: Path):
    meta = tmp_path / ".venv/lib/python3.12/site-packages/requests-2.19.0.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text("Metadata-Version: 2.1\nName: requests\nVersion: 2.19.0\n")
    assert installed_packages(tmp_path) == [("requests", "2.19.0")]

    def handler(request):
        return httpx.Response(200, json={"results": [{"vulns": [{"id": "GHSA-x84v-xcm2-53pg"}]}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = audit(tmp_path, client=client)
    assert r == {
        "packages": 1,
        "vulnerable": [
            {"package": "requests", "version": "2.19.0", "advisories": ["GHSA-x84v-xcm2-53pg"]}
        ],
    }
    assert audit(tmp_path / "none")["note"] == "no virtualenv found"

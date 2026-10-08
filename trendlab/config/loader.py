"""Load and merge TrendLab configuration.

Precedence (lowest → highest): built-in defaults → ``~/.trendlab/config.toml``
→ ``<project>/.trendlab/config.toml``. ``TRENDLAB_HOME`` overrides the global
directory (used by tests and portable installs).

A project's config is written by whoever controls the repository, so the settings that can run
commands, move the API key or loosen safety (hooks, MCP servers, providers, sandbox, diagnostics
commands, notifications, remote control, engine, permission mode) apply only after the user
trusts that exact content (``trendlab trust``). Until then they are left out and listed in
``AppConfig.untrusted_project``; every other project setting applies as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import ValidationError

from trendlab.config.schema import AppConfig

GLOBAL_DIR_NAME = ".trendlab"
PROJECT_DIR_NAME = ".trendlab"
CONFIG_FILE_NAME = "config.toml"
PROJECT_INSTRUCTIONS_FILE = "TRENDLAB.md"


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


def trendlab_home() -> Path:
    override = os.environ.get("TRENDLAB_HOME")
    return Path(override).expanduser() if override else Path.home() / GLOBAL_DIR_NAME


def global_config_path() -> Path:
    return trendlab_home() / CONFIG_FILE_NAME


def project_config_path(project_root: Path) -> Path:
    return project_root / PROJECT_DIR_NAME / CONFIG_FILE_NAME


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# sections a repository may not set without the user's say-so
SENSITIVE_SECTIONS = (
    "hooks",
    "mcp",
    "providers",
    "sandbox",
    "diagnostics",
    "notifications",
    "remote_approval",
    "telegram_bridge",
    "engine",
    "ui",
)
SENSITIVE_KEYS = (("defaults", "permission_mode"),)


def split_project_config(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(settings that apply freely, settings that need trust)."""
    safe = {k: v for k, v in data.items() if k not in SENSITIVE_SECTIONS}
    risky = {k: v for k, v in data.items() if k in SENSITIVE_SECTIONS}
    for section, key in SENSITIVE_KEYS:
        if isinstance(safe.get(section), dict) and key in safe[section]:
            safe[section] = {k: v for k, v in safe[section].items() if k != key}
            risky.setdefault(section, {})[key] = data[section][key]
    return safe, risky


def fingerprint(risky: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(risky, sort_keys=True, default=str).encode()).hexdigest()


def trust_file() -> Path:
    return trendlab_home() / "trusted_projects.json"


def _trusted() -> dict[str, str]:
    try:
        data = json.loads(trust_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _project_key(project_root: Path) -> str:
    return str(project_root.expanduser().resolve())


def trust_project(project_root: Path) -> str:
    """Trust the project's current risky settings (asked again if they change)."""
    _safe, risky = split_project_config(_read_toml(project_config_path(project_root)))
    data = _trusted()
    fp = fingerprint(risky)
    data[_project_key(project_root)] = fp
    path = trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return fp


def revoke_project(project_root: Path) -> bool:
    data = _trusted()
    if data.pop(_project_key(project_root), None) is None:
        return False
    trust_file().write_text(json.dumps(data, indent=1), encoding="utf-8")
    return True


def describe_risky(risky: dict[str, Any]) -> list[str]:
    """One line per thing the user is asked to trust."""
    out: list[str] = []
    for h in risky.get("hooks") or []:
        if isinstance(h, dict):
            out.append(f"hook on {h.get('event')}: runs `{h.get('command')}`")
    for name, srv in ((risky.get("mcp") or {}).get("servers") or {}).items():
        if isinstance(srv, dict):
            args = " ".join(str(a) for a in srv.get("args") or [])
            out.append(f"MCP server {name}: runs `{srv.get('command')} {args}`")
    for name, prov in (risky.get("providers") or {}).items():
        if isinstance(prov, dict):
            where = prov.get("base_url") or "(same address)"
            key = prov.get("api_key_env")
            extra = f", sends the key from {key}" if key else ""
            out.append(f"model provider {name}: talks to {where}{extra}")
    for cmds in ((risky.get("diagnostics") or {}).get("commands") or {}).values():
        for c in cmds if isinstance(cmds, list) else [cmds]:
            out.append(f"after-edit check: runs `{c}`")
    mode = (risky.get("defaults") or {}).get("permission_mode")
    if mode:
        out.append(f"permission mode: {mode}")
    named = {"hooks", "mcp", "providers", "diagnostics", "defaults"}
    for section in sorted(set(risky) - named):
        out.append(f"[{section}] settings: {', '.join(sorted(risky[section]))}"[:200])
    if (risky.get("diagnostics") or {}).keys() - {"commands"}:
        out.append("[diagnostics] settings: " + ", ".join(sorted(risky["diagnostics"])))
    return out


def load_config(project_root: Path | None = None) -> AppConfig:
    data = _read_toml(global_config_path())
    pending: list[str] = []
    if project_root is not None:
        path = project_config_path(project_root)
        same = path.expanduser().resolve() == global_config_path().expanduser().resolve()
        project = {} if same else _read_toml(path)  # the home folder's file is the user's own
        safe, risky = split_project_config(project)
        data = _deep_merge(data, safe)
        if risky:
            if _trusted().get(_project_key(project_root)) == fingerprint(risky):
                data = _deep_merge(data, risky)
            else:
                pending = describe_risky(risky) or ["project settings that need trust"]
    try:
        cfg = AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc
    cfg.untrusted_project = pending
    return cfg


def update_global_config(section: str, values: dict[str, Any]) -> Path:
    """Merge ``values`` into ``[section]`` of the global config file and write it back."""
    return _update_config_file(global_config_path(), section, values)


def update_project_config(project_root: Path, section: str, values: dict[str, Any]) -> Path:
    """Merge ``values`` into ``[section]`` of ``<project>/.trendlab/config.toml``."""
    return _update_config_file(project_config_path(project_root), section, values)


def _update_config_file(path: Path, section: str, values: dict[str, Any]) -> Path:
    data = _read_toml(path)
    data[section] = _deep_merge(data.get(section, {}), values)
    # Validate the merged result before persisting.
    try:
        AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"refusing to write invalid configuration: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)
    return path

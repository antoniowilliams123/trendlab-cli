"""Plugins and marketplaces.

A plugin is a folder (or git repository) with a ``trendlab-plugin.toml`` manifest that bundles
what TrendLab already knows how to load::

    my-plugin/
      trendlab-plugin.toml        name, version, description, [[hooks]], [mcp.servers.<name>]
      commands/<name>.md          slash commands (same format as ~/.trendlab/commands)
      skills/<name>/SKILL.md      skills (same format as ~/.trendlab/skills)

A marketplace is a folder, git repository or https URL with a ``trendlab-marketplace.json``
catalogue: ``{"name": ..., "plugins": [{"name", "description", "version", "source"}]}``. A
plugin ``source`` is a path relative to the catalogue, a git URL, or ``{"git": url, "ref": r}``.

Trust model: plugins live only under the user's TrendLab folder (``plugins/``); a project cannot
install or enable one. Installing shows every command the plugin's hooks and MCP servers would
run and asks first. Git installs are pinned to the commit that was reviewed; ``update`` shows
the new commit before it replaces the old one.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

MANIFEST = "trendlab-plugin.toml"
CATALOGUE = "trendlab-marketplace.json"
BUILTIN = "trendlab"
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "marketplace"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class PluginError(Exception):
    pass


@dataclass
class Plugin:
    name: str
    version: str
    description: str
    path: Path
    hooks: list[dict[str, Any]] = field(default_factory=list)
    mcp: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    def runs(self) -> list[str]:
        """Every command line this plugin can make TrendLab execute."""
        out = [f"hook {h.get('event')}: {h.get('command')}" for h in self.hooks]
        for name, srv in self.mcp.items():
            args = " ".join(str(a) for a in srv.get("args") or [])
            out.append(f"mcp {name}: {srv.get('command')} {args}".rstrip())
        return out


def read_plugin(path: Path) -> Plugin:
    """Parse and validate a plugin folder."""
    manifest = path / MANIFEST
    if not manifest.is_file():
        raise PluginError(f"no {MANIFEST} in {path.name}")
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PluginError(f"{MANIFEST}: {exc}") from exc
    name = str(data.get("name") or "")
    if not _NAME.match(name):
        raise PluginError(f"plugin name {name!r} must be lowercase letters, digits and dashes")
    hooks = [h for h in data.get("hooks") or [] if isinstance(h, dict)]
    for h in hooks:
        if not h.get("event") or not h.get("command"):
            raise PluginError("every [[hooks]] entry needs event and command")
    mcp = (data.get("mcp") or {}).get("servers") or {}
    cmds = sorted(p.stem for p in (path / "commands").glob("*.md"))
    skills = sorted(
        d.name for d in (path / "skills").glob("*") if d.is_dir() and (d / "SKILL.md").is_file()
    )
    return Plugin(
        name=name,
        version=str(data.get("version") or "0.0.0"),
        description=str(data.get("description") or "")[:200],
        path=path,
        hooks=hooks,
        mcp={str(k): dict(v) for k, v in mcp.items() if isinstance(v, dict)},
        commands=cmds,
        skills=skills,
    )


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PluginError(f"git {args[0]} failed: {exc}") from exc
    if out.returncode != 0:
        raise PluginError(f"git {args[0]} failed: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def _is_git(source: str) -> bool:
    return (
        source.endswith(".git")
        or source.startswith(("git@", "ssh://"))
        or (source.startswith("https://") and not source.endswith(".json"))
    )


def _tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file() and ".git" not in p.parts):
        h.update(str(f.relative_to(path)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


class PluginManager:
    def __init__(self, home: Path) -> None:
        self.root = home / "plugins"
        self.installed_dir = self.root / "installed"
        self.cache = self.root / "marketplaces"
        self.state_file = self.root / "state.json"

    # -- state ------------------------------------------------------------------------------
    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("marketplaces", {})
        data.setdefault("plugins", {})
        data["marketplaces"].setdefault(BUILTIN, {"source": str(BUILTIN_DIR), "builtin": True})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    # -- marketplaces ---------------------------------------------------------------------
    def add_marketplace(self, source: str, name: str = "") -> str:
        source = source.strip()
        local = Path(source).expanduser()
        if local.exists():
            source = str(local.resolve())
        catalogue = self._fetch_catalogue(source)
        name = name or str(catalogue.get("name") or "")
        if not _NAME.match(name):
            raise PluginError("give the marketplace a name: lowercase letters, digits, dashes")
        data = self._state()
        if name == BUILTIN:
            raise PluginError(f"{BUILTIN!r} is the built-in marketplace")
        data["marketplaces"][name] = {"source": source, "added": time.time()}
        self._save(data)
        return name

    def remove_marketplace(self, name: str) -> None:
        data = self._state()
        if name == BUILTIN or name not in data["marketplaces"]:
            raise PluginError(f"no removable marketplace {name!r}")
        del data["marketplaces"][name]
        shutil.rmtree(self.cache / name, ignore_errors=True)
        self._save(data)

    def marketplaces(self) -> dict[str, dict[str, Any]]:
        return self._state()["marketplaces"]

    def _fetch_catalogue(self, source: str, name: str = "") -> dict[str, Any]:
        """Read a catalogue from a folder, a git repository or a JSON URL."""
        base = self._marketplace_dir(source, name)
        if isinstance(base, dict):
            return base
        path = base / CATALOGUE
        if not path.is_file():
            raise PluginError(f"no {CATALOGUE} in {source}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise PluginError(f"{CATALOGUE}: {exc}") from exc
        data["_base"] = str(base)
        return data

    def _marketplace_dir(self, source: str, name: str) -> Path | dict[str, Any]:
        if source.startswith("https://") and source.endswith(".json"):
            try:
                resp = httpx.get(source, timeout=20, follow_redirects=True)
                resp.raise_for_status()
                return {**resp.json(), "_base": ""}
            except (httpx.HTTPError, ValueError) as exc:
                raise PluginError(f"could not fetch {source}: {exc}") from exc
        if _is_git(source):
            dest = self.cache / (name or hashlib.sha1(source.encode()).hexdigest()[:10])
            if (dest / ".git").is_dir():
                _git("pull", "--ff-only", "-q", cwd=dest)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _git("clone", "--depth", "1", "-q", source, str(dest))
            return dest
        path = Path(source).expanduser()
        if not path.is_dir():
            raise PluginError(f"marketplace not found: {source}")
        return path

    def available(self, query: str = "") -> list[dict[str, Any]]:
        """Every plugin every marketplace offers (unreachable marketplaces are skipped)."""
        out = []
        q = query.lower()
        for mname, m in self.marketplaces().items():
            try:
                cat = self._fetch_catalogue(m["source"], mname)
            except PluginError:
                continue
            for p in cat.get("plugins") or []:
                if not isinstance(p, dict) or not p.get("name"):
                    continue
                text = f"{p.get('name')} {p.get('description', '')}".lower()
                if q and q not in text:
                    continue
                out.append({**p, "marketplace": mname, "_base": cat.get("_base", "")})
        return out

    # -- install ----------------------------------------------------------------------------
    def fetch(self, spec: str) -> tuple[Plugin, dict[str, Any], Path]:
        """Download a plugin to a temporary folder for review. ``spec`` is ``name`` or
        ``name@marketplace``. Returns (plugin, catalogue entry, temp dir to clean up)."""
        name, _, market = spec.partition("@")
        matches = [p for p in self.available() if p["name"] == name]
        if market:
            matches = [p for p in matches if p["marketplace"] == market]
        if not matches:
            raise PluginError(f"no plugin {spec!r} in any marketplace (trendlab plugin search)")
        if len(matches) > 1:
            where = ", ".join(p["marketplace"] for p in matches)
            raise PluginError(f"{name!r} is in several marketplaces ({where}); use {name}@<one>")
        entry = matches[0]
        tmp = Path(tempfile.mkdtemp(prefix="trendlab-plugin-"))
        src = entry.get("source")
        ref = ""
        if isinstance(src, dict):
            src, ref = str(src.get("git") or ""), str(src.get("ref") or "")
        src = str(src or "")
        if _is_git(src):
            _git("clone", "-q", src, str(tmp / "p"))
            if ref:
                _git("checkout", "-q", ref, cwd=tmp / "p")
            entry["_commit"] = _git("rev-parse", "HEAD", cwd=tmp / "p")
        else:
            base = Path(entry.get("_base") or ".")
            path = (base / src).resolve()
            if not path.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
                raise PluginError(f"plugin folder not found: {src}")
            shutil.copytree(path, tmp / "p", ignore=shutil.ignore_patterns(".git"))
            entry["_commit"] = "tree:" + _tree_hash(tmp / "p")
        try:
            plugin = read_plugin(tmp / "p")
        except PluginError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        if plugin.name != name:
            shutil.rmtree(tmp, ignore_errors=True)
            raise PluginError(f"catalogue says {name!r} but the manifest says {plugin.name!r}")
        return plugin, entry, tmp

    def install(self, plugin: Plugin, entry: dict[str, Any], tmp: Path) -> Path:
        """Move a reviewed plugin into place and enable it."""
        dest = self.installed_dir / plugin.name
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp / "p"), dest)
        shutil.rmtree(tmp, ignore_errors=True)
        data = self._state()
        data["plugins"][plugin.name] = {
            "version": plugin.version,
            "marketplace": entry.get("marketplace"),
            "pinned": entry.get("_commit"),
            "enabled": True,
            "installed": time.time(),
        }
        self._save(data)
        return dest

    def uninstall(self, name: str) -> None:
        data = self._state()
        if name not in data["plugins"]:
            raise PluginError(f"{name!r} is not installed")
        shutil.rmtree(self.installed_dir / name, ignore_errors=True)
        del data["plugins"][name]
        self._save(data)

    def set_enabled(self, name: str, enabled: bool) -> None:
        data = self._state()
        if name not in data["plugins"]:
            raise PluginError(f"{name!r} is not installed")
        data["plugins"][name]["enabled"] = enabled
        self._save(data)

    def installed(self) -> list[tuple[Plugin, dict[str, Any]]]:
        out = []
        for name, meta in sorted(self._state()["plugins"].items()):
            try:
                out.append((read_plugin(self.installed_dir / name), meta))
            except PluginError:
                continue
        return out

    def enabled(self) -> list[Plugin]:
        return [p for p, meta in self.installed() if meta.get("enabled", True)]

    # -- what enabled plugins contribute ---------------------------------------------------------
    def command_dirs(self) -> list[tuple[Path, str]]:
        return [(p.path / "commands", f"plugin:{p.name}") for p in self.enabled()]

    def skill_dirs(self) -> list[Path]:
        return [p.path / "skills" for p in self.enabled()]

    def hooks(self) -> list[dict[str, Any]]:
        out = []
        for p in self.enabled():
            for h in p.hooks:
                cmd = str(h["command"]).replace("${PLUGIN_DIR}", str(p.path))
                out.append({**h, "command": cmd})
        return out

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        out = {}
        for p in self.enabled():
            for name, srv in p.mcp.items():
                fixed = {
                    **srv,
                    "command": str(srv.get("command", "")).replace("${PLUGIN_DIR}", str(p.path)),
                    "args": [
                        str(a).replace("${PLUGIN_DIR}", str(p.path)) for a in srv.get("args") or []
                    ],
                }
                out[f"{p.name}-{name}"] = fixed
        return out


def scaffold(dest: Path, name: str) -> Path:
    """A minimal plugin to start from: one command, one skill, commented hook and MCP blocks."""
    if not _NAME.match(name):
        raise PluginError("name: lowercase letters, digits and dashes")
    root = dest / name
    if root.exists():
        raise PluginError(f"{root} already exists")
    (root / "commands").mkdir(parents=True)
    (root / "skills" / name).mkdir(parents=True)
    (root / MANIFEST).write_text(
        f'name = "{name}"\nversion = "0.1.0"\ndescription = "What {name} adds"\n\n'
        '# [[hooks]]\n# event = "after_write"\n# command = "${PLUGIN_DIR}/format.sh"\n\n'
        '# [mcp.servers.example]\n# command = "npx"\n# args = ["-y", "some-mcp-server"]\n',
        encoding="utf-8",
    )
    (root / "commands" / f"{name}.md").write_text(
        f"---\ndescription: Example command from the {name} plugin\n---\n"
        "Explain what $ARGUMENTS does in this repository, in plain words.\n",
        encoding="utf-8",
    )
    (root / "skills" / name / "SKILL.md").write_text(
        f"# {name}\n\nInstructions the agent follows when this skill is active.\n",
        encoding="utf-8",
    )
    return root

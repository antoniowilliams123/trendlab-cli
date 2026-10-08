"""Plugins and marketplaces: review-then-install, pinning, loading, trust boundaries."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trendlab.cli import app as cli_app
from trendlab.config.loader import global_config_path
from trendlab.extensions.plugins import PluginError, PluginManager, read_plugin, scaffold


def _pm() -> PluginManager:
    return PluginManager(global_config_path().parent)


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _market(tmp: Path, *, source=None, hook=True) -> Path:
    m = tmp / "market"
    p = m / "plugins" / "demo"
    (p / "commands").mkdir(parents=True)
    (p / "commands" / "hello.md").write_text("Say hello to $ARGUMENTS\n")
    hooks = '[[hooks]]\nevent = "after_write"\ncommand = "echo ${PLUGIN_DIR}"\n' if hook else ""
    (p / "trendlab-plugin.toml").write_text(
        f'name = "demo"\nversion = "1.0.0"\ndescription = "demo"\n{hooks}'
        '[mcp.servers.files]\ncommand = "${PLUGIN_DIR}/srv"\nargs = ["${PLUGIN_DIR}/x"]\n'
    )
    (m / "trendlab-marketplace.json").write_text(
        json.dumps(
            {"name": "mine", "plugins": [{"name": "demo", "source": source or "./plugins/demo"}]}
        )
    )
    return m


def test_builtin_marketplace_installs_and_feeds_the_loaders(_trendlab_home: Path):
    pm = _pm()
    assert {p["name"] for p in pm.available()} >= {"plain-english", "review-pack", "python-format"}
    plugin, entry, tmp = pm.fetch("review-pack")
    assert plugin.commands == ["perf-review", "security-review"] and plugin.runs() == []
    pm.install(plugin, entry, tmp)
    assert [d.name for d, _ in pm.command_dirs()] == ["commands"]
    fmt, entry, tmp = pm.fetch("python-format")
    assert fmt.runs() and "format.sh" in fmt.runs()[0]
    pm.install(fmt, entry, tmp)
    assert pm.hooks()[0]["command"].endswith("python-format/format.sh")  # ${PLUGIN_DIR} filled
    pm.set_enabled("python-format", False)
    assert pm.hooks() == []
    pm.uninstall("review-pack")
    assert pm.command_dirs() == [] and not (pm.installed_dir / "review-pack").exists()


def test_local_marketplace_hooks_and_mcp_are_namespaced(_trendlab_home: Path, tmp_path: Path):
    pm = _pm()
    assert pm.add_marketplace(str(_market(tmp_path))) == "mine"
    plugin, entry, tmp = pm.fetch("demo@mine")
    assert any(r.startswith("hook after_write") for r in plugin.runs())
    assert any(r.startswith("mcp files") for r in plugin.runs())
    pm.install(plugin, entry, tmp)
    servers = pm.mcp_servers()
    assert list(servers) == ["demo-files"]
    assert servers["demo-files"]["command"].endswith("/demo/srv")
    assert pm._state()["plugins"]["demo"]["pinned"].startswith("tree:")  # noqa: SLF001
    with pytest.raises(PluginError):
        pm.fetch("nope")


def test_git_plugins_are_pinned_and_updates_are_detected(_trendlab_home: Path, tmp_path: Path):
    repo = tmp_path / "demo-repo"
    (repo / "commands").mkdir(parents=True)
    (repo / "trendlab-plugin.toml").write_text('name = "demo"\nversion = "1.0.0"\n')
    (repo / "commands" / "hi.md").write_text("hi\n")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "v1", cwd=repo)
    market = _market(tmp_path, source=str(repo) + "/.git")  # a git source, cloned
    pm = _pm()
    pm.add_marketplace(str(market))
    plugin, entry, tmp = pm.fetch("demo")
    first = entry["_commit"]
    assert len(first) == 40
    pm.install(plugin, entry, tmp)
    (repo / "commands" / "hi.md").write_text("hi again\n")
    _git("commit", "-q", "-am", "v2", cwd=repo)
    _, entry2, tmp2 = pm.fetch("demo")
    assert entry2["_commit"] != first and pm._state()["plugins"]["demo"]["pinned"] == first  # noqa: SLF001


def test_bad_manifests_and_names_are_refused(tmp_path: Path):
    (tmp_path / "trendlab-plugin.toml").write_text('name = "Bad Name"\n')
    with pytest.raises(PluginError):
        read_plugin(tmp_path)
    with pytest.raises(PluginError):
        read_plugin(tmp_path / "missing")
    root = scaffold(tmp_path, "my-tool")
    p = read_plugin(root)
    assert p.commands == ["my-tool"] and p.skills == ["my-tool"] and p.runs() == []


def test_cli_review_shows_what_runs_and_respects_no(_trendlab_home: Path, tmp_path: Path):
    runner = CliRunner()
    _pm().add_marketplace(str(_market(tmp_path)))
    res = runner.invoke(cli_app, ["plugin", "install", "demo"], input="n\n")
    assert "runs these commands on your machine" in res.output and "not installed" in res.output
    assert _pm().installed() == []
    res = runner.invoke(cli_app, ["plugin", "install", "demo", "--yes"])
    assert res.exit_code == 0 and "installed demo" in res.output
    res = runner.invoke(cli_app, ["plugin", "list"])
    assert "/hello" in res.output and "hook after_write" in res.output
    res = runner.invoke(cli_app, ["plugin", "search", "pack"])
    assert "review-pack" in res.output and "demo" not in res.output


async def test_session_loads_plugin_commands_and_skills(_trendlab_home: Path, project: Path):
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.config.loader import load_config
    from trendlab.providers.base import ModelResponse
    from trendlab.providers.scripted import ScriptedProvider

    pm = _pm()
    pm.install(*pm.fetch("plain-english"))
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    tl = TrendLabApp(
        project,
        cfg,
        provider=ScriptedProvider([ModelResponse(text="ok")]),
        model_ref="scripted:m",
        console=Console(quiet=True),
    )
    await tl.start(interactive=False)
    try:
        assert tl.custom_commands.get("plain").source == "plugin:plain-english"
        assert tl.skills.get("plain-english") is not None
    finally:
        await tl.stop()


@pytest.mark.parametrize(
    "source",
    [
        "--upload-pack=touch PWNED;.git",
        "ext::sh -c touch% PWNED.git",
        "https://x/y.git\n--upload-pack=touch PWNED",
        {"git": "https://example.invalid/p.git", "ref": "--output=PWNED"},
    ],
)
def test_hostile_catalogue_sources_never_reach_git(
    _trendlab_home: Path, tmp_path: Path, monkeypatch, source
):
    monkeypatch.chdir(tmp_path)
    m = tmp_path / "evil"
    m.mkdir()
    (m / "trendlab-marketplace.json").write_text(
        json.dumps({"name": "evil", "plugins": [{"name": "demo", "source": source}]})
    )
    pm = _pm()
    pm.add_marketplace(str(m))
    calls = []
    import trendlab.extensions.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_git", lambda *a, **k: calls.append(a) or "")
    with pytest.raises(PluginError, match="refusing"):
        pm.fetch("demo")
    assert calls == [] and not list(tmp_path.rglob("PWNED*"))


def test_git_runs_with_ext_transport_disabled(monkeypatch):
    import trendlab.extensions.plugins as plugins_mod

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(plugins_mod.subprocess, "run", fake_run)
    plugins_mod._git("clone", "-q", "--", "https://x/y.git", "d")  # noqa: SLF001
    assert seen["argv"][:3] == ["git", "-c", "protocol.allow=never"]
    assert "protocol.ext.allow=always" not in seen["argv"]
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["argv"].index("--") < seen["argv"].index("https://x/y.git")

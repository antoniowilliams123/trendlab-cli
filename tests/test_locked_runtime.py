"""IL-0 locked runtime (evals spec §30.2): editing the worker's writable dev clone or home cannot change
the live agent's configuration, instructions, memory, skills, commands, drivers or permission rules."""

import json
from pathlib import Path

import pytest

from trendlab.config import runtime
from trendlab.config.loader import ConfigError, load_config, update_global_config

POISON = "POISON-FROM-WRITABLE-PLACE"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def plant_behavior(root: Path, marker: str, *, cost: float) -> None:
    """Every behavioral file a project can carry, each tagged with ``marker``."""
    write(root / "TRENDLAB.md", f"# {marker} instructions\n")
    write(root / "AGENTS.md", f"{marker} agents\n")
    write(root / ".trendlab" / "memory.md", f"# Project memory\n- 2026-10-11: {marker} fact\n")
    write(
        root / ".trendlab" / "config.toml",
        f'[limits]\nmax_cost_usd = {cost}\n[[hooks]]\nevent = "before_tool"\ncommand = "echo {marker}"\n',
    )
    name = marker.lower()
    write(
        root / ".trendlab" / "skills" / f"{name}-skill" / "SKILL.md",
        f"---\nname: {name}-skill\ndescription: {marker}\n---\n{marker}\n",
    )
    write(root / ".trendlab" / "commands" / f"{name}.md", f"{marker} command\n")
    write(root / ".trendlab" / "skills" / "_model" / "scripted-m.md", f"{marker} project driver\n")
    write(root / ".trendlab" / "permissions.toml", f'[rules]\n"shell:{marker}" = "allow"\n')


@pytest.fixture
def places(tmp_path, monkeypatch):
    dev = tmp_path / "dev-clone"
    home = tmp_path / "worker-home"
    runtime_dir = tmp_path / "runtime"
    prod = tmp_path / "prod"
    plant_behavior(dev, POISON, cost=99.0)
    # the worker's own home: config, skills, commands, model drivers, and a trust entry for the dev clone
    write(
        home / "config.toml",
        '[limits]\nmax_cost_usd = 77.0\n[defaults]\npermission_mode = "plan"\n',
    )
    write(
        home / "skills" / "home-poison" / "SKILL.md",
        f"---\nname: home-poison\ndescription: {POISON}\n---\n{POISON}\n",
    )
    write(home / "commands" / "home-poison.md", f"{POISON}\n")
    write(home / "skills" / "_model" / "scripted-m.md", f"{POISON} driver\n")
    write(home / "trusted_projects.json", json.dumps({str(dev.resolve()): "x"}))
    # referee-owned runtime and production context
    write(runtime_dir / "config.toml", "[limits]\nmax_cost_usd = 5.0\n")
    write(
        runtime_dir / "skills" / "runtime-skill" / "SKILL.md",
        "---\nname: runtime-skill\ndescription: ok\n---\nok\n",
    )
    write(prod / "TRENDLAB.md", "# PROD-RULES\n")
    write(prod / ".trendlab" / "memory.md", "# Project memory\n- 2026-10-11: PROD-FACT\n")
    monkeypatch.setenv("TRENDLAB_HOME", str(home))
    monkeypatch.setenv("TRENDLAB_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("TRENDLAB_PROJECT_CONTEXT", str(prod))
    monkeypatch.chdir(dev)
    return dev, home, runtime_dir, prod


def test_locked_config_comes_only_from_the_runtime(places):
    dev, *_ = places
    cfg = load_config(dev)
    assert runtime.locked()
    assert cfg.limits.max_cost_usd == 5.0
    assert not cfg.hooks
    assert cfg.untrusted_project == []


def test_locked_prompt_uses_production_instructions_not_the_dev_clone(places):
    from trendlab.agent.prompt import build_system_prompt

    dev, *_ = places
    prompt = build_system_prompt(dev)
    assert "PROD-RULES" in prompt and POISON not in prompt


def test_locked_memory_skills_commands_and_drivers_ignore_writable_places(places):
    from trendlab.config.schema import AppConfig
    from trendlab.context.project_memory import ProjectMemory
    from trendlab.extensions.commands import CustomCommandLibrary
    from trendlab.extensions.skills import SkillLibrary
    from trendlab.prompts.drivers import driver_text

    dev, home, *_ = places
    ctx, beh = runtime.context_root(dev), runtime.behavior_home(home)
    memory = ProjectMemory(ctx).render_for_prompt()
    assert "PROD-FACT" in memory and POISON not in memory
    skills = [s.name for s in SkillLibrary(ctx, beh).list()]
    assert "runtime-skill" in skills and not any("poison" in n for n in skills)
    commands = [c.name for c in CustomCommandLibrary(ctx, beh).list()]
    assert not any("poison" in n for n in commands)
    assert POISON not in driver_text(AppConfig(), "scripted:m", ctx, beh)


def test_locked_project_rules_are_never_written(places):
    from trendlab.permissions.models import Decision
    from trendlab.permissions.rules import ProjectRules

    dev, *_ = places
    rules = ProjectRules(runtime.context_root(dev))
    assert rules.lookup(f"shell:{POISON}") is None
    rules.add("shell:echo hi", Decision.ALLOW)
    assert rules.lookup("shell:echo hi") == Decision.ALLOW  # this session only
    assert not (runtime.context_root(dev) / ".trendlab" / "permissions.toml").exists()


def test_locked_runtime_refuses_to_persist_global_settings(places):
    with pytest.raises(ConfigError, match="locked"):
        update_global_config("limits", {"max_cost_usd": 1000.0})


def test_live_app_system_prompt_is_unchanged_by_dev_clone_edits(places):
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
    from trendlab.providers.scripted import ScriptedProvider

    dev, *_ = places
    cfg = load_config(dev)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    app = TrendLabApp(
        dev,
        cfg,
        provider=ScriptedProvider([]),
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    before = app._system_prompt_for(dev)
    plant_behavior(
        dev, "SECOND-EDIT", cost=55.0
    )  # the worker edits its dev clone again mid-session
    after = app._system_prompt_for(dev)
    assert POISON not in before and "SECOND-EDIT" not in after and "PROD-RULES" in after
    assert load_config(dev).limits.max_cost_usd == 5.0


async def test_started_live_app_loads_extensions_only_from_locked_places(places):
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
    from trendlab.providers.scripted import ScriptedProvider

    dev, _home, runtime_dir, _prod = places
    cfg = load_config(dev)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    app = TrendLabApp(
        dev,
        cfg,
        provider=ScriptedProvider([]),
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    await app.start(interactive=False)
    try:
        assert app.plugins.root == runtime_dir / "plugins"
        skills = [s.name for s in app.skills.list()]
        assert "runtime-skill" in skills and not any("poison" in n for n in skills)
        assert not any("poison" in c.name for c in app.custom_commands.list())
        assert app.engine.project_rules.lookup(f"shell:{POISON}") is None
        assert POISON not in app.context.system_prompt
        app.switch_project_root(dev)  # a worktree switch keeps the locked sources
        assert app.engine.project_rules.lookup(f"shell:{POISON}") is None
        assert not any("poison" in c.name for c in app.custom_commands.list())
    finally:
        await app.stop()


def test_locked_statusline_command_ignores_the_worker_home(places):
    from trendlab.ui.statusline import configured_command

    _dev, home, runtime_dir, _prod = places
    write(home / "config.toml", f'[ui]\nstatusline = "echo {POISON}"\n')
    assert POISON not in configured_command()[0]
    write(runtime_dir / "config.toml", '[ui]\nstatusline = "echo runtime"\n')
    assert configured_command()[0] == "echo runtime"


def test_unlocked_sessions_still_read_the_project(tmp_path, monkeypatch):
    """The desktop and every unlocked use keep today's behavior (the poison is read)."""
    from trendlab.agent.prompt import build_system_prompt

    dev = tmp_path / "dev"
    plant_behavior(dev, POISON, cost=99.0)
    monkeypatch.setenv("TRENDLAB_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TRENDLAB_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TRENDLAB_PROJECT_CONTEXT", raising=False)
    monkeypatch.chdir(dev)
    assert not runtime.locked()
    assert POISON in build_system_prompt(dev)
    assert load_config(dev).limits.max_cost_usd == 99.0

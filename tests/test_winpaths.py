"""Windows paths pasted into prompts become WSL paths before the model sees them."""

from pathlib import Path

from trendlab.ui.winpaths import translate_windows_paths


def test_translate_unc_and_drive_paths():
    text = (
        r"\\wsl$\Ubuntu\home\tony\FUTURES_DATA\results\weekly_templates did my packs run? "
        r"also see \\wsl.localhost\Ubuntu\home\tony\x\y.txt and C:\Users\anton\Downloads\spec.md "
        "and D:/data/file.csv; leave https://x.y/a:b alone and 10:30 alone"
    )
    out, n = translate_windows_paths(text, force=True)
    assert n == 4
    assert "/home/tony/FUTURES_DATA/results/weekly_templates did my packs run?" in out
    assert "/home/tony/x/y.txt" in out
    assert "/mnt/c/Users/anton/Downloads/spec.md" in out and "/mnt/d/data/file.csv" in out
    assert "https://x.y/a:b" in out and "10:30" in out
    assert "\\" not in out


def test_noop_when_not_wsl(monkeypatch):
    import trendlab.ui.winpaths as w

    monkeypatch.setattr(w, "is_wsl", lambda: False)
    text = r"C:\Users\x"
    assert translate_windows_paths(text) == (text, 0)


async def test_run_prompt_translates_and_attaches(project: Path, _trendlab_home: Path):
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.config.loader import load_config
    from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
    from trendlab.providers.base import ModelResponse
    from trendlab.providers.scripted import ScriptedProvider
    from trendlab.telemetry.events import EventType

    class Rec(ScriptedProvider):
        seen: list[str] = []

        async def complete(self, messages, tools=None):
            self.seen.append(messages[-1]["content"])
            return await super().complete(messages, tools)

    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    tl = TrendLabApp(
        project,
        cfg,
        provider=Rec([ModelResponse(text="ok")]),
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(record=True, width=100, force_terminal=False),
    )
    await tl.start(interactive=False)
    try:
        import trendlab.ui.winpaths as w

        w.is_wsl = lambda: True
        await tl.run_prompt(r"look at C:\stuff\notes.txt please")
        assert Rec.seen[-1] == "look at /mnt/c/stuff/notes.txt please"
        assert any(
            e["type"] == EventType.PATHS_TRANSLATED.value for e in tl.store.events(tl.session_id)
        )
    finally:
        await tl.stop()

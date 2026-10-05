from pathlib import Path

from trendlab.benchmarks.fixtures import FIXTURES, materialize
from trendlab.benchmarks.runner import run_fixture, suite_passes
from trendlab.config.schema import AppConfig, ProviderConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider


def test_fixtures_start_broken(tmp_path: Path):
    for name in FIXTURES:
        root = tmp_path / name
        prompt = materialize(name, root)
        assert prompt and (root / "pyproject.toml").exists()
        # D (missing edge-case test) and E (refactor) start green by design; A-C start red.
        assert suite_passes(root) == (name in {"D", "E"}), name


async def test_runner_scores_a_scripted_fix(_trendlab_home: Path):
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="1", name="run_tests", arguments={"kind": "test"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="2",
                        name="patch_file",
                        arguments={"path": "orders.py", "old_text": " - 1", "new_text": ""},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="3", name="run_tests", arguments={"kind": "test"})]
            ),
            ModelResponse(text="Fixed the off-by-one in order_total; tests pass."),
        ]
    )
    cfg = AppConfig(providers={"scripted": ProviderConfig()})
    cfg.remote_approval.enabled = False
    result = await run_fixture(
        "A", "scripted:m", config=cfg, provider=provider, home=_trendlab_home
    )
    assert result["success"] and not result["tests_before"] and result["tests_after"]
    assert result["files_changed"] == 1 and result["unnecessary_changes"] == []
    assert (
        result["model_calls"] == 4
        and result["tool_calls"] == 3
        and result["human_interventions"] == 0
    )

"""Uplift U3: served-model and prompt-hash stamps, holdout split, canary cause hints, drift history."""

from pathlib import Path

from trendlab.agent.runtime import prompt_hash
from trendlab.benchmarks.runner import CANARY, select_tasks
from trendlab.benchmarks.suite import HOLDOUT_IDS, TASKS
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType

from .test_agent_runtime import make_agent


def test_holdout_split_is_disjoint_and_stable():
    hold = select_tasks(None, None, "all", holdout=True)
    rest = select_tasks(None, None, "all")
    assert len(hold) == 10 and {t.id.split("-")[0] for t in hold} == HOLDOUT_IDS
    assert not ({t.id for t in hold} & {t.id for t in rest}) and len(hold) + len(rest) == len(TASKS)
    assert all(
        not t.holdout for t in select_tasks(CANARY, None, "all")
    )  # canary never touches holdout


async def test_calls_carry_served_model_and_prompt_hash(
    project: Path, manager_factory, events, recorder
):
    provider = ScriptedProvider(
        [ModelResponse(text="hello", raw_metadata={"model": "deepseek-flash-2026-09"})]
    )
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    await agent.run("say hello")
    rec = agent.costs.records[0]
    assert rec.model_served == "deepseek-flash-2026-09" and rec.prompt_hash == prompt_hash("sys")
    ev = recorder.of_type(EventType.MODEL_CALL_COMPLETED)[0].data
    assert ev["model_served"] == "deepseek-flash-2026-09" and len(ev["prompt_hash"]) == 12
    assert prompt_hash("a") != prompt_hash("b") and prompt_hash("") == prompt_hash("")


async def test_canary_cause_hints_and_history(tmp_path: Path, monkeypatch, _trendlab_home: Path):
    from trendlab.config.schema import AppConfig
    from trendlab.engine import jobs
    from trendlab.engine.inbox import Inbox

    rows_a = [
        {"task": f"t{i}", "passes": True, "model_served": ["m-v1"], "prompt_hash": ["p1"]}
        for i in range(10)
    ]
    rows_b = [
        {"task": f"t{i}", "passes": i >= 3, "model_served": ["m-v2"], "prompt_hash": ["p1"]}
        for i in range(10)
    ]
    seq = [rows_a, rows_b]

    async def fake_suite(model, **kw):
        return seq.pop(0)

    monkeypatch.setattr(jobs, "run_suite", fake_suite)
    cfg = AppConfig()
    cfg.engine.canary_drop_alert = 2
    inbox = Inbox(tmp_path / "inbox.db")
    state: dict = {}
    first = await jobs.canary_run(inbox, cfg, state)
    assert (
        first["passes"] == 1.0
        and "vs_previous" not in first
        and state["canary_meta"]["model_served"] == ["m-v1"]
    )
    second = await jobs.canary_run(inbox, cfg, state)
    assert (
        second["vs_previous"]["b_losses"] == 3
        and "model version changed" in second["cause_hints"][0]
    )
    card = inbox.get(second["card"])
    assert (
        card["source"] == "canary"
        and "model version changed" in card["root_cause"]
        and "t0" in card["root_cause"]
    )
    hist = (_trendlab_home / "engine" / "canary_history.jsonl").read_text().splitlines()
    assert len(hist) == 2 and '"m-v2"' in hist[1]
    inbox.close()

"""Router: rule router, model router with safety floor, per-run settings and restore,
held-out accuracy floor."""

import json
from pathlib import Path

from trendlab.agent.router import Route, model_route, parse_route, rule_route, settings_for
from trendlab.benchmarks.suite import TASKS, expected_route
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType

from .test_agent_runtime import make_agent

HOLDOUT = Path(__file__).parent / "data" / "router_holdout.jsonl"


def test_rule_router_categories():
    assert rule_route("where is tax applied?").kind == "question"
    assert rule_route("fix the crash on login and add a regression test").kind == "small_fix"
    assert rule_route("add dark mode to the dashboard").kind == "feature"
    assert rule_route("drop the legacy users table").kind == "risky"
    assert rule_route("Unexpected token in JSON").kind == "small_fix"  # not a credential


def test_rule_router_accuracy_floors():
    suite = [(expected_route(t), rule_route(t.prompt).kind) for t in TASKS]
    assert sum(1 for e, g in suite if e == g) / len(suite) >= 0.95
    rows = [json.loads(ln) for ln in HOLDOUT.read_text().splitlines() if ln.strip()]
    held = [(r["kind"], rule_route(r["prompt"]).kind) for r in rows]
    assert len(rows) == 40 and sum(1 for e, g in held if e == g) / len(held) >= 0.9


async def test_model_router_parses_and_keeps_safety_floor():
    assert parse_route('{"kind": "feature", "confidence": 0.8}').kind == "feature"
    assert parse_route('{"kind": "nonsense"}') is None and parse_route("x") is None

    async def says_fix(msgs):
        return '{"kind": "small_fix", "confidence": 0.9, "reason": "bug"}'

    r = await model_route(says_fix, "drop the users table, it is buggy")
    assert r.kind == "risky" and r.by == "model"  # the rule floor upgrades a missed risk

    async def garbage(msgs):
        return "no idea"

    assert (await model_route(garbage, "where is X?")).by == "rules"  # falls back to rules


def test_settings_per_kind():
    assert settings_for(Route("question", 1, "", "rules")) == {"planner_min_chars": 10**9}
    assert settings_for(Route("small_fix", 1, "", "rules")) == {}
    assert settings_for(Route("feature", 1, "", "rules")) == {"planner_min_chars": 150}
    assert settings_for(Route("risky", 1, "", "rules")) == {
        "verify_min_diff_lines": 0,
        "verify_min_files": 1,
    }


async def test_route_applied_for_the_run_then_restored(
    project: Path, manager_factory, events, recorder
):
    agent, _ = make_agent(
        project,
        manager_factory(),
        events,
        ScriptedProvider([ModelResponse(text="ok")]),
        mode=PermissionMode.UNSAFE,
    )
    agent.verify_min_diff_lines, agent.verify_min_files = 80, 3

    async def risky(prompt):
        return Route("risky", 0.9, "sensitive", "model")

    agent.router = risky
    await agent.run("rotate the api key")
    ev = recorder.of_type(EventType.ROUTE_DECIDED)[0].data
    assert ev["kind"] == "risky" and ev["settings"] == {
        "verify_min_diff_lines": 0,
        "verify_min_files": 1,
    }
    assert (agent.verify_min_diff_lines, agent.verify_min_files) == (
        80,
        3,
    )  # restored after the run

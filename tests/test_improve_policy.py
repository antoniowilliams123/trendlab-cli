"""IL-B drafting policy (evals spec §30.4, §30.5, D-115): every gate fails closed, in order."""

import json
import os
import shutil
import stat
from datetime import datetime, timedelta

import pytest

from trendlab.config.schema import (
    AppConfig,
    HookConfig,
    McpServerConfig,
    ModelPricing,
    PermissionMode,
)
from trendlab.engine import improve_policy as pol
from trendlab.engine.improve_policy import (
    NY,
    Switch,
    blackout_reason,
    decide,
    draft_config,
    mark_needs_tony,
    price_check,
    read_consumed,
    read_switch,
    sandbox_probe,
)
from trendlab.engine.inbox import Inbox
from trendlab.engine.meta import HARNESS_PROJECT

RUN = "r-20261012T053000Z-aaaa"
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=NY)
OPEN = Switch(False, (), "")


def config(**imp):
    cfg = AppConfig()
    cfg.defaults.model = "deepseek:flash"
    cfg.pricing = {"deepseek:flash": ModelPricing(input_per_million=0.3, output_per_million=1.2)}
    cfg.improvement.propose = True
    for k, v in imp.items():
        setattr(cfg.improvement, k, v)
    return cfg


def add_item(inbox, case="case-1", cap="cap", split="main", sev="med", seen=NOW, codes="E.A"):
    sig = f"evals|{split}|{cap}|{case}|{codes}"
    item = inbox.record(
        project=HARNESS_PROJECT,
        title=f"Evals {split} fail: {cap} — {case}",
        signature=sig,
        source="evals",
        evidence=[f"evals:{RUN}:{case}"],
        severity=sev,
        cluster=sig[-16:],
    )
    stamp = seen.isoformat()
    with inbox._lock:
        inbox._conn.execute(
            "UPDATE issue SET first_seen=?, last_seen=? WHERE id=?", (stamp, stamp, item["id"])
        )
        inbox._conn.commit()
    return inbox.get(item["id"])


def attempt(inbox, item, prefix, status="tests_failed", spend=0.1, at=NOW, day=None):
    c = inbox.start_candidate(
        item_id=item["id"],
        case_prefix=prefix,
        day=day or at.astimezone(NY).strftime("%Y-%m-%d"),
        base_sha="a" * 40,
        reserved_usd=0.5,
        worktree="/wt",
        branch="improve/x",
    )
    inbox.finish_candidate(c["id"], status=status, spend_usd=spend)
    with inbox._lock:
        inbox._conn.execute(
            "UPDATE candidate SET started_at=? WHERE id=?", (at.isoformat(), c["id"])
        )
        inbox._conn.commit()
    return c


def run(inbox, cfg=None, **kw):
    args = {"now": NOW, "isolation_ok": True, "sandbox_active": True, "switch": OPEN}
    args.update(kw)
    return decide(inbox, cfg or config(), **args)


@pytest.fixture
def inbox(tmp_path):
    ib = Inbox(tmp_path / "inbox.db")
    yield ib
    ib.close()


def test_allowed_with_budget_prefix_and_deadline(inbox):
    item = add_item(inbox)
    d = run(inbox)
    assert d.allowed and d.item["id"] == item["id"]
    assert d.case_prefix == "evals|main|cap|case-1"
    assert d.budget_usd == 0.5 and d.deadline_minutes == 60


def test_refusals_in_order(inbox):
    add_item(inbox)
    cfg = config(propose=False)
    assert run(inbox, cfg, isolation_ok=False).reason == "propose is off"
    assert run(inbox, isolation_ok=False, switch=Switch(True)).reason.startswith("isolation")
    paused = run(inbox, switch=Switch(True, (), "x"), sandbox_active=False)
    assert paused.reason == "paused by the referee: x"
    cfg = config()
    cfg.routing["verifier"] = "openai:unpriced"
    assert run(inbox, cfg, sandbox_active=False).reason == "sandbox inactive"
    assert run(inbox, cfg).reason == "unpriced model: openai:unpriced"
    late = datetime(2026, 10, 12, 0, 30, tzinfo=NY)
    assert "blackout" in run(inbox, config(max_pending_handoffs=0), now=late).reason
    assert run(inbox, config(max_pending_handoffs=0)).reason == "too many un-consumed candidates"
    assert run(inbox, config(max_day_usd=0)).reason == "daily budget spent"
    assert run(inbox, config(max_month_usd=0)).reason == "monthly budget spent"
    assert run(inbox, item_id="nope").reason == "nothing eligible: item nope is not eligible"
    assert run(inbox, config(max_case_usd=0.04)).reason == "budget too small"


def test_budget_is_the_smallest_of_case_day_and_month(inbox):
    item = add_item(inbox)
    other = add_item(inbox, case="case-2")
    attempt(inbox, other, "evals|main|cap|case-2", spend=0.2)
    assert run(inbox, item_id=item["id"]).budget_usd == pytest.approx(0.3)  # day: 0.5 - 0.2
    cfg = config(max_day_usd=5.0, max_month_usd=10.0)
    attempt(inbox, item, "evals|main|cap|case-1", spend=0.15, day="2026-10-01")
    assert run(inbox, cfg, item_id=item["id"]).budget_usd == pytest.approx(0.35)  # case
    cfg = config(max_day_usd=5.0, max_case_usd=5.0, max_month_usd=0.5)
    assert run(inbox, cfg, item_id=item["id"]).budget_usd == pytest.approx(0.15)  # month
    # the monthly cap stops everything once the month-to-date sum reaches it
    assert run(inbox, config(max_day_usd=5.0, max_month_usd=0.35)).reason == (
        "monthly budget spent"
    )
    # a new month starts fresh
    nov = datetime(2026, 11, 2, 10, 0, tzinfo=NY)
    add_item(inbox, case="case-3", seen=nov)
    assert run(inbox, config(max_month_usd=0.35), now=nov).allowed


def test_month_uses_new_york_days(inbox):
    item = add_item(inbox)
    attempt(inbox, item, "evals|main|cap|case-1", spend=0.3, day="2026-10-31")
    # 2026-11-01 02:30 UTC is still October 31 in New York
    utc_late = datetime.fromisoformat("2026-11-01T02:30:00+00:00")
    d = run(inbox, config(max_month_usd=0.3, max_day_usd=5.0), now=utc_late)
    assert d.reason == "monthly budget spent"  # in UTC it would already be November


def test_eligibility_exclusions(inbox):
    keep = add_item(inbox, case="good", sev="low")
    inbox.record(  # aggregate: 3-part signature
        project=HARNESS_PROJECT, title="agg", signature="evals|main|cap", source="evals",
        evidence=[f"evals:{RUN}:main"], cluster="agg0000000000000",
    )  # fmt: skip
    inbox.record(  # ops item
        project=HARNESS_PROJECT, title="ops", signature="evals|ops|incomplete", source="evals",
        evidence=[f"evals:{RUN}:ops"], severity="high", cluster="ops0000000000000",
    )  # fmt: skip
    inbox.record(  # 4-part signature
        project=HARNESS_PROJECT, title="four", signature="evals|main|cap|c4", source="evals",
        evidence=[f"evals:{RUN}:c4"], severity="high", cluster="four000000000000",
    )  # fmt: skip
    add_item(inbox, case="stale", sev="high", seen=NOW - timedelta(days=8))
    handed = add_item(inbox, case="handed", sev="high")
    attempt(inbox, handed, "evals|main|cap|handed", status="handed_off")
    add_item(inbox, case="blocked", cap="secret", sev="high")
    add_item(inbox, case="holdout", split="holdout", sev="high")
    sw = Switch(False, ("evals|main|secret|",), "")
    d = run(inbox, switch=sw)
    assert d.allowed and d.item["id"] == keep["id"]


def test_priority_severity_then_oldest(inbox):
    add_item(inbox, case="low", sev="low", seen=NOW - timedelta(days=3))
    add_item(inbox, case="med-new", sev="med", seen=NOW - timedelta(days=1))
    old = add_item(inbox, case="med-old", sev="med", seen=NOW - timedelta(days=2))
    assert run(inbox).item["id"] == old["id"]
    high = add_item(inbox, case="high", sev="high")
    assert run(inbox).item["id"] == high["id"]


def test_cooldown_boundaries_and_needs_tony(inbox):
    item = add_item(inbox)
    prefix = "evals|main|cap|case-1"
    t0 = NOW - timedelta(days=10)
    cfg = config(max_day_usd=5.0, max_case_usd=5.0)
    for i in range(3):
        attempt(inbox, item, prefix, at=t0 + timedelta(hours=i), day="2026-10-01")
    last = t0 + timedelta(hours=2)
    assert run(inbox, cfg, now=last + timedelta(days=7, seconds=-1)).reason == "nothing eligible"
    assert run(inbox, cfg, now=last + timedelta(days=7)).allowed  # one more attempt
    attempt(inbox, item, prefix, at=last + timedelta(days=7), day="2026-10-01")
    assert mark_needs_tony(inbox, cfg, now=NOW) == [item["id"]]
    got = inbox.get(item["id"])
    assert got["status"] == "silenced" and got["feedback"] == "needs_tony"
    esc = [i for i in inbox.list(HARNESS_PROJECT) if i["source"] == "observe"]
    assert len(esc) == 1 and esc[0]["title"].startswith("IL-B: case needs Tony")
    assert esc[0]["cluster_key"].startswith("ilb-needs-tony-")
    assert mark_needs_tony(inbox, cfg, now=NOW) == []  # once
    assert run(inbox, cfg).reason == "nothing eligible"


def test_a_handoff_ends_eligibility_without_needs_tony(inbox):
    item = add_item(inbox)
    attempt(inbox, item, "evals|main|cap|case-1", status="handed_off")
    assert mark_needs_tony(inbox, config(), now=NOW) == []
    assert run(inbox).reason == "nothing eligible"


@pytest.mark.parametrize(
    ("hour", "minute", "minutes", "blocked"),
    [
        (23, 59, 60, False),  # ends 00:59, before 01:00
        (0, 1, 60, True),  # 60 minutes would cross 01:00
        (0, 1, 30, False),  # 30 minutes ends at 00:31
        (0, 0, 60, True),  # ends exactly at 01:00
        (1, 0, 20, True),
        (4, 59, 20, True),
        (5, 0, 60, False),
        (12, 0, 60, False),
    ],
)
def test_blackout_boundaries(hour, minute, minutes, blocked):
    now = datetime(2026, 10, 12, hour, minute, tzinfo=NY)
    assert (blackout_reason(now, minutes) is not None) is blocked


def test_blackout_in_decide_uses_max_draft_minutes(inbox):
    add_item(inbox)
    early = datetime(2026, 10, 12, 0, 1, tzinfo=NY)
    assert "blackout" in run(inbox, now=early).reason
    assert run(inbox, config(max_draft_minutes=30), now=early).allowed


# -- the referee switch -----------------------------------------------------------------------------
def write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_read_switch_open_and_paused(tmp_path):
    write(tmp_path / "improve.json", {"schema": 1, "paused": False, "no_autofix": ["evals|main|"]})
    sw = read_switch(tmp_path)
    assert not sw.paused and sw.no_autofix == ("evals|main|",)
    write(tmp_path / "improve.json", {"schema": 1, "paused": True, "no_autofix": [], "why": "x"})
    assert read_switch(tmp_path).paused


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        {"schema": 2, "paused": False, "no_autofix": []},
        {"schema": 1, "paused": "no", "no_autofix": []},
        {"schema": 1, "paused": 0, "no_autofix": []},
        {"schema": 1, "paused": False},
        {"schema": 1, "paused": False, "no_autofix": ["main|x"]},
        {"schema": 1, "paused": False, "no_autofix": ["evals|" + "x" * 200]},
        {"schema": 1, "paused": False, "no_autofix": [], "extra": 1},
        {"schema": 1, "paused": False, "no_autofix": [], "why": 5},
    ],
)
def test_read_switch_malformed_means_paused(tmp_path, content):
    write(tmp_path / "improve.json", content)
    sw = read_switch(tmp_path)
    assert sw.paused and sw.reason


def test_read_switch_missing_symlink_dir_and_oversize(tmp_path):
    assert read_switch(tmp_path).paused  # missing
    real = write(tmp_path / "real.json", {"schema": 1, "paused": False, "no_autofix": []})
    os.symlink(real, tmp_path / "improve.json")
    assert read_switch(tmp_path).paused  # symlink
    (tmp_path / "improve.json").unlink()
    (tmp_path / "improve.json").mkdir()
    assert read_switch(tmp_path).paused  # not a regular file
    (tmp_path / "improve.json").rmdir()
    big = {"schema": 1, "paused": False, "no_autofix": [], "why": "x"}
    write(tmp_path / "improve.json", json.dumps(big) + " " * 70_000)
    assert read_switch(tmp_path).paused  # larger than 64 KiB


def test_read_consumed(tmp_path):
    assert read_consumed(tmp_path) == frozenset()
    write(tmp_path / "candidates-consumed.json", {"schema": 1, "consumed": ["cand-ab1-2", "x"]})
    assert read_consumed(tmp_path) == frozenset({"cand-ab1-2"})
    write(tmp_path / "candidates-consumed.json", "nope")
    assert read_consumed(tmp_path) == frozenset()


# -- draft config and prices ------------------------------------------------------------------------
def test_draft_config_strips_spend_and_side_effects_without_touching_the_daemon_config():
    cfg = config()
    cfg.routing.update({"router": "deepseek:flash", "verifier": "deepseek:flash"})
    cfg.hooks = [HookConfig(event="session_end", command="true")]
    cfg.mcp = {"servers": {"x": McpServerConfig(command="x")}}
    cfg.telegram_bridge.enabled = True
    cfg.notifications.enabled = True
    cfg.verification.second_opinion = "deepseek:flash"
    before = cfg.model_dump()
    d = draft_config(cfg, 0.4, 60)
    assert cfg.model_dump() == before
    assert d.attempts.best_of == 1 and "delegate" in d.tools.deny
    assert d.planner.max_calls == 1 and "router" not in d.routing and "verifier" in d.routing
    assert d.limits.max_cost_usd == 0.4 and d.limits.max_wall_clock_minutes == 45
    assert d.limits.max_tokens_per_run == 2_000_000
    assert d.verification.second_opinion is None and d.verification.workspace == "inplace"
    assert not (d.engine.file_failed_runs or d.remote_approval.enabled)
    assert not (d.notifications.enabled or d.telegram_bridge.enabled)
    assert not d.governance.commit_per_turn and d.defaults.permission_mode == PermissionMode.UNSAFE
    assert d.hooks == [] and d.mcp_servers() == {}
    assert d.sandbox.mode == "on" and d.sandbox.writable_paths == []
    assert not d.sandbox.allow_network


def test_price_check_paths():
    cfg = config()
    assert price_check(cfg) == []
    cfg.routing["escalation"] = "anthropic:big"
    assert price_check(cfg) == ["anthropic:big"]
    cfg = config()
    cfg.routing["verifier"] = "openai:v"
    assert price_check(cfg) == ["openai:v"]
    cfg = config()
    cfg.fallback["deepseek:flash"] = ["openrouter:fb"]
    assert price_check(cfg) == ["openrouter:fb"]
    cfg = config()
    cfg.defaults.model = "ollama:local"
    assert price_check(cfg) == ["ollama:local"]  # a local model needs an explicit $0 price


# -- the sandbox probe ------------------------------------------------------------------------------
def test_forced_sandbox_ignores_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TRENDLAB_SANDBOX", "off")
    sb = pol.forced_sandbox(tmp_path)
    assert sb.mode == "on"
    if sb.bwrap:
        assert sb.wrap("true", cwd=tmp_path)[0] == sb.bwrap


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap is not installed here")
def test_probe_passes_under_bwrap_even_with_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TRENDLAB_SANDBOX", "off")
    home = tmp_path / "home"
    home.mkdir()
    ok, reason = sandbox_probe(home)
    assert ok, reason
    assert not (home / ".ilb-sandbox-probe").exists()


def test_probe_fails_on_an_unsandboxed_wrap(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "bwrap"
    fake.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done; shift; exec "$@"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    class Fake:
        mode = "on"
        bwrap = str(fake)

        def wrap(self, command, *, cwd, allow_network=None):
            return [str(fake), "--unshare-net", "--", "/bin/sh", "-c", command]

    ok, reason = sandbox_probe(home, sandbox=Fake())
    assert not ok and "write outside" in reason
    assert not (home / ".ilb-sandbox-probe").exists()  # its own marker cleaned up

    class Plain(Fake):
        def wrap(self, command, *, cwd, allow_network=None):
            return ["/bin/sh", "-c", command]

    ok, reason = sandbox_probe(home, sandbox=Plain())
    assert not ok and "bubblewrap" in reason

    class Docker(Fake):
        mode = "docker"

    assert sandbox_probe(home, sandbox=Docker()) == (
        False,
        "sandbox mode is 'docker', not bubblewrap",
    )


def test_probe_fails_when_bwrap_is_missing(tmp_path):
    sb = pol.forced_sandbox(tmp_path)
    sb.bwrap = None
    ok, reason = sandbox_probe(tmp_path, sandbox=sb)
    assert not ok and "bwrap" in reason


def test_agent_sandbox_env_override_is_refused(monkeypatch):
    monkeypatch.delenv("TRENDLAB_SANDBOX", raising=False)
    assert pol.agent_sandbox_ok()[0]
    monkeypatch.setenv("TRENDLAB_SANDBOX", "off")
    ok, reason = pol.agent_sandbox_ok()
    assert not ok and "off" in reason

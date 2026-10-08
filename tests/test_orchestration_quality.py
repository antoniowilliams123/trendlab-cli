"""Uplift U15: sub-agent handoff contract; U13 asynchronous review of new commits."""

import subprocess
from pathlib import Path

from trendlab.engine.inbox import Inbox
from trendlab.engine.jobs import review_commits
from trendlab.orchestration.handoff import check, header
from trendlab.orchestration.subagents import SubAgentReport, render_report


def test_handoff_contract_checks_sections_and_evidence(project: Path):
    good = (
        "SUMMARY: timeout is read once.\nFILES: src/app.py\n"
        "EVIDENCE: src/app.py:1 sets TIMEOUT = 30\nRECOMMENDED NEXT STEPS: none"
    )
    r = check("explorer", good, project)
    assert r == {
        "missing_sections": [],
        "evidence_cited": 1,
        "evidence_verified": 1,
        "unverified": [],
        "complete": True,
    }
    bad = "SUMMARY: see src/app.py:999 and src/ghost.py:3"
    r2 = check("explorer", bad, project)
    assert r2["missing_sections"] == ["FILES", "EVIDENCE"] and r2["evidence_cited"] == 2
    assert r2["evidence_verified"] == 0 and not r2["complete"]
    assert "evidence 0/2 verified" in header(r2) and "missing FILES, EVIDENCE" in header(r2)
    outside = check("debugger", "ROOT CAUSE x EVIDENCE ../../etc/passwd:1 PROPOSED FIX y", project)
    assert outside["evidence_verified"] == 0  # never follows a path out of the project
    rep = SubAgentReport(
        role="explorer",
        objective="o",
        status="COMPLETED",
        findings=bad,
        model="m",
        model_calls=1,
        cost_usd=0.0,
        elapsed_s=1.0,
        tool_calls=1,
        handoff=r2,
    )
    assert "[handoff: evidence 0/2 verified" in render_report(rep)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


async def test_async_review_reviews_only_new_commits(
    project: Path, tmp_path: Path, _trendlab_home: Path, monkeypatch
):
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    asked = []

    async def fake_call(self, messages):
        if "REAL defect" in messages[0]["content"]:
            return '{"keep": [], "drop": []}'
        asked.append(messages[0]["content"])
        return (
            '{"findings": [{"file": "src/app.py", "line": 1, "issue": "timeout lowered '
            'to 1s will break slow clients", "severity": "high", "lens": "correctness"}]}'
        )

    monkeypatch.setattr("trendlab.telemetry.recorded.RecordedCaller.__call__", fake_call)
    from trendlab.config.loader import load_config

    cfg = load_config(project)
    inbox = Inbox(tmp_path / "inbox.db")
    state: dict = {}
    first = await review_commits(inbox, cfg, state, [project])
    assert first[0]["reviewed"] is False and not asked  # first pass only records HEAD
    (project / "src/app.py").write_text("TIMEOUT = 1\n")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "faster")
    second = await review_commits(inbox, cfg, state, [project])
    third = await review_commits(inbox, cfg, state, [project])
    cards = inbox.list(str(project))
    inbox.close()
    assert second[0]["reviewed"] and len(asked) == 1 and "faster" in asked[0]
    assert third[0]["reviewed"] is False  # nothing new
    assert cards and cards[0]["severity"] == "high" and cards[0]["title"].startswith("review:")


async def test_delegate_eval_scores_location_and_handoff(_trendlab_home: Path):
    from trendlab.benchmarks.runner import delegate_eval_task, summarize_delegate_eval
    from trendlab.benchmarks.suite import get_task
    from trendlab.config.schema import AppConfig, ProviderConfig
    from trendlab.providers.base import ModelResponse, ToolCall
    from trendlab.providers.scripted import ScriptedProvider

    task = get_task("py01-off_by_one")
    line = task.answer_line
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="1", name="read_file", arguments={"path": "shop/util.py"})]
            ),
            ModelResponse(
                text=f"SUMMARY: chunks stops one short.\nFILES: shop/util.py\n"
                f"EVIDENCE: shop/util.py:{line} range(0, len(items) - 1, size)"
            ),
        ]
    )
    cfg = AppConfig(providers={"scripted": ProviderConfig()})
    cfg.remote_approval.enabled = False
    row = await delegate_eval_task(
        task, "scripted:m", config=cfg, provider=provider, home=_trendlab_home
    )
    assert row["located"] and row["line_hit"] and row["handoff_complete"]
    assert row["evidence_cited"] == row["evidence_verified"] == 1
    s = summarize_delegate_eval([row])
    assert s["located"] == 1.0 and s["evidence_verified_share"] == 1.0


def test_merge_marks_consensus_and_dissent():
    from trendlab.agent.critique import merge

    per = {
        "a": [
            {
                "claim": "webhook retries fulfil the order twice",
                "why": "no idempotency key",
                "severity": "med",
                "category": "correctness",
            }
        ],
        "b": [
            {
                "claim": "duplicate webhook deliveries fulfil the order twice",
                "why": "missing idempotency",
                "severity": "high",
                "category": "correctness",
            },
            {
                "claim": "endpoint has no signature verification",
                "why": "anyone can post",
                "severity": "high",
                "category": "security",
            },
        ],
    }
    pts = merge(per)
    assert pts[0]["consensus"] and pts[0]["reviewers"] == ["a", "b"]
    assert pts[0]["severity"] == "high"  # the stronger severity wins
    assert not pts[1]["consensus"] and pts[1]["reviewers"] == ["b"]


async def test_critique_eval_scores_planted_flaws():
    from trendlab.benchmarks.runner import critique_eval

    async def finds_idempotency(messages):
        return (
            '{"points": [{"claim": "retries and duplicate deliveries are not idempotent",'
            ' "why": "charged twice", "severity": "high", "category": "correctness"}]}'
        )

    async def finds_nothing(messages):
        return '{"points": []}'

    res = await critique_eval({"a": finds_idempotency}, adversary=finds_nothing)
    assert res["flaws"] == 8 and res["designs"] == 4
    # the idempotency claim matches the webhook and jobs designs' idempotency flaws
    assert res["recall"]["a"] == 0.25 and res["recall"]["adversary"] == 0.0
    assert res["recall"]["panel"] == 0.25

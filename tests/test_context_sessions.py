from pathlib import Path

from trendlab.agent.tasks import Plan, TaskStatus
from trendlab.config.schema import ContextConfig
from trendlab.context.compaction import deterministic_summary
from trendlab.context.ignore import IgnoreRules
from trendlab.context.manager import ContextManager, estimate_tokens
from trendlab.context.repository_map import RepositoryMap
from trendlab.sessions.checkpoints import CheckpointManager
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.events import EventBus, EventRecorder, EventType


def test_plan_lifecycle_and_render():
    plan = Plan()
    a = plan.add("Inspect")
    b = plan.add("Fix", dependencies=[a.id])
    plan.add("Test")
    assert [t.id for t in plan.tasks] == ["T-1", "T-2", "T-3"]
    plan.update(a.id, status=TaskStatus.ACTIVE)
    plan.update(b.id, status=TaskStatus.ACTIVE)  # only one active at a time
    assert plan.active.id == "T-2" and plan.get("T-1").status == TaskStatus.PENDING
    plan.update("T-1", status=TaskStatus.COMPLETED, evidence="pytest: 3 passed")
    assert "[✓] T-1 Inspect  — pytest: 3 passed" in plan.render() and "[→] T-2 Fix" in plan.render()
    assert not plan.done and len(plan.open) == 2
    plan.replace(["New A", "New B"])
    assert [t.title for t in plan.tasks] == ["Inspect", "New A", "New B"] and plan.tasks[
        1
    ].id == "T-4"
    restored = Plan.from_json(plan.to_json())
    assert restored.render() == plan.render()
    assert restored.add("Z").id == "T-6"


def test_repository_map_and_cache(project: Path):
    (project / "src" / "svc.py").write_text("class Service:\n    pass\n\ndef helper():\n    pass\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_svc.py").write_text("def test_x(): pass\n")
    (project / "pyproject.toml").write_text("[project]\nname='x'\n")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "big.js").write_text("x")
    rules = IgnoreRules.for_project(project)
    rm = RepositoryMap(project, rules, max_files=50).build(use_cache=False)
    text = rm.render()
    assert "src/svc.py" in text and "class Service, def helper" in text
    assert "node_modules" not in text and "Config: pyproject.toml" in text
    assert "Tests: 1 files" in text and rm.languages["python"] == 3
    assert (project / ".trendlab" / "cache" / "repo_map.json").exists()
    rm2 = RepositoryMap(project, rules, max_files=50).build()
    assert rm2.symbols == rm.symbols
    small = RepositoryMap(project, rules, max_files=2).build(use_cache=False)
    assert small.truncated and "map limited" in small.render()


async def test_context_budget_and_compaction(events: EventBus, recorder: EventRecorder):
    cfg = ContextConfig(
        recent_messages_budget_tokens=200,
        compact_threshold=0.5,
        default_context_window=600,
        min_compaction_tokens=0,
    )
    summaries = []

    async def summarizer(messages):
        summaries.append(messages)
        return "OBJECTIVE: test\nFILES MODIFIED: a.py\nNEXT ACTION: finish"

    cm = ContextManager(cfg, events, "s", system_prompt="SYS", summarizer=summarizer)
    cm.repo_map_text = "src/a.py"
    cm.plan_text = "[→] T-1 Fix"
    for i in range(10):
        cm.messages.append({"role": "user", "content": f"message {i} " * 30})
        cm.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            }
        )
        cm.messages.append(
            {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": "x " * 40}
        )
    built = cm.build()
    assert (
        built[0]["role"] == "system"
        and "Repository map" in built[0]["content"]
        and "Current plan" in built[0]["content"]
    )
    assert len(built) < len(cm.messages) + 1  # trimmed to budget
    assert built[1]["role"] != "tool"  # never starts mid tool exchange
    assert cm.needs_compaction()
    rec = await cm.compact(keep_last=3)
    assert rec.source == "model" and rec.messages_compacted == 27 and len(cm.messages) == 3
    assert cm.messages[0]["role"] != "tool"
    assert (
        "authoritative state" in cm.build()[0]["content"]
        and "FILES MODIFIED: a.py" in cm.build()[0]["content"]
    )
    assert summaries and "Transcript:" in summaries[0][1]["content"]
    ev = recorder.of_type(EventType.CONTEXT_COMPACTED)[0]
    assert ev.data["tokens_after"] < ev.data["tokens_before"]

    # Summarizer failure falls back to the deterministic summary; prior summary is carried forward.
    async def broken(messages):
        raise RuntimeError("down")

    cm.summarizer = broken
    cm.messages.extend([{"role": "user", "content": "more"}] * 8)
    rec2 = await cm.compact(keep_last=2)
    assert rec2.source == "deterministic" and "previous_summary" in rec2.structured
    assert estimate_tokens("abcd" * 10) == 10


def test_deterministic_summary_mentions_failures():
    msgs = [
        {"role": "user", "content": "Fix the bug"},
        {"role": "tool", "name": "patch_file", "content": "patch failed: old_text not found"},
    ]
    text = deterministic_summary(
        msgs, {"changed_files": ["a.py"], "validation_runs": [], "plan": "(none)"}
    )
    assert "OBJECTIVE: Fix the bug" in text and "patch failed" in text and "a.py" in text


def test_checkpoints_and_undo(project: Path, store: SessionStore):
    sid = store.create_session(str(project), "m")
    cm = CheckpointManager(project, store, sid)
    cp = cm.create(["src/app.py", "brand_new.py"], label="auto", git_head="abc")
    (project / "src" / "app.py").write_text("TIMEOUT = 60\n")
    (project / "brand_new.py").write_text("new\n")
    cm.extend(cp["id"], ["src/other.py"])
    cm.seal(cp["id"])
    [row] = cm.list()
    assert {e["path"] for e in row["files"]} == {"src/app.py", "brand_new.py", "src/other.py"}
    assert row["git_head"] == "abc" and row["files"][0]["post_sha256"]
    # External edit after the checkpoint blocks undo unless forced.
    (project / "src" / "app.py").write_text("TIMEOUT = 61  # user edit\n")
    res = cm.undo()
    assert not res["ok"] and res["conflicts"] == ["src/app.py"]
    res = cm.undo(force=True)
    assert res["ok"] and res["restored"] == ["src/app.py"] and res["removed"] == ["brand_new.py"]
    assert (project / "src" / "app.py").read_text() == "TIMEOUT = 30\n" and not (
        project / "brand_new.py"
    ).exists()
    assert cm.list() == [] and cm.undo()["error"] == "no checkpoints"


def test_store_messages_state_usage(store: SessionStore, project: Path):
    sid = store.create_session(str(project), "m", "openai:x")
    store.append_message(sid, {"role": "user", "content": "hi"})
    store.append_message(sid, {"role": "assistant", "content": "yo"})
    assert [m["role"] for m in store.messages(sid)] == ["user", "assistant"]
    store.set_state(sid, "plan", {"tasks": []})
    assert store.get_state(sid, "plan") == {"tasks": []} and store.get_state(sid, "nope", 1) == 1
    store.record_model_call(sid, "openai:x", "main", 100, 50, 0, 120, 0.002)
    store.record_model_call(sid, "openai:x", "main", 100, 50, 0, 120, 0.003)
    u = store.usage(sid)
    assert u["calls"] == 2 and u["input_tokens"] == 200 and abs(u["cost_usd"] - 0.005) < 1e-9
    assert store.sessions(str(project))[0]["id"] == sid
    store.set_session_status(sid, "closed")
    assert store.get_session(sid)["status"] == "closed"


async def test_compaction_skips_tiny_histories(events: EventBus):
    cfg = ContextConfig(
        compact_threshold=0.5, default_context_window=600, min_compaction_tokens=1500
    )
    cm = ContextManager(
        cfg, events, "s", system_prompt="S" * 2000
    )  # system alone crosses the threshold
    cm.messages = [{"role": "user", "content": "short"}] * 4
    assert cm.estimate_full() >= 300 and not cm.needs_compaction()
    cm.messages = [{"role": "user", "content": "word " * 800}] * 4
    assert cm.needs_compaction()

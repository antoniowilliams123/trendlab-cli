"""Uplift U28: sessions from any harness that logs OpenAI-style chat messages."""

import json
from pathlib import Path

from trendlab.sessions.store import SessionStore
from trendlab.telemetry.importers import detect_format, import_chat_log, parse_chat_log

LOG = [
    {"role": "user", "content": "fix the tax rounding"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "a",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "tax.py"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "a", "content": "def tax(x): return round(x * 1.08, 1)"},
    {"role": "assistant", "content": "Rounded to two decimals in tax.py."},
]


def test_jsonl_and_json_logs_parse_alike(tmp_path: Path):
    jl = tmp_path / "s.jsonl"
    jl.write_text("\n".join(json.dumps(m) for m in LOG))
    js = tmp_path / "s.json"
    js.write_text(
        json.dumps(
            {
                "model": "gpt-x",
                "project": "/repo",
                "messages": LOG,
                "usage": {"prompt_tokens": 900, "completion_tokens": 40},
            }
        )
    )
    a, b = parse_chat_log(jl), parse_chat_log(js)
    assert len(a["messages"]) == len(b["messages"]) == 4
    assert [e[0] for e in a["events"]] == ["tool.started", "tool.completed", "run.completed"]
    assert b["model"] == "gpt-x" and b["model_calls"][0]["input_tokens"] == 900
    assert detect_format(jl) == "chat" and detect_format(js) == "chat"
    cc = tmp_path / "cc.jsonl"
    cc.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}))
    assert detect_format(cc) == "claude-code"


def test_import_is_searchable_and_idempotent(tmp_path: Path):
    log = tmp_path / "s.jsonl"
    log.write_text("\n".join(json.dumps(m) for m in LOG))
    store = SessionStore(tmp_path / "db.sqlite")
    r = import_chat_log(store, log, harness="other-agent")
    assert r["session"].startswith("ch") and r["messages"] == 4 and r["tool_calls"] == 1
    assert import_chat_log(store, log)["skipped"] == "already imported"
    hits = store.search("tax rounding")
    assert hits and hits[0]["session_id"] == r["session"]
    assert store.get_session(r["session"])["label"] == "other-agent"
    store.close()

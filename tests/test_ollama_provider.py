"""Native Ollama provider: real context window, tools, streaming, usage, errors; re-plan guard."""

import json

import httpx
import pytest

from trendlab.providers.base import ProviderError, ProviderUnavailableError
from trendlab.providers.ollama_provider import OllamaProvider


class FakeOllama:
    def __init__(self, ctx_len=32768, reply=None, stream_lines=None, status=200):
        self.requests: list[tuple[str, dict]] = []
        self.ctx_len = ctx_len
        self.reply = reply or {
            "message": {"role": "assistant", "content": "hi"},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 120,
            "eval_count": 3,
        }
        self.stream_lines = stream_lines
        self.status = status

    def transport(self):
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content or b"{}")
            self.requests.append((req.url.path, body))
            if req.url.path == "/api/show":
                return httpx.Response(
                    200, json={"model_info": {"qwen2.context_length": self.ctx_len}}
                )
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "model 'x' not found"})
            if body.get("stream"):
                return httpx.Response(
                    200, content="\n".join(json.dumps(x) for x in self.stream_lines)
                )
            return httpx.Response(200, json=self.reply)

        return httpx.MockTransport(handler)


def _provider(fake, **kw):
    return OllamaProvider(
        base_url="http://localhost:11434/v1",
        model="qwen2.5-coder:14b",
        client=httpx.AsyncClient(transport=fake.transport()),
        **kw,
    )


async def test_num_ctx_from_show_and_payload_shape():
    fake = FakeOllama(
        ctx_len=32768,
        reply={
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "list_directory", "arguments": {"path": "src"}}}
                ],
            },
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 6043,
            "eval_count": 20,
        },
    )
    p = _provider(fake)
    msgs = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "image_path", "path": "/nope.png"},
                {"type": "text", "text": "look"},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "data"},
    ]
    res = await p.complete(
        msgs, tools=[{"type": "function", "function": {"name": "list_directory", "parameters": {}}}]
    )
    assert res.tool_calls[0].name == "list_directory" and res.tool_calls[0].arguments == {
        "path": "src"
    }
    assert (
        res.usage.input_tokens == 6043 and res.usage.output_tokens == 20
    )  # the whole prompt was seen
    assert p.capabilities().context_window == 32768 and p.capabilities().local
    path, body = fake.requests[-1]
    assert path == "/api/chat" and body["options"]["num_ctx"] == 32768 and body["tools"]
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == {"path": "a"}
    assert body["messages"][3]["tool_name"] == "read_file"
    assert "image unavailable" in body["messages"][1]["content"]
    # Configured window wins over /api/show and is capped.
    p2 = _provider(FakeOllama(), context_window=200_000, max_num_ctx=65_536)
    assert await p2.num_ctx() == 65_536
    p3 = _provider(FakeOllama(ctx_len=0))
    assert await p3.num_ctx() == 32_768  # default when the server does not say


async def test_streaming_ndjson():
    lines = [
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}],
            },
            "done": False,
        },
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 50,
            "eval_count": 7,
        },
    ]
    p = _provider(FakeOllama(stream_lines=lines), context_window=8192)
    chunks = [c async for c in p.stream([{"role": "user", "content": "hi"}])]
    assert "".join(c.text for c in chunks) == "Hello"
    final = chunks[-1].final
    assert final.text == "Hello" and final.tool_calls[0].name == "read_file"
    assert final.usage.input_tokens == 50 and final.usage.output_tokens == 7
    assert fake_last(p) == 8192


def fake_last(p):
    return p._num_ctx


async def test_errors_are_normalized():
    p = _provider(FakeOllama(status=404), context_window=4096)
    with pytest.raises(ProviderError, match="ollama pull"):
        await p.complete([{"role": "user", "content": "hi"}])

    def down(req):
        raise httpx.ConnectError("refused")

    p2 = OllamaProvider(
        base_url="http://localhost:11434",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(down)),
        context_window=4096,
    )
    with pytest.raises(ProviderUnavailableError, match="is Ollama running"):
        await p2.complete([{"role": "user", "content": "hi"}])


def test_registry_uses_native_ollama(project):
    from trendlab.config.loader import load_config
    from trendlab.config.schema import ModelInfo, ProviderConfig
    from trendlab.providers.registry import create_provider

    cfg = load_config(project)
    cfg.providers["ollama"] = ProviderConfig(type="ollama", base_url="http://localhost:11434/v1")
    cfg.models["ollama:qwen2.5-coder:14b"] = ModelInfo(context_window=32768, local=True)
    prov = create_provider(cfg, "ollama:qwen2.5-coder:14b")
    assert isinstance(prov, OllamaProvider) and prov.capabilities().context_window == 32768


async def test_replan_guard(project, events):
    from trendlab.agent.tasks import Plan
    from trendlab.tools.base import ToolContext
    from trendlab.tools.task_tool import TaskInput, TaskTool

    tool = TaskTool(Plan(), events)
    ctx = ToolContext(project_root=project, session_id="s")
    assert (await tool.run(TaskInput(action="plan", titles=["a", "b"]), ctx)).ok
    assert (
        await tool.run(TaskInput(action="plan", titles=["a2", "b2"]), ctx)
    ).ok  # one re-plan allowed
    third = await tool.run(TaskInput(action="plan", titles=["a3"]), ctx)
    assert not third.ok and "do NOT re-plan" in third.output and "a2" in third.output
    tid = tool.plan.tasks[0].id
    assert (await tool.run(TaskInput(action="complete", task_id=tid, evidence="done"), ctx)).ok
    tid2 = tool.plan.tasks[1].id
    assert (await tool.run(TaskInput(action="complete", task_id=tid2, evidence="done"), ctx)).ok
    assert (
        await tool.run(TaskInput(action="plan", titles=["next"]), ctx)
    ).ok  # all closed → fresh plan ok

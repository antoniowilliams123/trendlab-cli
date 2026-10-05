import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from anthropic.types import Message, TextBlock, ThinkingBlock, Usage

from trendlab.agent.prompt import build_system_prompt, project_instructions
from trendlab.providers.anthropic_provider import AnthropicProvider
from trendlab.providers.openai_compatible import OpenAICompatibleProvider

from .test_anthropic_provider import FakeClient


def test_project_instruction_files_in_priority_order(project: Path):
    (project / "CLAUDE.md").write_text("# For Claude\nUse pytest.\n")
    (project / "AGENTS.md").write_text("# Agents\nRun ruff before finishing.\n")
    files = project_instructions(project)
    assert [n for n, _ in files] == ["AGENTS.md", "CLAUDE.md"]
    (project / "TRENDLAB.md").write_text("# TrendLab\nPrefer patch_file.\n")
    assert [n for n, _ in project_instructions(project)][0] == "TRENDLAB.md"
    # Identical content in two files is included once.
    (project / "CLAUDE.md").write_text("# Agents\nRun ruff before finishing.\n")
    assert [n for n, _ in project_instructions(project)] == ["TRENDLAB.md", "AGENTS.md"]
    prompt = build_system_prompt(project)
    assert (
        "Project instructions from AGENTS.md" in prompt and "Run ruff before finishing." in prompt
    )
    assert "Project instructions from TRENDLAB.md" in prompt and "Prefer patch_file." in prompt


async def test_anthropic_thinking_is_streamed_and_kept():
    final = Message(
        id="m",
        type="message",
        role="assistant",
        model="claude-opus-5",
        stop_reason="end_turn",
        stop_sequence=None,
        content=[
            ThinkingBlock(
                type="thinking", thinking="Let me check the tests first.", signature="sig"
            ),
            TextBlock(type="text", text="Answer"),
        ],
        usage=Usage(input_tokens=1, output_tokens=1),
    )

    class ThinkingStream:
        def __init__(self, final):
            self._final = final

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def gen():
                yield SimpleNamespace(type="thinking", thinking="Let me ")
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="thinking_delta", thinking="check the tests first."),
                )
                yield SimpleNamespace(type="text", text="Answer")

            return gen()

        async def get_final_message(self):
            return self._final

    client = FakeClient([final])
    client.messages.stream = lambda **params: ThinkingStream(final)
    p = AnthropicProvider(model="claude-opus-5", client=client)
    chunks = [c async for c in p.stream([{"role": "user", "content": "x"}])]
    assert "".join(c.thinking for c in chunks) == "Let me check the tests first."
    assert "".join(c.text for c in chunks) == "Answer"
    assert chunks[-1].final.raw_metadata["thinking"] == "Let me check the tests first."
    r = await p.complete([{"role": "user", "content": "x"}])
    assert r.raw_metadata["thinking"] == "Let me check the tests first." and r.text == "Answer"


async def test_deepseek_reasoning_content_round_trip(monkeypatch):
    monkeypatch.setenv("K", "k")
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        if body.get("stream"):
            sse = "\n".join(
                [
                    'data: {"choices":[{"delta":{"reasoning_content":"Thinking "}}]}',
                    'data: {"choices":[{"delta":{"reasoning_content":"hard."}}]}',
                    'data: {"choices":[{"delta":{"content":"Reply"}}]}',
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                    "data: [DONE]",
                    "",
                ]
            )
            return httpx.Response(
                200, content=sse.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "reasoning_content": "because",
                            "tool_calls": [],
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        )

    p = OpenAICompatibleProvider(
        base_url="https://x/v1",
        model="deepseek-v4-pro",
        api_key_env="K",
        provider_name="deepseek",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    chunks = [c async for c in p.stream([{"role": "user", "content": "q"}])]
    assert (
        "".join(c.thinking for c in chunks) == "Thinking hard." and chunks[-1].final.text == "Reply"
    )
    assert chunks[-1].final.raw_metadata["reasoning_content"] == "Thinking hard."
    r = await p.complete([{"role": "user", "content": "q"}])
    assert r.raw_metadata["reasoning_content"] == "because"
    # The runtime stores reasoning on the assistant message; the same model gets it back, others do not.
    from trendlab.agent.runtime import _assistant_message

    msg = _assistant_message(r)
    assert msg["_reasoning_content"] == "because" and msg["_provider_model"] == "deepseek-v4-pro"
    await p.complete([{"role": "user", "content": "q"}, msg, {"role": "user", "content": "next"}])
    assert seen[-1]["messages"][1]["reasoning_content"] == "because"
    assert "_reasoning_content" not in seen[-1]["messages"][1]
    other = OpenAICompatibleProvider(
        base_url="https://x/v1",
        model="other",
        api_key_env="K",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await other.complete([{"role": "user", "content": "q"}, msg])
    assert "reasoning_content" not in seen[-1]["messages"][1]

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from trendlab.config.schema import AppConfig, ModelInfo, ProviderConfig
from trendlab.providers.anthropic_provider import (
    AnthropicProvider,
    fallbacks_enabled,
    thinking_param,
    translate_messages,
    translate_tools,
)
from trendlab.providers.base import (
    ModelResponse,
    ProviderAuthenticationError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ToolCall,
)
from trendlab.providers.openai_compatible import OpenAICompatibleProvider
from trendlab.providers.registry import create_provider

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


def _message(content, stop="end_turn", **usage):
    u = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    u.update(usage)
    return Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-opus-5",
        stop_reason=stop,
        stop_sequence=None,
        content=content,
        usage=Usage(**u),
    )


class FakeStream:
    def __init__(self, texts, final):
        self._texts, self._final = texts, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for t in self._texts:
                yield SimpleNamespace(type="text", text=t)
            yield SimpleNamespace(type="message_stop")

        return gen()

    async def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **params):
        self.calls.append(params)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, **params):
        self.calls.append(params)
        item = self.responses.pop(0)
        return FakeStream(["Hel", "lo"], item)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)

    async def close(self):
        pass


def test_translate_messages_full_round_trip():
    history = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "fix it"},
        {
            "role": "assistant",
            "content": "Looking.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{bad"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "A"},
        {"role": "tool", "tool_call_id": "call_2", "name": "read_file", "content": "B"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [],
            "_provider_content": [{"type": "text", "text": "skip"}],
            "_provider_model": "other-model",
        },
        {
            "role": "assistant",
            "content": "done",
            "_provider_content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "done"},
            ],
            "_provider_model": "claude-opus-5",
        },
    ]
    system, msgs = translate_messages(history, "claude-opus-5")
    assert system == "SYS"
    assert msgs[0] == {"role": "user", "content": [{"type": "text", "text": "fix it"}]}
    assert [b["type"] for b in msgs[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert msgs[1]["content"][1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "read_file",
        "input": {"path": "a.py"},
    }
    assert msgs[1]["content"][2]["input"] == {}
    assert msgs[2]["role"] == "user" and [b["tool_use_id"] for b in msgs[2]["content"]] == [
        "call_1",
        "call_2",
    ]
    assert msgs[2]["content"][0]["type"] == "tool_result"
    # Empty assistant turn from another model is dropped; same-model raw blocks are replayed verbatim.
    assert msgs[3]["content"][0]["type"] == "thinking"
    assert len(msgs) == 4
    # A history starting with an assistant turn gets a leading user message.
    _, msgs2 = translate_messages([{"role": "assistant", "content": "hi"}], "m")
    assert msgs2[0]["role"] == "user"


def test_translate_tools_thinking_fallbacks():
    t = translate_tools(TOOLS)
    assert t == [
        {
            "name": "read_file",
            "description": "read",
            "input_schema": TOOLS[0]["function"]["parameters"],
        }
    ]
    assert thinking_param("claude-opus-5", "auto") == {"type": "adaptive", "display": "summarized"}
    assert thinking_param("claude-sonnet-5", "auto") == {
        "type": "adaptive",
        "display": "summarized",
    }
    assert thinking_param("claude-haiku-4-5", "auto") is None
    assert thinking_param("claude-haiku-4-5", "adaptive") == {
        "type": "adaptive",
        "display": "summarized",
    }
    assert thinking_param("claude-opus-5", "off") is None
    assert fallbacks_enabled("claude-opus-5", "auto") and fallbacks_enabled(
        "claude-fable-5-1", "auto"
    )
    assert not fallbacks_enabled("claude-sonnet-5", "auto") and not fallbacks_enabled(
        "claude-opus-5-5", "auto"
    )
    assert fallbacks_enabled("claude-sonnet-5", "on") and not fallbacks_enabled(
        "claude-opus-5", "off"
    )


async def test_complete_maps_tool_calls_usage_and_params():
    msg = _message(
        [
            TextBlock(type="text", text="Reading."),
            ToolUseBlock(type="tool_use", id="toolu_1", name="read_file", input={"path": "a.py"}),
        ],
        stop="tool_use",
        input_tokens=100,
        cache_read_input_tokens=40,
        cache_creation_input_tokens=10,
    )
    client = FakeClient([msg])
    p = AnthropicProvider(model="claude-opus-5", client=client, effort="high")
    r = await p.complete(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "go"}], TOOLS
    )
    assert r.text == "Reading." and r.tool_calls == [
        ToolCall(id="toolu_1", name="read_file", arguments={"path": "a.py"})
    ]
    assert (
        r.usage.input_tokens == 150
        and r.usage.cached_input_tokens == 40
        and r.finish_reason == "tool_calls"
    )
    assert (
        r.raw_metadata["provider_content"][1]["type"] == "tool_use"
        and r.raw_metadata["provider_model"] == "claude-opus-5"
    )
    params = client.messages.calls[0]
    assert (
        params["model"] == "claude-opus-5"
        and params["system"]
        == [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}]
        and params["max_tokens"] == 16000
    )
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"} and params[
        "output_config"
    ] == {"effort": "high"}
    assert params["tools"][0]["name"] == "read_file" and params["messages"][0]["role"] == "user"
    assert params["extra_headers"] == {"anthropic-beta": "server-side-fallback-2026-07-01"}
    assert params["extra_body"] == {"fallbacks": "default"}
    caps = p.capabilities()
    assert caps.privacy_label == "REMOTE" and caps.context_window == 1_000_000 and caps.native_tools


async def test_haiku_has_no_thinking_or_fallbacks():
    client = FakeClient([_message([TextBlock(type="text", text="ok")])])
    p = AnthropicProvider(model="claude-haiku-4-5", client=client)
    await p.complete([{"role": "user", "content": "x"}])
    params = client.messages.calls[0]
    assert (
        "thinking" not in params
        and "extra_body" not in params
        and p.capabilities().context_window == 200_000
    )


async def test_stream_yields_text_then_final():
    client = FakeClient([_message([TextBlock(type="text", text="Hello")])])
    p = AnthropicProvider(model="claude-sonnet-5", client=client)
    chunks = [c async for c in p.stream([{"role": "user", "content": "x"}])]
    assert "".join(c.text for c in chunks) == "Hello" and chunks[-1].final.text == "Hello"
    assert chunks[-1].final.finish_reason == "stop"


def _http_err(cls, status, msg="boom"):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(msg, response=httpx2.Response(status, request=req), body={"error": {"message": msg}})


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_http_err(anthropic.RateLimitError, 429), ProviderRateLimitError),
        (_http_err(anthropic.AuthenticationError, 401), ProviderAuthenticationError),
        (_http_err(anthropic.PermissionDeniedError, 403), ProviderAuthenticationError),
        (
            _http_err(anthropic.BadRequestError, 400, "prompt is too long: 1200000 tokens"),
            ProviderContextOverflowError,
        ),
        (_http_err(anthropic.BadRequestError, 400, "invalid tool"), ProviderError),
        (_http_err(anthropic.InternalServerError, 500), ProviderUnavailableError),
        (anthropic.APITimeoutError(httpx2.Request("POST", "https://x")), ProviderTimeoutError),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            ProviderUnavailableError,
        ),
    ],
)
async def test_error_mapping(exc, expected):
    p = AnthropicProvider(model="claude-opus-5", client=FakeClient([exc]))
    with pytest.raises(expected):
        await p.complete([{"role": "user", "content": "x"}])


async def test_refusal_is_a_non_retryable_provider_error():
    msg = _message([TextBlock(type="text", text="")], stop="refusal")
    p = AnthropicProvider(model="claude-opus-5", client=FakeClient([msg]))
    with pytest.raises(ProviderError, match="declined") as exc:
        await p.complete([{"role": "user", "content": "x"}])
    assert not exc.value.retryable


def test_registry_builds_anthropic_provider():
    cfg = AppConfig(
        providers={
            "anthropic": ProviderConfig(
                type="anthropic", api_key_env="ANTHROPIC_API_KEY", effort="xhigh", max_tokens=32000
            )
        },
        models={"anthropic:claude-opus-5": ModelInfo(context_window=1_000_000)},
    )
    p = create_provider(cfg, "anthropic:claude-opus-5")
    assert isinstance(p, AnthropicProvider) and p.model == "claude-opus-5" and p.name == "anthropic"
    assert p._effort == "xhigh" and p._max_tokens == 32000  # noqa: SLF001
    assert p.capabilities().privacy_label == "REMOTE"


async def test_openai_provider_strips_private_keys(monkeypatch):
    import httpx

    monkeypatch.setenv("K", "k")
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    p = OpenAICompatibleProvider(
        base_url="https://x/v1",
        model="m",
        api_key_env="K",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await p.complete(
        [{"role": "assistant", "content": "a", "_provider_content": [1], "_provider_model": "z"}]
    )
    assert seen["body"]["messages"] == [{"role": "assistant", "content": "a"}]


def test_runtime_keeps_provider_content_for_replay():
    from trendlab.agent.runtime import _assistant_message

    r = ModelResponse(
        text="t",
        tool_calls=[ToolCall(id="1", name="x", arguments={})],
        raw_metadata={"provider_content": [{"type": "text", "text": "t"}], "provider_model": "m"},
    )
    m = _assistant_message(r)
    assert m["_provider_content"] == [{"type": "text", "text": "t"}] and m["_provider_model"] == "m"
    assert m["tool_calls"][0]["function"]["name"] == "x"
    assert "_provider_content" not in _assistant_message(ModelResponse(text="plain"))


async def test_secret_store_used_when_env_missing(_trendlab_home, monkeypatch):
    from trendlab.security.secrets import resolve_secret, secret_source, store_secret

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert (
        resolve_secret("ANTHROPIC_API_KEY") is None
        and secret_source("ANTHROPIC_API_KEY") == "missing"
    )
    path = store_secret("ANTHROPIC_API_KEY", "sk-ant-test-value\n")
    assert oct(path.stat().st_mode & 0o777) == "0o600" and path.parent.name == "secrets"
    assert (
        resolve_secret("ANTHROPIC_API_KEY") == "sk-ant-test-value"
        and secret_source("ANTHROPIC_API_KEY") == "store"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert (
        resolve_secret("ANTHROPIC_API_KEY") == "from-env"
        and secret_source("ANTHROPIC_API_KEY") == "env"
    )
    with pytest.raises(ValueError):
        store_secret("lowercase", "x")


def test_cli_secret_commands(_trendlab_home, monkeypatch):
    from typer.testing import CliRunner

    from trendlab.cli import app

    r = CliRunner().invoke(app, ["secret", "set", "ANTHROPIC_API_KEY"], input="sk-ant-abc123\n")
    assert (
        r.exit_code == 0
        and "Stored ANTHROPIC_API_KEY" in r.output
        and "sk-ant-abc123" not in r.output
    )
    assert (_trendlab_home / "secrets" / "ANTHROPIC_API_KEY").read_text().strip() == "sk-ant-abc123"
    r = CliRunner().invoke(app, ["secret", "list"])
    assert "ANTHROPIC_API_KEY" in r.output
    r = CliRunner().invoke(app, ["secret", "rm", "ANTHROPIC_API_KEY"])
    assert "removed" in r.output


async def test_cache_breakpoints_can_be_disabled_and_land_on_tool_results():
    client = FakeClient([_message([TextBlock(type="text", text="ok")])] * 2)
    p = AnthropicProvider(model="claude-opus-5", client=client)
    history = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "data"},
    ]
    await p.complete(history)
    last = client.messages.calls[0]["messages"][-1]
    assert last["content"][-1]["type"] == "tool_result" and last["content"][-1]["cache_control"]
    off = AnthropicProvider(model="claude-opus-5", client=client, cache=False)
    await off.complete([{"role": "system", "content": "S"}, *history])
    second = client.messages.calls[1]
    assert second["system"] == "S" and "cache_control" not in second["messages"][-1]["content"][-1]

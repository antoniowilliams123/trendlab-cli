import json

import httpx
import pytest

from trendlab.config.schema import AppConfig, ModelInfo, ModelPricing, ProviderConfig, RetryConfig
from trendlab.providers.base import (
    ModelResponse,
    ProviderAuthenticationError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    TokenUsage,
)
from trendlab.providers.gateway import ModelGateway
from trendlab.providers.openai_compatible import OpenAICompatibleProvider
from trendlab.providers.registry import (
    create_provider,
    fallback_chain,
    parse_model_ref,
    resolve_role,
)
from trendlab.providers.scripted import ScriptedProvider
from trendlab.providers.structured import StructuredToolProvider
from trendlab.telemetry.costs import CostTracker
from trendlab.telemetry.events import EventBus, EventRecorder, EventType

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "read",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


def _provider(handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider(
        base_url="https://api.example/v1",
        model="m",
        api_key_env="K",
        provider_name="ex",
        client=client,
        **kw,
    )


async def test_complete_parses_tool_calls_and_usage(monkeypatch):
    monkeypatch.setenv("K", "secret")
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "m",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path": "a.py"}',
                                    },
                                },
                                {
                                    "id": "c2",
                                    "function": {"name": "read_file", "arguments": "{broken"},
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 4},
                },
            },
        )

    p = _provider(handler)
    r = await p.complete([{"role": "user", "content": "hi"}], TOOLS)
    assert seen["auth"] == "Bearer secret" and seen["body"]["tools"] == TOOLS
    assert r.tool_calls[0].arguments == {"path": "a.py"}
    assert "_malformed_json" in r.tool_calls[1].arguments
    assert r.usage == TokenUsage(input_tokens=10, output_tokens=5, cached_input_tokens=4)
    assert p.capabilities().privacy_label == "REMOTE"


@pytest.mark.parametrize(
    "status,body,exc",
    [
        (401, "nope", ProviderAuthenticationError),
        (429, "slow down", ProviderRateLimitError),
        (503, "down", ProviderUnavailableError),
        (400, "This model's maximum context length is 8192 tokens", ProviderContextOverflowError),
        (400, "bad request", ProviderError),
    ],
)
async def test_error_mapping(monkeypatch, status, body, exc):
    monkeypatch.setenv("K", "k")
    p = _provider(lambda r: httpx.Response(status, text=body))
    with pytest.raises(exc):
        await p.complete([{"role": "user", "content": "x"}])


async def test_missing_key_is_auth_error(monkeypatch):
    monkeypatch.delenv("K", raising=False)
    p = _provider(lambda r: httpx.Response(200))
    with pytest.raises(ProviderAuthenticationError, match="K is not set"):
        await p.complete([])


async def test_streaming_assembles_text_and_tool_calls(monkeypatch):
    monkeypatch.setenv("K", "k")
    sse = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            "data: not json",
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"read_file","arguments":"{\\"pa"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"th\\": \\"x\\"}"}}]},"finish_reason":"tool_calls"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}',
            "data: [DONE]",
            "",
        ]
    )
    p = _provider(
        lambda r: httpx.Response(
            200, content=sse.encode(), headers={"content-type": "text/event-stream"}
        )
    )
    chunks = [c async for c in p.stream([{"role": "user", "content": "x"}], TOOLS)]
    assert "".join(c.text for c in chunks) == "Hello"
    final = chunks[-1].final
    assert final.text == "Hello" and final.tool_calls[0].arguments == {"path": "x"}
    assert final.usage.input_tokens == 7 and final.finish_reason == "tool_calls"


def test_local_detection_and_registry():
    cfg = AppConfig(
        providers={
            "ollama": ProviderConfig(type="ollama", base_url="http://localhost:11434/v1"),
            "deepseek": ProviderConfig(
                base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY"
            ),
            "weak": ProviderConfig(base_url="https://x/v1", tool_calling="structured"),
        },
        models={"deepseek:deepseek-chat": ModelInfo(context_window=64000)},
    )
    local = create_provider(cfg, "ollama:qwen3-coder")
    assert local.capabilities().local and local.capabilities().privacy_label == "LOCAL"
    remote = create_provider(cfg, "deepseek:deepseek-chat")
    assert not remote.capabilities().local and remote.capabilities().context_window == 64000
    weak = create_provider(cfg, "weak:m")
    assert isinstance(weak, StructuredToolProvider) and not weak.capabilities().native_tools
    with pytest.raises(ProviderError, match="not configured"):
        create_provider(cfg, "nope:m")
    with pytest.raises(ProviderError):
        parse_model_ref("gpt-5")


def test_routing_and_fallback():
    cfg = AppConfig(
        routing={"default": "openai:a", "reviewer": "deepseek:b"},
        fallback={"openai:a": ["deepseek:b", "openai:a", "moonshot:c"]},
    )
    assert resolve_role(cfg, "reviewer", "ollama:x") == "deepseek:b"
    assert resolve_role(cfg, "explorer", "ollama:x") == "openai:a"
    assert resolve_role(AppConfig(), "explorer", "ollama:x") == "ollama:x"
    assert fallback_chain(cfg, "openai:a") == ["openai:a", "deepseek:b", "moonshot:c"]


async def test_structured_fallback_round_trip():
    inner = ScriptedProvider(
        [
            ModelResponse(
                text='```json\n{"action": "read_file", "arguments": {"path": "a.py"}}\n```'
            ),
            ModelResponse(
                text='[{"action": "nope", "arguments": {}}, {"action": "read_file", "arguments": "bad"}]'
            ),
            ModelResponse(text="```json\n{not json\n```"),
            ModelResponse(text="FINAL: all good"),
            ModelResponse(text="just prose"),
        ]
    )
    p = StructuredToolProvider(inner)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "z"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "name": "read_file", "content": "contents"},
    ]
    r = await p.complete(msgs, TOOLS)
    assert r.tool_calls[0].name == "read_file" and r.tool_calls[0].arguments == {"path": "a.py"}
    sent = inner.calls[0]
    assert "TOOL PROTOCOL" in sent[0]["content"] and "read_file(path: string)" in sent[0]["content"]
    assert sent[2]["role"] == "assistant" and "read_file" in sent[2]["content"]
    assert sent[3]["role"] == "user" and "[tool result" in sent[3]["content"]
    assert all("tool_calls" not in m for m in sent)
    r = await p.complete(msgs, TOOLS)
    assert [c.name for c in r.tool_calls] == ["_malformed", "_malformed"]
    assert "unknown tool" in r.tool_calls[0].arguments["error"]
    r = await p.complete(msgs, TOOLS)
    assert (
        r.tool_calls[0].name == "_malformed"
        and "invalid JSON" in r.tool_calls[0].arguments["error"]
    )
    r = await p.complete(msgs, TOOLS)
    assert r.text == "all good" and not r.tool_calls
    r = await p.complete(msgs, TOOLS)
    assert r.text == "just prose" and not r.tool_calls


async def test_gateway_retries_then_falls_back():
    cfg = AppConfig(
        retry=RetryConfig(max_attempts=2, base_delay_seconds=0.001), fallback={"a:m": ["b:m"]}
    )
    events = EventBus()
    rec = EventRecorder()
    events.subscribe(rec)
    a = ScriptedProvider([ProviderRateLimitError("429"), ProviderUnavailableError("503")])
    b = ScriptedProvider([ModelResponse(text="from b")])
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    gw = ModelGateway(
        cfg, events, "s", provider_factory={"a:m": a, "b:m": b}.__getitem__, sleep=fake_sleep
    )
    resp, used = await gw.complete("a:m", [{"role": "user", "content": "x"}])
    assert resp.text == "from b" and used == "b:m"
    assert len(slept) == 1 and len(rec.of_type(EventType.PROVIDER_RETRY)) == 1
    assert rec.of_type(EventType.PROVIDER_FALLBACK)[0].data["from_model"] == "a:m"


async def test_gateway_does_not_fall_back_on_auth_or_overflow():
    cfg = AppConfig(fallback={"a:m": ["b:m"]})
    b = ScriptedProvider([ModelResponse(text="nope")])
    gw = ModelGateway(
        cfg,
        EventBus(),
        "s",
        provider_factory={
            "a:m": ScriptedProvider([ProviderAuthenticationError("401")]),
            "b:m": b,
        }.__getitem__,
    )
    with pytest.raises(ProviderAuthenticationError):
        await gw.complete("a:m", [])
    gw2 = ModelGateway(
        cfg,
        EventBus(),
        "s",
        provider_factory={
            "a:m": ScriptedProvider([ProviderContextOverflowError("ctx")]),
            "b:m": b,
        }.__getitem__,
    )
    with pytest.raises(ProviderContextOverflowError):
        await gw2.complete("a:m", [])
    assert b.calls == []


def test_cost_tracker_pricing_and_budget():
    cfg = AppConfig(
        pricing={
            "openai:gpt": ModelPricing(
                input_per_million=2.0, output_per_million=8.0, cached_input_per_million=0.5
            )
        }
    )
    cfg.limits.max_cost_usd = 0.01
    t = CostTracker(cfg)
    rec = t.record(
        "openai:gpt", TokenUsage(input_tokens=1000, output_tokens=500, cached_input_tokens=200), 120
    )
    # 800 uncached * 2 + 200 cached * 0.5 + 500 out * 8 = 1600 + 100 + 4000 = 5700 / 1e6
    assert rec.cost_usd == pytest.approx(0.0057)
    assert not t.over_budget() and not t.should_warn()
    t.record("ollama:q", TokenUsage(input_tokens=99999, output_tokens=99999), 10, local=True)
    assert t.by_model()["ollama:q"]["usd"] == 0.0 and t.by_model()["ollama:q"]["local"]
    t.record("openai:gpt", TokenUsage(input_tokens=1000, output_tokens=300), 100)
    assert t.should_warn() and not t.should_warn()  # warn exactly once
    t.record("openai:gpt", TokenUsage(input_tokens=1000, output_tokens=300), 100)
    assert t.over_budget() and t.model_calls == 4


async def test_gateway_fails_over_to_the_local_model():
    """Business continuity: a cloud outage hands the call to the configured local model."""
    from trendlab.config.schema import AppConfig
    from trendlab.providers.base import ModelResponse, ProviderError
    from trendlab.providers.gateway import ModelGateway
    from trendlab.providers.scripted import ScriptedProvider
    from trendlab.telemetry.events import EventBus, EventRecorder, EventType

    class Down(ScriptedProvider):
        async def complete(self, messages, tools=None):
            err = ProviderError("connection refused")
            err.retryable = True
            raise err

    cfg = AppConfig()
    cfg.retry.max_attempts = 1
    cfg.fallback = {"cloud:m": ["ollama:local"]}
    providers = {
        "cloud:m": Down([]),
        "ollama:local": ScriptedProvider([ModelResponse(text="local ok")]),
    }
    bus = EventBus()
    rec = EventRecorder()
    bus.subscribe(rec)
    gw = ModelGateway(cfg, bus, "s", provider_factory=lambda ref: providers[ref])
    response, used = await gw.complete("cloud:m", [{"role": "user", "content": "hi"}])
    assert used == "ollama:local" and response.text == "local ok"
    assert rec.of_type(EventType.PROVIDER_FALLBACK)[0].data["from_model"] == "cloud:m"

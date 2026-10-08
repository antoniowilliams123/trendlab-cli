"""Uplift U18: per-role sampling policy reaches the provider request."""

from trendlab.config.schema import AppConfig, SamplingParams
from trendlab.providers.base import ModelResponse
from trendlab.providers.gateway import ModelGateway
from trendlab.providers.openai_compatible import OpenAICompatibleProvider
from trendlab.telemetry.events import EventBus


class Capture:
    def __init__(self):
        self.seen = []

    async def complete(self, messages, tools=None):
        self.seen.append(messages)
        return ModelResponse(text="ok")

    def capabilities(self):
        from trendlab.providers.base import ProviderCapabilities

        return ProviderCapabilities()


def test_judging_roles_default_to_temperature_zero():
    cfg = AppConfig()
    assert cfg.sampling["verifier"].temperature == 0.0 and cfg.sampling["router"].temperature == 0
    assert "main" not in cfg.sampling  # the agent keeps the provider default


async def test_gateway_stamps_the_roles_sampling():
    cfg = AppConfig()
    cfg.sampling["planner"] = SamplingParams(temperature=0.2, top_p=0.9)
    cap = Capture()
    gw = ModelGateway(cfg, EventBus(), "s", provider_factory=lambda ref: cap)
    await gw.complete("x:m", [{"role": "user", "content": "hi"}], role="verifier")
    await gw.complete("x:m", [{"role": "user", "content": "hi"}], role="planner")
    await gw.complete("x:m", [{"role": "user", "content": "hi"}])  # no role: untouched
    assert cap.seen[0][0]["_sampling"] == {"temperature": 0.0}
    assert cap.seen[1][0]["_sampling"] == {"temperature": 0.2, "top_p": 0.9}
    assert "_sampling" not in cap.seen[2][0]


def test_openai_payload_carries_sampling():
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m", api_key_env=None)
    payload = p._payload(
        [{"role": "user", "content": "hi", "_sampling": {"temperature": 0.0}}], None, False
    )
    assert payload["temperature"] == 0.0 and "_sampling" not in payload["messages"][0]
    p.temperature = 0.7  # best-of-N diversity setting
    assert p._payload([{"role": "user", "content": "hi"}], None, False)["temperature"] == 0.7

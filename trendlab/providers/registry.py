from __future__ import annotations

from trendlab.config.schema import AppConfig
from trendlab.providers.base import ModelProvider, ProviderError
from trendlab.providers.openai_compatible import OpenAICompatibleProvider


def parse_model_ref(ref: str) -> tuple[str, str]:
    """``"deepseek:deepseek-chat"`` → ``("deepseek", "deepseek-chat")``."""
    if ":" not in ref:
        raise ProviderError(f"model reference {ref!r} must look like provider:model")
    provider, model = ref.split(":", 1)
    return provider, model


def create_provider(config: AppConfig, model_ref: str) -> ModelProvider:
    provider_name, model = parse_model_ref(model_ref)
    pcfg = config.providers.get(provider_name)
    if pcfg is None:
        raise ProviderError(
            f"provider {provider_name!r} is not configured; "
            f"add [providers.{provider_name}] to config"
        )
    if pcfg.type in {"openai_compatible", "openai", "ollama"}:
        api_key_env = None if pcfg.type == "ollama" else pcfg.api_key_env
        return OpenAICompatibleProvider(
            base_url=pcfg.base_url,
            model=model,
            api_key_env=api_key_env,
            provider_name=provider_name,
        )
    raise ProviderError(f"unknown provider type {pcfg.type!r}")

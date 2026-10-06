"""Model references, model registry, routing and fallback resolution (spec §10, §11, §58, §69)."""

from __future__ import annotations

from trendlab.config.schema import AppConfig, ModelInfo
from trendlab.providers.base import ModelProvider, ProviderError
from trendlab.providers.openai_compatible import OpenAICompatibleProvider
from trendlab.providers.structured import StructuredToolProvider

ROLES = ("default", "planning", "explorer", "debugger", "tester", "reviewer", "summarizer")


def parse_model_ref(ref: str) -> tuple[str, str]:
    """``"deepseek:deepseek-chat"`` → ``("deepseek", "deepseek-chat")``."""
    if ":" not in ref:
        raise ProviderError(f"model reference {ref!r} must look like provider:model")
    provider, model = ref.split(":", 1)
    if not provider or not model:
        raise ProviderError(f"model reference {ref!r} must look like provider:model")
    return provider, model


def model_info(config: AppConfig, model_ref: str) -> ModelInfo:
    return config.models.get(model_ref, ModelInfo())


def create_provider(config: AppConfig, model_ref: str) -> ModelProvider:
    provider_name, model = parse_model_ref(model_ref)
    pcfg = config.providers.get(provider_name)
    if pcfg is None:
        raise ProviderError(
            f"provider {provider_name!r} is not configured; "
            f"add [providers.{provider_name}] to config"
        )
    info = model_info(config, model_ref)
    if pcfg.type == "anthropic":
        from trendlab.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=model,
            api_key_env=pcfg.api_key_env,
            provider_name=provider_name,
            max_tokens=pcfg.max_tokens,
            thinking=pcfg.thinking,
            effort=pcfg.effort,
            refusal_fallbacks=pcfg.refusal_fallbacks,
            cache=pcfg.prompt_caching,
            timeout=pcfg.timeout_seconds,
            context_window=info.context_window,
        )
    if pcfg.type not in {"openai_compatible", "openai", "ollama"}:
        raise ProviderError(f"unknown provider type {pcfg.type!r}")
    is_ollama = pcfg.type == "ollama"
    native = info.supports_tools and pcfg.tool_calling != "structured"
    if is_ollama:
        from trendlab.providers.ollama_provider import OllamaProvider

        provider_: ModelProvider = OllamaProvider(
            base_url=pcfg.base_url,
            model=model,
            provider_name=provider_name,
            timeout=max(pcfg.timeout_seconds, 300.0),  # local 14B+ models are slow to prefill
            context_window=info.context_window,
            native_tools=native,
        )
        if not native:
            provider_ = StructuredToolProvider(provider_)
        return provider_
    provider: ModelProvider = OpenAICompatibleProvider(
        base_url=pcfg.base_url,
        model=model,
        api_key_env=None if is_ollama else pcfg.api_key_env,
        provider_name=provider_name,
        timeout=pcfg.timeout_seconds,
        context_window=info.context_window or config.context.default_context_window,
        native_tools=native,
        local=True if is_ollama else info.local,
    )
    if not native:
        provider = StructuredToolProvider(provider)
    return provider


def resolve_role(config: AppConfig, role: str, session_model: str) -> str:
    """Routing: which model serves ``role``? Falls back to the session model."""
    if role in config.routing:
        return config.routing[role]
    if role != "default" and "default" in config.routing:
        return config.routing["default"]
    return session_model


def fallback_chain(config: AppConfig, model_ref: str) -> list[str]:
    """``[model_ref, *configured fallbacks]`` without duplicates."""
    chain = [model_ref]
    for ref in config.fallback.get(model_ref, []):
        if ref not in chain:
            chain.append(ref)
    return chain

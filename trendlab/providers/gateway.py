"""ModelGateway — retries, backoff and provider fallback around any ModelProvider (spec §43, §69).

Fallback happens only for infrastructure failures (rate limit, timeout,
unavailable, malformed stream), never because of answer quality.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

from trendlab.config.schema import AppConfig
from trendlab.providers.base import (
    ModelProvider,
    ModelResponse,
    ProviderContextOverflowError,
    ProviderError,
    StreamChunk,
)
from trendlab.providers.registry import create_provider, fallback_chain
from trendlab.telemetry.events import EventBus, EventType

ProviderFactory = Callable[[str], ModelProvider]


class ModelGateway:
    def __init__(
        self,
        config: AppConfig,
        events: EventBus,
        session_id: str,
        *,
        provider_factory: ProviderFactory | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.config = config
        self.events = events
        self.session_id = session_id
        self._factory = provider_factory or (lambda ref: create_provider(config, ref))
        self._providers: dict[str, ModelProvider] = {}
        self.recorder = None  # (role, model, response) -> None; cassette recording (U20)
        self._sleep = sleep

    def provider(self, model_ref: str) -> ModelProvider:
        if model_ref not in self._providers:
            self._providers[model_ref] = self._factory(model_ref)
        return self._providers[model_ref]

    def register(self, model_ref: str, provider: ModelProvider) -> None:
        self._providers[model_ref] = provider

    async def complete(
        self, model_ref: str, messages, tools=None, *, role: str | None = None
    ) -> tuple[ModelResponse, str]:
        """Returns (response, model_ref_actually_used). ``role`` applies that role's
        ``[sampling]`` parameters to this request."""
        params = (getattr(self.config, "sampling", None) or {}).get(role) if role else None
        if params is not None and messages:
            sampling = {k: v for k, v in params.model_dump().items() if v is not None}
            if sampling:
                messages = [dict(m) for m in messages]
                messages[0]["_sampling"] = sampling
        last: ProviderError | None = None
        for ref in fallback_chain(self.config, model_ref):
            try:
                response = await self._with_retry(ref, lambda p: p.complete(messages, tools))
                if self.recorder is not None:
                    self.recorder(role or "main", ref, response)
                return response, ref
            except ProviderContextOverflowError:
                raise  # the context engine handles this; fallback would not help
            except ProviderError as exc:
                last = exc
                if not exc.retryable:
                    raise
                self.events.emit(
                    EventType.PROVIDER_FALLBACK,
                    session_id=self.session_id,
                    from_model=ref,
                    error=str(exc)[:200],
                )
        assert last is not None
        raise last

    async def stream(
        self, model_ref: str, messages, tools=None, *, role: str | None = None
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the primary model with the same retry/backoff as ``complete``; fall back
        to the next model only while nothing has been emitted yet."""
        rc = self.config.retry
        for ref in fallback_chain(self.config, model_ref):
            attempt = 0
            while True:
                attempt += 1
                emitted = False
                try:
                    async for chunk in self.provider(ref).stream(messages, tools):
                        emitted = True
                        if chunk.final is not None and self.recorder is not None:
                            self.recorder(role or "main", ref, chunk.final)
                        yield chunk
                    return
                except ProviderContextOverflowError:
                    raise
                except ProviderError as exc:
                    if emitted or not exc.retryable:
                        raise
                    if attempt < rc.max_attempts:
                        delay = min(
                            rc.max_delay_seconds, rc.base_delay_seconds * 2 ** (attempt - 1)
                        )
                        delay += random.uniform(0, delay * 0.1)  # noqa: S311 — jitter
                        self.events.emit(
                            EventType.PROVIDER_RETRY,
                            session_id=self.session_id,
                            model=ref,
                            attempt=attempt,
                            delay_seconds=round(delay, 2),
                            error=str(exc)[:200],
                        )
                        await self._sleep(delay)
                        continue
                    self.events.emit(
                        EventType.PROVIDER_FALLBACK,
                        session_id=self.session_id,
                        from_model=ref,
                        error=str(exc)[:200],
                    )
                    break
        raise ProviderError("all providers failed")

    async def _with_retry(self, ref: str, op: Callable[[ModelProvider], Any]):
        rc = self.config.retry
        provider = self.provider(ref)
        attempt = 0
        while True:
            attempt += 1
            try:
                return await op(provider)
            except ProviderError as exc:
                if not exc.retryable or attempt >= rc.max_attempts:
                    raise
                delay = min(rc.max_delay_seconds, rc.base_delay_seconds * 2 ** (attempt - 1))
                delay += random.uniform(0, delay * 0.1)  # noqa: S311 — jitter, not security
                self.events.emit(
                    EventType.PROVIDER_RETRY,
                    session_id=self.session_id,
                    model=ref,
                    attempt=attempt,
                    delay_seconds=round(delay, 2),
                    error=str(exc)[:200],
                )
                await self._sleep(delay)

    async def close(self) -> None:
        for p in self._providers.values():
            await p.close()
        self._providers.clear()

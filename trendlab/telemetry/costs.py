"""Cost tracking and budget controls (spec §40, §41). Prices come from config, never code."""

from __future__ import annotations

from dataclasses import dataclass, field

from trendlab.config.schema import AppConfig
from trendlab.providers.base import TokenUsage


@dataclass
class ModelCallRecord:
    model_ref: str
    role: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    latency_ms: int
    cost_usd: float
    local: bool


@dataclass
class CostTracker:
    config: AppConfig
    records: list[ModelCallRecord] = field(default_factory=list)
    _warned: bool = False

    def price(self, model_ref: str, usage: TokenUsage) -> float:
        pricing = self.config.pricing.get(model_ref)
        if pricing is None:
            return 0.0
        cached = usage.cached_input_tokens
        uncached = max(0, usage.input_tokens - cached)
        cached_rate = (
            pricing.cached_input_per_million
            if pricing.cached_input_per_million is not None
            else pricing.input_per_million
        )
        return (
            uncached * pricing.input_per_million
            + cached * cached_rate
            + usage.output_tokens * pricing.output_per_million
        ) / 1_000_000

    def record(
        self,
        model_ref: str,
        usage: TokenUsage,
        latency_ms: int,
        *,
        role: str = "default",
        local: bool = False,
    ) -> ModelCallRecord:
        rec = ModelCallRecord(
            model_ref,
            role,
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_input_tokens,
            latency_ms,
            self.price(model_ref, usage),
            local,
        )
        self.records.append(rec)
        return rec

    @property
    def total_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.records)

    @property
    def model_calls(self) -> int:
        return len(self.records)

    def by_model(self) -> dict[str, dict[str, float | int | bool]]:
        out: dict[str, dict] = {}
        for r in self.records:
            d = out.setdefault(
                r.model_ref, {"calls": 0, "input": 0, "output": 0, "usd": 0.0, "local": r.local}
            )
            d["calls"] += 1
            d["input"] += r.input_tokens
            d["output"] += r.output_tokens
            d["usd"] += r.cost_usd
        return out

    # -- budget -------------------------------------------------------------
    def over_budget(self) -> bool:
        limit = self.config.limits.max_cost_usd
        return limit is not None and self.total_usd >= limit

    def should_warn(self) -> bool:
        """True exactly once when spend crosses the warning fraction."""
        limit = self.config.limits.max_cost_usd
        if limit is None or self._warned:
            return False
        if self.total_usd >= limit * self.config.limits.warn_at_fraction:
            self._warned = True
            return True
        return False

    def over_call_limit(self) -> bool:
        limit = self.config.limits.max_model_calls
        return limit is not None and self.model_calls >= limit

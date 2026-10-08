"""Prompt perturbations and provider chaos for robustness evaluation.

Perturbations keep the meaning and change the surface: typos, terse lowercase, an irrelevant
preamble. A robust harness passes the same tasks either way. Chaos wraps the model provider so
a fraction of calls fail with a retryable error; the gateway's retry and fallback should absorb
it without changing outcomes.
"""

from __future__ import annotations

import random
import re
from typing import Any

from trendlab.providers.base import ModelProvider, ProviderError

PERTURBATIONS = ("typos", "terse", "verbose")
_PREAMBLE = (
    "Quick context before the actual request: we are migrating our CI to a new runner next "
    "week, the team standup moved to Thursdays, and the design review for the billing page is "
    "still pending. None of that matters for this task. Here is what I need: "
)


def perturb(prompt: str, kind: str, seed: str = "") -> str:
    rng = random.Random(f"{kind}:{seed}:{prompt[:40]}")
    if kind == "typos":
        words = prompt.split(" ")
        out = []
        for w in words:
            if len(w) > 4 and w.isalpha() and rng.random() < 0.25:
                i = rng.randrange(1, len(w) - 2)
                w = w[:i] + w[i + 1] + w[i] + w[i + 2 :]
            out.append(w)
        return " ".join(out)
    if kind == "terse":
        first = re.split(r"(?<=[.!?])\s", prompt.strip(), maxsplit=1)[0]
        return first.lower().rstrip(".")
    if kind == "verbose":
        return _PREAMBLE + prompt
    raise ValueError(f"unknown perturbation {kind!r}; choose from {', '.join(PERTURBATIONS)}")


class ChaosError(ProviderError):
    """Injected transient failure (retryable, like a 503)."""

    retryable = True


class ChaosProvider(ModelProvider):
    """Delegates to a real provider but raises a retryable ProviderError on a fraction of calls."""

    def __init__(self, inner: ModelProvider, rate: float, seed: str = "") -> None:
        self.inner = inner
        self.rate = rate
        self.rng = random.Random(f"chaos:{seed}")
        self.injected = 0
        self.name = getattr(inner, "name", "chaos")
        self.model = getattr(inner, "model", "chaos")

    def _maybe_fail(self) -> None:
        if self.rng.random() < self.rate:
            self.injected += 1
            raise ChaosError("chaos: injected transient failure")

    async def complete(self, messages, tools=None):
        self._maybe_fail()
        return await self.inner.complete(messages, tools)

    async def stream(self, messages, tools=None):
        self._maybe_fail()
        async for chunk in self.inner.stream(messages, tools):
            yield chunk

    def capabilities(self):
        return self.inner.capabilities()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

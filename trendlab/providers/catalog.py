"""Model catalog for the picker (spec §90.13): everything you could switch to, with the facts
that matter when choosing — where it runs, context size, price, whether its key is in place.

Sources, merged and de-duplicated: the current model, ``[models]`` and ``[pricing]`` entries,
routing targets, a curated list per provider type, and (for Ollama) the models actually pulled,
read live from ``/api/tags``.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from trendlab.config.schema import AppConfig
from trendlab.security.secrets import resolve_secret

# Curated per provider: shown even before they appear in config, so the picker is never empty.
KNOWN: dict[str, list[tuple[str, str, int]]] = {  # (model, note, context window)
    "anthropic": [
        ("claude-opus-5", "strongest", 1_000_000),
        ("claude-sonnet-5", "balanced", 1_000_000),
        ("claude-haiku-4-5", "fast · cheap", 200_000),
    ],
    "api.openai.com": [
        ("gpt-5", "strongest", 400_000),
        ("gpt-5-mini", "fast · cheap", 400_000),
        ("gpt-5-nano", "cheapest", 400_000),
    ],
    "api.deepseek.com": [
        ("deepseek-flash", "cheapest usable", 1_000_000),
        ("deepseek-v4-pro", "stronger", 1_000_000),
    ],
}
_KNOWN_CTX = {m: ctx for rows in KNOWN.values() for m, _n, ctx in rows}


@dataclass
class ModelChoice:
    ref: str
    provider: str
    model: str
    local: bool
    key_ok: bool
    context_window: int | None = None
    input_per_million: float | None = None
    output_per_million: float | None = None
    note: str = ""
    current: bool = False
    pulled: bool | None = None  # Ollama: present locally

    @property
    def price_label(self) -> str:
        if self.local:
            return "free · local"
        if self.input_per_million is None:
            return "price not set"
        return f"${self.input_per_million:.2f}/${self.output_per_million or 0:.2f} per M"

    @property
    def ctx_label(self) -> str:
        if not self.context_window:
            return "ctx ?"
        n = self.context_window
        return f"ctx {n // 1_000_000}M" if n >= 1_000_000 else f"ctx {n // 1000}k"

    @property
    def status_label(self) -> str:
        if self.local:
            if self.pulled is False:
                return "not pulled"
            return "LOCAL"
        return "key ✓" if self.key_ok else "key missing"

    def matches(self, query: str) -> bool:
        q = query.lower().strip()
        return not q or q in self.ref.lower() or q in self.note.lower()


def ollama_models(base_url: str, timeout: float = 1.5) -> list[str] | None:
    """Names pulled into a local Ollama, or None when it is not reachable."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        r = httpx.get(f"{root}/api/tags", timeout=timeout)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", []) if m.get("name")]
    except Exception:  # noqa: BLE001 — offline / not installed
        return None


def list_model_choices(
    config: AppConfig,
    current: str,
    *,
    ollama_tags: dict[str, list[str] | None] | None = None,
) -> list[ModelChoice]:
    """Ordered choices: current first, then by provider order in config."""
    refs: dict[str, str] = {}  # ref -> note

    def add(ref: str, note: str = "") -> None:
        if ":" not in ref:
            return
        if ref not in refs or (note and not refs[ref]):
            refs[ref] = note

    add(current)
    for ref in config.models:
        add(ref)
    for ref in config.pricing:
        add(ref)
    for ref in config.routing.values():
        add(ref)
    for pname, pcfg in config.providers.items():
        if pcfg.type == "anthropic":
            for model, note, _ctx in KNOWN["anthropic"]:
                add(f"{pname}:{model}", note)
        elif pcfg.type == "ollama":
            tags = (ollama_tags or {}).get(pname) if ollama_tags is not None else None
            if ollama_tags is None:
                tags = ollama_models(pcfg.base_url)
            for name in tags or []:
                add(f"{pname}:{name}", "pulled")
        else:
            for host, models in KNOWN.items():
                if host in (pcfg.base_url or ""):
                    for model, note, _ctx in models:
                        add(f"{pname}:{model}", note)

    choices: list[ModelChoice] = []
    for ref, note in refs.items():
        pname, model = ref.split(":", 1)
        pcfg = config.providers.get(pname)
        if pcfg is None:
            continue  # a routing/pricing entry for a provider that is not configured
        info = config.models.get(ref)
        local = pcfg.type == "ollama" or bool(info and info.local)
        pricing = config.pricing.get(ref)
        key_ok = local or not pcfg.api_key_env or bool(resolve_secret(pcfg.api_key_env))
        pulled = None
        if pcfg.type == "ollama":
            tags = (ollama_tags or {}).get(pname) if ollama_tags is not None else None
            if tags is not None:
                pulled = model in tags or note == "pulled"
        choices.append(
            ModelChoice(
                ref=ref,
                provider=pname,
                model=model,
                local=local,
                key_ok=key_ok,
                context_window=(info.context_window if info else None) or _KNOWN_CTX.get(model),
                input_per_million=pricing.input_per_million if pricing else None,
                output_per_million=pricing.output_per_million if pricing else None,
                note=note,
                current=(ref == current),
                pulled=pulled,
            )
        )
    order = {name: i for i, name in enumerate(config.providers)}
    choices.sort(key=lambda c: (not c.current, order.get(c.provider, 99), c.ref))
    return choices


def resolve_model_query(choices: list[ModelChoice], query: str) -> ModelChoice | list[ModelChoice]:
    """Exact ref → that choice; otherwise substring matches (one = pick it, many = ask)."""
    q = query.strip()
    for c in choices:
        if c.ref == q:
            return c
    hits = [c for c in choices if c.matches(q)]
    if len(hits) == 1:
        return hits[0]
    exact_model = [c for c in hits if c.model == q]
    if len(exact_model) == 1:
        return exact_model[0]
    return hits

"""Per-model prompt layer (cheap-model spec §6.3).

Short, editable guidance merged into the system prompt for the *current model only*:

1. ``trendlab/prompts/drivers/<provider-type>.md`` — packaged, per provider type
   (``openai_compatible``, ``anthropic``, ``ollama``);
2. ``trendlab/prompts/drivers/models/<provider>-<model>.md`` — packaged, per model
   (the Flash driver profile lives here);
3. ``~/.trendlab/skills/_model/<provider>-<model>.md`` — the owner's overrides;
4. ``<project>/.trendlab/skills/_model/<provider>-<model>.md`` — project overrides.

Later layers are appended, so a project note can refine a packaged profile. The text is
rendered once per run into the system prompt (byte-stable, cache friendly).
"""

from __future__ import annotations

import re
from pathlib import Path

from trendlab.config.schema import AppConfig
from trendlab.providers.registry import parse_model_ref

PACKAGED = Path(__file__).parent / "drivers"
MAX_LAYER_CHARS = 4_000


def model_slug(model_ref: str) -> str:
    provider, model = parse_model_ref(model_ref)
    return re.sub(r"[^a-z0-9.-]+", "-", f"{provider}-{model}".lower()).strip("-")


def driver_layers(config: AppConfig, model_ref: str, project_root: Path, home: Path) -> list[Path]:
    provider, _model = parse_model_ref(model_ref)
    ptype = config.providers[provider].type if provider in config.providers else "openai_compatible"
    slug = model_slug(model_ref)
    return [
        PACKAGED / f"{ptype}.md",
        PACKAGED / "models" / f"{slug}.md",
        home / "skills" / "_model" / f"{slug}.md",
        project_root / ".trendlab" / "skills" / "_model" / f"{slug}.md",
    ]


def driver_text(config: AppConfig, model_ref: str, project_root: Path, home: Path) -> str:
    """The merged model notes, or '' when no layer exists."""
    blocks: list[str] = []
    for path in driver_layers(config, model_ref, project_root, home):
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    blocks.append(text[:MAX_LAYER_CHARS])
        except OSError:
            continue
    if not blocks:
        return ""
    return f"\n\n## Model notes ({model_ref})\n" + "\n\n".join(blocks)

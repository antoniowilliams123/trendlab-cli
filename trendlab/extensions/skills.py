"""Skills (spec §38): reusable instruction packs at ``~/.trendlab/skills/<name>/SKILL.md`` or
``<project>/.trendlab/skills/<name>/SKILL.md``. Optional ``skill.toml`` adds metadata."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    path: Path
    tools: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    # Triggers (cheap-model spec §6.4) from ``[triggers]`` in skill.toml.
    trigger_paths: list[str] = field(default_factory=list)  # glob patterns
    trigger_tools: list[str] = field(default_factory=list)
    trigger_keywords: list[str] = field(default_factory=list)
    trigger_on_failure: list[str] = field(default_factory=list)  # substrings of a failure


class SkillLibrary:
    def __init__(
        self, project_root: Path, home: Path, plugin_dirs: list[Path] | None = None
    ) -> None:
        # later directories override earlier ones: plugins < global < project
        self.dirs = [*(plugin_dirs or []), home / "skills", project_root / ".trendlab" / "skills"]
        self.active: list[str] = []
        self._skills: dict[str, Skill] = {}
        self.reload()

    def reload(self) -> None:
        self._skills.clear()
        for base in self.dirs:
            if not base.is_dir():
                continue
            for d in sorted(p for p in base.iterdir() if p.is_dir()):
                md = d / "SKILL.md"
                if not md.is_file():
                    continue
                text = md.read_text(encoding="utf-8", errors="replace")
                meta: dict = {}
                if (d / "skill.toml").is_file():
                    try:
                        meta = tomllib.loads((d / "skill.toml").read_text(encoding="utf-8"))
                    except tomllib.TOMLDecodeError:
                        meta = {}
                first = next(
                    (ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), d.name
                )
                trig = meta.get("triggers") or {}
                if not isinstance(trig, dict):
                    trig = {}
                self._skills[d.name] = Skill(
                    name=d.name,
                    description=meta.get("description", first)[:200],
                    instructions=text[:12_000],
                    path=md,
                    tools=list(meta.get("tools", [])),
                    validation=list(meta.get("validation", [])),
                    trigger_paths=[str(x) for x in trig.get("paths", [])],
                    trigger_tools=[str(x) for x in trig.get("tools", [])],
                    trigger_keywords=[str(x).lower() for x in trig.get("keywords", [])],
                    trigger_on_failure=[str(x).lower() for x in trig.get("on_failure", [])],
                )

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def activate(self, name: str) -> None:
        if name in self._skills and name not in self.active:
            self.active.append(name)

    def deactivate(self, name: str) -> None:
        if name in self.active:
            self.active.remove(name)

    def match(
        self,
        *,
        task_text: str | None = None,
        tools: list[str] | None = None,
        paths: list[str] | None = None,
        failure: str | None = None,
    ) -> list[tuple[Skill, str]]:
        """Skills whose triggers match; each with the trigger that fired (spec §6.4).
        Already-active skills are skipped (they are in the system prompt)."""
        from fnmatch import fnmatch

        out: list[tuple[Skill, str]] = []
        text = (task_text or "").lower()
        fail = (failure or "").lower()
        for skill in self._skills.values():
            if skill.name in self.active:
                continue
            hit = None
            if text and any(k in text for k in skill.trigger_keywords):
                hit = "keyword"
            elif tools and any(t in skill.trigger_tools for t in tools):
                hit = "tool"
            elif paths and any(fnmatch(p, pat) for p in paths for pat in skill.trigger_paths):
                hit = "path"
            elif text and any(fnmatch(w, pat) for w in text.split() for pat in skill.trigger_paths):
                hit = "path"
            elif fail and any(k in fail for k in skill.trigger_on_failure):
                hit = "on_failure"
            if hit:
                out.append((skill, hit))
        return out

    def apply(self, system_prompt: str) -> str:
        """Append active skills' instructions to a system prompt (idempotent)."""
        marker = "\n\n## Active skills\n"
        base = system_prompt.split(marker)[0]
        if not self.active:
            return base
        blocks = [
            f"### Skill: {n}\n{self._skills[n].instructions}"
            for n in self.active
            if n in self._skills
        ]
        return base + marker + "\n\n".join(blocks)

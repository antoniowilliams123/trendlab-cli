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


class SkillLibrary:
    def __init__(self, project_root: Path, home: Path) -> None:
        self.dirs = [home / "skills", project_root / ".trendlab" / "skills"]
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
                self._skills[d.name] = Skill(
                    name=d.name,
                    description=meta.get("description", first)[:200],
                    instructions=text[:12_000],
                    path=md,
                    tools=list(meta.get("tools", [])),
                    validation=list(meta.get("validation", [])),
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

"""Project-persisted permission rules: ``<project>/.trendlab/permissions.toml`` (spec §17)."""

from __future__ import annotations

import threading
import tomllib
from pathlib import Path

import tomli_w

from trendlab.permissions.models import Decision

FILE_NAME = "permissions.toml"


class ProjectRules:
    def __init__(self, project_root: Path) -> None:
        self.path = project_root / ".trendlab" / FILE_NAME
        self._rules: dict[str, Decision] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return
        for key, value in (data.get("rules") or {}).items():
            try:
                self._rules[key] = Decision(value)
            except ValueError:
                continue

    def lookup(self, key: str) -> Decision | None:
        with self._lock:
            return self._rules.get(key)

    def add(self, key: str, decision: Decision) -> None:
        with self._lock:
            self._rules[key] = decision
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("wb") as fh:
                tomli_w.dump({"rules": {k: v.value for k, v in self._rules.items()}}, fh)

    def remove(self, key: str) -> bool:
        with self._lock:
            if key not in self._rules:
                return False
            del self._rules[key]
            with self.path.open("wb") as fh:
                tomli_w.dump({"rules": {k: v.value for k, v in self._rules.items()}}, fh)
            return True

    def all(self) -> dict[str, Decision]:
        with self._lock:
            return dict(self._rules)

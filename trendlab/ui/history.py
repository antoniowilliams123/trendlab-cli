"""Prompt history (spec §91.2): recall earlier prompts with Ctrl+↑ / Ctrl+↓ (↑/↓ when the mouse
is captured). Stored per project under ``~/.trendlab/history/`` as one JSON line per prompt, so it
survives restarts and never mixes projects.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trendlab.config.loader import trendlab_home

MAX_ENTRIES = 500


class PromptHistory:
    def __init__(self, project_root: Path, *, path: Path | None = None) -> None:
        key = hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()[:16]
        self.path = path or (trendlab_home() / "history" / f"{key}.jsonl")
        self.entries: list[str] = []
        self._cursor: int | None = None  # None = not browsing; else index into entries
        self._draft = ""
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-MAX_ENTRIES:]:
            try:
                text = json.loads(line).get("prompt", "")
            except (ValueError, AttributeError):
                continue
            if text:
                self.entries.append(text)

    def add(self, prompt: str) -> None:
        prompt = prompt.rstrip()
        if not prompt or prompt.startswith("/"):
            self.reset()
            return
        if self.entries and self.entries[-1] == prompt:
            self.reset()
            return
        self.entries.append(prompt)
        self.entries = self.entries[-MAX_ENTRIES:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"prompt": prompt}) + "\n")
        except OSError:
            pass
        self.reset()

    def reset(self) -> None:
        self._cursor = None
        self._draft = ""

    @property
    def browsing(self) -> bool:
        return self._cursor is not None

    def previous(self, current: str) -> str | None:
        """Older entry, remembering what was typed so ↓ can bring it back."""
        if not self.entries:
            return None
        if self._cursor is None:
            self._draft = current
            self._cursor = len(self.entries) - 1
        elif self._cursor > 0:
            self._cursor -= 1
        else:
            return self.entries[0]
        return self.entries[self._cursor]

    def next(self) -> str | None:
        """Newer entry; past the newest returns the saved draft and stops browsing."""
        if self._cursor is None:
            return None
        if self._cursor < len(self.entries) - 1:
            self._cursor += 1
            return self.entries[self._cursor]
        draft, self._draft, self._cursor = self._draft, "", None
        return draft

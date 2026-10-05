"""``@file`` references in prompts (spec §90): expansion and the fuzzy picker's ranking.

``@src/app.py`` in a prompt attaches that file's contents below the prompt; ``@src/`` attaches a
listing. Images (``@shot.png``) are handled by ``trendlab.ui.attachments`` and skipped here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from trendlab.context.ignore import IgnoreRules
from trendlab.ui.attachments import IMAGE_EXTS

MAX_FILE_BYTES = 60_000
MAX_TOTAL_BYTES = 240_000
_REF = re.compile(r"(?<![\w@])@((?:\.{1,2}/|~/|/)?[\w./\\-]+)")


def find_file_refs(text: str, project_root: Path) -> list[tuple[str, Path]]:
    """``(token, path)`` for every ``@path`` that exists inside the project (images excluded)."""
    root = project_root.resolve()
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for m in _REF.finditer(text):
        raw = m.group(1).rstrip(".,;:)")
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved != root and root not in resolved.parents:
            continue  # outside the project: never attach
        if not resolved.exists() or resolved.suffix.lower() in IMAGE_EXTS:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append((m.group(0), resolved))
    return found


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def expand_file_refs(text: str, project_root: Path) -> tuple[str, list[str]]:
    """Return ``(prompt_with_attachments, attached_relative_paths)``.

    Tokens stay in the prompt (the model sees the reference); contents are appended as fenced
    blocks, capped per file and in total so a stray ``@.`` cannot blow the context.
    """
    refs = find_file_refs(text, project_root)
    if not refs:
        return text, []
    root = project_root.resolve()
    blocks: list[str] = []
    attached: list[str] = []
    budget = MAX_TOTAL_BYTES
    for _token, path in refs:
        rel = path.relative_to(root).as_posix() if path != root else "."
        if path.is_dir():
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))[:200]
            listing = "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries)
            body = f"--- @{rel}/ (directory listing) ---\n{listing}"
        else:
            try:
                data = path.read_bytes()
            except OSError as exc:
                body = f"--- @{rel} ---\n(unreadable: {exc})"
            else:
                if _looks_binary(data):
                    body = f"--- @{rel} ---\n(binary file, {len(data)} bytes — not attached)"
                else:
                    truncated = len(data) > MAX_FILE_BYTES
                    content = data[:MAX_FILE_BYTES].decode("utf-8", "replace")
                    note = f"\n… truncated ({len(data)} bytes total)" if truncated else ""
                    body = f"--- @{rel} ---\n```\n{content.rstrip()}\n```{note}"
        if len(body) > budget:
            blocks.append(f"--- @{rel} --- (skipped: attachment budget reached)")
            continue
        budget -= len(body)
        blocks.append(body)
        attached.append(rel)
    return text.rstrip() + "\n\n" + "\n\n".join(blocks), attached


# -- fuzzy ranking for the TUI picker -----------------------------------------------------------
def fuzzy_score(query: str, candidate: str) -> int:
    """Subsequence match score; 0 means no match. Higher is better.

    Rewards matches in the basename, at word boundaries and contiguous runs; penalises length.
    """
    q = query.lower()
    c = candidate.lower()
    if not q:
        return 1
    score = 0
    ci = 0
    prev_hit = -2
    base_start = c.rfind("/") + 1
    for ch in q:
        idx = c.find(ch, ci)
        if idx < 0:
            return 0
        score += 10
        if idx >= base_start:
            score += 6  # basename match
        if idx == 0 or c[idx - 1] in "/_-. ":
            score += 8  # word boundary
        if idx == prev_hit + 1:
            score += 12  # contiguous
        prev_hit = idx
        ci = idx + 1
    if c[base_start:].startswith(q):
        score += 30
    return max(1, score - len(c) // 4)


@dataclass
class FileIndex:
    """Cached list of project files for the picker; refreshed lazily."""

    root: Path
    rules: IgnoreRules | None = None
    max_files: int = 8000
    ttl_seconds: float = 15.0
    _files: list[str] = field(default_factory=list)
    _built_at: float = 0.0

    def files(self) -> list[str]:
        if not self._files or time.monotonic() - self._built_at > self.ttl_seconds:
            rules = self.rules or IgnoreRules.for_project(self.root)
            root = self.root.resolve()
            paths = rules.walk(root, max_files=self.max_files)
            self._files = [p.relative_to(root).as_posix() for p in paths]
            self._built_at = time.monotonic()
        return self._files

    def search(self, query: str, limit: int = 8) -> list[str]:
        query = query.strip().lstrip("@")
        ranked = (
            (fuzzy_score(query, f), f) for f in self.files() if not query or fuzzy_score(query, f)
        )
        best = sorted(((s, f) for s, f in ranked if s > 0), key=lambda t: (-t[0], len(t[1]), t[1]))
        return [f for _, f in best[:limit]]


def current_at_token(line: str, col: int) -> tuple[int, str] | None:
    """If the cursor (``col``) sits in an ``@word`` token on ``line``, return ``(start, token)``."""
    start = col
    while start > 0 and not line[start - 1].isspace():
        start -= 1
    end = col
    while end < len(line) and not line[end].isspace():
        end += 1
    token = line[start:end]
    if not token.startswith("@") or token.lower().endswith(tuple(IMAGE_EXTS)):
        return None
    return start, token

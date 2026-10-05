"""Image attachments for prompts.

A prompt may reference images by path (``@shot.png`` or a bare path ending in an image
extension); ``/paste`` grabs the clipboard image (WSL via PowerShell, Linux via xclip/wl-paste,
macOS via pngpaste). Internally a message with attachments is a list of parts::

    [{"type": "text", "text": "..."}, {"type": "image_path", "path": "/abs/file.png"}]

Only the path is persisted; providers read and base64-encode the file at request time.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_TOKEN = re.compile(
    r"(?<!\w)@?((?:~|/|\.{1,2}/)?[\w./\\-]*?\.(?:png|jpe?g|gif|webp))(?=\s|$|[,;:)])", re.I
)


def find_image_paths(text: str, project_root: Path) -> list[tuple[str, Path]]:
    """Return (token, resolved_path) for image references in ``text`` that exist on disk."""
    found: list[tuple[str, Path]] = []
    for m in _TOKEN.finditer(text):
        raw = m.group(1).strip()
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        if candidate.is_file() and candidate.suffix.lower() in IMAGE_EXTS:
            found.append((m.group(0), candidate.resolve()))
    return found


def build_user_content(
    text: str, project_root: Path, extra: list[Path] | None = None
) -> str | list[dict[str, Any]]:
    """Plain string when there are no images; otherwise a parts list with the images first."""
    refs = find_image_paths(text, project_root)
    paths: list[Path] = []
    for token, path in refs:
        text = text.replace(token, path.name)
        if path not in paths:
            paths.append(path)
    for p in extra or []:
        if p not in paths:
            paths.append(p)
    if not paths:
        return text
    parts: list[dict[str, Any]] = [{"type": "image_path", "path": str(p)} for p in paths]
    parts.append({"type": "text", "text": text.strip() or "(see attached image)"})
    return parts


def encode_image(path: str | Path) -> tuple[str, str]:
    """(media_type, base64 data) for an image file; raises ValueError on size/type problems."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"image not found: {p}")
    if p.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"image too large (> {MAX_IMAGE_BYTES // (1024 * 1024)} MB): {p.name}")
    media = mimetypes.guess_type(p.name)[0] or "image/png"
    if media == "image/jpg":
        media = "image/jpeg"
    return media, base64.b64encode(p.read_bytes()).decode("ascii")


def text_of(content: str | list[dict[str, Any]] | None) -> str:
    """Flatten message content to text (images become a short marker)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for part in content:
        if part.get("type") == "text":
            out.append(part.get("text", ""))
        elif part.get("type") == "image_path":
            out.append(f"[image: {Path(part['path']).name}]")
    return "\n".join(out)


def image_count(content: str | list[dict[str, Any]] | None) -> int:
    return (
        sum(1 for p in content if p.get("type") == "image_path") if isinstance(content, list) else 0
    )


def grab_clipboard_image(dest_dir: Path | None = None) -> Path | None:
    """Save the clipboard image to a PNG and return its path, or None if there is none."""
    dest_dir = dest_dir or Path(tempfile.gettempdir()) / "trendlab-paste"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"paste-{int(time.time())}.png"
    attempts: list[list[str]] = []
    if shutil.which("powershell.exe"):  # WSL
        win_path = subprocess.run(
            ["wslpath", "-w", str(target)], capture_output=True, text=True, check=False
        ).stdout.strip()
        ps = (
            "$img = Get-Clipboard -Format Image; "
            f"if ($img) {{ $img.Save('{win_path}'); 'ok' }} else {{ 'none' }}"
        )
        attempts.append(["powershell.exe", "-NoProfile", "-Command", ps])
    if shutil.which("wl-paste"):
        attempts.append(["sh", "-c", f"wl-paste --type image/png > '{target}'"])
    if shutil.which("xclip"):
        attempts.append(["sh", "-c", f"xclip -selection clipboard -t image/png -o > '{target}'"])
    if shutil.which("pngpaste"):
        attempts.append(["pngpaste", str(target)])
    for cmd in attempts:
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if target.is_file() and target.stat().st_size > 0:
            return target
    return None

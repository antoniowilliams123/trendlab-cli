"""Update check (spec §90): compare the installed version with PyPI and GitHub Releases.

``trendlab update`` checks now; the interactive start shows a one-line hint at most once a day
(cached in ``~/.trendlab/update_check.json``). Nothing is installed automatically — the hint
prints the exact command for the way TrendLab was installed (pipx / pip / git checkout).
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from trendlab import __version__
from trendlab.config.loader import trendlab_home

PYPI_URL = "https://pypi.org/pypi/trendlab-cli/json"
GITHUB_URL = "https://api.github.com/repos/antoniowilliams123/trendlab-cli/releases/latest"
REPO_URL = "https://github.com/antoniowilliams123/trendlab-cli"
CACHE_TTL = 24 * 3600


@dataclass
class UpdateInfo:
    current: str
    latest: str | None
    source: str | None  # "pypi" | "github" | None
    url: str | None
    checked_at: float
    error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.latest) and version_tuple(self.latest) > version_tuple(self.current)

    def hint(self) -> str:
        """Human line (empty when up to date)."""
        if not self.available:
            return ""
        return (
            f"TrendLab {self.latest} is available (you have {self.current}) — "
            f"{install_command(self.source)}"
        )


def version_tuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def install_command(source: str | None) -> str:
    """The upgrade command for the way this copy was installed."""
    exe = sys.argv[0] if sys.argv else ""
    via_pipx = "pipx" in sys.prefix or "pipx" in exe
    if source == "pypi":
        return "pipx upgrade trendlab-cli" if via_pipx else "pip install -U trendlab-cli"
    git_url = f"git+{REPO_URL}.git"
    if via_pipx:
        return f"pipx install --force {git_url}"
    return f"pip install -U {git_url}  (or: git pull && pip install -e .)"


def _cache_path() -> Path:
    return trendlab_home() / "update_check.json"


def read_cache(path: Path | None = None) -> UpdateInfo | None:
    p = path or _cache_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return UpdateInfo(**{k: data.get(k) for k in UpdateInfo.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def write_cache(info: UpdateInfo, path: Path | None = None) -> None:
    p = path or _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(info)), encoding="utf-8")
    except OSError:
        pass


def check_for_update(
    *,
    current: str = __version__,
    client: httpx.Client | None = None,
    timeout: float = 4.0,
    pypi_url: str = PYPI_URL,
    github_url: str = GITHUB_URL,
) -> UpdateInfo:
    """Query PyPI first (the normal install path), then GitHub Releases. Never raises."""
    own = client is None
    client = client or httpx.Client(
        timeout=timeout, headers={"User-Agent": f"trendlab-cli/{current}"}
    )
    latest = source = url = None
    errors: list[str] = []
    try:
        try:
            r = client.get(pypi_url)
            if r.status_code == 200:
                latest = r.json().get("info", {}).get("version")
                source, url = "pypi", "https://pypi.org/project/trendlab-cli/"
            elif r.status_code != 404:
                errors.append(f"pypi HTTP {r.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"pypi {exc.__class__.__name__}")
        if latest is None:
            try:
                r = client.get(github_url)
                if r.status_code == 200:
                    body = r.json()
                    latest = str(body.get("tag_name", "")).lstrip("v") or None
                    source, url = "github", body.get("html_url") or f"{REPO_URL}/releases"
                else:
                    errors.append(f"github HTTP {r.status_code}")
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"github {exc.__class__.__name__}")
    finally:
        if own:
            client.close()
    return UpdateInfo(
        current=current,
        latest=latest,
        source=source,
        url=url,
        checked_at=time.time(),
        error="; ".join(errors) or None,
    )


def daily_hint(
    *, path: Path | None = None, now: float | None = None, checker=check_for_update
) -> str:
    """Cheap startup hook: uses the cache when fresh, otherwise checks once and caches."""
    now = now or time.time()
    cached = read_cache(path)
    if cached is not None and now - (cached.checked_at or 0) < CACHE_TTL:
        info = cached
    else:
        info = checker()
        write_cache(info, path)
    return info.hint()

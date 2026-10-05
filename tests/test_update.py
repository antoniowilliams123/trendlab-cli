"""Update check: PyPI first, GitHub fallback, daily cache, never raises."""

import json
import time
from pathlib import Path

import httpx
from typer.testing import CliRunner

from trendlab.cli import app
from trendlab.update import (
    UpdateInfo,
    check_for_update,
    daily_hint,
    install_command,
    read_cache,
    version_tuple,
    write_cache,
)


def _client(pypi_status=200, pypi_version="0.1.0", gh_status=200, gh_tag="v0.2.0"):
    def handler(req: httpx.Request) -> httpx.Response:
        if "pypi.org" in req.url.host:
            if pypi_status == 200:
                return httpx.Response(200, json={"info": {"version": pypi_version}})
            return httpx.Response(pypi_status)
        if "api.github.com" in req.url.host:
            if gh_status == 200:
                return httpx.Response(
                    200,
                    json={
                        "tag_name": gh_tag,
                        "html_url": "https://github.com/x/y/releases/tag/" + gh_tag,
                    },
                )
            return httpx.Response(gh_status)
        raise AssertionError(req.url)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_version_tuple_and_pypi_preferred():
    assert version_tuple("v1.2.3") == (1, 2, 3) and version_tuple("0.1.0") < version_tuple("0.1.1")
    info = check_for_update(current="0.1.0", client=_client(pypi_version="0.3.0"))
    assert (
        info.available and info.latest == "0.3.0" and info.source == "pypi" and info.error is None
    )
    assert "0.3.0" in info.hint()
    same = check_for_update(current="0.1.0", client=_client(pypi_version="0.1.0"))
    assert not same.available and same.hint() == ""


def test_github_fallback_and_errors_never_raise():
    info = check_for_update(current="0.1.0", client=_client(pypi_status=404))
    assert info.source == "github" and info.latest == "0.2.0" and info.available
    assert info.url.endswith("v0.2.0")
    dead = check_for_update(current="0.1.0", client=_client(pypi_status=500, gh_status=503))
    assert dead.latest is None and "pypi HTTP 500" in dead.error and "github HTTP 503" in dead.error
    assert not dead.available and dead.hint() == ""

    def boom(req):
        raise httpx.ConnectError("offline")

    off = check_for_update(
        current="0.1.0", client=httpx.Client(transport=httpx.MockTransport(boom))
    )
    assert off.latest is None and "ConnectError" in off.error


def test_daily_hint_uses_cache(tmp_path: Path):
    cache = tmp_path / "u.json"
    calls = []

    def checker():
        calls.append(1)
        return UpdateInfo(
            current="0.1.0", latest="0.9.0", source="pypi", url=None, checked_at=time.time()
        )

    assert "0.9.0" in daily_hint(path=cache, checker=checker)
    assert "0.9.0" in daily_hint(path=cache, checker=checker)
    assert len(calls) == 1 and read_cache(cache).latest == "0.9.0"
    stale = json.loads(cache.read_text())
    stale["checked_at"] = time.time() - 2 * 24 * 3600
    cache.write_text(json.dumps(stale))
    daily_hint(path=cache, checker=checker)
    assert len(calls) == 2
    cache.write_text("not json")
    assert read_cache(cache) is None
    write_cache(UpdateInfo("0.1.0", None, None, None, 1.0), cache)
    assert read_cache(cache).latest is None


def test_install_command_shapes():
    assert "trendlab-cli" in install_command("pypi")
    assert "git+https://github.com/antoniowilliams123/trendlab-cli.git" in install_command("github")


def test_cli_update_command(monkeypatch):
    import trendlab.update as upd

    monkeypatch.setattr(
        upd,
        "check_for_update",
        lambda **kw: check_for_update(current="0.1.0", client=_client(pypi_version="0.5.0")),
    )
    monkeypatch.setattr(upd, "write_cache", lambda info, path=None: None)
    res = CliRunner().invoke(app, ["update"])
    assert (
        res.exit_code == 0 and "0.5.0 is available" in res.output and "Upgrade with" in res.output
    )
    monkeypatch.setattr(
        upd,
        "check_for_update",
        lambda **kw: check_for_update(current="0.1.0", client=_client(pypi_version="0.1.0")),
    )
    res = CliRunner().invoke(app, ["update"])
    assert res.exit_code == 0 and "up to date" in res.output

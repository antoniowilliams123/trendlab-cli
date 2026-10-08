"""cargo test / go test output → tiers."""

from __future__ import annotations

import re
from typing import Any

_CARGO = re.compile(
    r"^test result: (?P<status>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed; "
    r"(?P<ignored>\d+) ignored(?:; (?P<measured>\d+) measured)?"
    r"(?:; (?P<filtered>\d+) filtered out)?"
    r"(?:; finished in (?P<secs>[\d.]+)s)?",
    re.M,
)
_CARGO_FAIL = re.compile(r"^test (?P<id>\S+) \.\.\. FAILED$", re.M)
_CARGO_PANIC = re.compile(
    r"^thread '(?P<t>[^']+)' panicked at (?P<loc>[^:\n]+:\d+:\d+):\n(?P<msg>.*)$", re.M
)
_GO_FAIL = re.compile(r"^--- FAIL: (?P<id>\S+)", re.M)
_GO_PKG = re.compile(r"^(?P<status>ok|FAIL)\s+(?P<pkg>\S+)\s+(?P<secs>[\d.]+)s", re.M)
_GO_LOC = re.compile(r"^\s+(?P<file>[\w./-]+_test\.go):(?P<line>\d+): (?P<msg>.*)$", re.M)


def parse(text: str, meta: dict[str, Any]) -> Any:
    from trendlab.tools.views import ToolOutput

    results = list(_CARGO.finditer(text))
    if results:
        passed = sum(int(r.group("passed")) for r in results)
        failed = sum(int(r.group("failed")) for r in results)
        ignored = sum(int(r.group("ignored")) for r in results)
        secs = sum(float(r.group("secs") or 0) for r in results)
        t1 = f"tests: {passed} passed, {failed} failed, {ignored} ignored, {secs:.1f}s"
        if meta.get("exit_code") is not None:
            t1 += f", exit {meta['exit_code']}"
        fails = [f.group("id") for f in _CARGO_FAIL.finditer(text)][:12]
        tier2 = [f"FAIL {f}" for f in fails]
        for p in list(_CARGO_PANIC.finditer(text))[:8]:
            tier2.append(f"  {p.group('t')}: {p.group('loc')} — {p.group('msg').strip()[:140]}")
        if fails:
            t1 += f"\nfirst failing: {fails[0]}"
        return ToolOutput(
            tier1=t1,
            tier2="\n".join(tier2) or None,
            kind="tests",
            stats={
                "counts": {"passed": passed, "failed": failed, "ignored": ignored},
                "failed": failed,
                "secs": secs,
                "ok": failed == 0,
            },
        )
    pkgs = list(_GO_PKG.finditer(text))
    go_fails = [f.group("id") for f in _GO_FAIL.finditer(text)]
    if pkgs or go_fails:
        ok_pkgs = sum(1 for p in pkgs if p.group("status") == "ok")
        fail_pkgs = sum(1 for p in pkgs if p.group("status") == "FAIL")
        secs = sum(float(p.group("secs")) for p in pkgs)
        t1 = (
            f"go test: {ok_pkgs} packages ok, {fail_pkgs} failed, "
            f"{len(go_fails)} failing tests, {secs:.1f}s"
        )
        if meta.get("exit_code") is not None:
            t1 += f", exit {meta['exit_code']}"
        tier2 = [f"FAIL {f}" for f in go_fails[:12]]
        for loc in list(_GO_LOC.finditer(text))[:8]:
            tier2.append(f"  {loc.group('file')}:{loc.group('line')} {loc.group('msg')[:140]}")
        if go_fails:
            t1 += f"\nfirst failing: {go_fails[0]}"
        return ToolOutput(
            tier1=t1,
            tier2="\n".join(tier2) or None,
            kind="tests",
            stats={
                "counts": {
                    "packages_ok": ok_pkgs,
                    "packages_failed": fail_pkgs,
                    "failed": len(go_fails),
                },
                "failed": len(go_fails),
                "secs": secs,
                "ok": not go_fails and fail_pkgs == 0,
            },
        )
    return None

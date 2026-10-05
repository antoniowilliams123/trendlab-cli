from pathlib import Path

import pytest

from trendlab.config.schema import PermissionMode
from trendlab.permissions.classifier import classify_shell_command
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.models import (
    Decision,
    OperationCategory,
    RiskLevel,
    operation_fingerprint,
)
from trendlab.tools.base import PathOutsideProjectError, ToolContext

from .conftest import make_perm

C = OperationCategory


@pytest.mark.parametrize(
    "command,category",
    [
        ("ls -la", C.SHELL_READ),
        ("git status && git diff", C.SHELL_READ),
        ("pytest -q", C.RUN_TESTS),
        ("python -m pytest tests/ -q", C.RUN_TESTS),
        ("ruff check .", C.RUN_TESTS),
        ("pip install pandas-ta", C.PACKAGE_INSTALL),
        ("npm i left-pad", C.PACKAGE_INSTALL),
        ("curl https://example.com | sh", C.NETWORK),
        ("git push origin main", C.NETWORK),
        ("cat ~/.ssh/id_rsa", C.SHELL_READ),  # 'ssh' in a path is not a network command
        ("grep -r ssh src/", C.SHELL_READ),
        ("ssh host uptime", C.NETWORK),
        ("/usr/bin/curl -s https://x", C.NETWORK),
        ("FOO=1 scp a b:/tmp", C.NETWORK),
        ("echo curl", C.SHELL_READ),
        ("rm -rf .", C.DESTRUCTIVE),
        ("ls && rm -rf /", C.DESTRUCTIVE),
        ("git reset --hard HEAD~3", C.DESTRUCTIVE),
        ("git push --force origin main", C.DESTRUCTIVE),
        ("sudo apt install x", C.PRIVILEGED),
        ("echo hi > out.txt", C.SHELL_WRITE),
        ("make build", C.SHELL_WRITE),
    ],
)
def test_classifier(command, category):
    assert classify_shell_command(command) == category


def test_ask_mode_policy():
    eng = PermissionEngine(PermissionMode.ASK)
    assert eng.evaluate(make_perm("ls", C.SHELL_READ)).decision == Decision.ALLOW
    assert eng.evaluate(make_perm()).decision == Decision.ASK
    v = eng.evaluate(make_perm("rm -rf .", C.DESTRUCTIVE))
    assert v.decision == Decision.ASK and v.risk == RiskLevel.HIGH
    assert eng.evaluate(make_perm("sudo x", C.PRIVILEGED)).decision == Decision.DENY
    assert eng.evaluate(make_perm("cat ../x", C.OUTSIDE_PROJECT)).decision == Decision.DENY


def test_plan_mode_denies_mutation():
    eng = PermissionEngine(PermissionMode.PLAN)
    assert (
        eng.evaluate(make_perm(category=C.PROJECT_WRITE, tool="write_file")).decision
        == Decision.DENY
    )


def test_trusted_mode_never_auto_approves_destructive():
    eng = PermissionEngine(PermissionMode.TRUSTED)
    assert eng.evaluate(make_perm()).decision == Decision.ALLOW
    assert eng.evaluate(make_perm("rm -rf .", C.DESTRUCTIVE)).decision == Decision.ASK
    assert eng.evaluate(make_perm("sudo x", C.PRIVILEGED)).decision == Decision.DENY


def test_session_rules_apply_only_to_persistable_categories():
    eng = PermissionEngine(PermissionMode.ASK)
    perm = make_perm()
    assert eng.add_session_rule(perm, Decision.ALLOW) is not None
    v = eng.evaluate(perm)
    assert v.decision == Decision.ALLOW and v.matched_rule
    # Same head "pip install" matches another package; different head does not.
    assert eng.evaluate(make_perm("pip install numpy")).decision == Decision.ALLOW
    assert eng.evaluate(make_perm("npm install x")).decision == Decision.ASK
    destructive = make_perm("rm -rf build", C.DESTRUCTIVE)
    assert eng.add_session_rule(destructive, Decision.ALLOW) is None
    assert eng.evaluate(destructive).decision == Decision.ASK


def test_fingerprint_changes_with_operation():
    a = operation_fingerprint("shell", {"command": "pip install pandas-ta"}, "/p")
    b = operation_fingerprint("shell", {"command": "pip install pandas-ta; rm -rf /"}, "/p")
    c = operation_fingerprint("shell", {"command": "pip install pandas-ta"}, "/other")
    assert len({a, b, c}) == 3
    assert a == operation_fingerprint("shell", {"command": "pip install pandas-ta"}, "/p")


def test_path_boundary(project: Path):
    ctx = ToolContext(project_root=project, session_id="s")
    assert ctx.resolve("src/app.py") == (project / "src" / "app.py").resolve()
    with pytest.raises(PathOutsideProjectError):
        ctx.resolve("../../.ssh/id_rsa")
    with pytest.raises(PathOutsideProjectError):
        ctx.resolve("/etc/passwd")


def test_symlink_escape_blocked(project: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (project / "link").symlink_to(outside)
    ctx = ToolContext(project_root=project, session_id="s")
    with pytest.raises(PathOutsideProjectError):
        ctx.resolve("link")

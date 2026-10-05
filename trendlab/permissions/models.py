"""Permission vocabulary shared by tools, the engine and the approval system."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any


class OperationCategory(StrEnum):
    """What kind of thing an operation is (drives the policy table)."""

    READ_ONLY = "read_only"
    PROJECT_WRITE = "project_write"
    FILE_DELETE = "file_delete"
    RUN_TESTS = "run_tests"
    PACKAGE_INSTALL = "package_install"
    SHELL_READ = "shell_read"
    SHELL_WRITE = "shell_write"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"
    OUTSIDE_PROJECT = "outside_project"


class RiskLevel(StrEnum):
    """Human-facing risk shown on approval prompts (local and remote)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


RISK_BY_CATEGORY: dict[OperationCategory, RiskLevel] = {
    OperationCategory.READ_ONLY: RiskLevel.LOW,
    OperationCategory.SHELL_READ: RiskLevel.LOW,
    OperationCategory.RUN_TESTS: RiskLevel.LOW,
    OperationCategory.PROJECT_WRITE: RiskLevel.MEDIUM,
    OperationCategory.SHELL_WRITE: RiskLevel.MEDIUM,
    OperationCategory.PACKAGE_INSTALL: RiskLevel.MEDIUM,
    OperationCategory.NETWORK: RiskLevel.MEDIUM,
    OperationCategory.FILE_DELETE: RiskLevel.HIGH,
    OperationCategory.DESTRUCTIVE: RiskLevel.HIGH,
    OperationCategory.PRIVILEGED: RiskLevel.HIGH,
    OperationCategory.OUTSIDE_PROJECT: RiskLevel.HIGH,
}


def operation_fingerprint(tool: str, args: dict[str, Any], cwd: str) -> str:
    """Stable hash of the exact operation being authorized.

    An approval binds to this fingerprint. If the tool, its arguments or the
    working directory change after approval, the fingerprint changes and the
    approval no longer applies.
    """
    canonical = json.dumps(
        {"tool": tool, "args": args, "cwd": cwd}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

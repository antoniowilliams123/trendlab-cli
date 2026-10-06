"""The permission engine: policy table + mode + persisted rules → verdict.

The engine never executes anything and never talks to a UI. It answers one
question: for this operation, in this mode, with these rules, is the answer
ALLOW, DENY, or ASK? Approvals (local or remote) exist only to resolve ASK.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from trendlab.config.schema import PermissionMode
from trendlab.permissions.models import (
    RISK_BY_CATEGORY,
    Decision,
    OperationCategory,
    RiskLevel,
    operation_fingerprint,
)
from trendlab.permissions.rules import ProjectRules

# Policy per mode. Categories absent from a mode's table fall back to ASK.
_POLICY: dict[PermissionMode, dict[OperationCategory, Decision]] = {
    PermissionMode.PLAN: {
        OperationCategory.READ_ONLY: Decision.ALLOW,
        OperationCategory.SHELL_READ: Decision.ALLOW,
        OperationCategory.RUN_TESTS: Decision.ALLOW,
        OperationCategory.PROJECT_WRITE: Decision.DENY,
        OperationCategory.FILE_DELETE: Decision.DENY,
        OperationCategory.SHELL_WRITE: Decision.DENY,
        OperationCategory.PACKAGE_INSTALL: Decision.DENY,
        OperationCategory.NETWORK: Decision.DENY,
        OperationCategory.DESTRUCTIVE: Decision.DENY,
        OperationCategory.PRIVILEGED: Decision.DENY,
        OperationCategory.OUTSIDE_PROJECT: Decision.DENY,
    },
    PermissionMode.ASK: {
        OperationCategory.READ_ONLY: Decision.ALLOW,
        OperationCategory.SHELL_READ: Decision.ALLOW,
        OperationCategory.RUN_TESTS: Decision.ALLOW,
        OperationCategory.PROJECT_WRITE: Decision.ASK,
        OperationCategory.FILE_DELETE: Decision.ASK,
        OperationCategory.SHELL_WRITE: Decision.ASK,
        OperationCategory.PACKAGE_INSTALL: Decision.ASK,
        OperationCategory.NETWORK: Decision.ASK,
        OperationCategory.DESTRUCTIVE: Decision.ASK,
        OperationCategory.PRIVILEGED: Decision.DENY,
        OperationCategory.OUTSIDE_PROJECT: Decision.DENY,
    },
    PermissionMode.AUTO_EDIT: {
        OperationCategory.READ_ONLY: Decision.ALLOW,
        OperationCategory.SHELL_READ: Decision.ALLOW,
        OperationCategory.RUN_TESTS: Decision.ALLOW,
        OperationCategory.PROJECT_WRITE: Decision.ALLOW,
        OperationCategory.FILE_DELETE: Decision.ASK,
        OperationCategory.SHELL_WRITE: Decision.ASK,
        OperationCategory.PACKAGE_INSTALL: Decision.ASK,
        OperationCategory.NETWORK: Decision.ASK,
        OperationCategory.DESTRUCTIVE: Decision.ASK,
        OperationCategory.PRIVILEGED: Decision.DENY,
        OperationCategory.OUTSIDE_PROJECT: Decision.DENY,
    },
    PermissionMode.TRUSTED: {
        OperationCategory.READ_ONLY: Decision.ALLOW,
        OperationCategory.SHELL_READ: Decision.ALLOW,
        OperationCategory.RUN_TESTS: Decision.ALLOW,
        OperationCategory.PROJECT_WRITE: Decision.ALLOW,
        OperationCategory.FILE_DELETE: Decision.ASK,
        OperationCategory.SHELL_WRITE: Decision.ALLOW,
        OperationCategory.PACKAGE_INSTALL: Decision.ALLOW,
        OperationCategory.NETWORK: Decision.ASK,
        OperationCategory.DESTRUCTIVE: Decision.ASK,  # never silently auto-approved
        OperationCategory.PRIVILEGED: Decision.DENY,
        OperationCategory.OUTSIDE_PROJECT: Decision.DENY,
    },
    PermissionMode.UNSAFE: {
        OperationCategory.READ_ONLY: Decision.ALLOW,
        OperationCategory.SHELL_READ: Decision.ALLOW,
        OperationCategory.RUN_TESTS: Decision.ALLOW,
        OperationCategory.PROJECT_WRITE: Decision.ALLOW,
        OperationCategory.FILE_DELETE: Decision.ALLOW,
        OperationCategory.SHELL_WRITE: Decision.ALLOW,
        OperationCategory.PACKAGE_INSTALL: Decision.ALLOW,
        OperationCategory.NETWORK: Decision.ALLOW,
        OperationCategory.DESTRUCTIVE: Decision.ASK,  # ALLOW only with allow_destructive
        OperationCategory.PRIVILEGED: Decision.DENY,  # hard boundary, even here
        OperationCategory.OUTSIDE_PROJECT: Decision.DENY,  # hard boundary, even here
    },
}

# Categories that session-scoped "always allow" rules may never cover.
NON_PERSISTABLE = {
    OperationCategory.DESTRUCTIVE,
    OperationCategory.PRIVILEGED,
    OperationCategory.OUTSIDE_PROJECT,
    OperationCategory.FILE_DELETE,
}


class PermissionRequest(BaseModel):
    """Everything the engine (and later the approval UI) needs to know about one operation."""

    tool: str
    category: OperationCategory
    summary: str  # one-line human description, e.g. "Install Python package"
    command: str | None = None  # the exact command / operation text
    cwd: str
    affected_files: list[str] = Field(default_factory=list)
    explanation: str = ""  # why the agent wants to do this (model-supplied, untrusted)
    preview: str | None = None  # e.g. unified diff for file edits; shown to approvers
    args: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None

    @property
    def risk(self) -> RiskLevel:
        return RISK_BY_CATEGORY[self.category]

    @property
    def fingerprint(self) -> str:
        return operation_fingerprint(self.tool, self.args, self.cwd)

    def rule_key(self) -> str:
        """Key used for session-scoped rules: tool + category + command head."""
        head = ""
        if self.command:
            parts = self.command.strip().split()
            head = " ".join(parts[:2]) if parts else ""
        return f"{self.tool}:{self.category.value}:{head}"


@dataclass
class Verdict:
    decision: Decision
    reason: str
    risk: RiskLevel
    matched_rule: str | None = None
    # True when unsafe mode auto-approved something that would otherwise have asked (audited).
    unsafe_auto: bool = False


@dataclass
class PermissionEngine:
    mode: PermissionMode = PermissionMode.ASK
    _session_rules: dict[str, Decision] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Rules persisted in <project>/.trendlab/permissions.toml ("always for this project").
    project_rules: ProjectRules | None = None
    # Second explicit flag: in UNSAFE mode, also run destructive commands without asking.
    allow_destructive: bool = False

    @property
    def unsafe(self) -> bool:
        return self.mode == PermissionMode.UNSAFE

    def evaluate(self, request: PermissionRequest) -> Verdict:
        table = _POLICY[self.mode]
        base = table.get(request.category, Decision.ASK)
        risk = request.risk
        if (
            self.unsafe
            and request.category == OperationCategory.DESTRUCTIVE
            and self.allow_destructive
        ):
            base = Decision.ALLOW
        if self.unsafe and base == Decision.ALLOW:
            would_ask = _POLICY[PermissionMode.ASK].get(request.category) != Decision.ALLOW
            if would_ask:
                return Verdict(Decision.ALLOW, "auto-approved in AUTO mode", risk, unsafe_auto=True)
        if base == Decision.DENY:
            return Verdict(
                Decision.DENY, f"{request.category.value} is denied in {self.mode} mode", risk
            )
        if base == Decision.ALLOW:
            return Verdict(
                Decision.ALLOW, f"{request.category.value} is allowed in {self.mode} mode", risk
            )
        key = request.rule_key()
        with self._lock:
            rule = self._session_rules.get(key)
        if rule is not None and request.category not in NON_PERSISTABLE:
            return Verdict(rule, "matched session rule", risk, matched_rule=key)
        if self.project_rules is not None and request.category not in NON_PERSISTABLE:
            project_rule = self.project_rules.lookup(key)
            if project_rule is not None:
                return Verdict(project_rule, "matched project rule", risk, matched_rule=key)
        return Verdict(Decision.ASK, f"{request.category.value} requires approval", risk)

    def can_persist(self, request: PermissionRequest) -> bool:
        return request.category not in NON_PERSISTABLE

    def add_session_rule(self, request: PermissionRequest, decision: Decision) -> str | None:
        """Remember a decision for the rest of the session. Returns the rule key or None."""
        if not self.can_persist(request) or decision == Decision.ASK:
            return None
        key = request.rule_key()
        with self._lock:
            self._session_rules[key] = decision
        return key

    def add_project_rule(self, request: PermissionRequest, decision: Decision) -> str | None:
        """Persist a decision for this project. Returns the rule key, or None if refused."""
        if self.project_rules is None or not self.can_persist(request) or decision == Decision.ASK:
            return None
        key = request.rule_key()
        self.project_rules.add(key, decision)
        return key

    def session_rules(self) -> dict[str, Decision]:
        with self._lock:
            return dict(self._session_rules)

    def clear_session_rules(self) -> None:
        with self._lock:
            self._session_rules.clear()

    def policy_table(self) -> dict[OperationCategory, Decision]:
        table = dict(_POLICY[self.mode])
        if self.unsafe and self.allow_destructive:
            table[OperationCategory.DESTRUCTIVE] = Decision.ALLOW
        return table

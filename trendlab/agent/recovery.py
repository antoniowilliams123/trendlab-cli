"""Failure classification and recovery policy (spec §24). Retries/backoff/fallback live in the
gateway; this module decides what the *loop* does about failures it can see."""

from __future__ import annotations

from enum import StrEnum

from trendlab.providers.base import (
    ProviderAuthenticationError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class FailureClass(StrEnum):
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    AUTH_FAILURE = "AUTH_FAILURE"
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
    MALFORMED_TOOL_CALL = "MALFORMED_TOOL_CALL"
    PATCH_CONFLICT = "PATCH_CONFLICT"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    TEST_FAILURE = "TEST_FAILURE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NO_PROGRESS = "NO_PROGRESS"


class RecoveryAction(StrEnum):
    RETRY = "retry"
    COMPACT = "compact"
    FEEDBACK = "feedback"  # return a corrective observation to the model
    ESCALATE = "escalate"  # switch to a stronger model if configured
    STOP = "stop"


def classify_provider_error(exc: ProviderError) -> FailureClass:
    if isinstance(exc, ProviderContextOverflowError):
        return FailureClass.CONTEXT_OVERFLOW
    if isinstance(exc, ProviderRateLimitError):
        return FailureClass.RATE_LIMIT
    if isinstance(exc, ProviderTimeoutError):
        return FailureClass.MODEL_TIMEOUT
    if isinstance(exc, ProviderAuthenticationError):
        return FailureClass.AUTH_FAILURE
    if isinstance(exc, ProviderUnavailableError):
        return FailureClass.PROVIDER_FAILURE
    return FailureClass.PROVIDER_FAILURE


def classify_tool_output(tool: str, output: str, ok: bool) -> FailureClass | None:
    if ok:
        return None
    if output.startswith("MALFORMED TOOL CALL") or output.startswith("invalid arguments"):
        return FailureClass.MALFORMED_TOOL_CALL
    if "conflict:" in output or "patch failed" in output:
        return FailureClass.PATCH_CONFLICT
    if "timed out" in output:
        return FailureClass.COMMAND_TIMEOUT
    if output.startswith(("DENIED", "NOT EXECUTED")):
        return FailureClass.PERMISSION_DENIED
    if tool in {"run_tests", "shell"}:
        return FailureClass.TEST_FAILURE
    return None


def recovery_for(
    failure: FailureClass,
    *,
    compaction_attempted: bool,
    escalation_available: bool,
    no_progress_strikes: int,
) -> RecoveryAction:
    if failure == FailureClass.CONTEXT_OVERFLOW:
        return RecoveryAction.COMPACT if not compaction_attempted else RecoveryAction.STOP
    if failure in {FailureClass.AUTH_FAILURE}:
        return RecoveryAction.STOP
    if failure in {
        FailureClass.RATE_LIMIT,
        FailureClass.MODEL_TIMEOUT,
        FailureClass.PROVIDER_FAILURE,
    }:
        return RecoveryAction.STOP  # the gateway already retried and fell back
    if failure == FailureClass.NO_PROGRESS:
        if no_progress_strikes >= 2:
            return RecoveryAction.ESCALATE if escalation_available else RecoveryAction.STOP
        return RecoveryAction.FEEDBACK
    return RecoveryAction.FEEDBACK


NO_PROGRESS_FEEDBACK = (
    "NO_PROGRESS detected: {reason}. Stop repeating this approach. Summarize what you have tried "
    "and why it failed, then choose a materially different strategy (different file, different "
    "command, more context) or explain why the task cannot proceed."
)

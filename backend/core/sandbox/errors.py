"""Sandbox error groups, phase timeout errors and the error classifier.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the groups come
from ``harbor/error_policy.py``. Changes: one class for each group instead of
one class for each provider error, one class for each phase timeout, and
:func:`classify` returns ``None`` for an unknown error so that the retry
policy never retries it by default (SKY-406 D10). Skynet's billing errors
map to the no-retry group.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from .environment import HealthcheckError


class Grading(StrEnum):
    """Say how a failed attempt counts when Skynet grades the trial."""

    EXCLUDED = "excluded"
    FAILED = "failed"
    REWARD = "reward"


class ErrorGroup(StrEnum):
    """Name the five groups of the error policy table."""

    INFRA = "infra"
    TRANSIENT = "transient"
    REJECTED = "rejected"
    FATAL = "fatal"
    RUN_OUTCOME = "run_outcome"


@dataclass(frozen=True)
class GroupRule:
    """Hold whether an error group can retry and how its attempt counts."""

    retryable: bool
    grading: Grading


GROUP_RULES: dict[ErrorGroup, GroupRule] = {
    ErrorGroup.INFRA: GroupRule(retryable=True, grading=Grading.EXCLUDED),
    ErrorGroup.TRANSIENT: GroupRule(retryable=True, grading=Grading.FAILED),
    ErrorGroup.REJECTED: GroupRule(retryable=False, grading=Grading.FAILED),
    ErrorGroup.FATAL: GroupRule(retryable=False, grading=Grading.EXCLUDED),
    ErrorGroup.RUN_OUTCOME: GroupRule(retryable=False, grading=Grading.REWARD),
}

# Matched by module and name, not by import: ``core.billing`` imports Stripe,
# SQLAlchemy and the storage models, and the sandbox package must stay a leaf.
_BILLING_MODULE_PREFIX = "core.billing"
_BILLING_ERROR_NAMES = frozenset({"BudgetError", "UsagePendingError", "UnpricedOperationError"})


class SandboxError(Exception):
    """Base class for every sandbox error that carries an error group."""

    group: ClassVar[ErrorGroup]


class SandboxInfraError(SandboxError):
    """Report an infra failure: start timeout, build, healthcheck or OOM."""

    group = ErrorGroup.INFRA


class SandboxTransientError(SandboxError):
    """Report a transient provider failure: rate limit, 5xx or connection."""

    group = ErrorGroup.TRANSIENT


class SandboxRejectedError(SandboxError):
    """Report a rejected request: bad request, budget exceeded or verifier timeout."""

    group = ErrorGroup.REJECTED


class SandboxFatalError(SandboxError):
    """Report a fatal failure: auth, not found, scope, missing extra or cancel."""

    group = ErrorGroup.FATAL


class SandboxRunOutcomeError(SandboxError):
    """Report a run outcome: agent timeout, non-zero exit or context window."""

    group = ErrorGroup.RUN_OUTCOME


class PhaseTimeoutError(SandboxError):
    """Base class for a phase that ran past its timeout."""

    phase: ClassVar[str]

    def __init__(self, seconds: float) -> None:
        """Store the timeout that the phase ran past.

        Args:
            seconds: The effective timeout of the phase, in seconds.
        """
        super().__init__(f"{self.phase} phase timed out after {seconds:g} s")
        self.seconds = seconds


class StartTimeoutError(PhaseTimeoutError, SandboxInfraError):
    """Report that the sandbox did not start in time."""

    phase = "start"


# Harbor has no setup row in its table. Setup installs the workload before
# the run starts, so a timeout there is an infra failure like a build.
class SetupTimeoutError(PhaseTimeoutError, SandboxInfraError):
    """Report that the workload setup did not finish in time."""

    phase = "setup"


class RunTimeoutError(PhaseTimeoutError, SandboxRunOutcomeError):
    """Report that the agent run did not finish in time."""

    phase = "run"


class ScoreTimeoutError(PhaseTimeoutError, SandboxRejectedError):
    """Report that the scorer did not finish in time."""

    phase = "score"


def is_billing_error(exc: BaseException) -> bool:
    """Say whether an error is one of Skynet's budget errors.

    Args:
        exc: The error to check.

    Returns:
        True when a class in the error's MRO is ``BudgetError``,
        ``UsagePendingError`` or ``UnpricedOperationError`` from ``core.billing``.
    """
    return any(
        cls.__name__ in _BILLING_ERROR_NAMES and cls.__module__.startswith(_BILLING_MODULE_PREFIX)
        for cls in type(exc).__mro__
    )


def classify(exc: BaseException) -> ErrorGroup | None:
    """Find the error group of an error.

    Args:
        exc: The error to classify.

    Returns:
        The error group, or ``None`` when the error is unknown.
    """
    if isinstance(exc, SandboxError):
        return exc.group
    if is_billing_error(exc):
        return ErrorGroup.REJECTED
    if isinstance(exc, asyncio.CancelledError):
        return ErrorGroup.FATAL
    if isinstance(exc, HealthcheckError):
        return ErrorGroup.INFRA
    return None

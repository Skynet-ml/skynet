"""Tests that the async sandbox bridge maps ``core.sandbox`` errors to ``ServiceError`` (SKY-564)."""

import asyncio

import pytest

from core.billing.budgets import BudgetError
from core.billing.runtime import UsagePendingError
from core.exceptions import ServiceError
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import HealthcheckError
from core.sandbox.errors import (
    RunTimeoutError,
    SandboxError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxRunOutcomeError,
    SandboxTransientError,
)
from core.sandbox.factory import ProviderUnavailableError
from core.sandbox.network import NetworkPolicyError
from core.sandbox.pacing import ProviderAtCapacityError
from core.sandbox.pool import TrialFingerprintError
from core.sandbox.transfer import UnsafeArchiveError

from ..sandbox_runtime import _bridge_errors


def _raise_through_bridge(error: BaseException, *, timed: bool = False) -> None:
    """Raise ``error`` inside the bridge error mapping.

    Args:
        error: The error that the fake provider call raises.
        timed: The fake call is a command with a timeout.
    """
    with _bridge_errors(timed=timed):
        raise error


def _kill_deadline_error() -> SandboxInfraError:
    """Build the infra error that a provider raises when a command outlives its kill deadline.

    Returns:
        A ``SandboxInfraError`` whose cause is a ``TimeoutError``.
    """
    try:
        try:
            raise TimeoutError
        except TimeoutError as timeout:
            raise SandboxInfraError("The sandbox command outlived its kill deadline.") from timeout
    except SandboxInfraError as error:
        return error


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (SandboxSpecError("bad spec"), "The sandbox cannot open this request: bad spec"),
        (SandboxRejectedError("rejected"), "The sandbox refused the request: rejected"),
        (SandboxFatalError("fatal"), "The sandbox refused the request: fatal"),
        (ProviderUnavailableError("not in this build"), "The sandbox refused the request: not in this build"),
        (SandboxError("no group"), "The sandbox refused the request: no group"),
        (NetworkPolicyError("not confirmed"), "The sandbox refused the request: not confirmed"),
        (UnsafeArchiveError("absolute path"), "The sandbox refused the request: absolute path"),
        (TrialFingerprintError("config differs"), "The sandbox refused the request: config differs"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else None,
)
def test_bridge_maps_each_error_class_to_service_error(error: BaseException, message: str) -> None:
    """Each error that a retry cannot fix reaches the caller as the API's ``SERVICE_ERROR``."""
    with pytest.raises(ServiceError) as raised:
        _raise_through_bridge(error)
    assert raised.value.error_code == "SERVICE_ERROR"
    assert raised.value.status_code == 500
    assert raised.value.message == message


def test_bridge_maps_the_kill_deadline_of_a_timed_command() -> None:
    """A timed command's infra error caused by its deadline is a ``ServiceError``."""
    with pytest.raises(ServiceError, match="outlived its kill deadline"):
        _raise_through_bridge(_kill_deadline_error(), timed=True)


@pytest.mark.parametrize(
    "error",
    [
        SandboxSpecError("bad spec"),
        SandboxFatalError("fatal"),
        NetworkPolicyError("not confirmed"),
        UnsafeArchiveError("absolute path"),
        _kill_deadline_error(),
    ],
    ids=lambda value: type(value).__name__,
)
def test_bridge_keeps_the_original_error_as_the_cause(error: BaseException) -> None:
    """The mapped ``ServiceError`` is raised ``from`` the provider error."""
    with pytest.raises(ServiceError) as raised:
        _raise_through_bridge(error, timed=True)
    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        asyncio.CancelledError(),
        BudgetError("no budget"),
        UsagePendingError("bill unconfirmed"),
        KeyboardInterrupt(),
    ],
    ids=lambda value: type(value).__name__,
)
def test_bridge_passes_cancellation_and_budget_errors_through(error: BaseException) -> None:
    """Cancellation, interrupts and budget errors keep their own type."""
    with pytest.raises(type(error)) as raised:
        _raise_through_bridge(error)
    assert raised.value is error


@pytest.mark.parametrize(
    "error",
    [
        SandboxInfraError("box died"),
        SandboxTransientError("try again"),
        ProviderAtCapacityError("no capacity"),
        HealthcheckError("not ready"),
        SandboxRunOutcomeError("agent failed"),
        RunTimeoutError(30.0),
    ],
    ids=lambda value: type(value).__name__,
)
def test_bridge_keeps_retryable_and_run_outcome_errors(error: BaseException) -> None:
    """Box-death and run-outcome errors keep their group for the callers' retry and grading."""
    with pytest.raises(type(error)) as raised:
        _raise_through_bridge(error, timed=True)
    assert raised.value is error


def test_bridge_keeps_an_untimed_infra_error_with_a_timeout_cause() -> None:
    """Only a timed command reads a ``TimeoutError`` cause as its kill deadline."""
    error = _kill_deadline_error()
    with pytest.raises(SandboxInfraError) as raised:
        _raise_through_bridge(error)
    assert raised.value is error

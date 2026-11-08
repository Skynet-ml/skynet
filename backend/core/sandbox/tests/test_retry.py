"""Tests for the sandbox error groups and the retry policy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.billing.budgets import BudgetError
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import UsagePendingError
from core.sandbox.environment import HealthcheckError
from core.sandbox.errors import (
    GROUP_RULES,
    ErrorGroup,
    Grading,
    RunTimeoutError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxRunOutcomeError,
    SandboxTransientError,
    ScoreTimeoutError,
    SetupTimeoutError,
    StartTimeoutError,
    classify,
    is_billing_error,
)
from core.sandbox.retry import DEFAULT_RETRY_POLICY, RetryPolicy, attempt_key, run_with_retry

# Zero waits keep the retry tests fast; the wait math has its own test.
FAST = {"wait_min": 0, "wait_max": 0}


def _all_errors() -> list[BaseException]:
    """Return one error for each group, each phase timeout, the mapped foreign errors and an unknown one."""
    return [
        SandboxInfraError("oom"),
        SandboxTransientError("503"),
        SandboxRejectedError("bad request"),
        SandboxFatalError("auth"),
        SandboxRunOutcomeError("exit 1"),
        StartTimeoutError(1),
        SetupTimeoutError(1),
        RunTimeoutError(1),
        ScoreTimeoutError(1),
        HealthcheckError("not ready"),
        BudgetError("over budget"),
        UsagePendingError("pending"),
        UnpricedOperationError("unpriced"),
        asyncio.CancelledError(),
        RuntimeError("unknown"),
    ]


class _Recorder:
    """Record the attempt keys and raise a set error until a set attempt succeeds."""

    def __init__(self, error: BaseException, succeed_on: int | None = None) -> None:
        """Store the error and the attempt that succeeds.

        Args:
            error: The error that each failing attempt raises.
            succeed_on: The attempt number that returns, or None to always fail.
        """
        self.error = error
        self.succeed_on = succeed_on
        self.keys: list[str] = []

    async def __call__(self, key: str) -> str:
        """Record the key, then fail or return it.

        Args:
            key: The attempt's operation key.

        Returns:
            The key, on the attempt that succeeds.
        """
        self.keys.append(key)
        if self.succeed_on is not None and len(self.keys) == self.succeed_on:
            return key
        raise self.error


def test_group_rules_match_the_policy_table() -> None:
    """Check each group's retry flag and grading against the Harbor table."""
    assert GROUP_RULES[ErrorGroup.INFRA].retryable is True
    assert GROUP_RULES[ErrorGroup.INFRA].grading is Grading.EXCLUDED
    assert GROUP_RULES[ErrorGroup.TRANSIENT].retryable is True
    assert GROUP_RULES[ErrorGroup.TRANSIENT].grading is Grading.FAILED
    assert GROUP_RULES[ErrorGroup.REJECTED].retryable is False
    assert GROUP_RULES[ErrorGroup.REJECTED].grading is Grading.FAILED
    assert GROUP_RULES[ErrorGroup.FATAL].retryable is False
    assert GROUP_RULES[ErrorGroup.FATAL].grading is Grading.EXCLUDED
    assert GROUP_RULES[ErrorGroup.RUN_OUTCOME].retryable is False
    assert GROUP_RULES[ErrorGroup.RUN_OUTCOME].grading is Grading.REWARD


@pytest.mark.parametrize(
    ("error", "group"),
    [
        (SandboxInfraError("x"), ErrorGroup.INFRA),
        (SandboxTransientError("x"), ErrorGroup.TRANSIENT),
        (SandboxRejectedError("x"), ErrorGroup.REJECTED),
        (SandboxFatalError("x"), ErrorGroup.FATAL),
        (SandboxRunOutcomeError("x"), ErrorGroup.RUN_OUTCOME),
        (StartTimeoutError(1), ErrorGroup.INFRA),
        (SetupTimeoutError(1), ErrorGroup.INFRA),
        (RunTimeoutError(1), ErrorGroup.RUN_OUTCOME),
        (ScoreTimeoutError(1), ErrorGroup.REJECTED),
        (HealthcheckError("x"), ErrorGroup.INFRA),
        (BudgetError("x"), ErrorGroup.REJECTED),
        (UsagePendingError("x"), ErrorGroup.REJECTED),
        (UnpricedOperationError("x"), ErrorGroup.REJECTED),
        (asyncio.CancelledError(), ErrorGroup.FATAL),
        (RuntimeError("x"), None),
    ],
)
def test_classify(error: BaseException, group: ErrorGroup | None) -> None:
    """Map each error to its group, and an unknown error to None."""
    assert classify(error) == group


def test_billing_match_needs_the_billing_module() -> None:
    """Treat a same-name class outside ``core.billing`` as unknown."""
    impostor = type("BudgetError", (ValueError,), {"__module__": "somewhere.else"})
    assert is_billing_error(impostor("x")) is False
    assert classify(impostor("x")) is None


@pytest.mark.parametrize("error", _all_errors(), ids=lambda e: type(e).__name__)
async def test_default_policy_never_retries(error: BaseException) -> None:
    """Run every error group and an unknown error exactly once by default."""
    recorder = _Recorder(error)
    assert DEFAULT_RETRY_POLICY.should_retry(error) is False
    with pytest.raises(type(error)):
        await run_with_retry(recorder, "op")
    assert recorder.keys == ["op:attempt-1"]


async def test_retryable_error_runs_three_attempts_with_own_keys() -> None:
    """Run a retryable error three times with max_retries=2, one key each."""
    error = SandboxTransientError("rate limit")
    recorder = _Recorder(error)
    with pytest.raises(SandboxTransientError) as raised:
        await run_with_retry(recorder, "run-1:trial-7", RetryPolicy(max_retries=2, **FAST))
    assert raised.value is error
    assert recorder.keys == ["run-1:trial-7:attempt-1", "run-1:trial-7:attempt-2", "run-1:trial-7:attempt-3"]


async def test_retry_returns_the_first_success() -> None:
    """Return the result of the attempt that succeeds and stop there."""
    recorder = _Recorder(StartTimeoutError(5), succeed_on=2)
    result = await run_with_retry(recorder, "op", RetryPolicy(max_retries=2, **FAST))
    assert result == "op:attempt-2"
    assert recorder.keys == ["op:attempt-1", "op:attempt-2"]


@pytest.mark.parametrize(
    "error",
    [
        SandboxRejectedError("bad request"),
        SandboxFatalError("auth"),
        SandboxRunOutcomeError("exit 1"),
        RunTimeoutError(1),
        ScoreTimeoutError(1),
        BudgetError("over"),
        UsagePendingError("pending"),
        UnpricedOperationError("unpriced"),
        asyncio.CancelledError(),
        RuntimeError("unknown"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_non_retryable_error_runs_once(error: BaseException) -> None:
    """Run a non-retryable or unknown error once, even with max_retries=2."""
    recorder = _Recorder(error)
    with pytest.raises(type(error)):
        await run_with_retry(recorder, "op", RetryPolicy(max_retries=2, **FAST))
    assert recorder.keys == ["op:attempt-1"]


async def test_include_retries_only_the_named_classes() -> None:
    """Retry an unknown error that include names, and skip a retryable one it does not name."""
    policy = RetryPolicy(max_retries=1, include=(KeyError,), **FAST)
    unknown = _Recorder(KeyError("x"))
    with pytest.raises(KeyError):
        await run_with_retry(unknown, "op", policy)
    assert len(unknown.keys) == 2

    transient = _Recorder(SandboxTransientError("x"))
    with pytest.raises(SandboxTransientError):
        await run_with_retry(transient, "op", policy)
    assert len(transient.keys) == 1


def test_include_cannot_retry_billing_errors_or_cancel() -> None:
    """Keep budget errors and cancellation on no retry, whatever include says."""
    policy = RetryPolicy(max_retries=3, include=(BaseException,))
    assert policy.should_retry(BudgetError("x")) is False
    assert policy.should_retry(UsagePendingError("x")) is False
    assert policy.should_retry(UnpricedOperationError("x")) is False
    assert policy.should_retry(asyncio.CancelledError()) is False
    assert policy.should_retry(RuntimeError("x")) is True


def test_exclude_wins_over_the_group_rule() -> None:
    """Skip a retryable group when exclude names it."""
    policy = RetryPolicy(max_retries=3, exclude=(StartTimeoutError,))
    assert policy.should_retry(StartTimeoutError(1)) is False
    assert policy.should_retry(SetupTimeoutError(1)) is True


@pytest.mark.parametrize("attempt", [1, 2, 3, 6, 10])
def test_wait_is_jittered_exponential_between_min_and_max(attempt: int) -> None:
    """Keep each wait inside [wait_min, min(wait_max, multiplier * 2**attempt)]."""
    policy = RetryPolicy(max_retries=5, wait_min=1, wait_max=60, multiplier=1)
    wait = policy.wait_strategy()
    high = min(60, 2**attempt)
    samples = [wait(SimpleNamespace(attempt_number=attempt)) for _ in range(50)]
    assert all(1 <= sample <= high for sample in samples)


def test_defaults_match_harbor_retry_config() -> None:
    """Default to no retries, 1 s min, 60 s max and multiplier 1."""
    assert RetryPolicy(max_retries=0, wait_min=1, wait_max=60, multiplier=1) == DEFAULT_RETRY_POLICY
    assert DEFAULT_RETRY_POLICY.max_attempts == 1


def test_attempt_key() -> None:
    """Append ``:attempt-N`` to the operation key."""
    assert attempt_key("op", 3) == "op:attempt-3"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_retries": -1},
        {"max_retries": True},
        {"max_retries": 1.0},
        {"wait_min": -1},
        {"wait_min": 5, "wait_max": 1},
        {"wait_max": float("inf")},
        {"multiplier": True},
        {"include": (int,)},
        {"exclude": ("ValueError",)},
    ],
)
def test_invalid_policy_raises(kwargs: dict[str, object]) -> None:
    """Reject out-of-range or wrongly typed fields."""
    with pytest.raises((TypeError, ValueError)):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]

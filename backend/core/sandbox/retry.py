"""Retry policy for sandbox operations, keyed by error class.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the fields come
from ``RetryConfig`` in ``harbor/models/job/config.py`` and the loop from
``harbor/orchestrators/queue.py``. Changes: the default never retries, for
known and unknown errors (SKY-406 D3, D10); an unknown error retries only
when ``include`` names it; Skynet's budget errors and cancellation never
retry; and each attempt gets its own operation key ``<key>:attempt-N`` so
that each attempt has its own budget reservation.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base

from .errors import GROUP_RULES, classify, is_billing_error

T = TypeVar("T")

ATTEMPT_SUFFIX = ":attempt-"


def attempt_key(operation_key: str, attempt: int) -> str:
    """Build the operation key of one attempt.

    Args:
        operation_key: The key of the operation, shared by all attempts.
        attempt: The attempt number, from 1.

    Returns:
        The key ``<operation_key>:attempt-<attempt>``.
    """
    return f"{operation_key}{ATTEMPT_SUFFIX}{attempt}"


def _check_number(name: str, value: object) -> float:
    """Return a finite, non-negative number or raise.

    Args:
        name: The field name for the error message.
        value: The value to check.

    Returns:
        The value as a float.

    Raises:
        TypeError: The value is a bool or not a number.
        ValueError: The value is negative or not finite.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return float(value)


@dataclass(frozen=True)
class RetryPolicy:
    """Say which errors retry, how often, and how long to wait between attempts.

    ``max_retries`` counts the retries after the first attempt. With
    ``include`` empty, only the retryable error groups retry. With
    ``include`` set, only those error classes retry. ``exclude`` always wins.
    """

    max_retries: int = 0
    include: tuple[type[BaseException], ...] = ()
    exclude: tuple[type[BaseException], ...] = ()
    wait_min: float = 1.0
    wait_max: float = 60.0
    multiplier: float = 1.0

    def __post_init__(self) -> None:
        """Validate the fields.

        Raises:
            TypeError: ``max_retries`` is not an int, or a wait field is not a number.
            ValueError: A field is out of range, ``wait_min`` > ``wait_max``, or
                ``include`` or ``exclude`` holds a non-exception.
        """
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise TypeError("max_retries must be an int")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        wait_min = _check_number("wait_min", self.wait_min)
        wait_max = _check_number("wait_max", self.wait_max)
        _check_number("multiplier", self.multiplier)
        if wait_min > wait_max:
            raise ValueError("wait_min must be <= wait_max")
        for name in ("include", "exclude"):
            classes = getattr(self, name)
            if not all(isinstance(cls, type) and issubclass(cls, BaseException) for cls in classes):
                raise ValueError(f"{name} must hold exception classes")

    @property
    def max_attempts(self) -> int:
        """Return the total number of attempts, the first one included."""
        return self.max_retries + 1

    def should_retry(self, exc: BaseException) -> bool:
        """Say whether an attempt that raised this error runs again.

        Args:
            exc: The error that the attempt raised.

        Returns:
            True when the policy retries the error.
        """
        if self.max_retries == 0:
            return False
        if is_billing_error(exc) or isinstance(exc, asyncio.CancelledError):
            return False
        if self.exclude and isinstance(exc, self.exclude):
            return False
        if self.include:
            return isinstance(exc, self.include)
        group = classify(exc)
        return group is not None and GROUP_RULES[group].retryable

    def wait_strategy(self) -> wait_base:
        """Build the jittered exponential wait between attempts.

        Returns:
            A tenacity wait that picks a random time in a window that doubles
            each attempt, between ``wait_min`` and ``wait_max`` seconds.
        """
        return wait_random_exponential(multiplier=self.multiplier, min=self.wait_min, max=self.wait_max)


DEFAULT_RETRY_POLICY = RetryPolicy()


async def run_with_retry(
    operation: Callable[[str], Awaitable[T]],
    operation_key: str,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> T:
    """Run an operation, and run it again when the policy retries its error.

    Args:
        operation: Takes the attempt's operation key and does the work.
        operation_key: The key that each attempt key starts with.
        policy: The retry policy. The default never retries.

    Returns:
        The result of the first attempt that succeeds.

    Raises:
        BaseException: The error of the last attempt, unchanged.
    """
    retrying = AsyncRetrying(
        stop=stop_after_attempt(policy.max_attempts),
        retry=retry_if_exception(policy.should_retry),
        wait=policy.wait_strategy(),
        reraise=True,
    )
    async for attempt in retrying:
        with attempt:
            return await operation(attempt_key(operation_key, attempt.retry_state.attempt_number))
    # With reraise=True tenacity raises the last error when it stops, so the
    # loop never ends without a result.
    raise AssertionError("tenacity stopped without a result or an error")

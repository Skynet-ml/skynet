"""Tests for the sandbox phase timeouts."""

from __future__ import annotations

import asyncio

import pytest

from core.sandbox.errors import (
    ErrorGroup,
    PhaseTimeoutError,
    RunTimeoutError,
    ScoreTimeoutError,
    SetupTimeoutError,
    StartTimeoutError,
    classify,
)
from core.sandbox.timeouts import Phase, PhaseTimeouts


def test_multiplier_and_cap_apply() -> None:
    """Multiply each base timeout, then cap it."""
    timeouts = PhaseTimeouts(start=10, setup=30, run=80, score=20, multiplier=2, cap=100)
    assert timeouts.seconds(Phase.START) == 20
    assert timeouts.seconds(Phase.SETUP) == 60
    assert timeouts.seconds(Phase.RUN) == 100
    assert timeouts.seconds(Phase.SCORE) == 40


def test_no_cap_and_fractional_multiplier() -> None:
    """Keep the product when there is no cap, also for a multiplier below 1."""
    timeouts = PhaseTimeouts(start=10, setup=30, run=900, score=20, multiplier=0.5)
    assert timeouts.seconds(Phase.RUN) == 450
    assert timeouts.seconds("start") == 5  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("phase", "error_cls", "group"),
    [
        (Phase.START, StartTimeoutError, ErrorGroup.INFRA),
        (Phase.SETUP, SetupTimeoutError, ErrorGroup.INFRA),
        (Phase.RUN, RunTimeoutError, ErrorGroup.RUN_OUTCOME),
        (Phase.SCORE, ScoreTimeoutError, ErrorGroup.REJECTED),
    ],
)
async def test_each_phase_raises_its_own_class(
    phase: Phase, error_cls: type[PhaseTimeoutError], group: ErrorGroup
) -> None:
    """Raise the phase's own class, with the effective seconds and group."""
    timeouts = PhaseTimeouts(start=1, setup=1, run=1, score=1, multiplier=0.01)
    with pytest.raises(PhaseTimeoutError) as raised:
        async with timeouts.limit(phase):
            await asyncio.sleep(5)
    assert type(raised.value) is error_cls
    assert raised.value.seconds == pytest.approx(0.01)
    assert raised.value.phase == phase.value
    assert classify(raised.value) == group
    assert isinstance(raised.value.__cause__, TimeoutError)


async def test_cap_shortens_the_limit() -> None:
    """Use the capped value as the real limit."""
    timeouts = PhaseTimeouts(start=60, setup=60, run=60, score=60, cap=0.01)
    with pytest.raises(RunTimeoutError):
        async with timeouts.limit(Phase.RUN):
            await asyncio.sleep(5)


async def test_body_inside_the_limit_returns() -> None:
    """Let a body that finishes in time return normally."""
    timeouts = PhaseTimeouts(start=5, setup=5, run=5, score=5)
    async with timeouts.limit(Phase.SETUP):
        await asyncio.sleep(0)


async def test_body_timeout_error_passes_through() -> None:
    """Keep a TimeoutError that the body raises itself unchanged."""
    timeouts = PhaseTimeouts(start=5, setup=5, run=5, score=5)
    with pytest.raises(TimeoutError) as raised:
        async with timeouts.limit(Phase.RUN):
            raise TimeoutError("inner")
    assert not isinstance(raised.value, PhaseTimeoutError)
    assert str(raised.value) == "inner"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": 0},
        {"setup": -1},
        {"run": True},
        {"score": float("nan")},
        {"multiplier": 0},
        {"cap": 0},
        {"cap": float("inf")},
        {"start": "10"},
    ],
)
def test_invalid_timeouts_raise(kwargs: dict[str, object]) -> None:
    """Reject non-positive, non-finite or wrongly typed fields."""
    base: dict[str, object] = {"start": 1, "setup": 1, "run": 1, "score": 1}
    with pytest.raises((TypeError, ValueError)):
        PhaseTimeouts(**{**base, **kwargs})  # type: ignore[arg-type]

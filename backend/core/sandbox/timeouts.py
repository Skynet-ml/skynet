"""Phase timeouts for one sandbox trial: start, setup, run and score.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the effective
timeout follows ``Trial._init_timeouts`` in ``harbor/trial/trial.py``, base
seconds times a multiplier, capped. Changes: one frozen dataclass holds the
four phases, and each phase raises its own error class from
:mod:`core.sandbox.errors`.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

from .errors import (
    PhaseTimeoutError,
    RunTimeoutError,
    ScoreTimeoutError,
    SetupTimeoutError,
    StartTimeoutError,
)


class Phase(StrEnum):
    """Name the timed phases of a sandbox trial."""

    START = "start"
    SETUP = "setup"
    RUN = "run"
    SCORE = "score"


PHASE_ERRORS: dict[Phase, type[PhaseTimeoutError]] = {
    Phase.START: StartTimeoutError,
    Phase.SETUP: SetupTimeoutError,
    Phase.RUN: RunTimeoutError,
    Phase.SCORE: ScoreTimeoutError,
}


def _check_positive(name: str, value: object) -> None:
    """Raise unless the value is a finite number above zero.

    Args:
        name: The field name for the error message.
        value: The value to check.

    Raises:
        TypeError: The value is a bool or not a number.
        ValueError: The value is not finite or <= 0.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number > 0")


@dataclass(frozen=True)
class PhaseTimeouts:
    """Hold the base timeout of each phase, in seconds, plus a multiplier and a cap.

    The effective timeout of a phase is ``min(base * multiplier, cap)``.
    ``cap=None`` means no cap.
    """

    start: float
    setup: float
    run: float
    score: float
    multiplier: float = 1.0
    cap: float | None = None

    def __post_init__(self) -> None:
        """Validate the fields.

        Raises:
            TypeError: A field is a bool or not a number.
            ValueError: A field is not finite or not above zero.
        """
        for phase in Phase:
            _check_positive(phase.value, getattr(self, phase.value))
        _check_positive("multiplier", self.multiplier)
        if self.cap is not None:
            _check_positive("cap", self.cap)

    def seconds(self, phase: Phase) -> float:
        """Return the effective timeout of a phase.

        Args:
            phase: The phase.

        Returns:
            The base timeout times the multiplier, capped, in seconds.
        """
        value = float(getattr(self, Phase(phase).value)) * self.multiplier
        return value if self.cap is None else min(value, float(self.cap))

    @asynccontextmanager
    async def limit(self, phase: Phase) -> AsyncGenerator[None, None]:
        """Run the body under the phase timeout.

        Args:
            phase: The phase that the body runs.

        Yields:
            Nothing. The body runs inside the timeout.

        Raises:
            PhaseTimeoutError: The phase's own subclass, when the body runs
                past the timeout. A ``TimeoutError`` that the body raises
                itself passes through unchanged.
        """
        phase = Phase(phase)
        seconds = self.seconds(phase)
        scope = asyncio.timeout(seconds)
        try:
            async with scope:
                yield
        except TimeoutError as exc:
            if scope.expired():
                raise PHASE_ERRORS[phase](seconds) from exc
            raise

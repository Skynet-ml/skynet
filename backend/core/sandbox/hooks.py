"""Box lifecycle events, async hook callbacks and live counts by phase.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: :class:`BoxEvent`
follows ``TrialEvent`` in ``harbor/trial/hooks.py``. Changes: the agent and
verification events become start and end events for the setup, run and
score phases; each box ends with exactly one ``ENDED`` or ``CANCELLED``; a
failing hook logs and never fails the box; and :class:`LiveCounts` derives
the number of boxes in each state from the events alone.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from .timeouts import Phase

logger = logging.getLogger(__name__)


class BoxEvent(StrEnum):
    """Name the events in the life of one box attempt, in the order they fire."""

    STARTED = "started"
    ENVIRONMENT_STARTED = "environment_started"
    SETUP_STARTED = "setup_started"
    SETUP_ENDED = "setup_ended"
    RUN_STARTED = "run_started"
    RUN_ENDED = "run_ended"
    SCORE_STARTED = "score_started"
    SCORE_ENDED = "score_ended"
    ENDED = "ended"
    CANCELLED = "cancelled"


PHASE_STARTED: dict[Phase, BoxEvent] = {
    Phase.SETUP: BoxEvent.SETUP_STARTED,
    Phase.RUN: BoxEvent.RUN_STARTED,
    Phase.SCORE: BoxEvent.SCORE_STARTED,
}

PHASE_ENDED: dict[Phase, BoxEvent] = {
    Phase.SETUP: BoxEvent.SETUP_ENDED,
    Phase.RUN: BoxEvent.RUN_ENDED,
    Phase.SCORE: BoxEvent.SCORE_ENDED,
}

TERMINAL_EVENTS = frozenset({BoxEvent.ENDED, BoxEvent.CANCELLED})


@dataclass(frozen=True)
class BoxEventInfo:
    """Describe one event of one box attempt.

    ``box_id`` is the attempt's operation key. ``phase`` is set on the phase
    events. ``error`` is set on ``ENDED`` when the attempt failed.
    """

    event: BoxEvent
    box_id: str
    phase: Phase | None = None
    error: BaseException | None = None


BoxHook = Callable[[BoxEventInfo], Awaitable[None]]


class BoxHooks:
    """Hold async hooks and call them in the order they were added."""

    def __init__(self) -> None:
        """Create an empty hook list."""
        self._hooks: list[tuple[BoxHook, frozenset[BoxEvent]]] = []

    def add(self, hook: BoxHook, *events: BoxEvent) -> None:
        """Add a hook for some events, or for every event.

        Args:
            hook: The async callback that receives each matching event.
            *events: The events that the hook receives. None means every event.
        """
        self._hooks.append((hook, frozenset(events)))

    async def emit(self, info: BoxEventInfo) -> None:
        """Call each matching hook in turn; log a hook that fails and go on.

        Args:
            info: The event to send.
        """
        for hook, events in self._hooks:
            if events and info.event not in events:
                continue
            try:
                await hook(info)
            except Exception:
                logger.exception("Sandbox hook %r failed on %s for box %s", hook, info.event, info.box_id)


class BoxState(StrEnum):
    """Name the live state of a box between its events."""

    STARTING = "starting"
    IDLE = "idle"
    SETUP = "setup"
    RUN = "run"
    SCORE = "score"


_STATE_AFTER: dict[BoxEvent, BoxState] = {
    BoxEvent.STARTED: BoxState.STARTING,
    BoxEvent.ENVIRONMENT_STARTED: BoxState.IDLE,
    BoxEvent.SETUP_STARTED: BoxState.SETUP,
    BoxEvent.SETUP_ENDED: BoxState.IDLE,
    BoxEvent.RUN_STARTED: BoxState.RUN,
    BoxEvent.RUN_ENDED: BoxState.IDLE,
    BoxEvent.SCORE_STARTED: BoxState.SCORE,
    BoxEvent.SCORE_ENDED: BoxState.IDLE,
}


class LiveCounts:
    """Count the live boxes in each state from the box events.

    Add an instance to :class:`BoxHooks` for every event. ``IDLE`` holds the
    boxes that run but wait between phases, for example for a phase permit.
    """

    def __init__(self) -> None:
        """Start with no live boxes."""
        self._states: dict[str, BoxState] = {}

    async def __call__(self, info: BoxEventInfo) -> None:
        """Move a box to the state after the event, or forget it when it ends.

        Args:
            info: The box event.
        """
        if info.event in TERMINAL_EVENTS:
            self._states.pop(info.box_id, None)
        else:
            self._states[info.box_id] = _STATE_AFTER[info.event]

    def snapshot(self) -> dict[BoxState, int]:
        """Return the number of live boxes in each state.

        Returns:
            A count for every state, zero included.
        """
        counts = Counter(self._states.values())
        return {state: counts[state] for state in BoxState}

    @property
    def active(self) -> int:
        """Return the number of live boxes in all states."""
        return len(self._states)

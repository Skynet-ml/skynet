"""Box lifecycle events, async hook callbacks and live counts by phase.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: :class:`BoxEvent`
follows ``TrialEvent`` in ``harbor/trial/hooks.py``. Changes: the agent and
verification events become start and end events for the setup, run and
score phases; each box ends with exactly one ``ENDED`` or ``CANCELLED``; a
failing hook logs and never fails the box; :class:`LiveCounts` derives
the number of boxes in each state from the events alone; and
:class:`BoxTimings` adds up the seconds each box spends in each phase.
"""

from __future__ import annotations

import asyncio
import logging
import time
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

        A cancel of the emitting task does not skip the later hooks: every
        matching hook gets the event, then the cancel goes on. Otherwise a
        hook could see ``STARTED`` and never see ``ENDED`` or ``CANCELLED``.

        Args:
            info: The event to send.

        Raises:
            asyncio.CancelledError: The emitting task was cancelled during a hook.
        """
        cancel: asyncio.CancelledError | None = None
        for hook, events in self._hooks:
            if events and info.event not in events:
                continue
            try:
                await hook(info)
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    cancel = exc
                else:
                    logger.exception(
                        "Sandbox hook %r raised a stray cancel on %s for box %s", hook, info.event, info.box_id
                    )
            except Exception:
                logger.exception("Sandbox hook %r failed on %s for box %s", hook, info.event, info.box_id)
        if cancel is not None:
            raise cancel


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


class TimedPhase(StrEnum):
    """Name the parts of a box attempt that :class:`BoxTimings` measures.

    ``START`` runs from ``STARTED`` to ``ENVIRONMENT_STARTED``: the wait for
    a provider permit, the create, the image wait and the start, which the
    events cannot tell apart. ``WAIT`` is the time between phases, for
    example for a phase permit. ``STOP`` runs from the end of the last phase
    to ``ENDED`` or ``CANCELLED``. A box that fails before
    ``ENVIRONMENT_STARTED`` counts its stop in ``START``.
    """

    START = "start"
    WAIT = "wait"
    SETUP = "setup"
    RUN = "run"
    SCORE = "score"
    STOP = "stop"


_TIMED_PHASE: dict[BoxState, TimedPhase] = {
    BoxState.STARTING: TimedPhase.START,
    BoxState.IDLE: TimedPhase.WAIT,
    BoxState.SETUP: TimedPhase.SETUP,
    BoxState.RUN: TimedPhase.RUN,
    BoxState.SCORE: TimedPhase.SCORE,
}


class BoxTimings:
    """Add up the seconds that the boxes spend in each phase, from the box events.

    Add an instance to :class:`BoxHooks` for every event. A box's own times
    are kept only while it lives; when it ends they go into the totals, so
    the memory is O(live boxes).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Start with no boxes and zero totals.

        Args:
            clock: Gives the time in seconds. Tests pass a fake clock.
        """
        self._clock = clock
        self._open: dict[str, tuple[BoxState, float, dict[TimedPhase, float]]] = {}
        self._totals: dict[TimedPhase, float] = dict.fromkeys(TimedPhase, 0.0)
        self.boxes = 0

    async def __call__(self, info: BoxEventInfo) -> None:
        """Close the box's open part, then open the next one or end the box.

        Args:
            info: The box event.
        """
        now = self._clock()
        current = self._open.get(info.box_id)
        if current is None:
            if info.event not in TERMINAL_EVENTS:
                self._open[info.box_id] = (_STATE_AFTER[info.event], now, dict.fromkeys(TimedPhase, 0.0))
            return
        state, since, phases = current
        part = _TIMED_PHASE[state]
        if info.event in TERMINAL_EVENTS:
            del self._open[info.box_id]
            if part is TimedPhase.WAIT:
                part = TimedPhase.STOP
            phases[part] += now - since
            for name, seconds in phases.items():
                self._totals[name] += seconds
            self.boxes += 1
            return
        phases[part] += now - since
        self._open[info.box_id] = (_STATE_AFTER[info.event], now, phases)

    def totals(self) -> dict[TimedPhase, float]:
        """Return the seconds of the ended boxes in each phase.

        Returns:
            A total for every phase, zero included.
        """
        return dict(self._totals)

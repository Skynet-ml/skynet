"""Phase timings for each sandbox, and the output sink of the current task.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the context
variable output sink comes from ``harbor/environments/base.py``. Changes:
the sink is a plain ``(stream, text)`` callable, and the per-box phase
timer is a small Skynet class instead of Harbor's setup record.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field

from .environment import OutputSink

logger = logging.getLogger(__name__)

_OUTPUT_SINK: ContextVar[OutputSink | None] = ContextVar("sandbox_output_sink", default=None)


@dataclass(frozen=True)
class PhaseTiming:
    """Hold the duration of one phase of one sandbox."""

    box: str
    phase: str
    seconds: float
    ok: bool


@dataclass
class BoxTimings:
    """Collect the phase timings of one sandbox, in the order they ended."""

    box: str
    clock: Callable[[], float] = time.monotonic
    records: list[PhaseTiming] = field(default_factory=list)

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time the body as one phase. A failed or cancelled body still records.

        Args:
            name: The phase name, for example ``create`` or ``run``.

        Yields:
            Nothing.
        """
        started = self.clock()
        ok = False
        try:
            yield
            ok = True
        finally:
            record = PhaseTiming(box=self.box, phase=name, seconds=self.clock() - started, ok=ok)
            self.records.append(record)
            logger.debug("sandbox %s phase %s took %.3fs ok=%s", self.box, name, record.seconds, ok)

    def total(self, name: str) -> float:
        """Return the summed seconds of every record of one phase.

        Args:
            name: The phase name.

        Returns:
            The total duration, or 0.0 when the phase never ran.
        """
        return sum(record.seconds for record in self.records if record.phase == name)


@contextlib.contextmanager
def use_output_sink(sink: OutputSink | None) -> Iterator[None]:
    """Send the output of every nested ``exec`` call without its own sink to ``sink``.

    Args:
        sink: Receives ``(stream, text)`` chunks, or ``None`` to clear it.

    Yields:
        Nothing.
    """
    token = _OUTPUT_SINK.set(sink)
    try:
        yield
    finally:
        _OUTPUT_SINK.reset(token)


def current_output_sink() -> OutputSink | None:
    """Return the output sink of the current task.

    Returns:
        The sink that ``use_output_sink`` set, or ``None``.
    """
    return _OUTPUT_SINK.get()

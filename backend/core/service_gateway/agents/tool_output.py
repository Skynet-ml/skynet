"""Stream a shell command's output into the chat while the command runs (SKY-1006).

A command's output reaches the model only when its tool call returns, which
can take up to 30 seconds. As in the Codex app, the chat also gets the new
output live: ``tool_output`` events ``{id, chunk}``, keyed by the call's
``tool_start`` id. They are for display only. The model's result and the
saved chat keep only the ``tool_end`` result, which replaces the live text.

The tool wrapper sets :data:`tool_stream` around each call. The command
runners read it in the worker thread, which ``asyncio.to_thread`` gives a copy
of the call's context.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable

TOOL_OUTPUT_EVENT = "tool_output"
FLUSH_SECONDS = 0.1
# The most text in one event; a faster command loses the middle of the batch.
MAX_CHUNK_CHARS = 8192
# The most text one call streams, as the Codex live preview has a budget too;
# the ``tool_end`` result still shows the rest.
MAX_CALL_CHARS = 1 << 20


class ToolOutputStream:
    """Batch one tool call's live output into ``tool_output`` events."""

    def __init__(self, call_id: str, emit: Callable[[dict], None]) -> None:
        """Start with no output.

        Args:
            call_id: The ``tool_start`` id of the call.
            emit: The thread-safe, non-blocking event callback of the turn.
        """
        self._id = call_id
        self._emit = emit
        self._lock = threading.Lock()
        self._buffer = ""
        self._skipped = False
        self._sent = 0
        self._timer: threading.Timer | None = None
        self._closed = False

    def write(self, text: str) -> None:
        """Queue new output; a timer sends it within ``FLUSH_SECONDS``.

        It never emits, so a runner may call it under its own lock.

        Args:
            text: The output chunk.
        """
        with self._lock:
            if self._closed or not text or self._sent >= MAX_CALL_CHARS:
                return
            self._buffer += text
            if len(self._buffer) > MAX_CHUNK_CHARS:
                self._buffer = self._buffer[-MAX_CHUNK_CHARS:]
                self._skipped = True
            if self._timer is None:
                self._timer = threading.Timer(FLUSH_SECONDS, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        """Send the queued output now."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._closed or not self._buffer:
                return
            chunk = self._buffer[: MAX_CALL_CHARS - self._sent]
            self._sent += len(chunk)
            data: dict = {"id": self._id, "chunk": chunk}
            # The chat marks the gap before this chunk.
            if self._skipped:
                data["skipped"] = True
            self._buffer, self._skipped = "", False
            # Emitting under the lock keeps the events in order across the timer and runner threads.
            self._emit({"event": TOOL_OUTPUT_EVENT, "data": data})

    def close(self) -> None:
        """Send the queued output and stop, so no event follows the call's ``tool_end``."""
        self.flush()
        with self._lock:
            self._closed = True


tool_stream: contextvars.ContextVar[ToolOutputStream | None] = contextvars.ContextVar("tool_stream", default=None)

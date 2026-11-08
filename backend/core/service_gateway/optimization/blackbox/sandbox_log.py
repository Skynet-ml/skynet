"""Copy what a sandboxed command writes to stderr into the run log.

A sandbox, and every file in it, is gone once the run ends, so its stderr is
the only record of why something inside it failed. Every session's ``run`` goes
through :func:`logged_command`: stderr lines stream to :data:`stream_logger` at
DEBUG while the command runs, and a command that fails, times out or raises
logs a WARNING with its last lines on :data:`logger`. Each record carries the
sandbox's job as ``sandbox_owner``; the worker routes records by it into that
job's run log, and the optimization subprocess forwards them as log events.

Sandbox output is untrusted, so it is stripped of terminal control sequences
and scrubbed of every secret the command was given. :func:`forward` relays a
structured record a sandbox sent, attributed and capped by the host. Inside an
:func:`event_scope`, the ``log()`` events a scorer writes to stderr are relayed
the same way, pinned to the scope's source, candidate and case. A multi-line
message, such as a stack trace or a printed error object, becomes one row.

:class:`BoxJobRecord` follows the box events of a sandbox pool: it writes
the live box counts by phase to the job's progress record at a bounded
rate, logs the box events into the run log, one line each for the first
boxes and throttled summary lines after them, and at the end writes the
seconds the boxes spent in each phase.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from .... import run_log
from ....billing.usage_tags import usage_scope
from ....sandbox.hooks import TERMINAL_EVENTS, BoxEvent, BoxEventInfo, BoxHooks, BoxTimings, LiveCounts
from . import check_terminal
from .runner import EVENT_PREFIX

logger = logging.getLogger(__name__)
# Streamed lines reach only the run-log handlers attached here, never the
# process's own console, which would otherwise carry every sandbox's stderr.
stream_logger = logging.getLogger(f"{__name__}.stream")
stream_logger.propagate = False
stream_logger.setLevel(logging.DEBUG)

_TAIL_LINES = 40
_LABEL_CHARS = 120
# Shorter values (ports, flags, "1") are not secrets, and scrubbing them would mangle ordinary text.
_SECRET_MIN_CHARS = 8
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]|[\x00-\x08\x0b-\x1f\x7f]")
# The highest level a sandbox may claim: CRITICAL is reserved for the host.
_FORWARDED_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
# Lines that only close a printed object, like Node's "}" after an error's fields.
_CLOSER = re.compile(r"^[\]\)}]+[,;]?$")
_CHAINED = ("Caused by", "During handling of the above exception", "The above exception was the direct cause")


# What the host knows about the command running now: the source its events
# come from and the candidate and case it serves. Unset, stderr is plain output.
_scope: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("sandbox_event_scope", default=None)


@contextlib.contextmanager
def event_scope(**tags: Any) -> Iterator[None]:
    """Relay the events commands write to stderr while the scope is open.

    The candidate and case also tag the model calls made inside the scope.
    Scopes nest: an inner one keeps the outer's tags and overrides those it sets.

    Args:
        **tags: ``source``, ``candidate`` and ``case`` to pin on every event; ``None`` keeps the outer value.

    Yields:
        Nothing; the scope ends when the block does.
    """
    merged = {**(_scope.get() or {}), **{key: value for key, value in tags.items() if value is not None}}
    token = _scope.set(merged)
    try:
        with usage_scope(candidate=tags.get("candidate"), case=tags.get("case")):
            yield
    finally:
        _scope.reset(token)


def ignore_output(stream: str, text: str) -> None:
    """Accept streamed output that :func:`logged_command` already records.

    Passing it as ``on_output`` makes a session stream a command's stderr live,
    instead of logging it once the command ends.

    Args:
        stream: ``stdout`` or ``stderr``.
        text: The chunk.
    """
    del stream, text


class Scrubber:
    """Make untrusted sandbox text safe to log."""

    def __init__(self, secrets: Iterable[str]) -> None:
        """Remember the values to redact.

        Args:
            secrets: Values the sandbox can see; each is replaced before logging.
        """
        self._secrets = sorted({value for value in secrets if len(value) >= _SECRET_MIN_CHARS}, key=len, reverse=True)

    def text(self, line: str) -> str:
        """Redact secrets and drop terminal control sequences from one line.

        Args:
            line: Raw output text.

        Returns:
            The text as it may be logged.
        """
        for secret in self._secrets:
            line = line.replace(secret, "[redacted]")
        return _CONTROL.sub("", line)

    def value(self, value: Any) -> Any:
        """Redact secrets from every string inside a JSON value.

        Args:
            value: Parsed JSON.

        Returns:
            The same structure with each string scrubbed.
        """
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        return value


def forward(
    data: Any,
    *,
    owner: str | None,
    scrub: Scrubber,
    source: str = "engine",
    pinned: Mapping[str, Any] | None = None,
) -> None:
    """Relay one structured record a sandbox sent into its job's run log.

    The host, not the sandbox, decides what the record may be: its logger is
    always under ``sandbox.``, its source one of the sandbox sources, and its
    level at most ERROR, so sandbox code can never pass for Skynet's own lines.

    Args:
        data: The decoded record; anything but a mapping is ignored.
        owner: Job the sandbox belongs to.
        scrub: Redacts the sandbox's secrets.
        source: Source used when the record names none it may use.
        pinned: Fields the host knows and the record may not change, such as
            a scorer event's source, candidate and case.
    """
    if not isinstance(data, dict):
        return
    claimed = run_log.normalize({**scrub.value(data), **(pinned or {})})
    if claimed["source"] not in run_log.SANDBOX_SOURCES:
        claimed["source"] = source
    level = _FORWARDED_LEVELS.get(str(data.get("level", "")).upper(), logging.INFO)
    name = scrub.text(str(data.get("logger") or "sandbox")).strip() or "sandbox"
    record = stream_logger.makeRecord(
        f"sandbox.{name}",
        level,
        __file__,
        0,
        "%s",
        (scrub.text(str(data.get("message", ""))),),
        None,
        extra=run_log.event_extra(**claimed, sandbox_owner=owner),
    )
    stream_logger.handle(record)


def stream_line(line: str, *, owner: str | None, scrub: Scrubber, label: str = "sandbox") -> None:
    """Log one plain line a sandbox printed, at DEBUG.

    Args:
        line: Raw line without its newline.
        owner: Job the sandbox belongs to.
        scrub: Redacts the sandbox's secrets.
        label: Prefix that says where the line came from.
    """
    if line.strip():
        stream_logger.debug(
            "[%s] %s", label, scrub.text(line), extra=run_log.event_extra(source="sandbox", sandbox_owner=owner)
        )


class LineGroups:
    """Join the lines of one multi-line message so it is logged as one row.

    A line continues the message before it when it is indented, only closes a
    bracket, or chains another exception. A Python traceback also takes its
    unindented last line, the exception itself. Messages are only cut at line
    boundaries, so a caller flushes at the end of each output chunk: one write,
    such as a whole error object, reaches the host in one chunk.
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        """Start with no open message.

        Args:
            emit: Called with each finished message, its lines joined by newlines.
        """
        self._emit = emit
        self._lines: list[str] = []

    def add(self, line: str) -> None:
        """Take one complete, non-blank line.

        Args:
            line: The line without its newline.
        """
        if self._lines and self._continues(line):
            self._lines.append(line)
            if self._lines[0].startswith("Traceback") and not line[:1].isspace():
                self.flush()
            return
        self.flush()
        self._lines.append(line)

    def _continues(self, line: str) -> bool:
        """Whether ``line`` belongs to the open message.

        Args:
            line: The next line.

        Returns:
            True when it continues the open message.
        """
        if line[:1].isspace() or _CLOSER.match(line.strip()) or line.startswith(_CHAINED):
            return True
        return self._lines[0].startswith("Traceback")

    def flush(self) -> None:
        """Emit the open message, if any."""
        if self._lines:
            text, self._lines = "\n".join(self._lines), []
            self._emit(text)


class SandboxOutputLog:
    """The log record of one sandboxed command's stderr."""

    def __init__(self, command: str, secrets: Iterable[str], owner: str | None = None) -> None:
        """Prepare to log one command.

        Args:
            command: The shell command line, shortened into the log label.
            secrets: Values the command can see; each is replaced before logging.
            owner: Job the sandbox belongs to, set on every record as ``sandbox_owner``.
        """
        self._owner = owner
        self._scope = _scope.get()
        self._extra = run_log.event_extra(source="sandbox", sandbox_owner=owner)
        self.scrub = Scrubber(secrets)
        first = command.strip().splitlines()[0] if command.strip() else ""
        self.label = self.scrub.text(first[:_LABEL_CHARS] + ("…" if len(first) > _LABEL_CHARS else ""))
        self._pending = ""
        self._tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._groups = LineGroups(lambda text: stream_line(text, owner=self._owner, scrub=self.scrub))
        self._lock = threading.Lock()

    def _line(self, line: str) -> None:
        """Stream one complete stderr line.

        Args:
            line: Raw stderr line without its newline.
        """
        if not line.strip():
            return
        if self._scope is not None and line.startswith(EVENT_PREFIX):
            self._groups.flush()
            self._event(line[len(EVENT_PREFIX) :])
            return
        self._tail.append(self.scrub.text(line))
        self._groups.add(line)

    def _event(self, body: str) -> None:
        """Relay one ``log()`` event, pinned to the scope it was written in.

        Args:
            body: The JSON after the event prefix; unreadable JSON is logged as plain output.
        """
        try:
            data = json.loads(body)
        except ValueError:
            stream_line(body, owner=self._owner, scrub=self.scrub)
            return
        scope = self._scope or {}
        pinned = {
            "source": scope.get("source", "sandbox"),
            "candidate": scope.get("candidate"),
            "case": scope.get("case"),
        }
        forward(data, owner=self._owner, scrub=self.scrub, pinned=pinned)

    def feed(self, text: str) -> None:
        """Take a stderr chunk, which may hold partial lines.

        Args:
            text: Newly written stderr.
        """
        with self._lock:
            self._pending += text
            *lines, self._pending = self._pending.split("\n")
            for line in lines:
                self._line(line)
            self._groups.flush()

    def tee(self, on_output: Callable[[str, str], None] | None) -> Callable[[str, str], None] | None:
        """Wrap the caller's output sink so stderr is logged as it streams.

        A command run without a sink is logged from its result instead: adding
        a sink would make the Vercel session read its output files on every poll.

        Args:
            on_output: The caller's sink, or ``None``.

        Returns:
            A sink that logs stderr and then calls the caller's, or ``None``.
        """
        if on_output is None:
            return None

        def sink(stream: str, text: str) -> None:
            """Log a stderr chunk, then hand every chunk on unchanged."""
            if stream == "stderr":
                self.feed(text)
            on_output(stream, text)

        return sink

    def finish(self, result: Any, *, streamed: bool) -> None:
        """Log the rest of stderr and, when the command failed, a summary with its tail.

        Args:
            result: The command's ``CommandResult``.
            streamed: Whether stderr already went through :meth:`tee`.
        """
        if not streamed:
            self.feed(result.stderr)
        with self._lock:
            if self._pending:
                self._line(self._pending)
                self._pending = ""
            self._groups.flush()
        if result.exit_code == 0 and not result.timed_out:
            return
        state = "timed out" if result.timed_out else f"exited {result.exit_code}"
        self._summary(logging.WARNING, f"{state}: {self.label}")

    def failed(self, error: BaseException) -> None:
        """Log what the command wrote before it raised.

        Args:
            error: The exception leaving ``run``.
        """
        with self._lock:
            if self._pending:
                self._line(self._pending)
                self._pending = ""
            self._groups.flush()
        self._summary(logging.WARNING, f"raised {type(error).__name__}: {self.label}")

    def _summary(self, level: int, headline: str) -> None:
        """Log one record with the headline and the last stderr lines.

        Args:
            level: Logging level.
            headline: What happened to which command.
        """
        lines = list(self._tail)
        body = "\n".join(lines) if lines else "(no stderr)"
        logger.log(level, "[sandbox] command %s\n%s", headline, body, extra=self._extra)


def logged_command(run: Callable[..., Any]) -> Callable[..., Any]:
    """Log the stderr of every command a session's ``run`` executes.

    Args:
        run: A ``SandboxSession.run`` implementation.

    Returns:
        The same method, with its command's stderr logged.
    """

    @functools.wraps(run)
    def wrapper(
        self: Any,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
        sudo: bool = False,
    ) -> Any:
        """Run the command with its stderr logged, keeping the method's own contract."""
        session_env = getattr(self, "_env", None)
        secrets = [*(env or {}).values(), *(session_env.values() if isinstance(session_env, Mapping) else ())]
        record = SandboxOutputLog(command, secrets, getattr(self, "log_owner", None))
        terminal = check_terminal.current_recorder()
        command_id = None
        if terminal is not None:
            command_id = terminal.command_started(
                command, scrub=record.scrub.text, sample=check_terminal.current_sample()
            )
            on_output = _mirrored(terminal, command_id, on_output)
        sink = record.tee(on_output)
        try:
            result = run(self, command, env=env, timeout_seconds=timeout_seconds, on_output=sink, sudo=sudo)
        except BaseException as error:
            record.failed(error)
            if terminal is not None and command_id is not None:
                terminal.command_finished(command_id, error=type(error).__name__)
            raise
        record.finish(result, streamed=sink is not None)
        if terminal is not None and command_id is not None:
            terminal.command_finished(command_id, exit_code=result.exit_code, timed_out=result.timed_out)
        return result

    return wrapper


def _mirrored(
    terminal: check_terminal.CheckTerminalRecorder,
    command_id: int,
    on_output: Callable[[str, str], None] | None,
) -> Callable[[str, str], None]:
    """Wrap a command's output sink so the Check's live transcript sees every chunk.

    Args:
        terminal: The check's recorder.
        command_id: The command's transcript id.
        on_output: The caller's sink, or ``None``.

    Returns:
        A sink that records each chunk and then hands it on unchanged.
    """

    def sink(stream: str, text: str) -> None:
        """Record one chunk, then pass it to the caller's sink."""
        terminal.command_output(command_id, stream, text)
        if on_output is not None:
            on_output(stream, text)

    return sink


SANDBOX_BOXES_EVENT = "sandbox_boxes"
SANDBOX_BOX_TIMINGS_EVENT = "sandbox_box_timings"
# One progress write per second keeps the job record's write rate flat however many boxes run.
_BOX_WRITE_INTERVAL_SECONDS = 1.0
# Past this many boxes, one line per box event would flood the run log, so only summaries follow.
_BOX_DETAIL_LIMIT = 20
_ERROR_CHARS = 200

ProgressWriter = Callable[[str, dict[str, Any]], None]


class BoxJobRecord:
    """Write the box counts, events and phase times of one job's sandbox pool.

    The hooks never fail a box: :class:`BoxHooks` logs a hook that raises.
    The count write is throttled. A write that the throttle skips schedules
    one trailing write at the end of the interval, so the counts lag the boxes
    by at most one interval. :meth:`flush` cancels it and writes the final state.
    """

    def __init__(
        self,
        hooks: BoxHooks,
        *,
        owner: str | None,
        progress: ProgressWriter,
        clock: Callable[[], float] = time.monotonic,
        interval: float = _BOX_WRITE_INTERVAL_SECONDS,
        detail_limit: int = _BOX_DETAIL_LIMIT,
        secrets: Iterable[str] = (),
    ) -> None:
        """Add the counting, timing, writing and logging hooks to ``hooks``.

        Args:
            hooks: The hooks of the pool that runs the job's boxes.
            owner: Job the boxes belong to, set on every log record as ``sandbox_owner``.
            progress: The job's progress sink: an event name and a metric snapshot.
            clock: Gives the time in seconds. Tests pass a fake clock.
            interval: The fewest seconds between two count writes, and between two summary lines.
            detail_limit: The number of first boxes that get one log line for each event.
            secrets: Values the boxes can see; each is redacted from the logged box errors.
        """
        self._owner = owner
        self._progress = progress
        self._clock = clock
        self._interval = interval
        self._detail_limit = detail_limit
        self._extra = run_log.event_extra(source="sandbox", sandbox_owner=owner)
        self._scrub = Scrubber(secrets)
        self.counts = LiveCounts()
        self.timings = BoxTimings(clock)
        self.started = 0
        self.ended = 0
        self.failed = 0
        self.cancelled = 0
        self._detailed: set[str] = set()
        self._last_write: float | None = None
        self._last_summary: float | None = None
        self._trailing: asyncio.TimerHandle | None = None
        self._flushed = False
        hooks.add(self.counts)
        hooks.add(self.timings)
        hooks.add(self._tally)
        hooks.add(self._write_counts)
        hooks.add(self._log_event)

    def metrics(self) -> dict[str, Any]:
        """Return the live counts as a progress metric snapshot.

        Returns:
            The boxes in each state, the live boxes, and the ended, failed and cancelled boxes.
        """
        return {
            "sandbox_boxes": {str(state): count for state, count in self.counts.snapshot().items()},
            "sandbox_boxes_active": self.counts.active,
            "sandbox_boxes_ended": self.ended,
            "sandbox_boxes_failed": self.failed,
            "sandbox_boxes_cancelled": self.cancelled,
        }

    async def _tally(self, info: BoxEventInfo) -> None:
        """Count the started, ended, failed and cancelled boxes.

        Args:
            info: The box event.
        """
        if info.event is BoxEvent.STARTED:
            self.started += 1
            if self.started <= self._detail_limit:
                self._detailed.add(info.box_id)
        elif info.event is BoxEvent.CANCELLED:
            self.cancelled += 1
        elif info.event is BoxEvent.ENDED:
            self.ended += 1
            if info.error is not None:
                self.failed += 1

    async def _write_counts(self, info: BoxEventInfo) -> None:
        """Write the live counts to the job's progress record, at most once per interval.

        Args:
            info: The box event.
        """
        now = self._clock()
        if self._last_write is not None and now - self._last_write < self._interval:
            if self._trailing is None and not self._flushed:
                delay = self._interval - (now - self._last_write)
                self._trailing = asyncio.get_running_loop().call_later(delay, self._write_trailing)
            return
        # Set before the write, so a writer that keeps failing is throttled too.
        self._last_write = now
        self._progress(SANDBOX_BOXES_EVENT, self.metrics())

    def _write_trailing(self) -> None:
        """Write the latest counts that the throttle skipped.

        It runs as a loop callback, so a failing write is logged here instead of by the hooks.
        """
        self._trailing = None
        if self._flushed:
            return
        self._last_write = self._clock()
        try:
            self._progress(SANDBOX_BOXES_EVENT, self.metrics())
        except Exception:
            logger.exception("Could not write the sandbox box counts for job %s", self._owner)

    async def _log_event(self, info: BoxEventInfo) -> None:
        """Log one line for an event of a first box, else a throttled summary line.

        Args:
            info: The box event.
        """
        if info.box_id in self._detailed:
            if info.event in TERMINAL_EVENTS:
                self._detailed.discard(info.box_id)
            if info.error is not None:
                logger.warning("[sandbox] box %s failed: %s", info.box_id, self._error(info.error), extra=self._extra)
            else:
                logger.debug("[sandbox] box %s %s", info.box_id, info.event, extra=self._extra)
            return
        now = self._clock()
        if self._last_summary is not None and now - self._last_summary < self._interval:
            return
        self._last_summary = now
        logger.info("[sandbox] %s", self._summary(), extra=self._extra)

    def flush(self) -> None:
        """Write the final counts and phase seconds to the job record and log a summary.

        Call it once after the pool's run, also after an error. A failing
        write is logged, so the job keeps its own outcome.
        """
        self._flushed = True
        if self._trailing is not None:
            self._trailing.cancel()
            self._trailing = None
        seconds = {str(phase): round(total, 3) for phase, total in self.timings.totals().items()}
        metrics = {**self.metrics(), "sandbox_phase_seconds": seconds, "sandbox_boxes_timed": self.timings.boxes}
        try:
            self._progress(SANDBOX_BOX_TIMINGS_EVENT, metrics)
        except Exception:
            logger.exception("Could not write the sandbox box timings for job %s", self._owner)
        spent = ", ".join(f"{phase} {total:.1f}s" for phase, total in seconds.items())
        logger.info("[sandbox] %s; box seconds: %s", self._summary(), spent, extra=self._extra)

    def _summary(self) -> str:
        """Describe the live boxes in each state and the boxes that ended.

        Returns:
            One line, for example ``boxes: 212 run, 18 score; 40 ended, 2 failed, 0 cancelled``.
        """
        live = ", ".join(f"{count} {state}" for state, count in self.counts.snapshot().items() if count) or "none live"
        return f"boxes: {live}; {self.ended} ended, {self.failed} failed, {self.cancelled} cancelled"

    def _error(self, error: BaseException) -> str:
        """Describe a box error in one short, safe line.

        Args:
            error: The error of the box.

        Returns:
            The error type and the start of its message, without control sequences.
        """
        message = self._scrub.text(str(error).replace("\n", " "))[:_ERROR_CHARS]
        return f"{type(error).__name__}: {message}" if message else type(error).__name__

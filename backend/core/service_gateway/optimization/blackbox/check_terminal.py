"""A bounded, redacted live transcript of one sandboxed contract check.

The Check on the Metric & run screen was three silent rows for tens of
seconds. While it runs, every sandboxed command — install, then one harness
run per sample — passes through :func:`sandbox_log.logged_command`, which
mirrors it here: the command line, its scrubbed output as it streams, and how
it ended, pinned to the Check row (phase) it belongs to and, inside the
evaluator, to the sample it serves. The API route streams each event over SSE
as it happens and the snapshot is stored in the check's preflight evidence, so
a finished, failed or deduplicated check replays the same transcript without
running again.

The transcript is untrusted text on its way to a browser, so every line passes
the command's own :class:`sandbox_log.Scrubber` (every environment value the
command saw) and then the recorder's (provider keys and the scoped gateway
token, which reach the box only encoded inside the harness's init message).
Harness plumbing never appears: reply frames are dropped and scorer ``log()``
frames are shown as their message alone. Each line, each phase and the
transcript as a whole have a budget, and a phase that spends its own is
marked truncated rather than silently cut.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
import shlex
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from .runner import EVENT_PREFIX
from .workspace_harness import REPLY_PREFIX

# The Check screen's rows, in order; a phase outside them (``models``,
# ``usage``) keeps the current bucket so its commands stay visible.
PHASES = ("budget", "sandbox", "evaluator")
TRANSCRIPT_VERSION = 1

LINE_MAX_CHARS = 500
PHASE_MAX_OUTPUT_CHARS = 24_000
MAX_EVENTS = 4_000
# The event cap counts output lines only. Command, exit, sample and note
# events drive the UI's lanes and row progress, so they get their own room and
# are never crowded out by a chatty scorer.
STRUCTURAL_EVENT_HEADROOM = 1_000
# A line without a newline (a \r progress bar, one huge JSON blob) is held
# only this far; past the line budget it would be clipped anyway, and the
# margin keeps a secret near the cut whole until the scrubber has seen it.
PENDING_MAX_CHARS = LINE_MAX_CHARS * 4
# Shorter values (ports, flags, "1") are not secrets, and redacting them would mangle ordinary text.
_SECRET_MIN_CHARS = 8
_REDACTED = "[redacted]"
_SECRET_KEY_PARTS = ("token", "api_key", "apikey", "secret", "password", "authorization")
LABEL_MAX_CHARS = 160
# The model mailbox (core.billing.model_mailbox) wraps a guest command in its
# proxy and talks to the host through nonce-tagged stdout frames. Both are
# plumbing: the frames carry raw provider requests, headers included.
_MAILBOX_FRAME = re.compile(r"APP_MODEL_[0-9a-f]{32}:")
_MAILBOX_WRAPPER = re.compile(
    r"python3 -I \.app-model-[0-9a-f]{32}/proxy\.py \.app-model-[0-9a-f]{32}/config\.json bash --noprofile --norc -c (?P<inner>.+)",
    re.DOTALL,
)


def secret_values(value: Any) -> set[str]:
    """Collect every credential-shaped string inside a protected payload.

    Args:
        value: Parsed JSON, such as a protected run payload with its routes.

    Returns:
        The string values stored under token, key, secret or password fields.
    """
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).lower()
            if isinstance(item, str) and any(part in name for part in _SECRET_KEY_PARTS):
                found.add(item)
            else:
                found |= secret_values(item)
    elif isinstance(value, list):
        for item in value:
            found |= secret_values(item)
    return found


class _Command:
    """What the recorder keeps about one running command."""

    def __init__(self, phase: str, sample: int | None, scrub: Callable[[str], str]) -> None:
        """Remember where the command's output belongs.

        Args:
            phase: The Check row its output is filed under.
            sample: The evaluator sample it serves, if any.
            scrub: The command's own redaction.
        """
        self.phase = phase
        self.sample = sample
        self.scrub = scrub
        self.started = time.monotonic()
        self.pending = {"stdout": "", "stderr": ""}


class CheckTerminalRecorder:
    """Accumulate one check's terminal events, fanning each out to a live sink.

    The evaluator scores its samples on a thread pool, so every method is
    safe to call from any thread.

    Args:
        emit: Called with each event as it is recorded — the streaming route
            turns it into an SSE event.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Start an empty transcript."""
        self._emit = emit
        self._started = time.monotonic()
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._phase = PHASES[0]
        self._next_id = 0
        self._commands: dict[int, _Command] = {}
        self._phase_chars = dict.fromkeys(PHASES, 0)
        self._truncated: set[str] = set()
        self._output_events = 0
        self._secrets: list[str] = []

    def redact(self, values: Iterable[str]) -> None:
        """Add values that must never reach the transcript, whatever command prints them.

        Args:
            values: Secrets such as provider keys and gateway tokens.
        """
        with self._lock:
            merged = {*self._secrets, *(value for value in values if len(value) >= _SECRET_MIN_CHARS)}
            self._secrets = sorted(merged, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        """Redact every registered secret from one piece of text.

        Args:
            text: Text on its way into the transcript.

        Returns:
            The text with each secret replaced.
        """
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        return text

    def set_phase(self, phase: str) -> None:
        """Attribute what follows to one of the Check screen's rows.

        Args:
            phase: Public preflight phase; anything but a Check row is ignored.
        """
        if phase in PHASES:
            with self._lock:
                self._phase = phase

    def note(self, code: str, *, phase: str | None = None, **params: str | int | float) -> None:
        """Record a host milestone the client words in the viewer's locale.

        Args:
            code: Stable identifier the client maps to a catalog message.
            phase: The Check row to file it under; the current one when omitted.
            **params: Placeholder values for that message.
        """
        event: dict[str, Any] = {"kind": "note", "note": code}
        if params:
            event["params"] = params
        self._append(event, phase=phase)

    def command_started(self, command: str, *, scrub: Callable[[str], str], sample: int | None = None) -> int:
        """Record a sandboxed command's start and return its transcript id.

        Args:
            command: The command line as the sandbox runs it.
            scrub: The command's own redaction, applied to everything it prints.
            sample: Evaluator sample the command serves, when known.

        Returns:
            The id ``command_output`` and ``command_finished`` refer to.
        """
        with self._lock:
            self._next_id += 1
            command_id = self._next_id
            phase = "evaluator" if sample is not None else self._phase
            self._commands[command_id] = _Command(phase, sample, scrub)
        event: dict[str, Any] = {
            "kind": "command",
            "id": command_id,
            "label": self.scrub(scrub(command_label(command))),
        }
        self._append(event, phase=phase, sample=sample)
        return command_id

    def command_output(self, command_id: int, stream: str, text: str) -> None:
        """Record a chunk of a command's output, one event per complete line.

        A line split across chunks is held until its newline arrives, so a
        secret or a protocol frame is never cut in two before it is matched.

        Args:
            command_id: The id ``command_started`` returned.
            stream: ``stdout`` or ``stderr``.
            text: The chunk, which may hold several lines or a partial one.
        """
        command = self._commands.get(command_id)
        if command is None:
            return
        with self._lock:
            buffered = command.pending.get(stream, "") + text
            *lines, rest = buffered.split("\n")
            command.pending[stream] = rest[:PENDING_MAX_CHARS]
        for line in lines:
            self._line(command_id, command, stream, line)

    def command_finished(
        self,
        command_id: int,
        *,
        exit_code: int | None = None,
        timed_out: bool = False,
        error: str | None = None,
    ) -> None:
        """Record how a command ended and how long it took.

        Args:
            command_id: The id ``command_started`` returned.
            exit_code: The process exit code, when the process ended at all.
            timed_out: Whether the command's allowance ran out.
            error: The exception type that interrupted the run, if one did.
        """
        command = self._commands.get(command_id)
        if command is None:
            return
        with self._lock:
            leftovers = [(stream, rest) for stream, rest in command.pending.items() if rest]
            command.pending = {"stdout": "", "stderr": ""}
        for stream, rest in leftovers:
            self._line(command_id, command, stream, rest)
        event: dict[str, Any] = {
            "kind": "exit",
            "id": command_id,
            "duration_ms": int((time.monotonic() - command.started) * 1000),
        }
        if exit_code is not None:
            event["exit_code"] = exit_code
        if timed_out:
            event["timed_out"] = True
        if error is not None:
            event["error"] = error
        self._append(event, phase=command.phase, sample=command.sample)

    def sample_started(self, index: int, total: int) -> None:
        """Record one evaluator sample starting to score.

        Args:
            index: Zero-based sample position, shown as its lane number.
            total: How many samples this check scores.
        """
        self._append({"kind": "sample", "total": total, "state": "running"}, phase="evaluator", sample=index)

    def sample_finished(
        self,
        index: int,
        total: int,
        *,
        score: float | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Record one evaluator sample's outcome.

        Args:
            index: Zero-based sample position.
            total: How many samples this check scores.
            score: The metric's score, when the sample produced one.
            error: The ``<Type>: <message>`` failure the sample ended with.
            duration_ms: How long the sample took.
        """
        event: dict[str, Any] = {"kind": "sample", "total": total, "state": "failed" if error else "done"}
        if score is not None:
            event["score"] = score
        if error:
            event["error"] = self._clip(self.scrub(error))
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        self._append(event, phase="evaluator", sample=index)

    def snapshot(self) -> dict[str, Any]:
        """Return the transcript so far as it is stored and replayed.

        Returns:
            ``{"version", "events", "truncated"}`` with the events in order and
            the phases whose output was cut.
        """
        with self._lock:
            return {
                "version": TRANSCRIPT_VERSION,
                "events": [dict(event) for event in self._events],
                "truncated": sorted(self._truncated),
            }

    def _line(self, command_id: int, command: _Command, stream: str, raw: str) -> None:
        """Scrub, filter and budget one complete output line.

        Args:
            command_id: The command it came from.
            command: What the recorder knows about that command.
            stream: ``stdout`` or ``stderr``.
            raw: The line without its newline.
        """
        line = self.scrub(command.scrub(raw.rstrip("\r")))
        if line.startswith(REPLY_PREFIX) or _MAILBOX_FRAME.match(line):
            return
        if line.startswith(EVENT_PREFIX):
            line = _event_message(line[len(EVENT_PREFIX) :])
            stream = "log"
        if not line.strip():
            return
        line = self._clip(line)
        phase = command.phase
        with self._lock:
            spent = self._phase_chars[phase] + len(line)
            # Once a phase is marked truncated it stays closed, so no shorter
            # line slips in after the "output capped" note.
            over = phase in self._truncated or spent > PHASE_MAX_OUTPUT_CHARS or self._output_events >= MAX_EVENTS
            first_over = over and phase not in self._truncated
            if over:
                self._truncated.add(phase)
            else:
                self._phase_chars[phase] = spent
                self._output_events += 1
        if first_over:
            self._append({"kind": "note", "note": "output_truncated"}, phase=phase, sample=command.sample)
        if over:
            return
        event = {"kind": "output", "id": command_id, "stream": stream, "text": line}
        self._append(event, phase=phase, sample=command.sample)

    @staticmethod
    def _clip(text: str) -> str:
        """Cut one line to the per-line budget.

        Args:
            text: The line.

        Returns:
            The line, ending in an ellipsis when it was cut.
        """
        return text if len(text) <= LINE_MAX_CHARS else text[:LINE_MAX_CHARS] + "…"

    def _append(self, event: dict[str, Any], *, phase: str | None = None, sample: int | None = None) -> None:
        """Store one event with its timestamp, phase and sample, and fan it out live.

        Args:
            event: The event without ``t``, ``phase`` and ``sample``.
            phase: The Check row to file it under; the current one when omitted.
            sample: The evaluator sample it belongs to, if any.
        """
        with self._lock:
            if len(self._events) >= MAX_EVENTS + STRUCTURAL_EVENT_HEADROOM:
                return
            record = {"t": int((time.monotonic() - self._started) * 1000), "phase": phase or self._phase, **event}
            if sample is not None:
                record["sample"] = sample
            self._events.append(record)
            emit = self._emit
        if emit is not None:
            # A viewer that went away must never break the check it watched.
            with contextlib.suppress(Exception):
                emit(record)


def command_label(command: str) -> str:
    """Return the first line of the command a person would recognise, shortened.

    Args:
        command: The command line as the sandbox runs it, possibly wrapped
            in the model mailbox's proxy.

    Returns:
        The wrapped command itself, cut to ``LABEL_MAX_CHARS``.
    """
    wrapped = _MAILBOX_WRAPPER.fullmatch(command.strip())
    if wrapped is not None:
        with contextlib.suppress(ValueError):
            [command] = shlex.split(wrapped.group("inner"))
    first = command.strip().splitlines()[0] if command.strip() else ""
    return first[:LABEL_MAX_CHARS] + ("…" if len(first) > LABEL_MAX_CHARS else "")


def _event_message(body: str) -> str:
    """Return the message of one scorer ``log()`` frame, without its JSON envelope.

    Args:
        body: The JSON after the event prefix.

    Returns:
        The frame's message, or nothing when the frame is unreadable.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) else ""


_recorder: contextvars.ContextVar[CheckTerminalRecorder | None] = contextvars.ContextVar(
    "check_terminal_recorder", default=None
)
_sample: contextvars.ContextVar[int | None] = contextvars.ContextVar("check_terminal_sample", default=None)


def current_recorder() -> CheckTerminalRecorder | None:
    """Return the recorder of the check this thread is serving, if any."""
    return _recorder.get()


def current_sample() -> int | None:
    """Return the evaluator sample this thread is scoring, if any."""
    return _sample.get()


def note(code: str, *, phase: str | None = None, **params: str | int | float) -> None:
    """Record a host milestone on the current check's transcript, if one is recording.

    Args:
        code: Stable identifier the client maps to a catalog message.
        phase: The Check row to file it under; the current one when omitted.
        **params: Placeholder values for that message.
    """
    recorder = _recorder.get()
    if recorder is not None:
        recorder.note(code, phase=phase, **params)


@contextlib.contextmanager
def recording(recorder: CheckTerminalRecorder | None) -> Iterator[CheckTerminalRecorder | None]:
    """Make ``recorder`` the current thread's transcript while the block runs.

    Args:
        recorder: The check's recorder; ``None`` records nothing.

    Yields:
        The recorder.
    """
    token = _recorder.set(recorder)
    try:
        yield recorder
    finally:
        _recorder.reset(token)


@contextlib.contextmanager
def sample_scope(index: int) -> Iterator[None]:
    """Pin the evaluator sample the block's commands belong to.

    Args:
        index: Zero-based sample position.

    Yields:
        Nothing; the scope ends when the block does.
    """
    token = _sample.set(index)
    try:
        yield
    finally:
        _sample.reset(token)

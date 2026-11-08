"""Sandbox stderr reaches the run log, bounded and scrubbed, from every session runtime."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from core.service_gateway.optimization.blackbox import sandbox_log
from core.service_gateway.optimization.blackbox.runner import EVENT_PREFIX
from core.service_gateway.optimization.blackbox.sandbox import (
    JOB_TAG,
    CommandResult,
    LocalSubprocessSession,
    SandboxSpec,
)
from core.worker.log_handler import route_sandbox_logs


class _Capture(logging.Handler):
    """Keep every sandbox record emitted while attached."""

    def __init__(self) -> None:
        """Start with no records."""
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep ``record``."""
        self.records.append(record)

    def messages(self, level: int | None = None) -> list[str]:
        """Return formatted messages, optionally at one level only."""
        return [r.getMessage() for r in self.records if level is None or r.levelno == level]


@pytest.fixture
def captured() -> Iterator[_Capture]:
    """Attach a capture to both sandbox loggers."""
    handler = _Capture()
    for target in (sandbox_log.logger, sandbox_log.stream_logger):
        target.addHandler(handler)
    previous = sandbox_log.logger.level
    sandbox_log.logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        sandbox_log.logger.setLevel(previous)
        for target in (sandbox_log.logger, sandbox_log.stream_logger):
            target.removeHandler(handler)


class _FakeSession:
    """A session whose command writes a fixed stderr and exit code."""

    def __init__(self, stderr: str, exit_code: int = 0, *, env: dict[str, str] | None = None) -> None:
        """Script one command's outcome."""
        self._stderr = stderr
        self._exit_code = exit_code
        self._env = env or {}
        self.log_owner = "job-1"

    @sandbox_log.logged_command
    def run(
        self,
        command: str,
        *,
        env: Any = None,
        timeout_seconds: float | None = None,
        on_output: Any = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Stream the scripted stderr in two chunks that split a line, then return."""
        self.sudo = sudo
        if on_output is not None:
            half = len(self._stderr) // 2
            on_output("stdout", "out\n")
            on_output("stderr", self._stderr[:half])
            on_output("stderr", self._stderr[half:])
        return CommandResult(exit_code=self._exit_code, stdout="out\n", stderr=self._stderr)


def test_a_failed_command_logs_a_warning_with_its_stderr_tail(captured: _Capture) -> None:
    """The last lines of a failing command land in one WARNING tagged with the job."""
    _FakeSession("Traceback\nImportError: no module named x\n", exit_code=1).run("python3 runner.py --flag")
    [warning] = captured.messages(logging.WARNING)
    assert warning.startswith("[sandbox] command exited 1: python3 runner.py --flag")
    assert warning.endswith("Traceback\nImportError: no module named x")
    assert {r.sandbox_owner for r in captured.records} == {"job-1"}


def test_streamed_stderr_is_logged_line_by_line_and_the_sink_still_gets_every_chunk(captured: _Capture) -> None:
    """Chunks that split a line are joined, and the caller's sink sees the original stream."""
    seen: list[tuple[str, str]] = []
    _FakeSession("first line\nsecond line\n").run("cmd", on_output=lambda s, t: seen.append((s, t)))
    assert captured.messages(logging.DEBUG) == ["[sandbox] first line", "[sandbox] second line"]
    assert captured.messages(logging.WARNING) == []
    assert "".join(t for s, t in seen if s == "stderr") == "first line\nsecond line\n"
    assert ("stdout", "out\n") in seen


def test_secrets_given_to_the_command_never_reach_the_log(captured: _Capture) -> None:
    """Values from the command's and the session's environment are redacted; short ones are left alone."""
    session = _FakeSession(
        "token=sk-session-secret-123 key=per-call-secret-456 port=8080\n",
        1,
        env={"A": "sk-session-secret-123", "P": "8080"},
    )
    session.run("cmd", env={"KEY": "per-call-secret-456"})
    text = "\n".join(captured.messages())
    assert "sk-session-secret-123" not in text
    assert "per-call-secret-456" not in text
    assert "token=[redacted] key=[redacted] port=8080" in text


def test_every_stderr_line_streams_whole_with_control_sequences_stripped(captured: _Capture) -> None:
    """No line is dropped or cut; only escape codes are removed."""
    lines = [f"\x1b[31mline {i}\x1b[0m" for i in range(500)]
    lines.append("x" * 5000)
    _FakeSession("\n".join(lines) + "\n", exit_code=2).run("cmd", on_output=lambda s, t: None)
    streamed = captured.messages(logging.DEBUG)
    assert len(streamed) == 501
    assert streamed[0] == "[sandbox] line 0"
    assert streamed[-1] == "[sandbox] " + "x" * 5000
    [warning] = captured.messages(logging.WARNING)
    assert "\x1b" not in warning
    assert len(warning.splitlines()) == sandbox_log._TAIL_LINES + 1


def test_forwarded_records_cannot_pass_for_the_host(captured: _Capture) -> None:
    """The host picks the logger, source and highest level of what a sandbox sends."""
    sandbox_log.forward(
        {
            "level": "CRITICAL",
            "logger": "core.worker",
            "message": "Run finished",
            "source": "host",
            "event": "phase.end",
        },
        owner="job-1",
        scrub=sandbox_log.Scrubber(()),
    )
    [record] = captured.records
    assert record.name == "sandbox.core.worker"
    assert record.levelno == logging.INFO
    assert record.run_log["source"] == "engine"
    assert record.run_log["event"] == "phase.end"
    assert record.sandbox_owner == "job-1"


def test_forwarded_records_keep_their_level_and_source_and_lose_secrets(captured: _Capture) -> None:
    """A sandbox source and level up to ERROR pass; secrets go from message and fields alike."""
    secret = "sk-very-secret-token"
    sandbox_log.forward(
        {
            "level": "ERROR",
            "logger": "app.engine",
            "message": f"leaked {secret}",
            "source": "proposer",
            "fields": {"nested": [f"also {secret}"], secret: 1},
            "candidate": 4,
        },
        owner="job-1",
        scrub=sandbox_log.Scrubber([secret]),
    )
    [record] = captured.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "leaked [redacted]"
    assert record.run_log["source"] == "proposer"
    assert record.run_log["fields"] == {"nested": ["also [redacted]"], "[redacted]": 1}
    assert record.run_log["candidate"] == "4"
    assert secret not in repr(record.__dict__)


def test_forward_ignores_anything_but_a_record(captured: _Capture) -> None:
    """A JSON value that is not an object is not a record."""
    sandbox_log.forward(["not", "a", "record"], owner="job-1", scrub=sandbox_log.Scrubber(()))
    assert captured.records == []


def _event_line(**fields: Any) -> str:
    """Frame one scorer event the way ``skynet.log`` writes it."""
    return EVENT_PREFIX + json.dumps({"level": "INFO", "logger": "scorer", "message": "m", **fields}) + "\n"


def test_scorer_events_in_a_scope_are_pinned_to_its_source_candidate_and_case(captured: _Capture) -> None:
    """A scorer cannot claim another source, candidate or case, nor pass for the host."""
    forged = _event_line(event="judge.retry", source="host", candidate="99", case="99", level="CRITICAL")
    with sandbox_log.event_scope(candidate="3", case="1"), sandbox_log.event_scope(source="scorer"):
        _FakeSession(forged).run("cmd", on_output=sandbox_log.ignore_output)
    [record] = captured.records
    assert record.name == "sandbox.scorer"
    assert record.levelno == logging.INFO
    assert record.run_log == {"source": "scorer", "event": "judge.retry", "fields": None, "candidate": "3", "case": "1"}


def test_event_lines_outside_a_scope_stay_plain_output(captured: _Capture) -> None:
    """Only commands the host scoped may send events; elsewhere the line is ordinary stderr."""
    _FakeSession(_event_line(event="judge.retry")).run("cmd", on_output=sandbox_log.ignore_output)
    [record] = captured.records
    assert record.levelno == logging.DEBUG
    assert record.run_log["source"] == "sandbox"
    assert record.run_log["event"] is None


def test_an_unreadable_event_line_is_kept_as_plain_output(captured: _Capture) -> None:
    """A torn event line is logged, not lost and not fatal."""
    with sandbox_log.event_scope(source="scorer"):
        _FakeSession(EVENT_PREFIX + "{not json\n").run("cmd", on_output=sandbox_log.ignore_output)
    [record] = captured.records
    assert record.getMessage() == "[sandbox] {not json"


@pytest.mark.parametrize("sudo", [False, True])
def test_a_logged_command_passes_sudo_to_the_session(captured: _Capture, sudo: bool) -> None:
    """The log wrapper keeps the root flag, so a root command never runs as the default user."""
    session = _FakeSession("")

    session.run("id -u", sudo=sudo)

    assert session.sudo is sudo


def test_a_command_that_raises_logs_what_it_wrote_first(captured: _Capture) -> None:
    """A transport error mid-command still leaves the stderr seen so far in the log."""

    class _Broken(_FakeSession):
        @sandbox_log.logged_command
        def run(
            self,
            command: str,
            *,
            env: Any = None,
            timeout_seconds: Any = None,
            on_output: Any = None,
            sudo: Any = False,
        ) -> Any:
            """Write a partial line, then fail."""
            on_output("stderr", "partial before the drop")
            raise ConnectionError("gone")

    with pytest.raises(ConnectionError):
        _Broken("").run("cmd", on_output=lambda s, t: None)
    [warning] = captured.messages(logging.WARNING)
    assert warning.startswith("[sandbox] command raised ConnectionError: cmd")
    assert warning.endswith("partial before the drop")


def test_a_local_session_logs_its_commands_stderr(captured: _Capture) -> None:
    """The real local runtime goes through the same logging, tagged with its job."""
    session = LocalSubprocessSession(SandboxSpec(lifetime_seconds=60, tags={JOB_TAG: "job-9"}))
    try:
        result = session.run("echo boom >&2; exit 3")
    finally:
        session.close()
    assert result.exit_code == 3
    [warning] = captured.messages(logging.WARNING)
    assert warning == "[sandbox] command exited 3: echo boom >&2; exit 3\nboom"
    assert {r.sandbox_owner for r in captured.records} == {"job-9"}


def test_the_worker_persists_only_its_own_jobs_sandbox_records() -> None:
    """Records from another job's sandbox, or with no owner, stay out of this job's run log."""

    class _Store:
        def __init__(self) -> None:
            self.logs: list[tuple[str, str, str]] = []

        def append_log(self, optimization_id: str, *, level: str, message: str, **_: Any) -> None:
            self.logs.append((optimization_id, level, message))

    store = _Store()
    with route_sandbox_logs("job-1", store):  # type: ignore[arg-type]
        _FakeSession("mine\n", exit_code=1).run("cmd")
        other = _FakeSession("theirs\n", exit_code=1)
        other.log_owner = "job-2"
        other.run("cmd")
        stray = _FakeSession("nobody\n", exit_code=1)
        stray.log_owner = None
        stray.run("cmd")
    _FakeSession("after\n", exit_code=1).run("cmd")
    assert [(job, level) for job, level, _ in store.logs] == [("job-1", "DEBUG"), ("job-1", "WARNING")]
    assert store.logs[1][2].endswith("mine")


def _groups(lines: list[str]) -> list[str]:
    """Return the messages ``lines`` group into."""
    out: list[str] = []
    groups = sandbox_log.LineGroups(out.append)
    for line in lines:
        groups.add(line)
    groups.flush()
    return out


def test_node_error_object_is_one_message() -> None:
    """A printed Node error, its fields and its closing brace form one message."""
    lines = [
        "[notion] Unable to auto-discover data source TypeError: fetch failed",
        "    at async t.request (chunk.js:1:19874)",
        "  [cause]: Error: getaddrinfo EAI_AGAIN api.notion.com",
        "    errno: -3001,",
        "  }",
        "}",
        "next line",
    ]
    assert _groups(lines) == ["\n".join(lines[:6]), "next line"]


def test_python_traceback_takes_its_exception_line() -> None:
    """A traceback ends with its unindented exception line, not before it."""
    lines = ["Traceback (most recent call last):", '  File "x.py", line 1', "ValueError: bad", "after"]
    assert _groups(lines) == ["\n".join(lines[:3]), "after"]


def test_plain_lines_stay_separate() -> None:
    """Unrelated unindented lines are each their own message."""
    assert _groups(["one", "two"]) == ["one", "two"]


def test_streamed_error_is_one_row(captured: _Capture) -> None:
    """A multi-line stderr chunk is logged as one DEBUG row."""
    log = sandbox_log.SandboxOutputLog("npm run build", ())
    log.feed("Error: boom\n    at a (b.js:1)\n    at c (d.js:2)\ndone\n")
    assert captured.messages(logging.DEBUG) == [
        "[sandbox] Error: boom\n    at a (b.js:1)\n    at c (d.js:2)",
        "[sandbox] done",
    ]

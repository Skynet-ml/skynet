"""Sandbox stderr reaches the run log, bounded and scrubbed, from every session runtime."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from core.sandbox import ResourceRequest, SandboxEnvironment
from core.sandbox import SandboxSpec as BoxSpec
from core.sandbox.hooks import BoxEvent, BoxEventInfo, BoxHooks, BoxTimings, TimedPhase
from core.sandbox.pool import BoxOutcome, BoxWork, SandboxPool
from core.sandbox.tests.fake_provider import InMemorySandbox
from core.sandbox.timeouts import Phase
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


class _Clock:
    """A fake clock that moves only when told to, or by ``tick`` on each read."""

    def __init__(self, tick: float = 0.0) -> None:
        """Start at zero.

        Args:
            tick: The seconds each read adds after it returns the time.
        """
        self.now = 0.0
        self.tick = tick

    def __call__(self) -> float:
        """Return the time, then move it by ``tick``."""
        now = self.now
        self.now += self.tick
        return now


class _Writes:
    """Keep every progress write, or raise on each one."""

    def __init__(self, *, fail: bool = False) -> None:
        """Start with no writes.

        Args:
            fail: Raise on every write.
        """
        self.items: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def __call__(self, event: str, metrics: dict[str, Any]) -> None:
        """Keep one write, or raise."""
        self.items.append((event, metrics))
        if self.fail:
            raise RuntimeError("progress store is down")

    def named(self, event: str) -> list[dict[str, Any]]:
        """Return the metrics of the writes with one event name."""
        return [metrics for name, metrics in self.items if name == event]


class _ClockedSandbox(InMemorySandbox):
    """Act as the in-memory sandbox; its start and stop move the fake clock."""

    def __init__(self, clock: _Clock) -> None:
        """Create a stopped box.

        Args:
            clock: The clock that the start and stop move.
        """
        super().__init__(BoxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=1)))
        self.clock = clock

    async def start(self) -> None:
        """Start the box after half a second."""
        self.clock.now += 0.5
        await super().start()

    async def stop(self) -> None:
        """Stop the box after a quarter second."""
        self.clock.now += 0.25
        await super().stop()


async def _numbers(count: int) -> AsyncIterator[int]:
    """Yield the numbers from 0 to count - 1."""
    for number in range(count):
        yield number


async def _settle() -> None:
    """Let every ready task run until it blocks again."""
    for _ in range(50):
        await asyncio.sleep(0)


class _Outcomes:
    """Collect the box outcomes."""

    def __init__(self) -> None:
        """Start with no outcomes."""
        self.items: list[BoxOutcome[int, object]] = []

    async def __call__(self, outcome: BoxOutcome[int, object]) -> None:
        """Store one outcome."""
        self.items.append(outcome)


def _plain_box(clock: _Clock) -> BoxWork[int, object]:
    """Build box work whose run step yields once and returns the candidate."""

    async def run(env: SandboxEnvironment, candidate: int) -> int:
        """Yield to the loop once, then return the candidate."""
        await asyncio.sleep(0)
        return candidate

    return BoxWork(create=lambda _candidate: _ClockedSandbox(clock), run=run)


async def test_job_record_counts_match_the_boxes_in_each_phase(captured: _Capture) -> None:
    """Five boxes under a run limit of two show two in run and three waiting, then all ended."""
    clock, writes, gate = _Clock(), _Writes(), asyncio.Event()
    hooks = BoxHooks()
    record = sandbox_log.BoxJobRecord(hooks, owner="job-1", progress=writes, clock=clock, interval=0.0)
    pool = SandboxPool({"fake": 5}, phase_limits={Phase.RUN: 2}, hooks=hooks)

    async def setup(env: SandboxEnvironment, candidate: int) -> None:
        """Return at once."""

    async def run(env: SandboxEnvironment, candidate: int) -> int:
        """Wait for the gate."""
        await gate.wait()
        return candidate

    work: BoxWork[int, object] = BoxWork(create=lambda _c: _ClockedSandbox(clock), setup=setup, run=run)
    outcomes = _Outcomes()
    task = asyncio.create_task(pool.run(_numbers(5), work, outcomes, workers=5))
    await _settle()
    live = writes.named(sandbox_log.SANDBOX_BOXES_EVENT)[-1]
    assert live["sandbox_boxes"] == {"starting": 0, "idle": 3, "setup": 0, "run": 2, "score": 0}
    assert live["sandbox_boxes_active"] == 5
    gate.set()
    await asyncio.wait_for(task, 2.0)
    record.flush()
    final = writes.named(sandbox_log.SANDBOX_BOX_TIMINGS_EVENT)[-1]
    assert final["sandbox_boxes_active"] == 0
    assert set(final["sandbox_boxes"].values()) == {0}
    assert (final["sandbox_boxes_ended"], final["sandbox_boxes_failed"]) == (5, 0)
    assert all(outcome.ok for outcome in outcomes.items)


async def test_job_record_holds_the_phase_seconds_after_the_run(captured: _Capture) -> None:
    """Each phase total is the sum of the seconds every box spent in it."""
    clock, writes = _Clock(), _Writes()
    hooks = BoxHooks()
    record = sandbox_log.BoxJobRecord(hooks, owner="job-1", progress=writes, clock=clock)

    def step(seconds: float) -> Any:
        """Build a phase step that moves the clock by ``seconds``."""

        async def phase(env: SandboxEnvironment, candidate: int) -> int:
            """Move the clock, then return the candidate."""
            clock.now += seconds
            return candidate

        return phase

    work: BoxWork[int, object] = BoxWork(
        create=lambda _c: _ClockedSandbox(clock), setup=step(1.0), run=step(2.0), score=step(3.0)
    )
    await SandboxPool({"fake": 1}, hooks=hooks).run(_numbers(3), work, _Outcomes())
    record.flush()
    final = writes.named(sandbox_log.SANDBOX_BOX_TIMINGS_EVENT)[-1]
    assert final["sandbox_phase_seconds"] == {
        "start": 1.5,
        "wait": 0.0,
        "setup": 3.0,
        "run": 6.0,
        "score": 9.0,
        "stop": 0.75,
    }
    assert final["sandbox_boxes_timed"] == 3
    assert "box seconds: start 1.5s" in captured.messages(logging.INFO)[-1]


async def test_box_timings_count_the_stop_of_a_box_that_fails_in_a_phase() -> None:
    """A box that fails in setup counts the time after setup as its stop, not as a wait."""
    clock = _Clock()
    timings = BoxTimings(clock)
    for event, at in (
        (BoxEvent.STARTED, 0.0),
        (BoxEvent.ENVIRONMENT_STARTED, 1.0),
        (BoxEvent.SETUP_STARTED, 1.5),
        (BoxEvent.SETUP_ENDED, 3.5),
        (BoxEvent.ENDED, 4.0),
    ):
        clock.now = at
        await timings(BoxEventInfo(event, "box-0"))
    assert timings.totals() == {
        TimedPhase.START: 1.0,
        TimedPhase.WAIT: 0.5,
        TimedPhase.SETUP: 2.0,
        TimedPhase.RUN: 0.0,
        TimedPhase.SCORE: 0.0,
        TimedPhase.STOP: 0.5,
    }
    assert timings.boxes == 1


async def test_job_record_write_rate_stays_bounded_with_1000_boxes(captured: _Capture) -> None:
    """A thousand boxes give about one count write per second and summary log lines, not one per event."""
    clock, writes = _Clock(tick=0.001), _Writes()
    hooks = BoxHooks()
    record = sandbox_log.BoxJobRecord(hooks, owner="job-1", progress=writes, clock=clock)
    outcomes = _Outcomes()
    await SandboxPool({"fake": 50}, hooks=hooks).run(_numbers(1000), _plain_box(clock), outcomes)
    record.flush()
    assert len(outcomes.items) == 1000
    elapsed = clock.now
    count_writes = writes.named(sandbox_log.SANDBOX_BOXES_EVENT)
    assert elapsed > 10
    assert len(count_writes) <= int(elapsed) + 2
    assert len(writes.named(sandbox_log.SANDBOX_BOX_TIMINGS_EVENT)) == 1
    detail = [m for m in captured.messages(logging.DEBUG) if m.startswith("[sandbox] box ")]
    summaries = [m for m in captured.messages(logging.INFO) if m.startswith("[sandbox] boxes: ")]
    assert len(detail) == 20 * 5
    assert len(summaries) <= int(elapsed) + 2
    assert summaries[-1].startswith("[sandbox] boxes: none live; 1000 ended, 0 failed, 0 cancelled")


async def test_a_failing_writer_does_not_fail_the_box(captured: _Capture) -> None:
    """The progress store raises on every write; each box still succeeds and the error is logged."""
    clock, writes = _Clock(), _Writes(fail=True)
    hooks = BoxHooks()
    record = sandbox_log.BoxJobRecord(hooks, owner="job-1", progress=writes, clock=clock, interval=0.0)
    outcomes = _Outcomes()
    await SandboxPool({"fake": 2}, hooks=hooks).run(_numbers(3), _plain_box(clock), outcomes)
    record.flush()
    assert len(outcomes.items) == 3
    assert all(outcome.ok for outcome in outcomes.items)
    assert writes.named(sandbox_log.SANDBOX_BOX_TIMINGS_EVENT)
    assert any("Could not write the sandbox box timings" in m for m in captured.messages(logging.ERROR))


async def test_a_failed_box_logs_one_warning_with_its_error(captured: _Capture) -> None:
    """A box whose run raises gets one warning line with the error type and message."""
    clock, writes = _Clock(), _Writes()
    hooks = BoxHooks()
    record = sandbox_log.BoxJobRecord(hooks, owner="job-1", progress=writes, clock=clock)

    async def run(env: SandboxEnvironment, candidate: int) -> int:
        """Raise a box error."""
        raise ValueError("bad\x1b[31m input")

    work: BoxWork[int, object] = BoxWork(create=lambda _c: _ClockedSandbox(clock), run=run)
    await SandboxPool({"fake": 1}, hooks=hooks).run(_numbers(1), work, _Outcomes())
    record.flush()
    warnings = [m for m in captured.messages(logging.WARNING) if m.startswith("[sandbox] box ")]
    assert warnings == ["[sandbox] box box-0:attempt-1 failed: ValueError: bad input"]
    final = writes.named(sandbox_log.SANDBOX_BOX_TIMINGS_EVENT)[-1]
    assert (final["sandbox_boxes_ended"], final["sandbox_boxes_failed"]) == (1, 1)
    assert all(record.sandbox_owner == "job-1" for record in captured.records if hasattr(record, "sandbox_owner"))


async def test_counts_after_a_burst_reach_the_record_one_interval_later() -> None:
    """A burst of events and then silence still gives the true counts after one interval."""
    clock, writes = _Clock(), _Writes()
    hooks = BoxHooks()
    record = sandbox_log.BoxJobRecord(hooks, owner="job-1", progress=writes, clock=clock, interval=0.05)
    for index in range(5):
        await hooks.emit(BoxEventInfo(BoxEvent.STARTED, f"box-{index}"))
        await hooks.emit(BoxEventInfo(BoxEvent.ENVIRONMENT_STARTED, f"box-{index}"))
        await hooks.emit(BoxEventInfo(BoxEvent.RUN_STARTED, f"box-{index}"))
    assert len(writes.named(sandbox_log.SANDBOX_BOXES_EVENT)) == 1
    await asyncio.sleep(0.15)
    count_writes = writes.named(sandbox_log.SANDBOX_BOXES_EVENT)
    assert len(count_writes) == 2
    assert count_writes[-1]["sandbox_boxes"]["run"] == 5
    record.flush()
    await asyncio.sleep(0.1)
    assert len(writes.named(sandbox_log.SANDBOX_BOXES_EVENT)) == 2


async def test_flush_cancels_the_pending_trailing_write() -> None:
    """A trailing write that is still pending at the flush never runs after it."""
    clock, writes = _Clock(), _Writes()
    hooks = BoxHooks()
    record = sandbox_log.BoxJobRecord(hooks, owner="job-1", progress=writes, clock=clock, interval=0.05)
    await hooks.emit(BoxEventInfo(BoxEvent.STARTED, "box-0"))
    await hooks.emit(BoxEventInfo(BoxEvent.ENVIRONMENT_STARTED, "box-0"))
    record.flush()
    await asyncio.sleep(0.15)
    await hooks.emit(BoxEventInfo(BoxEvent.RUN_STARTED, "box-0"))
    await asyncio.sleep(0.15)
    assert [name for name, _ in writes.items] == [
        sandbox_log.SANDBOX_BOXES_EVENT,
        sandbox_log.SANDBOX_BOX_TIMINGS_EVENT,
    ]


async def test_a_secret_in_a_box_error_never_reaches_the_log_or_the_record(captured: _Capture) -> None:
    """A secret given to the record is redacted from the warning line of a failed box."""
    clock, writes = _Clock(), _Writes()
    hooks = BoxHooks()
    record = sandbox_log.BoxJobRecord(
        hooks, owner="job-1", progress=writes, clock=clock, secrets=["per-box-secret-789"]
    )
    await hooks.emit(BoxEventInfo(BoxEvent.STARTED, "box-0"))
    await hooks.emit(
        BoxEventInfo(BoxEvent.ENDED, "box-0", error=RuntimeError("setup failed: value per-box-secret-789"))
    )
    record.flush()
    warnings = [m for m in captured.messages(logging.WARNING) if m.startswith("[sandbox] box ")]
    assert warnings == ["[sandbox] box box-0 failed: RuntimeError: setup failed: value [redacted]"]
    assert all("per-box-secret" not in message for message in captured.messages(logging.DEBUG))
    assert "per-box-secret" not in json.dumps(writes.items)

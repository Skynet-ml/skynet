"""Tests for the tree sandbox's warm box lease, its process scan and the metric box per score (SKY-732).

The tests use local subprocess boxes and fake /proc trees, so no test calls
a paid provider or the network.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetError
from core.billing.signals import BudgetReached
from core.sandbox.box_lease import BoxLease
from core.sandbox.errors import SandboxInfraError
from core.sandbox.grants import Grant, GrantError
from core.sandbox.placement import MODAL, VERCEL, Placement
from core.sandbox.trees import BoxFailureError, GrantDeniedError
from core.service_gateway.optimization.blackbox import tree_sandbox
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.sandbox_contract import CommandResult, SandboxSession
from core.service_gateway.optimization.blackbox.tree_sandbox import (
    PROCESS_SCAN,
    SCAN_SCRIPT,
    RuntimeBoxLease,
    RuntimeBoxSource,
    StepRef,
    TreeSandbox,
    _clean,
    _processes,
)
from core.storage.models import Base, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore

RUN = "job-ada"
_SPEC = SandboxSpec(lifetime_seconds=60, operation_key="op")
# Test limits only: no test reaches them unless it sets its own.
_LIMITS: dict[str, Any] = {"vercel_cap": 100, "starts_per_minute": 1000}
_REAPER = "box-lease-reaper"
# Each lease of the running test, for the autouse teardown to close.
_LEASES: list[BoxLease[Any]] = []
# The scan reads /proc, which a macOS host does not have; these tests need the host's real processes.
_HOST_PROC = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="the host has no /proc")
# The flags of a real kthreadd: PF_KTHREAD (0x00200000), PF_NOFREEZE and PF_FORKNOEXEC.
_KTHREADD_FLAGS = 0x00208040


def _stat_line(pid: int | str, ppid: int | str, start: int | str, name: str = "a) (b c", flags: int = 0) -> str:
    """Return a /proc/<pid>/stat line with a name that holds spaces and parens.

    Args:
        pid: Field 1.
        ppid: Field 4, the parent pid.
        start: Field 22, the start time in clock ticks.
        name: The process name, field 2 without its parens.
        flags: Field 9, the kernel flags of the process.

    Returns:
        The line, with its newline.
    """
    return f"{pid} ({name}) S {ppid} 0 0 0 0 {flags} " + "0 " * 12 + f"{start} 0 0\n"


def _fake_proc(
    root: Path,
    rows: Sequence[tuple[int, int, int]] = ((1, 0, 5),),
    kthreadd: bool = False,
    pid_2_flags: int | None = None,
) -> Path:
    """Write a fake /proc tree with one stat file for each row.

    Args:
        root: The folder that stands for /proc.
        rows: The pid, parent pid and start ticks of each process.
        kthreadd: Name pid 2 kthreadd in its comm file.
        pid_2_flags: The flags of pid 2; ``None`` gives the real kthreadd
            flags when ``kthreadd`` is set, and no flags when it is not.

    Returns:
        The root.
    """
    if pid_2_flags is None:
        pid_2_flags = _KTHREADD_FLAGS if kthreadd else 0
    root.mkdir(exist_ok=True)
    for pid, ppid, start in rows:
        (root / str(pid)).mkdir()
        (root / str(pid) / "stat").write_text(_stat_line(pid, ppid, start, flags=pid_2_flags if pid == 2 else 0))
    if kthreadd:
        (root / "2" / "comm").write_text("kthreadd\n")
    return root


def _scan_with(proc: Path) -> str:
    """Return the process scan command that reads a fake /proc tree.

    The real command runs under env -i, which drops SCAN_PROC_ROOT, so the
    test puts the value inside that env -i.

    Args:
        proc: The fake /proc tree.

    Returns:
        The command.
    """
    return PROCESS_SCAN.replace("/usr/bin/env -i ", f"/usr/bin/env -i SCAN_PROC_ROOT={shlex.quote(str(proc))} ", 1)


def _run_scan(proc: Path, extra: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the scan script on a fake /proc tree.

    Args:
        proc: The fake /proc tree.
        extra: More of the scan's environment.

    Returns:
        The finished scan.
    """
    return subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", SCAN_SCRIPT],
        env={"PATH": "/usr/bin:/bin", "SCAN_PROC_ROOT": str(proc), **(extra or {})},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.fixture(autouse=True)
def _close_every_lease(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every lease that the test built, and fail when a reaper thread outlives the test.

    A healthy release starts a reaper thread that runs while an idle box is
    left, and many tests never close their lease or let the box sleep.
    Without this, each such test leaves a reaper thread alive for up to the
    idle time.

    Args:
        monkeypatch: Records each lease, also a RuntimeBoxLease or one built
            without the provider helper.

    Yields:
        Nothing; the teardown closes the leases and checks the threads.
    """
    before = {thread for thread in threading.enumerate() if thread.name == _REAPER}
    build = BoxLease.__init__

    def record(self: BoxLease[Any], *args: Any, **kwargs: Any) -> None:
        """Build the lease, then record it.

        Args:
            self: The new lease.
            *args: The lease's arguments.
            **kwargs: The lease's options.
        """
        build(self, *args, **kwargs)
        _LEASES.append(self)

    monkeypatch.setattr(BoxLease, "__init__", record)
    try:
        yield
    finally:
        for lease in _LEASES:
            # The test already checked the errors it cares about; the teardown only stops the reaper.
            with contextlib.suppress(BaseException):
                lease.close()
        _LEASES.clear()
    left = [thread for thread in threading.enumerate() if thread.name == _REAPER and thread not in before]
    for thread in left:
        thread.join(timeout=5)
    assert not [thread for thread in left if thread.is_alive()], "A box lease reaper thread outlived the test."


@dataclass(eq=False)
class _Box:
    """Stand for one provider box."""

    placement: Placement
    grants: tuple[Grant, ...]
    closed: bool = False


@dataclass
class _Clock:
    """Give a fake monotonic time that only the fake sleep moves."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        """Return the fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Move the fake time forward.

        Args:
            seconds: The wait.
        """
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class _Provider:
    """Open fake boxes and record each open, its time and each close."""

    clock: _Clock = field(default_factory=_Clock)
    opened: list[_Box] = field(default_factory=list)
    start_times: list[float] = field(default_factory=list)
    closed: list[_Box] = field(default_factory=list)
    fail_opens: int = 0

    def open(self, placement: Placement, grants: Sequence[Grant]) -> _Box:
        """Open a fake box.

        Args:
            placement: The box's provider.
            grants: The box's grants.

        Returns:
            The box.

        Raises:
            SandboxInfraError: For the first ``fail_opens`` opens.
        """
        if self.fail_opens:
            self.fail_opens -= 1
            raise SandboxInfraError("The provider is down.")
        box = _Box(placement, tuple(grants))
        self.opened.append(box)
        self.start_times.append(self.clock.now)
        return box

    def close(self, box: _Box) -> None:
        """Close a fake box.

        Args:
            box: The box.
        """
        box.closed = True
        self.closed.append(box)

    def lease(self, **options: Any) -> BoxLease[_Box]:
        """Return a lease over this provider with the fake clock.

        Args:
            **options: Extra lease options.

        Returns:
            The lease.
        """
        return BoxLease(self.open, self.close, **{"clock": self.clock, "sleep": self.clock.sleep, **_LIMITS, **options})


@pytest.mark.parametrize("spill", [VERCEL, "", None])
def test_a_runtime_lease_refuses_a_bad_spill_provider(spill: Any) -> None:
    """A Vercel spill would count past the cap, so the cap would have no effect.

    Args:
        spill: A spill provider that is not valid.
    """
    with pytest.raises(ValueError, match="spill provider"):
        RuntimeBoxLease({VERCEL: _Runtime()}, _SPEC, spill=spill, **_LIMITS)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Return a SQLite engine with the schema and one job at generation 1.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(JobModel(optimization_id=RUN, username="ada", execution_generation=1))
        session.commit()
    return engine


@pytest.fixture
def step_log(engine: Engine, tmp_path: Path) -> Iterator[StepLogStore]:
    """Yield a step log with a short flush window.

    Args:
        engine: The test engine.
        tmp_path: Scratch folder.

    Yields:
        The step log; it closes after the test.
    """
    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w1", flush_window_seconds=0.02)
    yield store
    store.close()


@pytest.fixture
def trees(step_log: StepLogStore) -> TreeStore:
    """Return a tree store over the step log.

    Args:
        step_log: The test step log.

    Returns:
        The tree store.
    """
    return TreeStore(step_log)


class _DyingBox:
    """Wrap a local box whose agent commands raise once ``dead`` is set, as a box that dies mid-step."""

    def __init__(self, inner: SandboxSession) -> None:
        """Wrap the box.

        Args:
            inner: The local box.
        """
        self._inner = inner
        self.dead = False
        self.closed = False
        # The health check's outcome: None runs it, an exit code fakes it, an error is raised from it.
        self.check: int | BaseException | None = None
        # The process scan's output, its whole result or its error; None runs the real scan. The host's own
        # processes come and go, so a real scan on a test host can see a new process that no step started.
        self.processes: str | CommandResult | BaseException | None = "1 Tue Nov 17 00:00:00 2026\n"
        # Stands for the provider's root kind; the lease tests run the scan as a provider whose root is capable.
        self.root_has_capabilities = True
        # The sudo option of each process scan, in order.
        self.scan_sudo: list[bool] = []

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write files into the inner box.

        Args:
            files: Relative path to text.
        """
        self._inner.write_files(files)

    def run(self, command: str, **options: Any) -> CommandResult:
        """Raise on an agent command of a dead box, fake the set health check and process scan, and run the rest.

        Args:
            command: The shell command.
            **options: The run options.

        Returns:
            The inner box's result, or the faked health check or process scan result.

        Raises:
            SandboxInfraError: For an agent command once the box is dead.
            BaseException: The set health check error, for the ``true`` command, or the set process scan error.
        """
        # A local box refuses root on the developer's machine, so a root command runs as the test's user.
        sudo = options.pop("sudo", False)
        if command == PROCESS_SCAN:
            self.scan_sudo.append(sudo)
        if command == "true" and isinstance(self.check, BaseException):
            raise self.check
        if command == PROCESS_SCAN and isinstance(self.processes, BaseException):
            raise self.processes
        if command == "true" and self.check is not None:
            return CommandResult(exit_code=self.check)
        if command == PROCESS_SCAN and isinstance(self.processes, CommandResult):
            return self.processes
        if command == PROCESS_SCAN and self.processes is not None:
            return CommandResult(exit_code=0, stdout=self.processes)
        if self.dead and command.startswith("cd tree"):
            raise SandboxInfraError("The box stopped.")
        return self._inner.run(command, **options)

    def read_file(self, path: str) -> str | None:
        """Read a file from the inner box.

        Args:
            path: Relative path.

        Returns:
            The text, or ``None``.
        """
        return self._inner.read_file(path)

    def close(self) -> None:
        """Close the inner box."""
        self.closed = True
        self._inner.close()


@dataclass
class _Runtime:
    """Open local boxes that can die, and record each one."""

    injects_headers: bool = False
    boxes: list[_DyingBox] = field(default_factory=list)
    # When set, each box gets a new TMPDIR in this folder, outside its working directory.
    tmp_root: Path | None = None
    # Wraps each new local box.
    wrap: Callable[[SandboxSession], _DyingBox] = _DyingBox

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open one local box.

        Args:
            spec: The box spec.

        Returns:
            The box.
        """
        if self.tmp_root is not None:
            spec = replace(spec, env={**spec.env, "TMPDIR": tempfile.mkdtemp(dir=self.tmp_root)})
        box = self.wrap(LocalSubprocessRuntime().open(spec))
        self.boxes.append(box)
        return box  # type: ignore[return-value]


def _on_both(runtime: _Runtime) -> dict[str, _Runtime]:
    """Map Vercel and the Modal spill to one local runtime.

    Args:
        runtime: The runtime.

    Returns:
        The provider map.
    """
    return {VERCEL: runtime, MODAL: runtime}


def _tree(trees: TreeStore, files: Mapping[str, str]) -> str:
    """Store flat files as one tree.

    Args:
        trees: The tree store.
        files: Name to text.

    Returns:
        The tree id.
    """
    return trees.put_tree(
        RUN, [TreeEntry(name, "file", trees.put_blob(RUN, text.encode())) for name, text in files.items()]
    )


def _step(seq: int, agent_id: str = "agent-1") -> StepRef:
    """Return one step of an agent at generation 1.

    Args:
        seq: The step's place.
        agent_id: The agent.

    Returns:
        The step.
    """
    return StepRef(run_id=RUN, agent_id=agent_id, seq=seq, execution_generation=1)


def _sandbox(
    trees: TreeStore, step_log: StepLogStore, metric_runtime: _Runtime | None = None, **options: Any
) -> tuple[TreeSandbox, _Runtime, _Runtime, RuntimeBoxLease, RuntimeBoxLease]:
    """Build the layer over a warm candidate lease and a metric lease that keeps no box warm.

    Args:
        trees: The tree store.
        step_log: The step log.
        metric_runtime: The runtime of the metric boxes; ``None`` uses a plain local runtime.
        **options: Extra :class:`TreeSandbox` options.

    Returns:
        The layer, the candidate and metric runtimes, and the two leases.
    """
    candidate_runtime, metric_runtime = _Runtime(), metric_runtime or _Runtime()
    candidates = RuntimeBoxLease(_on_both(candidate_runtime), _SPEC, **_LIMITS)
    metrics = RuntimeBoxLease(_on_both(metric_runtime), _SPEC, keep_warm=False, **_LIMITS)
    sandbox = TreeSandbox(trees, step_log, candidates, metrics, **options)
    return sandbox, candidate_runtime, metric_runtime, candidates, metrics


def test_an_agent_runs_its_steps_in_one_warm_box(trees: TreeStore, step_log: StepLogStore) -> None:
    """Two steps of one agent use one box, and each step starts from its own tree.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    try:
        _, first = sandbox.run(_step(1), _tree(trees, {"a.txt": "one\n"}), "echo two > b.txt", [])
        result, _ = sandbox.run(_step(2), _tree(trees, {"a.txt": "other\n"}), "ls; cat a.txt", [])
    finally:
        candidates.close()
        metrics.close()

    assert len(runtime.boxes) == 1
    assert trees.get_tree(RUN, first) is not None
    assert result.stdout == "a.txt\nother\n"


def test_two_runs_with_one_agent_id_never_share_a_box(trees: TreeStore, step_log: StepLogStore, engine: Engine) -> None:
    """The box key holds the run id, so a file that one run leaves outside its tree never reaches the other run.

    Args:
        trees: The tree store.
        step_log: The step log.
        engine: The test engine; it gets the second run's job.
    """
    other = "job-bea"
    with Session(engine) as session:
        session.add(JobModel(optimization_id=other, username="bea", execution_generation=1))
        session.commit()
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    empty = _tree(trees, {})
    other_empty = trees.put_tree(other, [])
    try:
        sandbox.run(_step(1), empty, "echo left > ../left.txt", [])
        result, _ = sandbox.run(replace(_step(1), run_id=other), other_empty, "cat ../left.txt", [])
        again, _ = sandbox.run(_step(2), empty, "cat ../left.txt", [])
    finally:
        candidates.close()
        metrics.close()

    assert result.exit_code != 0
    assert again.stdout == "left\n"
    assert len(runtime.boxes) == 2


def test_a_step_that_times_out_gives_its_box_back_unhealthy(trees: TreeStore, step_log: StepLogStore) -> None:
    """The timeout kills only the command's process group, so the next step gets a new box.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log, timeout_seconds=1)
    empty = _tree(trees, {})
    try:
        result, _ = sandbox.run(_step(1), empty, "sleep 30", [])
        sandbox.run(_step(2), empty, "true", [])
    finally:
        candidates.close()
        metrics.close()

    assert result.timed_out
    assert len(runtime.boxes) == 2
    assert runtime.boxes[0].closed


@_HOST_PROC
def test_a_step_that_leaves_a_background_job_gives_its_box_back_unhealthy(
    trees: TreeStore, step_log: StepLogStore
) -> None:
    """A job that outlives its step could write into the next step's tree, so the next step gets a new box.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    empty = _tree(trees, {})
    try:
        sandbox.run(_step(1), empty, "true", [])
        runtime.boxes[0].processes = None
        # The job writes into the tree folder after its step ends, where the next step would unpack.
        sandbox.run(_step(2), empty, 'here=$PWD; (sleep 1; echo stray > "$here/late.txt") >/dev/null 2>&1 &', [])
        result, _ = sandbox.run(_step(3), empty, "sleep 1.5; ls", [])
    finally:
        candidates.close()
        metrics.close()

    assert result.stdout == ""
    assert len(runtime.boxes) == 2
    assert runtime.boxes[0].closed


@_HOST_PROC
def test_the_process_scan_sees_a_background_job_and_not_itself() -> None:
    """A job that a command leaves running is a new line in the next scan, and two scans in a row see no own process."""
    box = LocalSubprocessRuntime().open(_SPEC)
    try:
        before = box.run(PROCESS_SCAN, timeout_seconds=30)
        started = box.run("sleep 30 >/dev/null 2>&1 & echo $!", timeout_seconds=30)
        after = box.run(PROCESS_SCAN, timeout_seconds=30)
        box.run(f"kill {started.stdout.strip()}", timeout_seconds=30)
    finally:
        box.close()

    assert before.ok
    assert after.ok
    pids = {line.split()[0] for line in after.stdout.splitlines()} - {
        line.split()[0] for line in before.stdout.splitlines()
    }
    assert started.stdout.strip() in pids


_SNAPSHOT = "1 Tue Nov 17 00:00:00 2026\n"


@pytest.mark.parametrize(
    "scan",
    [
        CommandResult(exit_code=1, stdout=_SNAPSHOT),
        CommandResult(exit_code=0, stdout=_SNAPSHOT, timed_out=True),
        CommandResult(exit_code=0, stdout=""),
    ],
    ids=["fails", "times-out", "prints-nothing"],
)
def test_a_step_whose_process_scan_fails_gives_its_box_back_unhealthy(
    trees: TreeStore, step_log: StepLogStore, scan: CommandResult
) -> None:
    """A scan that fails, times out or prints nothing cannot show that no process is left, so the box drops.

    Args:
        trees: The tree store.
        step_log: The test step log.
        scan: The faked result of each process scan.
    """
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    empty = _tree(trees, {})
    try:
        sandbox.run(_step(1), empty, "true", [])
        runtime.boxes[0].processes = scan
        sandbox.run(_step(2), empty, "true", [])
        sandbox.run(_step(3), empty, "true", [])
    finally:
        candidates.close()
        metrics.close()

    assert len(runtime.boxes) == 2
    assert runtime.boxes[0].closed


@pytest.mark.parametrize(
    ("scan", "message"),
    [
        (
            CommandResult(exit_code=127, stderr="ps: not found " + "x" * 300 + "\nsecond line"),
            "The process scan of a _DyingBox box failed (exit code 127, timed out False): ps: not found " + "x" * 186,
        ),
        (
            CommandResult(exit_code=0, stdout=_SNAPSHOT, timed_out=True),
            "The process scan of a _DyingBox box failed (exit code 0, timed out True): ",
        ),
        (
            SandboxInfraError("box-secret-name"),
            "The process scan of a _DyingBox box did not run (SandboxInfraError)",
        ),
    ],
    ids=["fails", "times-out", "raises"],
)
def test_a_failed_process_scan_logs_the_box_type_and_the_cause(
    trees: TreeStore,
    step_log: StepLogStore,
    caplog: pytest.LogCaptureFixture,
    scan: CommandResult | BaseException,
    message: str,
) -> None:
    """A dropped box tells the operator why: the box type, the exit code and the first stderr line, capped.

    Args:
        trees: The tree store.
        step_log: The test step log.
        caplog: Pytest log capture.
        scan: The faked result or error of each process scan.
        message: The log line of each failed scan; the step scans before and after its command.
    """
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    empty = _tree(trees, {})
    try:
        sandbox.run(_step(1), empty, "true", [])
        runtime.boxes[0].processes = scan
        with caplog.at_level(logging.WARNING, logger=TreeSandbox.__module__):
            sandbox.run(_step(2), empty, "true", [])
    finally:
        candidates.close()
        metrics.close()

    assert [record.getMessage() for record in caplog.records if record.name == TreeSandbox.__module__] == [message] * 2
    assert "box-secret-name" not in caplog.text


class _ScriptedScanBox(_DyingBox):
    """Answer each process scan from a script that gets the scan's number and its sudo option."""

    def __init__(self, inner: SandboxSession, script: Callable[[int, bool], CommandResult], capable: bool) -> None:
        """Wrap the box.

        Args:
            inner: The local box.
            script: Gives the result of scan number n (from 1) with its sudo option.
            capable: The provider's root has capabilities.
        """
        super().__init__(inner)
        self._script = script
        self.root_has_capabilities = capable

    def run(self, command: str, **options: Any) -> CommandResult:
        """Answer a process scan from the script, and run the rest as the plain fake does.

        Args:
            command: The shell command.
            **options: The run options.

        Returns:
            The scripted scan result, or the plain fake's result.
        """
        if command != PROCESS_SCAN:
            return super().run(command, **options)
        self.scan_sudo.append(bool(options.get("sudo", False)))
        return self._script(len(self.scan_sudo), self.scan_sudo[-1])


def _scripted_sandbox(
    trees: TreeStore, step_log: StepLogStore, script: Callable[[int, bool], CommandResult], capable: bool = True
) -> tuple[TreeSandbox, _Runtime, RuntimeBoxLease, RuntimeBoxLease]:
    """Build the layer over a warm candidate lease whose boxes answer each process scan from a script.

    Args:
        trees: The tree store.
        step_log: The step log.
        script: Gives the result of each scan of a box.
        capable: The provider's root has capabilities.

    Returns:
        The layer, the candidate runtime and the two leases.
    """
    runtime = _Runtime(wrap=lambda inner: _ScriptedScanBox(inner, script, capable))
    candidates = RuntimeBoxLease(_on_both(runtime), _SPEC, **_LIMITS)
    metrics = RuntimeBoxLease(_on_both(_Runtime()), _SPEC, keep_warm=False, **_LIMITS)
    return TreeSandbox(trees, step_log, candidates, metrics), runtime, candidates, metrics


def test_a_step_whose_first_scan_fails_gives_its_box_back_unhealthy_also_when_the_second_scan_matches(
    trees: TreeStore, step_log: StepLogStore
) -> None:
    """Without the scan before the step no baseline shows that no process is left, so the next step gets a new box.

    Args:
        trees: The tree store.
        step_log: The test step log.
    """

    def script(number: int, sudo: bool) -> CommandResult:
        """Fail the first scan of each box and print the same lines after it.

        Args:
            number: The scan's number in its box.
            sudo: Not used.

        Returns:
            The scan result.
        """
        return CommandResult(exit_code=1) if number == 1 else CommandResult(exit_code=0, stdout=_SNAPSHOT)

    sandbox, runtime, candidates, metrics = _scripted_sandbox(trees, step_log, script)
    empty = _tree(trees, {})
    try:
        sandbox.run(_step(1), empty, "true", [])
        sandbox.run(_step(2), empty, "true", [])
    finally:
        candidates.close()
        metrics.close()

    assert len(runtime.boxes) == 2
    assert runtime.boxes[0].closed


_STRAY = "77 Tue Nov 17 00:00:07 2026\n"


@pytest.mark.parametrize("capable", [True, False], ids=["root-scan", "capless-root"])
def test_a_scan_that_a_stray_process_replays_as_the_box_user_cannot_keep_the_box(
    trees: TreeStore, step_log: StepLogStore, capable: bool
) -> None:
    """The scan runs as root where the provider's root is capable, so a replayed box user output file is not read.

    On Vercel a box user command's output file belongs to the box user. The fake
    box's box user channel gives the scan before the step again, as a stray
    process that replaces the file would. Only the root channel shows the stray
    process. A root with no capability (local Docker) gives its output through a
    pipe, so it keeps the box user scan.

    Args:
        trees: The tree store.
        step_log: The test step log.
        capable: The provider's root has capabilities.
    """

    def script(number: int, sudo: bool) -> CommandResult:
        """Show the stray process in each scan after a step, but only through the root channel.

        Args:
            number: The scan's number in its box; each step scans twice.
            sudo: The scan runs as root.

        Returns:
            The scan result.
        """
        return CommandResult(exit_code=0, stdout=_SNAPSHOT + (_STRAY if sudo and number % 2 == 0 else ""))

    sandbox, runtime, candidates, metrics = _scripted_sandbox(trees, step_log, script, capable)
    empty = _tree(trees, {})
    try:
        sandbox.run(_step(1), empty, "true", [])
        sandbox.run(_step(2), empty, "true", [])
        # The cleanup below closes every box, so read the first box's state here.
        closed = runtime.boxes[0].closed
    finally:
        candidates.close()
        metrics.close()

    # A kept box scans before and after each of the two steps.
    assert runtime.boxes[0].scan_sudo == [capable] * (2 if capable else 4)
    assert closed is capable
    assert len(runtime.boxes) == (2 if capable else 1)


def test_a_local_box_scans_without_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The local subprocess box refuses sudo, so its scan runs as the test's user and still gives its lines.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
        monkeypatch: Points the scan at the fake /proc tree.
    """
    monkeypatch.setattr(tree_sandbox, "PROCESS_SCAN", _scan_with(_fake_proc(tmp_path / "proc")))
    box = LocalSubprocessRuntime().open(_SPEC)
    try:
        lines = _processes(box)
    finally:
        box.close()

    assert lines is not None
    assert any(line.startswith("state ") for line in lines)


def _sudo_tools(folder: Path, home: Path) -> None:
    """Write a fake id that says root and a fake getent that gives the box user's home.

    Args:
        folder: The folder of the fake tools.
        home: The box user's home.
    """
    folder.mkdir()
    (folder / "id").write_text("#!/bin/sh\necho 0\n")
    entry = f"box:x:1001:0::{home}:/bin/bash"
    (folder / "getent").write_text(f'#!/bin/sh\n[ "$1 $2" = "passwd 1001" ] && echo "{entry}"\n')
    for tool in ("id", "getent"):
        (folder / tool).chmod(0o755)


@pytest.mark.parametrize(
    ("env", "same"),
    [
        ({"SUDO_UID": "1001"}, True),
        ({"SUDO_UID": "0"}, False),
        ({"SUDO_UID": "1002"}, False),
        ({"SUDO_UID": "1001", "ENTRY_PATH": ""}, False),
    ],
    ids=["box-user", "root-user", "unknown-user", "no-entry-path"],
)
def test_a_root_scan_under_sudo_checks_the_box_user_folders_or_fails(
    tmp_path: Path, env: dict[str, str], same: bool
) -> None:
    """Under sudo root has its own HOME and PATH, so the root scan checks the box user's, or fails without them.

    Args:
        tmp_path: Scratch folder; it holds the fake tools, the folders and the homes.
        env: The sudo environment that differs from the box user's case.
        same: The root scan gives the box user's state line; else it fails.
    """
    tools, folder, home, root_home = (tmp_path / name for name in ("tools", "bin", "home", "root"))
    _sudo_tools(tools, home)
    folder.mkdir()
    (folder / "tool").write_text("aaaa\n")
    home.mkdir()
    (home / ".profile").write_text("echo hi\n")
    root_home.mkdir()
    proc = _fake_proc(tmp_path / "proc")

    def scan(extra: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
        """Run the scan script with the fake tools first on PATH.

        Args:
            extra: The scan's own environment.

        Returns:
            The finished scan.
        """
        return subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", SCAN_SCRIPT],
            env={"PATH": f"{tools}:/usr/bin:/bin", "SCAN_PROC_ROOT": str(proc), **extra},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    box_user = scan({"BOX_PATH": str(folder), "HOME": str(home)})
    root = scan({"BOX_PATH": "/usr/bin:/bin", "HOME": str(root_home), "ENTRY_PATH": str(folder), **env})

    assert box_user.returncode == 0
    if same:
        assert root.returncode == 0
        assert root.stdout.splitlines()[0] == box_user.stdout.splitlines()[0]
    else:
        assert root.returncode == 1
        assert root.stdout == ""


def _fake_tools(folder: Path, marker: Path, names: Sequence[str]) -> None:
    """Write a tool for each name that records its run in the marker file and fails.

    Args:
        folder: The folder of the fake tools.
        marker: The file that each fake tool appends its name to.
        names: The tool names.
    """
    folder.mkdir()
    for name in names:
        tool = folder / name
        tool.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\nexit 1\n")
        tool.chmod(0o755)


def test_the_control_commands_never_run_a_tool_from_the_box_path(
    trees: TreeStore, step_log: StepLogStore, tmp_path: Path
) -> None:
    """A tool that a step plants on the box PATH never runs in the scan, the unpack or the pack.

    Args:
        trees: The tree store.
        step_log: The test step log.
        tmp_path: Scratch folder; it holds the fake tools and their marker file.
    """
    marker = tmp_path / "ran.txt"
    names = ("ps", "awk", "sort", "cat", "tr", "find", "stat", "sha256sum", "shasum", "tar", "base64", "rm", "mkdir")
    names += ("split", "wc")
    _fake_tools(tmp_path / "bin", marker, names)
    runtime = _Runtime()
    spec = replace(_SPEC, env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"})
    candidates = RuntimeBoxLease(_on_both(runtime), spec, **_LIMITS)
    metrics = RuntimeBoxLease(_on_both(_Runtime()), _SPEC, keep_warm=False, **_LIMITS)
    sandbox = TreeSandbox(trees, step_log, candidates, metrics)
    try:
        sandbox.run(_step(1), _tree(trees, {"a.txt": "one\n"}), "true", [])
        runtime.boxes[0].processes = None
        _, second = sandbox.run(_step(2), _tree(trees, {"a.txt": "two\n"}), "echo three > b.txt", [])
    finally:
        candidates.close()
        metrics.close()

    assert not marker.exists()
    assert sorted(entry.name for entry in trees.get_tree(RUN, second) or []) == ["a.txt", "b.txt"]


def _state(box: SandboxSession) -> str:
    """Return the state line of one process scan.

    Args:
        box: The box.

    Returns:
        The line that starts with ``state``.
    """
    with tempfile.TemporaryDirectory() as folder:
        result = box.run(_scan_with(_fake_proc(Path(folder) / "proc")), timeout_seconds=30)
    assert result.ok
    return next(line for line in result.stdout.splitlines() if line.startswith("state "))


def test_the_scan_state_changes_when_a_path_folder_or_a_profile_changes(tmp_path: Path) -> None:
    """A new tool on the box PATH, a rewrite with its old size and time, or a new profile changes the state line.

    Args:
        tmp_path: Scratch folder; it holds the box PATH folder.
    """
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "tool").write_text("aaaa\n")
    box = LocalSubprocessRuntime().open(replace(_SPEC, env={"PATH": f"{folder}:/usr/bin:/bin"}))
    try:
        first = _state(box)
        again = _state(box)
        box.run(f"echo planted > '{folder}/python3'", timeout_seconds=30)
        planted = _state(box)
        # The change time has whole seconds, so the rewrite comes in a later second than the last write.
        time.sleep(1.1)
        box.run(
            f"cp -p '{folder}/tool' ref; echo bbbb > '{folder}/tool'; touch -r ref '{folder}/tool'", timeout_seconds=30
        )
        rewritten = _state(box)
        box.run('echo "echo hi" > "$HOME/.profile"', timeout_seconds=30)
        profiled = _state(box)
    finally:
        box.close()

    assert first == again
    assert len({first, planted, rewritten, profiled}) == 4


@pytest.mark.parametrize(("kthreadd", "expected"), [(True, {"1"}), (False, {"1", "2", "3"})])
def test_the_scan_skips_pid_2_only_when_it_is_kthreadd(tmp_path: Path, kthreadd: bool, expected: set[str]) -> None:
    """On Docker and gVisor pid 2 can be a normal process, so the scan keeps it and its children.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
        kthreadd: The comm file of pid 2 names kthreadd.
        expected: The pids that the scan must print.
    """
    proc = _fake_proc(tmp_path / "proc", ((1, 0, 5), (2, 0, 6), (3, 2, 7)), kthreadd=kthreadd)

    result = _run_scan(proc)

    assert result.returncode == 0
    assert {line.split()[0] for line in result.stdout.splitlines() if not line.startswith("state ")} == expected


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (_KTHREADD_FLAGS, {"1"}),
        (0x00200000, {"1"}),
        (0, {"1", "2", "3"}),
        # Every flag bit except PF_KTHREAD, also the ones next to it.
        (0xFFFFFFFF - 0x00200000, {"1", "2", "3"}),
    ],
)
def test_the_scan_skips_pid_2_only_when_the_kernel_marks_it_a_kernel_thread(
    tmp_path: Path, flags: int, expected: set[str]
) -> None:
    """A box user process can name itself kthreadd, but only the kernel sets PF_KTHREAD in its flags.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
        flags: The flags of pid 2, which names itself kthreadd.
        expected: The pids that the scan must print.
    """
    proc = _fake_proc(tmp_path / "proc", ((1, 0, 5), (2, 0, 6), (3, 2, 7)), kthreadd=True, pid_2_flags=flags)

    result = _run_scan(proc)

    assert result.returncode == 0
    assert {line.split()[0] for line in result.stdout.splitlines() if not line.startswith("state ")} == expected


def test_the_scan_skips_its_own_shell_its_ancestors_and_its_children(tmp_path: Path) -> None:
    """The scan's shell, the processes above it and the tools that it starts change with each scan, so none prints.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
    """
    proc = _fake_proc(tmp_path / "proc", ((1, 0, 5), (4242, 1, 100), (77777, 1, 50)))
    # The wrapper writes the stat files of its own pid and of a child, then execs the scan, which keeps that pid.
    # Its parent 77777 stands for the caller's process.
    wrapper = (
        'mkdir "$P/$$" "$P/55555"; printf "%s\\n" "$$ (sh) S 77777 $Z 60 0 0" > "$P/$$/stat";'
        ' printf "%s\\n" "55555 (awk) S $$ $Z 70 0 0" > "$P/55555/stat";'
        ' exec /bin/bash --noprofile --norc -c "$SCAN"'
    )

    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", wrapper],
        env={
            "PATH": "/usr/bin:/bin",
            "SCAN_PROC_ROOT": str(proc),
            "P": str(proc),
            "Z": "0 " * 16 + "0",
            "SCAN": SCAN_SCRIPT,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert [line for line in result.stdout.splitlines() if not line.startswith("state ")] == ["1 5", "4242 100"]


def test_the_scan_names_each_process_by_its_pid_and_start_ticks(tmp_path: Path) -> None:
    """The fields come after the last paren, so a name with spaces and parens does not move them.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
    """
    proc = _fake_proc(tmp_path / "proc", ((1, 0, 5),))
    (proc / "300").mkdir()
    (proc / "300" / "stat").write_text(_stat_line(300, 1, 987654, name="x) S 1 2 (y"))

    result = _run_scan(proc)

    assert result.returncode == 0
    assert result.stdout.splitlines()[1:] == ["1 5", "300 987654"]


def test_the_scan_fails_when_a_listed_process_has_no_stat_file(tmp_path: Path) -> None:
    """A process that ends after the folder list could have forked a child the scan never sees, so the scan fails.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
    """
    proc = _fake_proc(tmp_path / "proc")
    (proc / "400").mkdir()
    (proc / "400" / "stat").symlink_to(tmp_path / "gone")

    result = _run_scan(proc)

    assert result.returncode != 0


def _scan_while(proc: Path, change: Callable[[int], None]) -> subprocess.CompletedProcess[str]:
    """Run the scan and change the fake /proc tree after the scan lists it, before the scan reads pid 1.

    The stat file of pid 1 becomes a named pipe, so the scan's read of it waits until the change is done.

    Args:
        proc: The fake /proc tree; it must hold pid 1.
        change: Changes the tree; it gets the pid of the scan's shell.

    Returns:
        The finished scan.
    """
    stat = proc / "1" / "stat"
    line = stat.read_text()
    stat.unlink()
    os.mkfifo(stat)
    scan = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-c", SCAN_SCRIPT],
        env={"PATH": "/usr/bin:/bin", "SCAN_PROC_ROOT": str(proc)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while True:
        # A non-blocking open fails with ENXIO until the scan opens the pipe, so a scan that ends first cannot hang.
        try:
            pipe = os.open(stat, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as error:
            if error.errno != errno.ENXIO or scan.poll() is not None or time.monotonic() > deadline:
                scan.kill()
                scan.communicate()
                raise AssertionError("The scan did not read the stat file of pid 1.") from error
            time.sleep(0.01)
    try:
        change(scan.pid)
        os.write(pipe, line.encode())
    finally:
        os.close(pipe)
    out, err = scan.communicate(timeout=30)
    return subprocess.CompletedProcess(scan.args, scan.returncode, out, err)


def _add(proc: Path, pid: int, ppid: int) -> None:
    """Add a process to the fake /proc tree.

    Args:
        proc: The fake /proc tree.
        pid: The pid of the process.
        ppid: Its parent pid.
    """
    (proc / str(pid)).mkdir()
    (proc / str(pid) / "stat").write_text(_stat_line(pid, ppid, 9))


def test_the_scan_fails_when_a_listed_process_ends_during_the_scan(tmp_path: Path) -> None:
    """A process that ends between the list and its read could hide a child, so the scan fails.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
    """
    proc = _fake_proc(tmp_path / "proc", ((1, 0, 5), (400, 1, 9)))

    def end(_: int) -> None:
        """Remove pid 400 after the list.

        Args:
            _: The scan's pid; not used.
        """
        (proc / "400" / "stat").unlink()
        (proc / "400").rmdir()

    assert _scan_while(proc, lambda _: None).returncode == 0
    proc = _fake_proc(tmp_path / "again", ((1, 0, 5), (400, 1, 9)))
    assert _scan_while(proc, end).returncode != 0


@pytest.mark.parametrize(
    ("parent", "kthreadd", "ok"),
    [("scan", False, True), ("kernel", True, True), ("kernel", False, False), ("other", False, False)],
    ids=["child-of-the-scan", "kernel-thread", "child-of-a-fake-kthreadd", "other-process"],
)
def test_the_scan_fails_when_a_process_starts_after_the_list(
    tmp_path: Path, parent: str, kthreadd: bool, ok: bool
) -> None:
    """A fork-and-exit chain starts each child after the list, so a new pid fails the scan.

    Only the scan's own children (awk and sort can start after the list) and kernel threads are new without a failure.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
        parent: The parent of the new process: the scan's shell, pid 2 or pid 1.
        kthreadd: Pid 2 is kthreadd with the kernel's flags.
        ok: The scan passes.
    """
    proc = _fake_proc(tmp_path / "proc", ((1, 0, 5), (2, 0, 6)), kthreadd=kthreadd)

    def start(scan: int) -> None:
        """Add pid 9000 after the list.

        Args:
            scan: The pid of the scan's shell.
        """
        _add(proc, 9000, {"scan": scan, "kernel": 2, "other": 1}[parent])

    result = _scan_while(proc, start)

    assert (result.returncode == 0) is ok
    if ok:
        assert "9000" not in {line.split()[0] for line in result.stdout.splitlines()}


@pytest.mark.parametrize(
    "fake",
    ["exit 1", "echo 1234567 89", "echo nothex"],
    ids=["fails", "crc-output", "bad-output"],
)
def test_the_scan_fails_when_the_hash_tool_fails_or_gives_no_sha256(tmp_path: Path, fake: str) -> None:
    """A missing or wrong hash tool gives no SHA-256 state line, so the scan fails and the box is not kept.

    Args:
        tmp_path: Scratch folder; it holds the fake hash tools and the fake /proc tree.
        fake: The body of the fake sha256sum and shasum.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("sha256sum", "shasum"):
        (tools / name).write_text(f"#!/bin/sh\ncat >/dev/null\n{fake}\n")
        (tools / name).chmod(0o755)
    proc = _fake_proc(tmp_path / "proc")
    real = _run_scan(proc)

    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", SCAN_SCRIPT],
        env={"PATH": f"{tools}:/usr/bin:/bin", "SCAN_PROC_ROOT": str(proc)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert real.returncode == 0
    assert re.fullmatch(r"state [0-9a-f]{64}", real.stdout.splitlines()[0])
    assert result.returncode != 0
    assert result.stdout == ""


def _unreadable(proc: Path) -> None:
    """Add a stat file that exists and that the scan cannot read.

    Args:
        proc: The fake /proc tree.
    """
    (proc / "500").mkdir()
    (proc / "500" / "stat").write_text(_stat_line(500, 1, 9))
    (proc / "500" / "stat").chmod(0)


def _bad_line(proc: Path) -> None:
    """Add a stat line with no paren.

    Args:
        proc: The fake /proc tree.
    """
    (proc / "500").mkdir()
    (proc / "500" / "stat").write_text("500 a S 1\n")


def _bad_start(proc: Path) -> None:
    """Add a stat line whose start field is not a number.

    Args:
        proc: The fake /proc tree.
    """
    (proc / "500").mkdir()
    (proc / "500" / "stat").write_text(_stat_line(500, 1, "x"))


def _no_process(proc: Path) -> None:
    """Remove every process from the fake /proc tree.

    Args:
        proc: The fake /proc tree.
    """
    (proc / "1" / "stat").unlink()
    (proc / "1").rmdir()


@pytest.mark.parametrize("damage", [_unreadable, _bad_line, _bad_start, _no_process])
def test_the_scan_fails_when_it_cannot_read_every_process(tmp_path: Path, damage: Callable[[Path], None]) -> None:
    """A stat file that does not read, a bad line or an empty /proc fails the scan, so the box is not kept.

    Args:
        tmp_path: Scratch folder; it holds the fake /proc tree.
        damage: Changes the fake /proc tree.
    """
    proc = _fake_proc(tmp_path / "proc")
    damage(proc)

    result = _run_scan(proc)

    assert result.returncode != 0


def test_the_process_scan_passes_the_sudo_values_into_the_clean_shell_and_drops_the_proc_root(
    tmp_path: Path,
) -> None:
    """env -i drops the box env, so the wrapper passes SUDO_UID and APP_ENTRY_PATH by name, and nothing else.

    Args:
        tmp_path: Scratch folder; it stands for the box user's home.
    """
    script = shlex.quote(SCAN_SCRIPT)
    assert PROCESS_SCAN.endswith(script)
    probe = shlex.quote('printf "%s|%s|%s" "$SUDO_UID" "$ENTRY_PATH" "$SCAN_PROC_ROOT"')
    outer = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "SUDO_UID": "1001",
        "APP_ENTRY_PATH": "/app/.venv/bin:/usr/bin",
        "SCAN_PROC_ROOT": str(tmp_path),
    }

    result = subprocess.run(
        ["/bin/bash", "-c", PROCESS_SCAN[: -len(script)] + probe],
        env=outer,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "1001|/app/.venv/bin:/usr/bin|"


@pytest.mark.parametrize("profile", [".profile", ".bashrc", ".bash_profile", ".bash_login"])
def test_the_scan_state_changes_when_any_shell_profile_changes(profile: str) -> None:
    """A login or interactive shell reads each of these files, so a write to any one changes the state line.

    Args:
        profile: The profile file in the box's HOME.
    """
    box = LocalSubprocessRuntime().open(_SPEC)
    try:
        first = _state(box)
        box.run(f'echo "echo hi" > "$HOME/{profile}"', timeout_seconds=30)
        changed = _state(box)
    finally:
        box.close()

    assert first != changed


def test_the_scan_state_changes_when_the_target_of_a_linked_tool_changes(tmp_path: Path) -> None:
    """A tool on the box PATH can be a link, so the state covers the file that the link names.

    Args:
        tmp_path: Scratch folder; it holds the box PATH folder and the link target.
    """
    folder = tmp_path / "bin"
    folder.mkdir()
    target = tmp_path / "target"
    target.write_text("aaaa\n")
    (folder / "python3").symlink_to(target)
    box = LocalSubprocessRuntime().open(replace(_SPEC, env={"PATH": f"{folder}:/usr/bin:/bin"}))
    try:
        first = _state(box)
        target.write_text("a longer text\n")
        changed = _state(box)
    finally:
        box.close()

    assert first != changed


def test_the_scan_state_with_gnu_stat_covers_each_file_size(tmp_path: Path) -> None:
    """A box has GNU stat, so a fake stat that maps the GNU format to BSD runs the GNU branch on a macOS host too.

    Args:
        tmp_path: Scratch folder; it holds the fake stat, its marker file and the box PATH folder.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    marker = tmp_path / "gnu.txt"
    # On a host with GNU stat the fake only runs the real one. On macOS it accepts the probe and maps each
    # GNU format field to its BSD field, so a GNU format that drops a field drops it here too.
    (tools / "stat").write_text(
        "#!/bin/sh\n"
        f"echo used >> '{marker}'\n"
        'if /usr/bin/stat -c %i / >/dev/null 2>&1; then exec /usr/bin/stat "$@"; fi\n'
        'if [ "$1" = -c ] && [ "$3" = / ]; then echo 2; exit 0; fi\n'
        'link=; if [ "$1" = -L ]; then link=-L; shift; fi\n'
        '[ "$1" = -c ] || exit 64\n'
        "fmt=$(printf '%s' \"$2\" | sed -e 's/%n/%N/g' -e 's/%s/%z/g' -e 's/%Y/%m/g' -e 's/%Z/%c/g')\n"
        "shift 2\n"
        'exec /usr/bin/stat $link -f "$fmt" "$@"\n'
    )
    (tools / "stat").chmod(0o755)
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "tool").write_text("aaaa\n")
    home = tmp_path / "home"
    home.mkdir()
    proc = str(_fake_proc(tmp_path / "proc"))

    def state() -> str:
        """Return the state line of one scan with the fake stat first on PATH.

        Returns:
            The line that starts with ``state``.
        """
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", SCAN_SCRIPT],
            env={"PATH": f"{tools}:/usr/bin:/bin", "BOX_PATH": str(folder), "HOME": str(home), "SCAN_PROC_ROOT": proc},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0
        return next(line for line in result.stdout.splitlines() if line.startswith("state "))

    first = state()
    again = state()
    (folder / "tool").write_text("a longer text\n")
    changed = state()

    assert marker.exists()
    assert first == again
    assert first != changed


def test_the_clean_shell_keeps_the_box_locale() -> None:
    """tar and ps run in the clean shell, and they must read file names and dates in the box shell's locale."""
    box = LocalSubprocessRuntime().open(replace(_SPEC, env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}))
    try:
        result = box.run(_clean('printf "%s|%s" "$LANG" "$LC_ALL"'), timeout_seconds=30)
    finally:
        box.close()

    assert result.stdout == "C.UTF-8|C.UTF-8"


def test_a_box_that_dies_mid_step_is_replaced_and_the_step_reruns_from_its_tree(
    trees: TreeStore, step_log: StepLogStore
) -> None:
    """The dead box drops; the retry opens a new box and starts from the last committed tree.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    try:
        _, committed = sandbox.run(_step(1), _tree(trees, {"a.txt": "one\n"}), "echo two > b.txt", [])
        runtime.boxes[0].dead = True
        with pytest.raises(BoxFailureError):
            sandbox.run(_step(2), committed, "echo three > c.txt", [])
        result, _ = sandbox.run(_step(2), committed, "ls", [])
    finally:
        candidates.close()
        metrics.close()

    assert len(runtime.boxes) == 2
    assert result.stdout == "a.txt\nb.txt\n"


def test_a_score_never_sees_the_files_or_processes_of_an_earlier_score(
    trees: TreeStore, step_log: StepLogStore, tmp_path: Path
) -> None:
    """Each score gets a new box, so a stray TMPDIR file or background process of the last score stays out.

    Args:
        trees: The tree store.
        step_log: The step log.
        tmp_path: Scratch folder; it holds the metric boxes' TMPDIR folders.
    """
    tmp_root = tmp_path / "box-tmp"
    tmp_root.mkdir()
    sandbox, _, metric_runtime, candidates, metrics = _sandbox(trees, step_log, _Runtime(tmp_root=tmp_root))
    metric = _tree(trees, {"score.sh": "echo 1.0\n"})
    # The metric fails when TMPDIR or the working directory holds a file from an earlier score. Then it
    # leaves a file in each and a bounded background loop that keeps writing to TMPDIR after it ends.
    command = (
        'sleep 0.3; test -z "$(ls -A "$TMPDIR")" || exit 1;'
        ' test -z "$(find . -type f ! -path "./outputs/*" ! -path "./metric/*")" || exit 1;'
        ' echo seen > left.txt; echo seen > "$TMPDIR/left.txt";'
        ' (for i in 1 2 3 4 5 6 7 8 9 10; do echo seen > "$TMPDIR/loop.txt"; sleep 0.2; done) >/dev/null 2>&1 &'
        " sh metric/score.sh"
    )
    try:
        first = sandbox.score(_step(1), _tree(trees, {"answer.txt": "1\n"}), metric, command)
        second = sandbox.score(_step(2), _tree(trees, {"answer.txt": "2\n"}), metric, command)
    finally:
        candidates.close()
        metrics.close()

    assert (first, second) == (1.0, 1.0)
    assert len(metric_runtime.boxes) == 2


def test_the_metric_box_never_gets_candidate_code(trees: TreeStore, step_log: StepLogStore) -> None:
    """The metric box holds only the outputs tree and the metric tree, never the candidate tree.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    sandbox, _, _, candidates, metrics = _sandbox(trees, step_log)
    candidate = _tree(trees, {"solve.py": "print('secret')\n"})
    metric = _tree(trees, {"score.sh": "echo 1.0\n"})
    # The metric fails unless the box holds exactly the files of the two trees.
    command = (
        'test "$(find . -type f | LC_ALL=C sort | paste -s -d " " -)" = "./metric/score.sh ./outputs/answer.txt"'
        " || exit 1; sh metric/score.sh"
    )
    try:
        _, outputs = sandbox.run(_step(1), candidate, "rm solve.py; echo 1 > answer.txt", [])
        score = sandbox.score(_step(2), outputs, metric, command)
    finally:
        candidates.close()
        metrics.close()

    assert score == 1.0


def test_a_warm_lease_cannot_give_the_metric_boxes(trees: TreeStore, step_log: StepLogStore) -> None:
    """A warm metric box would keep the files and processes of an earlier score or candidate.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    candidates = RuntimeBoxLease(_on_both(_Runtime()), _SPEC, **_LIMITS)
    warm = RuntimeBoxLease(_on_both(_Runtime()), _SPEC, **_LIMITS)

    with pytest.raises(ValueError, match="keep_warm=False"):
        TreeSandbox(trees, step_log, candidates, warm)
    with pytest.raises(ValueError, match="keep_warm=False"):
        TreeSandbox(trees, step_log, candidates, candidates)


def test_a_box_session_refuses_a_lease_that_keeps_boxes_warm() -> None:
    """A session gives its box back with no process scan, so a warm box could keep a stray process for the next one."""
    runtime = _Runtime()
    lease = RuntimeBoxLease(_on_both(runtime), _SPEC, **_LIMITS)

    with pytest.raises(ValueError, match="does not keep boxes warm"):
        TreeSandbox.open_session(lease, "agent-1", [])

    assert runtime.boxes == []


@pytest.mark.parametrize("use_lease", [False, True])
def test_a_box_session_from_a_source_that_does_not_keep_boxes_warm_runs_and_closes_its_box(use_lease: bool) -> None:
    """A source that closes each box at release still gives a session, and the session's end closes the box.

    Args:
        use_lease: Use a lease with keep_warm=False; otherwise a source that opens a new box for each acquire.
    """
    runtime = _Runtime()
    source: RuntimeBoxSource | RuntimeBoxLease = (
        RuntimeBoxLease(_on_both(runtime), _SPEC, keep_warm=False, **_LIMITS)
        if use_lease
        else RuntimeBoxSource(runtime, _SPEC)  # type: ignore[arg-type]
    )

    with TreeSandbox.open_session(source, "agent-1", []) as session:
        result = session.run("echo hi", timeout_seconds=30)

    assert result.ok
    assert result.stdout == "hi\n"
    assert [box.closed for box in runtime.boxes] == [True]


@pytest.mark.parametrize(("source_kind", "scans"), [("fresh", 0), ("cold_lease", 0), ("warm_lease", 2)])
def test_only_a_warm_lease_scans_the_processes_of_a_step(
    trees: TreeStore, step_log: StepLogStore, source_kind: str, scans: int
) -> None:
    """A source that closes each box at release sends no process scan; a warm lease scans before and after the step.

    Args:
        trees: The tree store.
        step_log: The step log.
        source_kind: A source that opens a new box for each acquire, a lease with keep_warm=False, or a warm lease.
        scans: The number of process scans that the candidate box must get.
    """
    runtime = _Runtime()
    candidates: RuntimeBoxSource | RuntimeBoxLease = (
        RuntimeBoxSource(runtime, _SPEC)  # type: ignore[arg-type]
        if source_kind == "fresh"
        else RuntimeBoxLease(_on_both(runtime), _SPEC, keep_warm=source_kind == "warm_lease", **_LIMITS)
    )
    metrics = RuntimeBoxLease(_on_both(_Runtime()), _SPEC, keep_warm=False, **_LIMITS)
    sandbox = TreeSandbox(trees, step_log, candidates, metrics)
    try:
        result, _ = sandbox.run(_step(1), _tree(trees, {"a.txt": "one\n"}), "cat a.txt", [])
    finally:
        if isinstance(candidates, RuntimeBoxLease):
            candidates.close()
        metrics.close()

    assert result.stdout == "one\n"
    assert len(runtime.boxes) == 1
    assert len(runtime.boxes[0].scan_sudo) == scans


def test_a_lease_must_use_the_layer_registry(trees: TreeStore, step_log: StepLogStore) -> None:
    """The step records the grant list that the layer resolves, so the lease must resolve the same way.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    registry = {"api.example.com": {"Authorization": "x"}}

    with pytest.raises(ValueError, match="registry"):
        TreeSandbox(
            trees,
            step_log,
            RuntimeBoxLease(_on_both(_Runtime()), _SPEC, registry, **_LIMITS),
            _Provider().lease(keep_warm=False),
        )


def test_a_gpu_step_is_refused_before_any_box_opens(trees: TreeStore, step_log: StepLogStore) -> None:
    """No runtime here opens a GPU box, so a GPU grant is a denied grant, not a box without its GPU.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    try:
        with pytest.raises(GrantDeniedError, match="GPU"):
            sandbox.run(_step(1), _tree(trees, {}), "true", [Grant("gpu:a100")])
    finally:
        candidates.close()
        metrics.close()

    assert runtime.boxes == []


def test_a_step_with_a_host_that_is_not_text_is_a_denied_grant(trees: TreeStore, step_log: StepLogStore) -> None:
    """The step records a denied grant, not an AttributeError, and no box opens.

    Args:
        trees: The tree store.
        step_log: The step log.
    """
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    try:
        with pytest.raises(GrantDeniedError):
            sandbox.run(_step(1), _tree(trees, {}), "true", [Grant(7)])  # type: ignore[arg-type]
    finally:
        candidates.close()
        metrics.close()

    assert runtime.boxes == []


def test_a_refused_gpu_step_keeps_the_warm_box_and_uses_no_start() -> None:
    """The GPU refusal comes before the claim, so the agent's warm box stays and no start is counted."""
    runtime, clock = _Runtime(), _Clock()
    lease = RuntimeBoxLease(
        _on_both(runtime), _SPEC, vercel_cap=100, starts_per_minute=2, clock=clock, sleep=clock.sleep
    )
    try:
        box = lease.acquire("agent-1", [])
        lease.release(box, healthy=True)
        with pytest.raises(GrantError, match="GPU"):
            lease.acquire("agent-1", [Grant("gpu:a100")])
        again = lease.acquire("agent-1", [])
        lease.release(again, healthy=True)
        # The budget holds two starts: the first box and this one, with no wait.
        lease.release(lease.acquire("agent-2", []), healthy=True)
    finally:
        lease.close()

    assert again is box
    assert len(runtime.boxes) == 2
    assert clock.sleeps == []


def test_a_denied_grant_list_keeps_the_warm_box_and_uses_no_start() -> None:
    """A network grant with no registered credential is refused before the claim, like a GPU grant."""
    runtime, clock = _Runtime(), _Clock()
    lease = RuntimeBoxLease(
        _on_both(runtime), _SPEC, vercel_cap=100, starts_per_minute=2, clock=clock, sleep=clock.sleep
    )
    try:
        box = lease.acquire("agent-1", [])
        lease.release(box, healthy=True)
        with pytest.raises(GrantError):
            lease.acquire("agent-1", [Grant("api.example.com", credential=True)])
        again = lease.acquire("agent-1", [])
        lease.release(again, healthy=True)
        # The budget holds two starts: the first box and this one, with no wait.
        lease.release(lease.acquire("agent-2", []), healthy=True)
    finally:
        lease.close()

    assert again is box
    assert len(runtime.boxes) == 2
    assert clock.sleeps == []


@pytest.mark.parametrize("check", [1, SandboxInfraError("The box stopped.")])
def test_a_warm_box_that_fails_its_health_check_is_replaced(check: int | Exception) -> None:
    """The runtime check runs ``true`` in the warm box; a failed or broken run opens a new box.

    Args:
        check: The faked exit code of the health check, or the error it raises.
    """
    runtime = _Runtime()
    lease = RuntimeBoxLease(_on_both(runtime), _SPEC, **_LIMITS)
    try:
        lease.release(lease.acquire("agent-1", []), healthy=True)
        runtime.boxes[0].check = check
        lease.release(lease.acquire("agent-1", []), healthy=True)
    finally:
        lease.close()

    assert len(runtime.boxes) == 2
    assert runtime.boxes[0].closed


def test_a_budget_stop_in_the_health_check_reaches_the_caller() -> None:
    """A budget signal from the check stops the run instead of counting as a dead box, and the box closes."""
    runtime = _Runtime()
    lease = RuntimeBoxLease(_on_both(runtime), _SPEC, **_LIMITS)
    try:
        lease.release(lease.acquire("agent-1", []), healthy=True)
        runtime.boxes[0].check = BudgetReached()
        with pytest.raises(BudgetReached):
            lease.acquire("agent-1", [])
    finally:
        lease.close()

    assert len(runtime.boxes) == 1
    assert runtime.boxes[0].closed


def test_a_lease_needs_a_runtime_for_vercel_and_the_spill() -> None:
    """A missing runtime fails at once, not later as a box failure when Vercel reaches the cap."""
    runtime = _Runtime()

    with pytest.raises(ValueError, match="modal"):
        RuntimeBoxLease({VERCEL: runtime}, _SPEC, **_LIMITS)
    with pytest.raises(ValueError, match="vercel"):
        RuntimeBoxLease({MODAL: runtime}, _SPEC, **_LIMITS)
    with pytest.raises(ValueError, match="daytona"):
        RuntimeBoxLease(_on_both(runtime), _SPEC, spill="daytona", **_LIMITS)

    lease = RuntimeBoxLease({VERCEL: runtime, "daytona": runtime}, _SPEC, spill="daytona", **_LIMITS)
    assert lease.registry == {}
    lease.close()
    assert runtime.boxes == []


def test_a_runtime_lease_keeps_its_first_budget_error_over_a_later_budget_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every budget error stops the run, so a later BudgetReached does not replace a kept BudgetError.

    Args:
        monkeypatch: Replaces the parts that the test makes fail or records.
    """
    runtime = _Runtime()
    lease = RuntimeBoxLease(_on_both(runtime), _SPEC, **_LIMITS)
    boxes = [lease.acquire(agent, []) for agent in ("agent-1", "agent-2")]
    errors: list[BaseException] = [BudgetError("over budget"), BudgetReached("budget stopped")]
    for box, error in zip(runtime.boxes, errors, strict=True):

        def fail_close(close: Any = box.close, error: BaseException = error) -> None:
            """Close the local box, then fail as a settlement does.

            Args:
                close: The box's own close.
                error: The settlement's error.

            Raises:
                BaseException: ``error``, always.
            """
            close()
            raise error

        monkeypatch.setattr(box, "close", fail_close)
    for box in boxes:
        lease.release(box, healthy=True)

    with pytest.raises(BudgetError) as raised:
        lease.close()

    assert raised.value is errors[0]
    assert all(box.closed for box in runtime.boxes)

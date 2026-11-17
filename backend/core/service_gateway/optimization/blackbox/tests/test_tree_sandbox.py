"""Tests for running steps and metrics on tree ids in separate boxes (SKY-718)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetError
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached
from core.sandbox import trees as tree_helpers
from core.sandbox.errors import SandboxInfraError
from core.sandbox.grants import Grant
from core.sandbox.trees import (
    BoxFailureError,
    FenceLostError,
    GrantDeniedError,
    MetricFailedError,
    TreeRejectedError,
)
from core.storage.models import Base, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore

from .. import tree_sandbox
from ..sandbox import LocalSubprocessRuntime, SandboxSpec
from ..sandbox_contract import CommandResult, SandboxSession
from ..tree_sandbox import RuntimeBoxSource, StepRef, TreeSandbox

RUN = "job-ada"
_SPEC = SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",), operation_key="op")
_LIST_FILES = "find . \\( -type f -o -type l \\) | LC_ALL=C sort"


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
    """Yield a step log with a short flush window, since each call waits until its step is durable.

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


class _DyingSession:
    """Wrap a local box whose first agent command dies, as a box that stops mid-command."""

    def __init__(self, inner: SandboxSession) -> None:
        """Wrap the box.

        Args:
            inner: The local box.
        """
        self._inner = inner

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write files into the inner box.

        Args:
            files: Relative path to text.
        """
        self._inner.write_files(files)

    def run(self, command: str, **options: Any) -> CommandResult:
        """Raise on the agent command; run the others.

        Args:
            command: The shell command.
            **options: The run options.

        Returns:
            The inner box's result.

        Raises:
            SandboxInfraError: For the agent command.
        """
        if command.startswith("cd tree"):
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
        self._inner.close()


class _FailingTarSession(_DyingSession):
    """Wrap a local box whose pack ``tar`` writes its archive and then fails, as on an unreadable file."""

    def run(self, command: str, **options: Any) -> CommandResult:
        """Run the command; in the pack command, ``tar`` exits 2 after it writes.

        Args:
            command: The shell command.
            **options: The run options.

        Returns:
            The inner box's result.
        """
        if "tar -czf" in command:
            command = 'tar() { command tar "$@"; return 2; }; ' + command
        return self._inner.run(command, **options)


@dataclass
class _Runtime:
    """Open local boxes and record each spec; the first ``dying`` boxes die mid-command."""

    dying: int = 0
    failing_tar: bool = False
    specs: list[SandboxSpec] = field(default_factory=list)

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open one local box.

        Args:
            spec: The box spec.

        Returns:
            The box.
        """
        self.specs.append(spec)
        box = LocalSubprocessRuntime().open(spec)
        if self.dying:
            self.dying -= 1
            return _DyingSession(box)  # type: ignore[return-value]
        if self.failing_tar:
            return _FailingTarSession(box)  # type: ignore[return-value]
        return box


class _Source(RuntimeBoxSource):
    """A fresh-box source that records each release and the files of the box at release."""

    def __init__(self, runtime: _Runtime, registry: Mapping[str, Mapping[str, str]] | None = None) -> None:
        """Bind the source to the recording runtime.

        Args:
            runtime: Opens the boxes.
            registry: The capability registry.
        """
        super().__init__(runtime, _SPEC, registry)
        self.acquired: list[tuple[str, tuple[Grant, ...]]] = []
        self.released: list[bool] = []
        self.files: list[list[str]] = []

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> SandboxSession:
        """Record the call and open a box.

        Args:
            agent_id: The agent.
            grants: The grants.

        Returns:
            The box.
        """
        self.acquired.append((agent_id, tuple(grants)))
        return super().acquire(agent_id, grants)

    def release(self, box: SandboxSession, healthy: bool) -> None:
        """Record the box's files and health, then close it.

        Args:
            box: The box.
            healthy: Whether the box is still good.
        """
        if healthy:
            self.files.append(box.run(_LIST_FILES).stdout.split())
        self.released.append(healthy)
        super().release(box, healthy)


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


def _files(trees: TreeStore, tree_id: str) -> dict[str, bytes]:
    """Read a flat tree back as names and bytes.

    Args:
        trees: The tree store.
        tree_id: The tree id.

    Returns:
        Name to bytes.
    """
    return {entry.name: trees.get_blob(RUN, entry.object_id) or b"" for entry in trees.get_tree(RUN, tree_id) or []}


def _sandbox(
    trees: TreeStore, step_log: StepLogStore, runtime: _Runtime, **options: Any
) -> tuple[TreeSandbox, _Source, _Source]:
    """Build the layer over two recording sources that share one runtime.

    Args:
        trees: The tree store.
        step_log: The step log.
        runtime: The recording runtime.
        **options: Extra :class:`TreeSandbox` options.

    Returns:
        The layer, the candidate source and the metric source.
    """
    candidates, metrics = _Source(runtime), _Source(runtime)
    return TreeSandbox(trees, step_log, candidates, metrics, **options), candidates, metrics


def _step(seq: int = 1, generation: int = 1) -> StepRef:
    """Return one step of agent ``agent-1``.

    Args:
        seq: The step's place.
        generation: The run generation.

    Returns:
        The step.
    """
    return StepRef(run_id=RUN, agent_id="agent-1", seq=seq, execution_generation=generation)


def test_run_gives_the_output_and_the_new_tree_and_commits_one_step(trees: TreeStore, step_log: StepLogStore) -> None:
    """The command runs in the tree's folder; its file change is in the new tree and the step."""
    runtime = _Runtime()
    sandbox, candidates, _ = _sandbox(trees, step_log, runtime)
    start = _tree(trees, {"a.txt": "one\n"})

    result, new_tree = sandbox.run(_step(), start, "cat a.txt; echo two > b.txt", [Grant("api.example.com")])

    assert result.stdout == "one\n"
    assert _files(trees, new_tree) == {"a.txt": b"one\n", "b.txt": b"two\n"}
    step = step_log.get_step(f"{RUN}:agent-1:1")
    assert step is not None
    assert step["tree_id"] == new_tree
    assert step["body"]["command"] == "cat a.txt; echo two > b.txt"
    assert step["body"]["stdout"] == "one\n"
    assert candidates.acquired == [("agent-1", (Grant("api.example.com"),))]
    assert (runtime.specs[0].network_disabled, runtime.specs[0].allowed_hosts) == (False, ("api.example.com",))
    assert candidates.released == [True]


def test_a_failing_command_is_output_not_an_error(trees: TreeStore, step_log: StepLogStore) -> None:
    """A non-zero exit returns with the tree the command left."""
    sandbox, _, _ = _sandbox(trees, step_log, _Runtime())

    result, new_tree = sandbox.run(_step(), _tree(trees, {}), "echo x > out.txt; exit 3", [])

    assert result.exit_code == 3
    assert _files(trees, new_tree) == {"out.txt": b"x\n"}


def test_each_run_gets_a_fresh_box(trees: TreeStore, step_log: StepLogStore) -> None:
    """Nothing one call writes outside the tree reaches the next call."""
    runtime = _Runtime()
    sandbox, candidates, _ = _sandbox(trees, step_log, runtime)
    start = _tree(trees, {})

    sandbox.run(_step(1), start, "echo left > ../leftover.txt", [])
    result, _ = sandbox.run(_step(2), start, "cat ../leftover.txt", [])

    assert result.exit_code != 0
    assert len(runtime.specs) == 2
    assert candidates.released == [True, True]


def test_score_runs_the_metric_in_a_box_with_only_the_outputs_and_metric_trees(
    trees: TreeStore, step_log: StepLogStore
) -> None:
    """The metric box holds the two trees and no candidate file, and has no network."""
    runtime = _Runtime()
    sandbox, candidates, metrics = _sandbox(trees, step_log, runtime)
    candidate = _tree(trees, {"solver.py": "SECRET_CANDIDATE_CODE\n"})
    _, after = sandbox.run(_step(1), candidate, "echo 42 > answer.txt", [Grant("api.example.com")])
    outputs = _tree(trees, {"answer.txt": _files(trees, after)["answer.txt"].decode()})
    metric = _tree(trees, {"score.sh": 'if [ "$(cat outputs/answer.txt)" = 42 ]; then echo 1.0; else echo 0.0; fi\n'})

    value = sandbox.score(_step(2), outputs, metric, "sh metric/score.sh")

    assert value == 1.0
    assert metrics.files == [["./metric/score.sh", "./outputs/answer.txt"]]
    assert (runtime.specs[-1].network_disabled, runtime.specs[-1].allowed_hosts) == (True, ())
    assert metrics.acquired == [("agent-1", ())]
    assert len(candidates.acquired) == 1
    step = step_log.get_step(f"{RUN}:agent-1:2")
    assert step is not None
    assert (step["tree_id"], step["body"]["score"]) == (outputs, 1.0)


def test_a_metric_grant_opens_only_its_host(trees: TreeStore, step_log: StepLogStore) -> None:
    """The metric box gets network only when its grant list names a host."""
    runtime = _Runtime()
    sandbox, _, _ = _sandbox(trees, step_log, runtime)
    empty = _tree(trees, {})

    sandbox.score(_step(), empty, empty, "echo 0.5", [Grant("judge.example.com")])

    assert (runtime.specs[-1].network_disabled, runtime.specs[-1].allowed_hosts) == (False, ("judge.example.com",))


def test_a_metric_that_prints_no_number_is_a_metric_error(trees: TreeStore, step_log: StepLogStore) -> None:
    """A metric that fails gives a typed error that names the step, and no step."""
    sandbox, _, _ = _sandbox(trees, step_log, _Runtime())
    empty = _tree(trees, {})

    with pytest.raises(MetricFailedError) as raised:
        sandbox.score(_step(), empty, empty, "echo nope")

    assert raised.value.step_id == f"{RUN}:agent-1:1"
    assert step_log.get_step(f"{RUN}:agent-1:1") is None


@pytest.mark.parametrize("call", ["run", "score"])
def test_a_denied_grant_is_a_typed_error_and_opens_no_box(trees: TreeStore, step_log: StepLogStore, call: str) -> None:
    """A credential grant for a host outside the registry stops the step before any box opens."""
    runtime = _Runtime()
    sandbox, _, _ = _sandbox(trees, step_log, runtime)
    empty = _tree(trees, {})
    denied = [Grant("api.example.com", credential=True)]

    with pytest.raises(GrantDeniedError) as raised:
        if call == "run":
            sandbox.run(_step(), empty, "true", denied)
        else:
            sandbox.score(_step(), empty, empty, "echo 1", denied)

    assert raised.value.step_id == f"{RUN}:agent-1:1"
    assert runtime.specs == []


def test_a_lost_fence_is_a_typed_error_that_names_the_step(
    trees: TreeStore, step_log: StepLogStore, engine: Engine
) -> None:
    """After a newer generation takes the run, the agent's next step stops with a fence error."""
    sandbox, _, _ = _sandbox(trees, step_log, _Runtime())
    empty = _tree(trees, {})
    sandbox.run(_step(1), empty, "true", [])
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN).values(execution_generation=2))
        session.commit()
    step_log.flush()

    with pytest.raises(FenceLostError) as raised:
        sandbox.run(_step(2), empty, "true", [])

    assert raised.value.step_id == f"{RUN}:agent-1:2"


def test_a_box_that_dies_mid_command_is_a_box_failure_and_the_step_retries_on_a_new_box(
    trees: TreeStore, step_log: StepLogStore
) -> None:
    """The dead box goes back unhealthy; the same step then runs on a new box and commits once."""
    runtime = _Runtime(dying=1)
    sandbox, candidates, _ = _sandbox(trees, step_log, runtime)
    start = _tree(trees, {"a.txt": "one\n"})

    with pytest.raises(BoxFailureError) as raised:
        sandbox.run(_step(), start, "echo two > b.txt", [])
    assert raised.value.step_id == f"{RUN}:agent-1:1"
    assert step_log.get_step(f"{RUN}:agent-1:1") is None

    _, new_tree = sandbox.run(_step(), start, "echo two > b.txt", [])

    assert _files(trees, new_tree) == {"a.txt": b"one\n", "b.txt": b"two\n"}
    assert candidates.released == [False, True]
    assert len(runtime.specs) == 2
    step = step_log.get_step(f"{RUN}:agent-1:1")
    assert step is not None
    assert step["tree_id"] == new_tree


def test_a_box_that_does_not_open_is_a_box_failure(trees: TreeStore, step_log: StepLogStore) -> None:
    """A source error at acquire gives a box-failure error that names the step."""

    class _Broken:
        """Open no box."""

        def open(self, spec: SandboxSpec) -> SandboxSession:
            """Fail to open.

            Args:
                spec: The box spec.

            Raises:
                SandboxInfraError: Always.
            """
            raise SandboxInfraError("No capacity.")

    source = RuntimeBoxSource(_Broken(), _SPEC)  # type: ignore[arg-type]
    sandbox = TreeSandbox(trees, step_log, source, source)

    with pytest.raises(BoxFailureError) as raised:
        sandbox.run(_step(), _tree(trees, {}), "true", [])

    assert raised.value.step_id == f"{RUN}:agent-1:1"


class _FakeBox:
    """A box that raises a chosen error on its first file write, or answers every call."""

    def __init__(self, error: BaseException | None = None, read_back: str | None = None) -> None:
        """Set what the box does.

        Args:
            error: Raised by the first file write; ``None`` writes nothing and goes on.
            read_back: What every file read returns.
        """
        self.error = error
        self.read_back = read_back

    def write_files(self, files: Mapping[str, str]) -> None:
        """Raise the chosen error, if any.

        Args:
            files: Not used.

        Raises:
            BaseException: The chosen error.
        """
        if self.error is not None:
            raise self.error

    def run(self, command: str, **options: Any) -> CommandResult:
        """Answer every command with success.

        Args:
            command: Not used.
            **options: Not used.

        Returns:
            An empty successful result.
        """
        return CommandResult(exit_code=0, stdout="", stderr="")

    def read_file(self, path: str) -> str | None:
        """Return the chosen text.

        Args:
            path: Not used.

        Returns:
            The text.
        """
        return self.read_back

    def close(self) -> None:
        """Do nothing."""


class _FakeSource:
    """A box source that resolves no grants, gives one fake box and records each call."""

    def __init__(self, box: _FakeBox, release_error: BaseException | None = None) -> None:
        """Bind the source to its box.

        Args:
            box: The box every acquire gives.
            release_error: Raised by each release, after it is recorded.
        """
        self.box = box
        self.release_error = release_error
        self.acquired: list[str] = []
        self.released: list[bool] = []

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> SandboxSession:
        """Record the call and give the box.

        Args:
            agent_id: The agent.
            grants: Not checked.

        Returns:
            The box.
        """
        self.acquired.append(agent_id)
        return self.box  # type: ignore[return-value]

    def release(self, box: SandboxSession, healthy: bool) -> None:
        """Record the health, then raise the chosen error, if any.

        Args:
            box: The box.
            healthy: Whether the box is still good.

        Raises:
            BaseException: The chosen release error.
        """
        self.released.append(healthy)
        if self.release_error is not None:
            raise self.release_error


def _fake_sandbox(
    trees: TreeStore, step_log: StepLogStore, box: _FakeBox, release_error: BaseException | None = None
) -> tuple[TreeSandbox, _FakeSource]:
    """Build the layer with one fake source for both roles.

    Args:
        trees: The tree store.
        step_log: The step log.
        box: The fake box.
        release_error: Raised by each release.

    Returns:
        The layer and the source.
    """
    source = _FakeSource(box, release_error)
    return TreeSandbox(trees, step_log, source, source), source


def test_a_stale_worker_gets_a_fence_error_for_the_step_it_just_ran(
    trees: TreeStore, step_log: StepLogStore, engine: Engine
) -> None:
    """A step whose generation is no longer current never returns a normal result, even as the agent's first step."""
    sandbox, candidates, _ = _sandbox(trees, step_log, _Runtime())
    empty = _tree(trees, {})
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN).values(execution_generation=2))
        session.commit()

    with pytest.raises(FenceLostError) as raised:
        sandbox.run(_step(1), empty, "true", [])

    assert raised.value.step_id == f"{RUN}:agent-1:1"
    assert candidates.released == [True]


def test_a_cancel_inside_the_box_still_gives_the_box_back_unhealthy(trees: TreeStore, step_log: StepLogStore) -> None:
    """A ``BaseException`` such as ``KeyboardInterrupt`` goes up unchanged, after the release."""
    sandbox, source = _fake_sandbox(trees, step_log, _FakeBox(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        sandbox.run(_step(), _tree(trees, {}), "true", [])

    assert source.released == [False]


@pytest.mark.parametrize("error", [BudgetReached("stopped"), BudgetError("no room")])
def test_a_budget_stop_inside_the_box_gives_the_box_back_unhealthy_and_stays_a_budget_stop(
    trees: TreeStore, step_log: StepLogStore, error: BaseException
) -> None:
    """A budget stop is not a box failure to retry; ``BudgetError`` is an ordinary exception, so it needs its own case.

    Args:
        trees: The tree store.
        step_log: The step log.
        error: The budget stop.
    """
    sandbox, source = _fake_sandbox(trees, step_log, _FakeBox(error))

    with pytest.raises(type(error)):
        sandbox.run(_step(), _tree(trees, {}), "true", [])

    assert source.released == [False]


def test_a_release_error_does_not_replace_the_box_failure(trees: TreeStore, step_log: StepLogStore) -> None:
    """The caller gets the step's box failure, not the release's own error."""
    sandbox, source = _fake_sandbox(
        trees, step_log, _FakeBox(SandboxInfraError("The box stopped.")), RuntimeError("no close")
    )

    with pytest.raises(BoxFailureError) as raised:
        sandbox.run(_step(), _tree(trees, {}), "true", [])

    assert raised.value.step_id == f"{RUN}:agent-1:1"
    assert source.released == [False]


@pytest.mark.parametrize(
    "error",
    [
        BudgetReached("stopped"),
        BudgetError("no room"),
        UsagePendingError("pending"),
        UnpricedOperationError("unpriced"),
    ],
)
def test_a_budget_error_of_a_failed_box_release_replaces_the_box_failure(
    trees: TreeStore, step_log: StepLogStore, error: BaseException
) -> None:
    """Every budget error of an unhealthy release stops the caller, so a budget stop is never lost.

    Args:
        trees: The tree store.
        step_log: The step log.
        error: The budget error the release raises.
    """
    sandbox, source = _fake_sandbox(trees, step_log, _FakeBox(SandboxInfraError("The box stopped.")), error)

    with pytest.raises(type(error)):
        sandbox.run(_step(), _tree(trees, {}), "true", [])

    assert source.released == [False]


def test_a_pack_tar_that_fails_is_a_box_failure_not_a_partial_tree(trees: TreeStore, step_log: StepLogStore) -> None:
    """A tar that writes its archive and exits non-zero stops the step; the tree is not stored."""
    sandbox, candidates, _ = _sandbox(trees, step_log, _Runtime(failing_tar=True))

    with pytest.raises(BoxFailureError):
        sandbox.run(_step(), _tree(trees, {}), "echo data > a.txt", [])

    assert candidates.released == [False]
    assert step_log.get_step(f"{RUN}:agent-1:1") is None


@pytest.mark.parametrize("call", ["run", "score"])
def test_a_denied_grant_never_reaches_a_source_that_does_not_check_grants(
    trees: TreeStore, step_log: StepLogStore, call: str
) -> None:
    """The layer refuses the grants itself, before it asks any source for a box."""
    sandbox, source = _fake_sandbox(trees, step_log, _FakeBox())
    empty = _tree(trees, {})
    denied = [Grant("api.example.com:8443")]

    with pytest.raises(GrantDeniedError):
        if call == "run":
            sandbox.run(_step(), empty, "true", denied)
        else:
            sandbox.score(_step(), empty, empty, "echo 1", denied)

    assert source.acquired == []


@pytest.mark.parametrize("printed", ["nan", "inf", "-infinity"])
def test_a_metric_that_prints_a_non_finite_number_is_a_metric_error(
    trees: TreeStore, step_log: StepLogStore, printed: str
) -> None:
    """NaN or infinity with exit code 0 gives no score and commits no step.

    Args:
        trees: The tree store.
        step_log: The step log.
        printed: The last stdout line of the metric.
    """
    sandbox, _, _ = _sandbox(trees, step_log, _Runtime())
    empty = _tree(trees, {})

    with pytest.raises(MetricFailedError) as raised:
        sandbox.score(_step(), empty, empty, f"echo {printed}")

    assert raised.value.step_id == f"{RUN}:agent-1:1"
    assert step_log.get_step(f"{RUN}:agent-1:1") is None


def test_a_metric_that_prints_a_number_and_fails_is_a_metric_error(trees: TreeStore, step_log: StepLogStore) -> None:
    """A non-zero exit gives no score, even after a number."""
    sandbox, _, _ = _sandbox(trees, step_log, _Runtime())
    empty = _tree(trees, {})

    with pytest.raises(MetricFailedError) as raised:
        sandbox.score(_step(), empty, empty, "echo 0.9; exit 1")

    assert raised.value.step_id == f"{RUN}:agent-1:1"


def test_a_candidate_link_to_a_metric_file_never_enters_the_metric_box(
    trees: TreeStore, step_log: StepLogStore
) -> None:
    """An outputs tree with a link, here to the metric's expected answer, stops the score before any metric box."""
    runtime = _Runtime()
    sandbox, _, metrics = _sandbox(trees, step_log, runtime)
    _, outputs = sandbox.run(_step(1), _tree(trees, {}), "ln -s ../metric/expected.txt answer.txt", [])
    metric = _tree(trees, {"expected.txt": "42\n"})

    with pytest.raises(TreeRejectedError) as raised:
        sandbox.score(_step(2), outputs, metric, "echo 1.0")

    assert raised.value.step_id == f"{RUN}:agent-1:2"
    assert metrics.acquired == []


@pytest.mark.parametrize("target", ["/proc/self/environ", "a.txt"])
def test_any_link_in_the_outputs_tree_is_refused(trees: TreeStore, step_log: StepLogStore, target: str) -> None:
    """An absolute link and a link that looks local are both refused: a folder link can move either one.

    Args:
        trees: The tree store.
        step_log: The step log.
        target: The link target.
    """
    sandbox, source = _fake_sandbox(trees, step_log, _FakeBox())
    outputs = trees.put_tree(RUN, [TreeEntry("answer.txt", "symlink", trees.put_blob(RUN, target.encode()))])

    with pytest.raises(TreeRejectedError):
        sandbox.score(_step(), outputs, _tree(trees, {}), "echo 1.0")

    assert source.acquired == []


def test_a_tree_over_the_box_size_limit_is_refused_in_the_box(
    trees: TreeStore, step_log: StepLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box checks the archive size, so the worker never reads it; the step does not retry.

    Args:
        trees: The tree store.
        step_log: The step log.
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setattr(tree_sandbox, "_MAX_OUT_FILE_BYTES", 10)
    sandbox, candidates, _ = _sandbox(trees, step_log, _Runtime())

    with pytest.raises(TreeRejectedError) as raised:
        sandbox.run(_step(), _tree(trees, {}), "echo data > a.txt", [])

    assert raised.value.step_id == f"{RUN}:agent-1:1"
    assert candidates.released == [False]
    assert step_log.get_step(f"{RUN}:agent-1:1") is None


def test_an_archive_that_grows_after_the_box_check_is_refused_by_the_worker(
    trees: TreeStore, step_log: StepLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidate controls the box, so the worker checks the size it read again.

    Args:
        trees: The tree store.
        step_log: The step log.
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setattr(tree_sandbox, "_MAX_OUT_FILE_BYTES", 10)
    sandbox, source = _fake_sandbox(trees, step_log, _FakeBox(read_back="A" * 11))

    with pytest.raises(TreeRejectedError):
        sandbox.run(_step(), _tree(trees, {}), "true", [])

    assert source.released == [False]


def test_a_box_tree_with_too_many_files_is_refused_not_retried(
    trees: TreeStore, step_log: StepLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad archive is a tree error that names the step, not a box failure.

    Args:
        trees: The tree store.
        step_log: The step log.
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setattr(tree_helpers, "MAX_TREE_MEMBERS", 3)
    sandbox, _, _ = _sandbox(trees, step_log, _Runtime())

    with pytest.raises(TreeRejectedError) as raised:
        sandbox.run(_step(), _tree(trees, {}), "touch a b c d e", [])

    assert not isinstance(raised.value, BoxFailureError)
    assert raised.value.step_id == f"{RUN}:agent-1:1"

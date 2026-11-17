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

from core.sandbox.errors import SandboxInfraError
from core.sandbox.grants import Grant
from core.sandbox.trees import BoxFailureError, FenceLostError, GrantDeniedError, MetricFailedError
from core.storage.models import Base, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore

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
    """Yield a step log that writes only on an explicit flush.

    Args:
        engine: The test engine.
        tmp_path: Scratch folder.

    Yields:
        The step log; it closes after the test.
    """
    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w1", flush_window_seconds=60.0)
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


@dataclass
class _Runtime:
    """Open local boxes and record each spec; the first ``dying`` boxes die mid-command."""

    dying: int = 0
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

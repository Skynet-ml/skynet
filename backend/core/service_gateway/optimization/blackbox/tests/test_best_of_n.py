"""Tests for repository Best-of-N on the worker engine loop (SKY-721).

The tests use a SQLite file, the local-filesystem object store, local
subprocess boxes and a fake model: no network and no paid call.
"""

from __future__ import annotations

import shlex
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.usage_tags import usage_scope
from core.storage.models import AgentStepModel, Base, BillingCustomerModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore
from core.storage.workspace_store import WorkspaceStore
from core.worker.actors import User
from core.worker.engine_loop import ROOT_AGENT_ID, EngineLoop

from .. import best_of_n
from ..best_of_n import BestOfNEngine, _repository_brief
from ..harness import GatewayConfig
from ..harness_bridge import hidden_hashes
from ..native_runtime import NativeOptions
from ..protocol import EngineContext, ModelReply, RepoSeed, Task
from ..sandbox import LocalSubprocessRuntime, SandboxSpec
from ..sandbox_contract import SandboxSession
from ..tree_sandbox import RuntimeBoxSource

RUN_ID = "run-1"
_SPEC = SandboxSpec(lifetime_seconds=60, allowed_hosts=(), operation_key="op")
_METRIC = "wc -c < outputs/answer.txt"
_CASES = [{"id": "a"}, {"id": "b"}]
_HIDDEN = "hidden-metric-value"
# The run's hidden paths; the loop and the seed must hide the same ones.
_HIDDEN_PATHS = ("metric_secret.txt",)
# One sample is one model step, then one apply command and one metric command for each case.
_SAMPLE = ["model"] + ["command", "command"] * len(_CASES)


class _WorkerDiedError(BaseException):
    """The worker dies in the middle of a model call: like a kill, no handler of ``Exception`` sees it."""


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a SQLite file engine with a funded wallet for ``alice`` and the running job ``run-1``.

    Args:
        tmp_path: Pytest's temporary folder.

    Yields:
        The engine.
    """
    database = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}", connect_args={"timeout": 30})
    Base.metadata.create_all(database)
    with Session(database) as session:
        session.add(
            BillingCustomerModel(
                username="alice", stripe_customer_id="local-alice", balance_cents=10_000, grant_remaining=0
            )
        )
        session.add(JobModel(optimization_id=RUN_ID, username="alice", status="running", execution_generation=1))
        session.commit()
    yield database
    database.dispose()


@pytest.fixture
def step_log(engine: Engine, tmp_path: Path) -> Iterator[StepLogStore]:
    """Yield a step log with a short flush window, since each step waits until it is durable.

    Args:
        engine: The database.
        tmp_path: Pytest's temporary folder.

    Yields:
        The step log.
    """
    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "worker-1", flush_window_seconds=0.02)
    yield store
    store.close()


@pytest.fixture
def trees(step_log: StepLogStore) -> TreeStore:
    """Return a tree store over the step log.

    Args:
        step_log: The step log.

    Returns:
        The tree store.
    """
    return TreeStore(step_log)


@pytest.fixture
def owner(engine: Engine) -> tuple[User, int]:
    """Return alice with a run budget on her workspace, and the budget's generation.

    Args:
        engine: The database.

    Returns:
        The user and the budget generation.
    """
    budgets = BudgetService(engine=engine)
    budget = budgets.create("alice", 1_000, idempotency_key="workspace-budget")
    budget = budgets.attach_to_job(budget.id, "alice", RUN_ID, expected_revision=budget.revision)
    alice = User("alice", engine=engine, budgets=budgets)
    WorkspaceStore(engine).set_budget(alice.workspace().id, budget.id)
    return alice, budget.generation


class _Boxes:
    """Count the boxes that the sources opened, and the ones that each agent has open now."""

    def __init__(self) -> None:
        """Start with no box."""
        self.lock = threading.Lock()
        self.open: Counter[str] = Counter()
        self.opened = 0


class _CountingSource(RuntimeBoxSource):
    """Open a local box for each acquire, and count it."""

    def __init__(self, boxes: _Boxes) -> None:
        """Bind the source to the shared counter.

        Args:
            boxes: The counter.
        """
        super().__init__(LocalSubprocessRuntime(), _SPEC)
        self._boxes = boxes
        self._agents: dict[int, str] = {}

    def acquire(self, agent_id: str, grants: Any) -> SandboxSession:
        """Open a box and count it.

        Args:
            agent_id: The agent that runs the step.
            grants: The step's grants.

        Returns:
            The box.
        """
        box = super().acquire(agent_id, grants)
        with self._boxes.lock:
            self._boxes.open[agent_id] += 1
            self._boxes.opened += 1
        self._agents[id(box)] = agent_id
        return box

    def release(self, box: SandboxSession, healthy: bool) -> None:
        """Close the box and count it as closed.

        Args:
            box: The box.
            healthy: Whether the step finished.
        """
        try:
            super().release(box, healthy)
        finally:
            with self._boxes.lock:
                self._boxes.open[self._agents.pop(id(box))] -= 1


class _Model:
    """A fake model: it gives its replies in order, and it records each prompt and the boxes open during each call."""

    def __init__(self, boxes: _Boxes, replies: list[str], *, die_at: int | None = None) -> None:
        """Bind the model to the box counter and its replies.

        Args:
            boxes: The counter.
            replies: The replies, one for each call.
            die_at: The index of the call in which the worker dies; ``None`` never dies.
        """
        self.boxes = boxes
        self.replies = list(replies)
        self.die_at = die_at
        self.calls = 0
        self.prompts: list[str] = []
        self.open_boxes: list[int] = []

    def __call__(self, prompt: str | list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ModelReply:
        """Answer one prompt.

        Args:
            prompt: The prompt messages.
            tools: Not used; Best-of-N sends no tools.

        Returns:
            The next reply and its cost in cents.

        Raises:
            _WorkerDiedError: In the call at ``die_at``.
        """
        with self.boxes.lock:
            self.open_boxes.append(sum(self.boxes.open.values()))
        self.prompts.append(str(prompt))
        if self.calls == self.die_at:
            raise _WorkerDiedError
        self.calls += 1
        return ModelReply(self.replies.pop(0), Decimal(1))


def _tree(trees: TreeStore, name: str, text: str) -> str:
    """Store one file as a tree.

    Args:
        trees: The tree store.
        name: The file name.
        text: The file text.

    Returns:
        The tree id.
    """
    return trees.put_tree(RUN_ID, [TreeEntry(name, "file", trees.put_blob(RUN_ID, text.encode()))])


def _repo(trees: TreeStore) -> str:
    """Store the repository seed: an editable answer, a readme and a hidden metric file.

    Args:
        trees: The tree store.

    Returns:
        The tree id.
    """
    files = {"answer.txt": "old answer", "README": "repo", "metric_secret.txt": _HIDDEN}
    return trees.put_tree(
        RUN_ID, [TreeEntry(name, "file", trees.put_blob(RUN_ID, text.encode())) for name, text in files.items()]
    )


def _scorer(repo_tree: str, metric_tree: str) -> Any:
    """Return a repository scorer: apply the patch in one box, and measure the result in a metric box.

    Args:
        repo_tree: The repository tree that each command starts from.
        metric_tree: The metric's tree.

    Returns:
        The scorer.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Score one patch on one case.

        Args:
            loop: The engine loop.
            candidate: The patch text.
            case: Not used.

        Returns:
            The length of the patched file and no side information.
        """
        result, tree_id = loop.run(repo_tree, f"printf %s {shlex.quote(str(candidate))} > answer.txt")
        assert result.exit_code == 0
        return loop.score(tree_id, metric_tree, _METRIC), {}

    return score


def _loop(
    owner: tuple[User, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    replies: list[str],
    *,
    generation: int,
    die_at: int | None = None,
    hidden: tuple[str, ...] = _HIDDEN_PATHS,
) -> tuple[EngineLoop, _Model, _Boxes]:
    """Start the run at a generation and build a loop over local boxes and the fake model.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        replies: The fake model's replies.
        generation: The job's execution generation.
        die_at: The model call in which the worker dies.
        hidden: The run's hidden paths, stored as production path-hash keys.

    Returns:
        The loop, its model and its box counter.
    """
    user, budget_generation = owner
    run = user.workspace().start_run(
        RUN_ID,
        step_log=step_log,
        execution_generation=generation,
        budget_generation=budget_generation,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=4,
    )
    boxes = _Boxes()
    model = _Model(boxes, replies, die_at=die_at)
    loop = EngineLoop(
        run,
        engine=engine,
        trees=trees,
        candidates=_CountingSource(boxes),
        metrics=_CountingSource(boxes),
        model=model,
        model_max_cents=2,
        run_dir=tmp_path / f"loop-{generation}",
        hidden_hashes=hidden_hashes(hidden),
    )
    return loop, model, boxes


def _context(loop: EngineLoop, repo_tree: str) -> EngineContext:
    """Return the loop's context for a repository run.

    The options carry no gateway: the loop's model step is the only model route a repository sample uses.
    The hidden metric file is also listed as editable, to show that the brief never reads a hidden file.
    Its key is the production form: the hash of the normalized path.

    Args:
        loop: The engine loop.
        repo_tree: The repository seed tree.

    Returns:
        The context.
    """
    repo = {"chunks": [], "editable_paths": ["answer.txt"], "readonly_paths": []}
    seed = RepoSeed(
        repo_tree,
        editable_paths=("answer.txt", "metric_secret.txt"),
        readonly_paths=("README",),
        hidden_hashes=hidden_hashes(_HIDDEN_PATHS),
    )
    return loop.context(
        native_options=NativeOptions("vercel", "model", GatewayConfig("", ""), 1.0, repo=repo), repo_seed=seed
    )


def _steps(engine: Engine, agent_id: str = ROOT_AGENT_ID) -> list[tuple[str, int]]:
    """Return an agent's committed steps.

    Args:
        engine: The database.
        agent_id: The agent.

    Returns:
        ``(kind, execution_generation)`` for each step, in order.
    """
    with Session(engine) as session:
        rows = session.execute(
            select(AgentStepModel.kind, AgentStepModel.execution_generation)
            .where(AgentStepModel.run_id == RUN_ID, AgentStepModel.agent_id == agent_id)
            .order_by(AgentStepModel.seq)
        )
        return [(row.kind, row.execution_generation) for row in rows]


def _new_generation(engine: Engine) -> None:
    """Move the job to generation 2, as the orphan sweep does after a worker dies.

    Args:
        engine: The database.
    """
    with Session(engine) as session:
        session.execute(update(JobModel).where(JobModel.optimization_id == RUN_ID).values(execution_generation=2))
        session.commit()


def test_a_repository_run_opens_boxes_only_for_commands_and_commits_one_step_each(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Sample two patches on the loop: no box during a model call, and one step for each reply and command."""
    repo_tree, metric_tree = _repo(trees), _tree(trees, "metric.txt", "len")
    loop, model, boxes = _loop(
        owner, engine, step_log, trees, tmp_path, ["```\naa\n```", "```\nbbbb\n```"], generation=1
    )

    result = BestOfNEngine().run(
        Task("", cases=list(_CASES)),
        loop.server(_scorer(repo_tree, metric_tree), max_evals=4),
        _context(loop, repo_tree),
    )

    assert (result.best_candidate, result.best_score) == ("bbbb", 4.0)
    assert model.calls == 2
    assert model.open_boxes == [0, 0]
    assert boxes.opened == 8
    assert not +boxes.open
    assert [kind for kind, _ in _steps(engine)] == _SAMPLE * 2


def test_a_killed_repository_run_resumes_and_repeats_no_committed_sample(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Kill the worker in the second sample's model call, then resume on a new generation."""
    repo_tree, metric_tree = _repo(trees), _tree(trees, "metric.txt", "len")
    first, first_model, _ = _loop(
        owner, engine, step_log, trees, tmp_path, ["```\naa\n```", "```\nunused\n```"], generation=1, die_at=1
    )
    with pytest.raises(_WorkerDiedError):
        BestOfNEngine().run(
            Task("", cases=list(_CASES)),
            first.server(_scorer(repo_tree, metric_tree), max_evals=4),
            _context(first, repo_tree),
        )
    assert first_model.calls == 1
    assert [kind for kind, _ in _steps(engine)] == _SAMPLE

    _new_generation(engine)
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, ["```\nbbbb\n```"], generation=2)
    result = BestOfNEngine().run(
        Task("", cases=list(_CASES)),
        loop.server(_scorer(repo_tree, metric_tree), max_evals=4),
        _context(loop, repo_tree),
    )

    assert (result.best_candidate, result.best_score) == ("bbbb", 4.0)
    # The first sample's reply and commands come back from the log: one new model call, and boxes only for it.
    assert model.calls == 1
    assert model.open_boxes == [0]
    assert boxes.opened == 4
    assert not +boxes.open
    steps = _steps(engine)
    assert [kind for kind, _ in steps] == _SAMPLE * 2
    assert [generation for _, generation in steps] == [1] * len(_SAMPLE) + [2] * len(_SAMPLE)


def test_a_repository_sample_prompt_shows_the_editable_files_and_never_a_hidden_one(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The brief reads the seed with no box: the diff form, the path rules and the editable file's text."""
    repo_tree, metric_tree = _repo(trees), _tree(trees, "metric.txt", "len")
    loop, model, boxes = _loop(owner, engine, step_log, trees, tmp_path, ["```\naa\n```"], generation=1)

    BestOfNEngine().run(
        Task("", objective="longer answer", cases=list(_CASES)),
        loop.server(_scorer(repo_tree, metric_tree), max_evals=2),
        _context(loop, repo_tree),
    )

    [prompt] = model.prompts
    assert "longer answer" in prompt
    assert "unified diff" in prompt
    assert "You may change: `answer.txt`." in prompt
    assert "metric_secret.txt" not in prompt
    assert "Do not change: `README`." in prompt
    assert "old answer" in prompt
    assert _HIDDEN not in prompt
    assert model.open_boxes == [0]
    assert boxes.opened == 4


def test_a_best_of_n_lane_commits_its_model_steps_to_its_own_agent(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Upstream calls the model on a loopback server thread; the step still goes to the lane's agent."""
    repo_tree, metric_tree = _repo(trees), _tree(trees, "metric.txt", "len")
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, ["```\naa\n```"], generation=1)

    with usage_scope(lane="explore-best_of_n"):
        BestOfNEngine().run(
            Task("", cases=list(_CASES)),
            loop.server(_scorer(repo_tree, metric_tree), max_evals=2),
            _context(loop, repo_tree),
        )

    assert model.calls == 1
    assert [kind for kind, _ in _steps(engine, "explore-best_of_n")] == _SAMPLE
    assert _steps(engine) == []


@pytest.fixture
def brief(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> Callable[..., str]:
    """Return a function that builds a seed and its loop, and reads the brief through the loop's context.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.

    Returns:
        The brief function.
    """

    def build(
        files: dict[str, bytes],
        editable: tuple[str, ...],
        *,
        hidden: tuple[str, ...] = (),
        readonly: tuple[str, ...] = (),
        constrained: tuple[str, ...] = (),
        reads: list[str] | None = None,
    ) -> str:
        """Return the repository brief for a seed that holds ``files``.

        Args:
            files: File bytes by path.
            editable: The editable paths.
            hidden: The hidden paths, stored as production path-hash keys in the loop and the seed.
            readonly: The read-only paths.
            constrained: The constrained paths.
            reads: Collects each path the brief reads, when given.

        Returns:
            The brief text.
        """
        loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, [], generation=1, hidden=hidden)
        seed = RepoSeed(
            loop.put_files(files),
            editable_paths=editable,
            readonly_paths=readonly,
            constrained_paths=constrained,
            hidden_hashes=hidden_hashes(hidden),
        )
        ctx = loop.context(repo_seed=seed)
        read_file, list_files = ctx.read_file, ctx.list_files
        assert read_file is not None
        assert list_files is not None

        def read(tree_id: str, path: str) -> bytes | None:
            """Record one read, then read through the loop's context.

            Args:
                tree_id: The tree.
                path: The file path.

            Returns:
                The file bytes, or ``None``.
            """
            if reads is not None:
                reads.append(path)
            return read_file(tree_id, path)

        return _repository_brief(seed, read, list_files)

    return build


def test_a_repository_brief_walks_an_editable_root_and_skips_fixed_and_hidden_paths(
    brief: Callable[..., str],
) -> None:
    """Editable ``.`` shows every file in sorted order, except read-only, constrained and hidden ones."""
    files = {
        "src/b.py": b"B-SOURCE",
        "src/a.py": b"A-SOURCE",
        "README": b"README-TEXT",
        "tests/test_a.py": b"TEST-SOURCE",
        "config.yaml": b"CONFIG-TEXT",
        "eval/metric.py": b"METRIC-SECRET",
        "optimize.toml": b"OPTIMIZE-SECRET",
    }

    text = brief(files, (".",), hidden=("eval", "optimize.toml"), readonly=("tests",), constrained=("./config.yaml",))

    assert "You may change: the whole repository." in text
    assert "Do not change: `tests`, `config.yaml`." in text
    assert text.index("`README`") < text.index("`src/a.py`") < text.index("`src/b.py`")
    assert "A-SOURCE" in text
    assert "B-SOURCE" in text
    assert "README-TEXT" in text
    assert "TEST-SOURCE" not in text
    assert "CONFIG-TEXT" not in text
    assert "METRIC-SECRET" not in text
    assert "OPTIMIZE-SECRET" not in text


def test_a_repository_brief_never_shows_a_file_in_a_hidden_folder_or_through_a_dot_slash_path(
    brief: Callable[..., str],
) -> None:
    """A hidden folder hides each file in it, and a ``./`` path names the same hidden file."""
    files = {"eval/metric.py": b"METRIC-SECRET", "optimize.toml": b"OPTIMIZE-SECRET", "app.py": b"print('app')"}

    text = brief(files, ("eval/metric.py", "./optimize.toml", "./app.py"), hidden=("eval", "optimize.toml"))

    assert "METRIC-SECRET" not in text
    assert "OPTIMIZE-SECRET" not in text
    assert "print('app')" in text


def test_a_repository_brief_shows_no_binary_file(brief: Callable[..., str]) -> None:
    """A file that is not UTF-8, or that holds a NUL byte, gets a "not text" line and none of its bytes."""
    files = {"image.bin": b"\xff\xfeLATIN", "program": b"\x00\x01ELF-HEADER", "app.py": b"print('app')"}

    text = brief(files, ("image.bin", "program", "app.py"))

    assert "`image.bin` is not text, so it is not shown." in text
    assert "`program` is not text, so it is not shown." in text
    assert "LATIN" not in text
    assert "ELF-HEADER" not in text
    assert "print('app')" in text


def test_a_repository_brief_skips_a_file_larger_than_the_room_left(
    brief: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file larger than the room left gets a "no room" line, and a later smaller file still fits."""
    monkeypatch.setattr(best_of_n, "_BRIEF_MAX_BYTES", 150)
    files = {"first.txt": b"FIRST!", "large.txt": b"LARGE-FILE" * 20, "last.txt": b"END"}

    text = brief(files, ("first.txt", "large.txt", "last.txt"))

    assert "FIRST!" in text
    assert "`large.txt` is not shown: the brief has no room for it." in text
    assert "LARGE-FILE" not in text
    assert "END" in text


def test_a_repository_brief_names_no_hidden_path_in_its_path_rules(brief: Callable[..., str]) -> None:
    """A hidden editable or constrained path, or one inside a hidden folder, is not named in the path rules."""
    files = {"eval/metric.py": b"m", "optimize.toml": b"t", "src/app.py": b"print('app')"}

    text = brief(
        files,
        ("eval/metric.py", "src"),
        hidden=("eval", "optimize.toml"),
        readonly=("./eval/data",),
        constrained=("optimize.toml",),
    )

    assert "You may change: `src`." in text
    assert "Do not change" not in text
    assert "eval" not in text
    assert "optimize.toml" not in text
    assert "print('app')" in text


def test_a_repository_brief_counts_its_notices_and_stops_reading_when_the_room_is_spent(
    brief: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many binary files give a brief near the cap, one summary line, and no read after the room is spent."""
    monkeypatch.setattr(best_of_n, "_BRIEF_MAX_BYTES", 200)
    files = {f"bin/{index:03}": b"\x00" for index in range(100)}
    reads: list[str] = []

    text = brief(files, ("bin",), reads=reads)

    notices = text.count("is not text, so it is not shown.")
    assert 0 < notices == len(reads) < 10
    assert text.endswith(f"\n{100 - len(reads)} more files are not shown.")
    assert len(text) < 200 + 400


def test_a_repository_brief_fence_is_longer_than_any_backtick_run_in_the_file(brief: Callable[..., str]) -> None:
    """A file that holds a fence line cannot close the brief's fence early."""
    text = brief({"README.md": b"intro\n```\ncode\n````\nend"}, ("README.md",))

    assert "Current content of `README.md`:\n`````\nintro\n```\ncode\n````\nend\n`````" in text

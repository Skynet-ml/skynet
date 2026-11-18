"""Tests for repository Best-of-N on the worker engine loop (SKY-721).

The tests use a SQLite file, the local-filesystem object store, local
subprocess boxes and a fake model: no network and no paid call.
"""

from __future__ import annotations

import re
import shlex
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from gepa.oa.engines.best_of_n import _parse_candidate
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
from ..best_of_n import BestOfNEngine, _as_patch, _escape_backticks, _repository_brief, _unescape_backticks
from ..feedback import STR_CANDIDATE_KEY
from ..harness import GatewayConfig
from ..harness_bridge import hidden_hashes
from ..native_runtime import NativeOptions
from ..protocol import EngineContext, ModelReply, RepoSeed, Task
from ..repo_tree import apply_patch
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


def _store(trees: TreeStore, files: dict[str, bytes]) -> str:
    """Store files as a tree straight in the tree store, as the caller of a loop stores a seed.

    ``put_files`` refuses a hidden path, so a seed that holds one is written here.

    Args:
        trees: The tree store.
        files: The file bytes by ``/``-separated path.

    Returns:
        The tree id.
    """
    root: dict[str, Any] = {}
    for path, data in files.items():
        *folders, name = path.split("/")
        level = root
        for folder in folders:
            level = level.setdefault(folder, {})
        level[name] = data

    def put(level: dict[str, Any]) -> str:
        """Store one folder and the folders below it.

        Args:
            level: The folder's entries by name: a nested folder, or the file bytes.

        Returns:
            The folder's tree id.
        """
        return trees.put_tree(
            RUN_ID,
            [
                TreeEntry(name, "tree", put(value))
                if isinstance(value, dict)
                else TreeEntry(name, "file", trees.put_blob(RUN_ID, value))
                for name, value in level.items()
            ],
        )

    return put(root)


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
    """Sample two patches on the loop: no box during a model call, and one step for each reply and command.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    repo_tree, metric_tree = _repo(trees), _tree(trees, "metric.txt", "len")
    loop, model, boxes = _loop(
        owner, engine, step_log, trees, tmp_path, ["```\naa\n```", "```\nbbbb\n```"], generation=1
    )

    result = BestOfNEngine().run(
        Task("", cases=list(_CASES)),
        loop.server(_scorer(repo_tree, metric_tree), max_evals=4),
        _context(loop, repo_tree),
    )

    assert (result.best_candidate, result.best_score) == ("bbbb\n", 5.0)
    assert model.calls == 2
    assert model.open_boxes == [0, 0]
    assert boxes.opened == 8
    assert not +boxes.open
    assert [kind for kind, _ in _steps(engine)] == _SAMPLE * 2


def test_a_killed_repository_run_resumes_and_repeats_no_committed_sample(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Kill the worker in the second sample's model call, then resume on a new generation.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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

    assert (result.best_candidate, result.best_score) == ("bbbb\n", 5.0)
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
    """The brief reads the seed with no box: the diff form, the path rules and the editable file's text.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
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
    """Upstream calls the model on a loopback server thread; the step still goes to the lane's agent.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    repo_tree, metric_tree = _repo(trees), _tree(trees, "metric.txt", "len")
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, ["```\naa\n```"], generation=1)

    with usage_scope(lane="explore-best_of_n"):
        BestOfNEngine().run(
            Task("", cases=list(_CASES)),
            loop.server(_scorer(repo_tree, metric_tree), max_evals=2),
            _context(loop, repo_tree),
        )

    assert model.calls == 1
    assert [kind for kind, _ in _steps(engine, f"{ROOT_AGENT_ID}/explore-best_of_n")] == _SAMPLE
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
        ctx = loop.context(
            repo_seed=RepoSeed(
                _store(trees, files),
                editable_paths=editable,
                readonly_paths=readonly,
                constrained_paths=constrained,
                hidden_hashes=hidden_hashes(hidden),
            )
        )
        seed, read_file, list_files = ctx.repo_seed, ctx.read_file, ctx.list_files
        assert seed is not None
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
    """Editable ``.`` shows every file in sorted order, except read-only and hidden ones.

    Args:
        brief: Builds the brief for a seed.
    """
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
    assert "Do not change: `tests`." in text
    assert "You may change these paths only within the rules of the constraints file: `config.yaml`." in text
    assert text.index("`README`") < text.index("`config.yaml`:") < text.index("`src/a.py`") < text.index("`src/b.py`")
    assert "A-SOURCE" in text
    assert "B-SOURCE" in text
    assert "README-TEXT" in text
    assert "CONFIG-TEXT" in text
    assert "TEST-SOURCE" not in text
    assert "METRIC-SECRET" not in text
    assert "OPTIMIZE-SECRET" not in text


def test_a_repository_brief_lists_constrained_paths_on_their_own_line_and_shows_their_files(
    brief: Callable[..., str],
) -> None:
    """A constrained path gets its own sorted line and its content, and a hidden constrained path never shows.

    Args:
        brief: Builds the brief for a seed.
    """
    files = {
        "src/app.py": b"APP-SOURCE",
        "tests/test_app.py": b"TEST-SOURCE",
        "conf/b.yaml": b"CONF-B",
        "conf/a.yaml": b"CONF-A",
        "config.yaml": b"CONFIG-TEXT",
        "conf/secret.yaml": b"CONF-SECRET",
        "optimize.toml": b"OPTIMIZE-SECRET",
    }

    text = brief(
        files,
        ("src",),
        hidden=("conf/secret.yaml", "optimize.toml"),
        readonly=("tests",),
        constrained=("./config.yaml", "conf", "optimize.toml", "conf/secret.yaml"),
    )

    assert "Do not change: `tests`." in text
    assert "You may change these paths only within the rules of the constraints file: `conf`, `config.yaml`." in text
    assert text.index("`src/app.py`:") < text.index("`conf/a.yaml`:") < text.index("`conf/b.yaml`:")
    assert text.index("`conf/b.yaml`:") < text.index("`config.yaml`:")
    assert "CONF-A" in text
    assert "CONF-B" in text
    assert "CONFIG-TEXT" in text
    assert "APP-SOURCE" in text
    assert "TEST-SOURCE" not in text
    assert "secret" not in text
    assert "SECRET" not in text
    assert "optimize.toml" not in text


def test_a_repository_brief_never_shows_a_file_in_a_hidden_folder_or_through_a_dot_slash_path(
    brief: Callable[..., str],
) -> None:
    """A hidden folder hides each file in it, and a ``./`` path names the same hidden file.

    Args:
        brief: Builds the brief for a seed.
    """
    files = {"eval/metric.py": b"METRIC-SECRET", "optimize.toml": b"OPTIMIZE-SECRET", "app.py": b"print('app')"}

    text = brief(files, ("eval/metric.py", "./optimize.toml", "./app.py"), hidden=("eval", "optimize.toml"))

    assert "METRIC-SECRET" not in text
    assert "OPTIMIZE-SECRET" not in text
    assert "print('app')" in text


def test_a_repository_brief_shows_no_binary_file(brief: Callable[..., str]) -> None:
    """A file that is not UTF-8, or that holds a NUL byte, gets a "not text" line and none of its bytes.

    Args:
        brief: Builds the brief for a seed.
    """
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
    """A file larger than the room left gets a "no room" line, and a later smaller file still fits.

    Args:
        brief: Builds the brief for a seed.
        monkeypatch: Pytest's monkeypatch, to lower the brief's byte cap.
    """
    monkeypatch.setattr(best_of_n, "_BRIEF_MAX_BYTES", 150)
    files = {"first.txt": b"FIRST!", "large.txt": b"LARGE-FILE" * 20, "last.txt": b"END"}

    text = brief(files, ("first.txt", "large.txt", "last.txt"))

    assert "FIRST!" in text
    assert "`large.txt` is not shown: the brief has no room for it." in text
    assert "LARGE-FILE" not in text
    assert "END" in text


def test_a_repository_brief_names_no_hidden_path_in_its_path_rules(brief: Callable[..., str]) -> None:
    """A hidden editable or constrained path, or one inside a hidden folder, is not named in the path rules.

    Args:
        brief: Builds the brief for a seed.
    """
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
    """Many binary files give a brief near the cap, one summary line, and no read after the room is spent.

    Args:
        brief: Builds the brief for a seed.
        monkeypatch: Pytest's monkeypatch, to lower the brief's byte cap.
    """
    monkeypatch.setattr(best_of_n, "_BRIEF_MAX_BYTES", 200)
    files = {f"bin/{index:03}": b"\x00" for index in range(100)}
    reads: list[str] = []

    text = brief(files, ("bin",), reads=reads)

    notices = text.count("is not text, so it is not shown.")
    assert 0 < notices == len(reads) < 10
    assert text.endswith(f"\n{100 - len(reads)} more files are not shown.")
    # The fixed header lines and the path rules add about 400 bytes on top of the cap.
    assert len(text) < 200 + 600


def test_a_repository_brief_reads_no_file_past_its_read_cap(
    brief: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree entries carry no size, so the brief caps its reads and never reads a large file past the cap.

    Args:
        brief: Builds the brief for a seed.
        monkeypatch: Pytest's monkeypatch, to lower the brief's read cap.
    """
    monkeypatch.setattr(best_of_n, "_BRIEF_MAX_READS", 3)
    files = {f"bin/{index}": b"\x00" for index in range(3)} | {"bin/z-large": b"LARGE" * 100_000}
    reads: list[str] = []

    text = brief(files, ("bin",), reads=reads)

    assert reads == ["bin/0", "bin/1", "bin/2"]
    assert text.endswith("\n1 more files are not shown.")


class _UndecodableBytes(bytes):
    """File bytes that fail the test when the brief decodes them."""

    def decode(self, *args: Any, **kwargs: Any) -> str:
        """Fail the test: the brief must not decode a file that has no room.

        Args:
            *args: The decode arguments.
            **kwargs: The decode keyword arguments.

        Returns:
            Never returns.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("The brief decoded a file that has no room.")


def test_a_repository_brief_does_not_decode_a_file_larger_than_the_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file larger than the room left gets the "no room" line before any decode or escape.

    Args:
        monkeypatch: Pytest's monkeypatch, to lower the brief's byte cap.
    """
    monkeypatch.setattr(best_of_n, "_BRIEF_MAX_BYTES", 100)
    files: dict[str, bytes] = {"big.txt": _UndecodableBytes(b"x" * 101), "small.txt": b"ok"}
    seed = RepoSeed("tree", editable_paths=("big.txt", "small.txt"))

    text = _repository_brief(seed, lambda _tree, path: files.get(path), lambda _tree, _path: None)

    assert "`big.txt` is not shown: the brief has no room for it." in text
    assert "Current content of `small.txt`:\n```\nok\n```" in text


def test_a_repository_brief_spells_each_backtick_run_as_a_token_inside_a_plain_fence(
    brief: Callable[..., str],
) -> None:
    """A file that holds 3- and 4-backtick fences shows them as tokens, so no fence in the brief is longer than 3.

    Args:
        brief: Builds the brief for a seed.
    """
    text = brief({"README.md": b"intro\n```python\ncode\n```\n````\nend `x` ``y``"}, ("README.md",))

    assert (
        "Current content of `README.md`:\n```\nintro\n<<3 backticks>>python\ncode\n<<3 backticks>>\n"
        "<<4 backticks>>\nend `x` ``y``\n```"
    ) in text
    assert "````" not in text


# A diff that edits fenced code in README.md: a 3-backtick block with a tag, and a 4-backtick fence.
_README_DIFF = (
    "--- a/README.md\n+++ b/README.md\n@@ -1,6 +1,7 @@\n intro\n ```python\n code\n+more code\n ```\n ````\n end"
)


def test_upstream_cuts_a_raw_diff_at_its_first_backtick_run() -> None:
    """Pin the upstream parser that the token form works around: a raw fenced diff keeps only its header."""
    assert (
        _parse_candidate(f"```diff\n{_README_DIFF}\n```")
        == "--- a/README.md\n+++ b/README.md\n@@ -1,6 +1,7 @@\n intro\n "
    )


def test_a_repository_sample_with_backtick_tokens_reaches_the_scorer_as_the_whole_diff(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The model spells 3- and 4-backtick runs as tokens; the scorer, the tree and the result get the whole diff.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    repo_tree = _repo(trees)
    reply = _README_DIFF.replace("````", "<<4 backticks>>").replace("```", "<<3 backticks>>")
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, [f"```diff\n{reply}\n```"], generation=1)
    scored: list[tuple[Any, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    ctx = _context(loop, repo_tree)
    ctx.progress_callback = lambda name, payload: events.append((name, payload))

    result = BestOfNEngine().run(Task("", cases=list(_CASES)), loop.server(_recording(scored), max_evals=2), ctx)

    patch = _README_DIFF + "\n"
    assert scored == [(patch, case) for case in _CASES]
    assert result.best_candidate == patch
    assert [payload["prompt"] for name, payload in events if name == "candidate"] == [{STR_CANDIDATE_KEY: patch}]


def test_a_repository_sample_and_its_result_apply_to_a_real_tree(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """Upstream strips the reply's final newline; the scorer and the result still get a diff that ``git apply`` takes.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    repo_tree = _repo(trees)
    loop, _, _ = _loop(
        owner, engine, step_log, trees, tmp_path, [f"```diff\n{_escape_backticks(_README_DIFF)}\n```"], generation=1
    )
    scored: list[tuple[Any, Any]] = []

    result = BestOfNEngine().run(
        Task("", cases=list(_CASES)), loop.server(_recording(scored), max_evals=2), _context(loop, repo_tree)
    )

    readme = "intro\n```python\ncode\n```\n````\nend\n"
    for index, patch in enumerate([candidate for candidate, _ in scored] + [result.best_candidate]):
        checkout = tmp_path / f"checkout-{index}"
        checkout.mkdir()
        (checkout / "README.md").write_text(readme)
        apply_patch(checkout, patch)
        assert (checkout / "README.md").read_text() == readme.replace("code\n", "code\nmore code\n")


@pytest.mark.parametrize("blanks", [1, 2])
@pytest.mark.parametrize("fenced", [True, False])
def test_a_diff_that_ends_with_empty_context_lines_still_applies_after_upstream_strips_them(
    blanks: int, fenced: bool, tmp_path: Path
) -> None:
    """Upstream strips the empty context lines that end the last hunk; the patch gets them back and applies.

    Args:
        blanks: How many empty context lines end the hunk.
        fenced: Whether the reply puts the diff in a fence, which upstream parses with another strip.
        tmp_path: Pytest's temporary folder.
    """
    readme = "a\nb\n" + "\n" * blanks
    size = 2 + blanks
    diff = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        f"@@ -1,{size} +1,{size} @@\n a\n-b\n+B\n" + "\n" * blanks
    )
    reply = f"```diff\n{diff}```" if fenced else diff
    sample = _parse_candidate(reply)
    assert sample is not None
    assert not sample.endswith("\n")

    for index, patch in enumerate([diff, _as_patch(sample)]):
        checkout = tmp_path / f"checkout-{index}"
        checkout.mkdir()
        (checkout / "README.md").write_text(readme)
        apply_patch(checkout, patch)
        assert (checkout / "README.md").read_text() == readme.replace("b", "B")
    assert _as_patch(sample) == diff
    assert _as_patch(diff) == diff


@pytest.mark.parametrize(("count", "padding"), [("17", 16), ("18", 0), ("999999999", 0), ("9999999999999", 0)])
def test_a_hunk_header_with_a_huge_count_gets_no_padding(count: str, padding: int) -> None:
    """The model writes the hunk header, so a count past what the strip can drop adds no line.

    Args:
        count: The old and new line count that the hunk header claims.
        padding: How many empty lines the patch may get back after its final newline.
    """
    sample = f"diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,{count} +1,{count} @@\n a"

    assert _as_patch(sample) == sample + "\n" + "\n" * padding


_HEAD = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"


@pytest.mark.parametrize(
    "patch",
    [
        _HEAD + "@@ -1,3 +1,4 @@\n a\n",
        _HEAD + "@@ -1,4 +1,3 @@\n a\n",
        _HEAD + "@@ -1,3 +1,3 @@\n a\ndiff --git a/x b/x\n",
        _HEAD + "@@ -1,3 +1,3 @@\n a\n" + _HEAD + "@@ -1,3 +1,3 @@\n a\n-b\n+B\nnot a diff line\n",
        _HEAD + "@@ -1,3 +1,3 @@\n-a\n\\ No newline at end of file\n+a\n",
    ],
)
def test_a_hunk_that_empty_lines_cannot_complete_gets_no_padding(patch: str) -> None:
    """A last hunk short of its header by unequal counts, or followed by a non-hunk line, stays as it is.

    Only empty context lines lower both counts by one, so unequal gaps or a line that no hunk holds
    mean that the strip did not cause the gap.

    Args:
        patch: A diff that ends with a newline.
    """
    assert _as_patch(patch) == patch


@pytest.mark.parametrize(
    ("old", "new", "hunk"),
    [
        (
            "x\n\nlast",
            "x\n\nlast\nmore\n",
            "@@ -1,3 +1,4 @@\n x\n \n-last\n\\ No newline at end of file\n+last\n+more\n",
        ),
        (
            "x\n\nlast\n",
            "x\n\nlast\nmore",
            "@@ -1,3 +1,4 @@\n x\n \n last\n+more\n\\ No newline at end of file\n",
        ),
    ],
)
def test_a_diff_with_a_no_newline_marker_comes_back_whole_and_applies(
    old: str, new: str, hunk: str, tmp_path: Path
) -> None:
    """A marker that ``git diff`` writes for a file with no final newline keeps the patch exact.

    Each hunk is the real ``git diff`` output for the two texts. The marker ends one side of the file,
    so no context line follows it and the strip takes only the final newline.

    Args:
        old: The README text before the change.
        new: The README text after the change.
        hunk: The ``git diff`` hunk from ``old`` to ``new``.
        tmp_path: Pytest's temporary folder.
    """
    diff = _HEAD + hunk
    sample = _parse_candidate(diff)
    assert sample is not None

    assert _as_patch(sample) == diff
    (tmp_path / "README.md").write_text(old)
    apply_patch(tmp_path, _as_patch(sample))
    assert (tmp_path / "README.md").read_text() == new


def test_a_repository_sample_reaches_the_validation_re_score_as_the_whole_diff(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With a validation split, the finalist re-score gets the real diff, and so does the validated result.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    repo_tree = _repo(trees)
    reply = _escape_backticks(_README_DIFF)
    replies = [f"```diff\n{reply}\n```"] * 2
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, replies, generation=1)
    scored: list[tuple[Any, Any]] = []
    valset = [{"id": "v"}]

    result = BestOfNEngine().run(
        Task("", cases=list(_CASES), valset=valset),
        loop.server(_recording(scored), max_evals=5),
        _context(loop, repo_tree),
    )

    patch = _README_DIFF + "\n"
    assert (patch, valset[0]) in scored
    assert {candidate for candidate, _ in scored} == {patch}
    assert result.metadata["selection_scope"] == "validation"
    assert result.best_candidate == patch


def test_a_repository_seed_comes_back_whole_and_never_breaks_the_prompt_fence(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """With no sample scored, the result is the seed with a final newline, and the prompt shows the seed encoded.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    seed = "keep <<<< and ``` and <<3 backticks>> here"
    repo_tree = _repo(trees)
    loop, model, _ = _loop(owner, engine, step_log, trees, tmp_path, ["```\naa\n```"], generation=1)

    # A metric error on every case leaves the one sample unscored, so upstream keeps its seed.
    result = BestOfNEngine().run(
        Task(seed, cases=list(_CASES)),
        loop.server(lambda loop, candidate, case: (None, {}), max_evals=2),
        _context(loop, repo_tree),
    )

    assert result.best_candidate == seed + "\n"
    assert result.best_score is None
    [prompt] = model.prompts
    assert _escape_backticks(seed) in prompt
    assert "``` and" not in prompt


def _recording(scored: list[tuple[Any, Any]]) -> Any:
    """Return a scorer that records each candidate and case it gets.

    Args:
        scored: The list that receives ``(candidate, case)`` for each scorer run.

    Returns:
        The scorer; it gives each run a fixed score and no side information.
    """

    def score(loop: EngineLoop, candidate: Any, case: Any) -> tuple[float | None, dict[str, Any]]:
        """Record the candidate and the case the scorer gets.

        Args:
            loop: Not used.
            candidate: The patch text.
            case: The case scored.

        Returns:
            A fixed score and no side information.
        """
        scored.append((candidate, case))
        return 1.0, {}

    return score


@pytest.mark.parametrize(
    "text",
    [
        "<<3 backticks>>",
        "x << y <<<< z <<<3 backticks>> <",
        "<```<<```<<<```<<<<````",
        "`" * 10_002 + "<<" + "`" * 10_000,
        "plain `a` ``b``",
    ],
)
def test_backtick_escaping_round_trips_exactly(text: str) -> None:
    """Escape then unescape gives back the exact text, also text that looks like a token, and leaves no fence.

    Args:
        text: The text to escape and unescape.
    """
    escaped = _escape_backticks(text)

    assert "```" not in escaped
    assert _unescape_backticks(escaped) == text


def test_a_backtick_run_longer_than_one_token_splits_into_tokens_that_decode() -> None:
    """A run of more than 99 backticks becomes several tokens within the bound, never a raw fence."""
    assert _escape_backticks("`" * 99) == "<<99 backticks>>"
    assert _escape_backticks("`" * 102) == "<<99 backticks>><<3 backticks>>"
    assert _escape_backticks("`" * 101) == "<<99 backticks>>``"


@pytest.mark.parametrize("length", [98, 99, 100, 101, 102, 198, 199, 200, 1_000, 30_001])
def test_a_long_backtick_run_round_trips_exactly_in_tokens_of_at_most_99(length: int) -> None:
    """A long run of backticks comes back exactly, and no token the encoder writes is above the bound.

    Args:
        length: The run's length.
    """
    text = "a" + "`" * length + "b"
    escaped = _escape_backticks(text)

    assert "```" not in escaped
    assert all(int(count) <= 99 for count in re.findall(r"<<(\d+) backticks>>", escaped))
    assert _unescape_backticks(escaped) == text


@pytest.mark.parametrize("token", ["<<100 backticks>>", "<<9999 backticks>>", "<<099 backticks>>"])
def test_a_reply_token_above_the_bound_stays_text(token: str) -> None:
    """A model token for more than 99 backticks is not expanded, so a short reply cannot decode to a huge diff.

    Args:
        token: A token above the bound.
    """
    assert _unescape_backticks(token) == token
    assert _as_patch(token * 3) == token * 3 + "\n"


def test_a_file_that_holds_a_literal_token_reaches_the_scorer_as_the_exact_diff(
    owner: tuple[User, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A diff whose context line is the literal text ``<<3 backticks>>`` keeps that text, not three backticks.

    Args:
        owner: Alice and her budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
    """
    diff = "--- a/doc.md\n+++ b/doc.md\n@@ -1,2 +1,3 @@\n <<3 backticks>> and a << b\n ```\n+new"
    repo_tree = _repo(trees)
    reply = _escape_backticks(diff)
    loop, _, _ = _loop(owner, engine, step_log, trees, tmp_path, [f"```diff\n{reply}\n```"], generation=1)
    scored: list[tuple[Any, Any]] = []

    result = BestOfNEngine().run(
        Task("", cases=list(_CASES)), loop.server(_recording(scored), max_evals=2), _context(loop, repo_tree)
    )

    assert scored == [(diff + "\n", case) for case in _CASES]
    assert result.best_candidate == diff + "\n"

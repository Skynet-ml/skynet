"""Tests for the production engine-loop host of a repository run (SKY-829).

The tests use a SQLite file, the local-filesystem object store, local
subprocess boxes, fake scorers and fake model replies: no network and no paid
call.
"""

from __future__ import annotations

import functools
import io
import json
import logging
import queue
import tarfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing import mcp_broker
from core.billing.budgets import AGENT_LEASE_COST_KIND, BudgetBoundExceededError, BudgetError, BudgetService
from core.billing.model_gateway import ROUTE_KEY
from core.billing.openrouter_quotes import price_text_request
from core.billing.operation_pricing import ChargePolicy, UnpricedOperationError
from core.billing.runtime import UsagePendingError
from core.constants import PROGRESS_LANE_STARTED
from core.exceptions import (
    DETERMINISTIC_FAILURE,
    INFRASTRUCTURE_INTERRUPTION,
    InfrastructureInterruptionError,
    ServiceError,
)
from core.models.blackbox import BlackboxBudget, BlackboxRunRequest
from core.models.tests.test_blackbox import _repo_request
from core.sandbox.trees import tar_to_tree
from core.service_gateway.optimization.blackbox import auto, service
from core.service_gateway.optimization.blackbox.protocol import (
    EngineContext,
    EvalServer,
    ModelReply,
    RepoSeed,
    Result,
    Task,
)
from core.service_gateway.optimization.blackbox.runner import DEFAULT_MAX_TOKENS
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxSource
from core.service_gateway.optimization.blackbox.upstream import AUTO_ENGINES
from core.storage.base import JobStore
from core.storage.models import (
    AgentStepModel,
    Base,
    BillingCustomerModel,
    ExecutionOperationModel,
    ExecutionUsageEvidenceModel,
    JobModel,
)
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeStore
from core.storage.workspace_store import WorkspaceStore
from core.worker import loop_host
from core.worker.actors import LEASE_COST_KIND, User
from core.worker.engine import BackgroundWorker, reset_worker_for_tests
from core.worker.engine_loop import (
    ROOT_AGENT_ID,
    EngineLoop,
    LoopInterruptedError,
    LoopStoppedError,
    ModelCallBilledError,
    StepDivergedError,
)
from core.worker.failure_events import failure_event
from core.worker.loop_host import (
    LOOP_HOST_KEY,
    LeaseModelClient,
    _LoopBinding,
    host_loop_job,
    run_loop_job,
    tree_patch,
)
from core.worker.tests import test_engine_process_job as process_job_tests
from core.worker.tests.conftest import FakeJobStore
from core.worker.tests.mocks import make_mp_context, make_result_event
from core.worker.tests.test_step_recovery import StepJobStore, _claim, _funded_run, _kill, make_sqlite_store

RUN_ID = "run-1"
_SPEC = SandboxSpec(lifetime_seconds=60, allowed_hosts=(), operation_key="op")
_FAKE_KEY = "sk-fake-managed-key-41c2"


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
def owner(engine: Engine) -> tuple[User, str, int]:
    """Return alice with a run budget on her workspace, the budget's id and its generation.

    Args:
        engine: The database.

    Returns:
        The user, the budget id and the budget generation.
    """
    budgets = BudgetService(engine=engine)
    budget = budgets.create("alice", 1_000, idempotency_key="workspace-budget")
    budget = budgets.attach_to_job(budget.id, "alice", RUN_ID, expected_revision=budget.revision)
    alice = User("alice", engine=engine, budgets=budgets)
    WorkspaceStore(engine).set_budget(alice.workspace().id, budget.id)
    return alice, budget.id, budget.generation


class _Model:
    """A fake model: it answers ``new <prompt>``, costs one cent, and can kill the worker in one call."""

    def __init__(self, die_at: int | None = None) -> None:
        """Set the call in which the worker dies.

        Args:
            die_at: The index of the call in which the worker dies; ``None`` never dies.
        """
        self.die_at = die_at
        self.prompts: list[str] = []

    def __call__(self, prompt: str | list[dict[str, Any]], tools: Any) -> ModelReply:
        """Answer one prompt.

        Args:
            prompt: The prompt text.
            tools: The tool definitions; not used.

        Returns:
            The reply and its cost in cents.

        Raises:
            _WorkerDiedError: In the call at ``die_at``.
        """
        if len(self.prompts) == self.die_at:
            raise _WorkerDiedError
        self.prompts.append(str(prompt))
        return ModelReply(f"new {prompt}", Decimal(1), [])


def _archive(files: dict[str, str]) -> bytes:
    """Pack files into a gzip tar archive.

    Args:
        files: The repository-relative path and text of each file.

    Returns:
        The archive.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _loop(
    owner: tuple[User, str, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    *,
    generation: int,
) -> EngineLoop:
    """Start the run at a generation and build a loop over local boxes and the fake model.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store.
        tmp_path: Pytest's temporary folder.
        generation: The job's execution generation.

    Returns:
        The loop.
    """
    user, _, budget_generation = owner
    run = user.workspace().start_run(
        RUN_ID,
        step_log=step_log,
        execution_generation=generation,
        budget_generation=budget_generation,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=4,
    )
    return EngineLoop(
        run,
        engine=engine,
        trees=trees,
        candidates=RuntimeBoxSource(LocalSubprocessRuntime(), _SPEC),
        metrics=RuntimeBoxSource(LocalSubprocessRuntime(), _SPEC),
        model=_Model(),
        model_max_cents=2,
        run_dir=tmp_path / f"loop-{generation}",
    )


def _steps(engine: Engine) -> list[tuple[str, int]]:
    """Return the root agent's committed steps.

    Args:
        engine: The database.

    Returns:
        ``(kind, execution_generation)`` for each step, in order.
    """
    with Session(engine) as session:
        rows = session.execute(
            select(AgentStepModel.kind, AgentStepModel.execution_generation)
            .where(AgentStepModel.run_id == RUN_ID, AgentStepModel.agent_id == ROOT_AGENT_ID)
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


def _seed(trees: TreeStore) -> RepoSeed:
    """Store a one-file repository and return its seed.

    Args:
        trees: The tree store.

    Returns:
        The seed.
    """
    return RepoSeed(tree_id=tar_to_tree(trees, RUN_ID, _archive({"src/app.py": "x = 1\n"})), editable_paths=("src",))


def test_an_evaluation_commits_once_and_a_resume_replays_it_with_no_work(
    owner: tuple[User, str, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The first generation runs the evaluation; the second gets the committed result and runs nothing.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    runs: list[int] = []

    def work() -> dict[str, Any]:
        """Run the fake evaluation once.

        Returns:
            Its result.
        """
        runs.append(1)
        return {"score": 0.5, "side_info": {"feedback": "ok"}}

    first = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    assert first.evaluate({"candidate": "p", "case": None}, work) == {"score": 0.5, "side_info": {"feedback": "ok"}}
    first.close()

    _new_generation(engine)
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=2)

    def no_work() -> dict[str, Any]:
        """Fail the test: a replay must not run the evaluation.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("the replay ran the evaluation")

    assert loop.evaluate({"candidate": "p", "case": None}, no_work) == {"score": 0.5, "side_info": {"feedback": "ok"}}
    loop.finish()
    assert runs == [1]
    assert _steps(engine) == [("command", 1)]


def test_an_evaluation_replay_with_another_key_diverges(
    owner: tuple[User, str, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A resume that evaluates another version at the same place gets no committed result.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    first = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    first.evaluate({"candidate": "p", "case": None}, lambda: {"score": 1.0})
    first.close()

    _new_generation(engine)
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(StepDivergedError):
        loop.evaluate({"candidate": "other", "case": None}, lambda: {"score": 0.0})


def test_an_evaluation_error_stops_the_loop_and_commits_nothing(
    owner: tuple[User, str, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An error of the evaluation is no result: the loop stops, keeps the stop and has no step.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    def broken() -> dict[str, Any]:
        """Fail as a dropped scorer connection does.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("the scorer dropped the call")

    with pytest.raises(LoopStoppedError, match="ConnectionError"):
        loop.evaluate({"candidate": "p", "case": None}, broken)
    with pytest.raises(LoopStoppedError):
        loop.evaluate({"candidate": "p", "case": None}, lambda: {"score": 1.0})
    assert _steps(engine) == []


def test_a_refused_version_commits_and_its_replay_raises_the_same_error_with_no_scorer_call(
    owner: tuple[User, str, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A 422 from the repository scorer is a final answer, so the step keeps it and a resume does not ask again.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    calls: list[tuple[str, Any]] = []

    def refuses(candidate: Any, case: Any = None) -> tuple[float | None, dict[str, Any]]:
        """Refuse every version as the repository scorer does for a bad patch.

        Args:
            candidate: The patch.
            case: The case.

        Raises:
            ServiceError: Always.
        """
        calls.append((candidate, case))
        raise ServiceError("repository scorer failed (HTTP 422): the patch does not apply")

    first = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _LoopBinding(first, trees, RUN_ID, _seed(trees)).scorer(refuses)
    with pytest.raises(ServiceError, match="HTTP 422") as live:
        scorer("bad patch", {"n": 1})
    first.close()

    _new_generation(engine)
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=2)
    with pytest.raises(ServiceError) as replayed:
        _LoopBinding(loop, trees, RUN_ID, _seed(trees)).scorer(refuses)("bad patch", {"n": 1})
    assert str(replayed.value) == str(live.value)
    assert calls == [("bad patch", {"n": 1})]


def _failing(error: BaseException) -> Any:
    """Return a scorer that always fails with one error.

    Args:
        error: The scorer's error.

    Returns:
        The scorer.
    """

    def fails(candidate: Any, case: Any = None) -> tuple[float | None, dict[str, Any]]:
        """Fail with the error.

        Args:
            candidate: The patch.
            case: The case.

        Raises:
            BaseException: Always, the error.
        """
        raise error

    return fails


def _status_error(status: int) -> ServiceError:
    """Return the relay client's error for an HTTP answer with no status in its text.

    Args:
        status: The answer's status.

    Returns:
        The error, with the HTTP error as its cause.
    """
    request = httpx.Request("POST", "http://127.0.0.1:9/evaluate")
    cause = httpx.HTTPStatusError("failed", request=request, response=httpx.Response(status, request=request))
    error = ServiceError("repository scorer failed")
    error.__cause__ = cause
    return error


def _dropped() -> ServiceError:
    """Return the relay client's error when the connection drops.

    Returns:
        The error, with the transport error as its cause.
    """
    error = ServiceError("repository scorer is unreachable")
    error.__cause__ = httpx.ConnectError("dropped")
    return error


@pytest.mark.parametrize(
    "error",
    [
        ServiceError("repository scorer failed (HTTP 502): bad gateway"),
        ServiceError("repository scorer failed (HTTP 429): slow down"),
        ServiceError("repository scorer failed (HTTP 408): timeout"),
        _status_error(503),
        _dropped(),
    ],
    ids=["server-error", "rate-limit", "timeout", "status-cause", "transport"],
)
def test_a_scorer_error_that_can_pass_on_a_new_try_requeues_the_run_and_commits_nothing(
    error: ServiceError,
    owner: tuple[User, str, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A server error, a rate limit or a dropped relay is not the version's score, so the run goes back to the queue.

    Args:
        error: The scorer's error.
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    server = EvalServer(_LoopBinding(loop, trees, RUN_ID, _seed(trees)).scorer(_failing(error)), max_evals=4)
    with pytest.raises(LoopInterruptedError) as stopped:
        server.evaluate("patch")
    assert failure_event(stopped.value)["failure_kind"] == INFRASTRUCTURE_INTERRUPTION
    assert _steps(engine) == []


@pytest.mark.parametrize(
    "error",
    [
        ServiceError("repository scorer failed (HTTP 401): bad token"),
        ServiceError("repository scorer failed (HTTP 404): no route"),
        _status_error(403),
        ServiceError("repository scorer failed"),
    ],
    ids=["auth", "not-found", "status-cause", "no-status"],
)
def test_a_scorer_error_that_fails_again_on_a_new_try_ends_the_run_and_commits_nothing(
    error: ServiceError,
    owner: tuple[User, str, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A broken route fails the same way on each try, so the run ends and does not loop in the queue.

    Args:
        error: The scorer's error.
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _LoopBinding(loop, trees, RUN_ID, _seed(trees)).scorer(_failing(error))
    with pytest.raises(LoopStoppedError) as stopped:
        scorer("patch")
    assert not isinstance(stopped.value, InfrastructureInterruptionError)
    assert failure_event(stopped.value)["failure_kind"] == DETERMINISTIC_FAILURE
    assert _steps(engine) == []


@pytest.mark.parametrize(
    "error",
    [_status_error(422), ServiceError("repository scorer failed (HTTP 400): the patch does not apply")],
    ids=["status-cause", "status-text"],
)
def test_a_scorer_refusal_commits_the_refusal(
    error: ServiceError,
    owner: tuple[User, str, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A version that the scorer refuses is refused on each try, so the step records the refusal.

    Args:
        error: The scorer's error.
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _LoopBinding(loop, trees, RUN_ID, _seed(trees)).scorer(_failing(error))
    with pytest.raises(ServiceError) as refused:
        scorer("patch")
    assert not isinstance(refused.value, LoopStoppedError)
    assert _steps(engine) == [("command", 1)]


def test_a_scorer_bug_stops_the_loop_and_commits_nothing(
    owner: tuple[User, str, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """An error that is not the scorer's answer is a bug: no step records it, and the run ends.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _LoopBinding(loop, trees, RUN_ID, _seed(trees)).scorer(_failing(ValueError("bug")))
    with pytest.raises(LoopStoppedError) as stopped:
        scorer("patch")
    assert not isinstance(stopped.value, InfrastructureInterruptionError)
    assert _steps(engine) == []


@pytest.mark.parametrize(
    ("error", "interrupted"),
    [
        (BudgetError("the budget refused"), False),
        (UnpricedOperationError("no price"), False),
        (UsagePendingError("a bill is pending"), True),
    ],
    ids=["budget", "unpriced", "usage-pending"],
)
def test_an_evaluation_budget_error_stops_the_loop_and_only_pending_usage_requeues(
    error: Exception,
    interrupted: bool,
    owner: tuple[User, str, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A refused budget or price ends the run; a pending bill settles later, so that run goes back to the queue.

    Args:
        error: The evaluation's error.
        interrupted: Whether the run must requeue.
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """
    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)

    def work() -> dict[str, Any]:
        """Fail with the parametrized error.

        Raises:
            Exception: Always, the error.
        """
        raise error

    with pytest.raises(LoopStoppedError) as stopped:
        loop.evaluate({"candidate": "patch"}, work)
    assert isinstance(stopped.value, InfrastructureInterruptionError) is interrupted
    assert _steps(engine) == []


def test_a_live_score_gives_the_side_info_in_the_form_of_its_replay(
    owner: tuple[User, str, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The replay reads the side info from JSON, so the live engine sees the same values and makes the same steps.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
    """

    def scores(candidate: Any, case: Any = None) -> tuple[float | None, dict[str, Any]]:
        """Score with side info that JSON changes.

        Args:
            candidate: The patch.
            case: The case.

        Returns:
            The score and its side info.
        """
        return 0.5, {"pair": (1, 2), "cost": Decimal("1.5")}

    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _LoopBinding(loop, trees, RUN_ID, _seed(trees)).scorer(scores)
    assert scorer("patch") == (0.5, {"pair": [1, 2], "cost": "1.5"})


def test_a_tree_version_reaches_the_scorer_as_a_patch_against_the_seed(
    owner: tuple[User, str, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine's tree id becomes the patch that the repository scorer takes, and the score comes back.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        step_log: The step log.
        trees: The tree store over the step log.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    # Git ignores inode and ctime here, as it does when Linux reuses the inode, so
    # the same-size edit stays hidden on every OS unless the stats are dropped (SKY-843).
    for index, (key, value) in enumerate((("core.checkStat", "minimal"), ("core.trustctime", "false"))):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    patches: list[str] = []

    def scores(candidate: Any, case: Any = None) -> tuple[float | None, dict[str, Any]]:
        """Record the patch and score it.

        Args:
            candidate: The patch.
            case: The case.

        Returns:
            The score and its feedback.
        """
        patches.append(candidate)
        return 0.75, {"feedback": "better"}

    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    binding = _LoopBinding(loop, trees, RUN_ID, _seed(trees))
    ctx = binding.context()
    assert ctx.put_files is not None
    assert ctx.repo_seed is not None
    tree = ctx.put_files({"src/app.py": b"x = 2\n"}, ctx.repo_seed.tree_id)

    assert binding.scorer(scores)(tree) == (0.75, {"feedback": "better"})
    assert binding.scorer(scores)({"program": ""}) == (0.75, {"feedback": "better"})
    [changed, empty] = patches
    assert "src/app.py" in changed
    assert "-x = 1" in changed
    assert "+x = 2" in changed
    assert empty == ""


def test_tree_patch_is_empty_for_the_same_tree(trees: TreeStore) -> None:
    """Two trees with the same files give no patch.

    Args:
        trees: The tree store over the step log.
    """
    seed = _seed(trees).tree_id

    assert tree_patch(trees, RUN_ID, seed, seed) == ""


_CATALOG = {
    "id": "openai/gpt-4o",
    "endpoints": [
        {
            "tag": "openai",
            "provider_name": "OpenAI",
            "context_length": 128000,
            "max_completion_tokens": 16384,
            "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
        }
    ],
}


def _openrouter(chat: Any, catalog: dict[str, Any] | None = None) -> Any:
    """Answer OpenRouter's catalog reads, and pass each chat call to a fake provider.

    Args:
        chat: The fake chat provider.
        catalog: The model's endpoint prices; :data:`_CATALOG` when ``None``.

    Returns:
        The fake transport handler.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Route one request.

        Args:
            request: The request.

        Returns:
            The catalog, the model list, or the chat provider's reply.
        """
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(200, json={"data": catalog or _CATALOG})
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-4o"}, {"id": "anthropic/claude"}]})
        return chat(request)

    return handler


def _managed_client(
    handler: Any, *, name: str = "openrouter/openai/gpt-4o", catalog: dict[str, Any] | None = None, **config: Any
) -> LeaseModelClient:
    """Build a managed client over a fake OpenRouter.

    Args:
        handler: The fake chat provider.
        name: The model name.
        catalog: The model's endpoint prices; :data:`_CATALOG` when ``None``.
        **config: The model config fields to add.

    Returns:
        The client.
    """
    data_policy = config.pop("data_policy", "deny")
    return LeaseModelClient(
        {"name": name, **config},
        payload_token_source=None,
        managed_key=_FAKE_KEY,
        data_policy=data_policy,
        client=httpx.Client(transport=httpx.MockTransport(_openrouter(handler, catalog))),
    )


def _reply(request: httpx.Request, cost: float = 0.002) -> httpx.Response:
    """Answer as OpenRouter does, with a usage receipt.

    Args:
        request: The request.
        cost: The cost in dollars.

    Returns:
        The reply.
    """
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {"cost": cost}},
    )


def test_a_managed_call_sends_the_request_and_gives_the_provider_cost() -> None:
    """A managed call goes to OpenRouter with the worker's key and its cost comes from the usage receipt."""
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Record the call and answer it.

        Args:
            request: The request.

        Returns:
            The reply.
        """
        requests.append(request)
        return _reply(request)

    reply = _managed_client(provider, temperature=0.2, extra={"reasoning_effort": "high"})("hello", None)

    assert reply.text == "hi"
    assert reply.cost_cents == ChargePolicy("managed_model").convert(loop_host.usage_charge({"cost": 0.002})).total
    assert reply.cost_cents > 0
    [request] = requests
    assert str(request.url) == loop_host.OPENROUTER_CHAT_URL
    assert request.headers["Authorization"] == f"Bearer {_FAKE_KEY}"
    body = json.loads(request.content)
    assert body == {
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.2,
        "reasoning_effort": "high",
        "max_tokens": DEFAULT_MAX_TOKENS,
        "provider": {
            "data_collection": "deny",
            "max_price": {"prompt": 2.5, "completion": 10.0, "request": 0.0, "image": 0.0},
            "only": ["openai"],
            "allow_fallbacks": False,
        },
        "usage": {"include": True},
    }


def test_a_managed_call_follows_the_owner_data_policy() -> None:
    """The owner's zero-retention setting reaches the provider routing of each call."""
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Record the call and answer it.

        Args:
            request: The request.

        Returns:
            The reply.
        """
        requests.append(request)
        return _reply(request)

    _managed_client(provider, data_policy="zdr")("hello", None)

    routing = json.loads(requests[0].content)["provider"]
    assert (routing["data_collection"], routing["zdr"]) == ("deny", True)


def test_a_data_policy_refusal_stops_the_loop_with_the_owner_reason() -> None:
    """No provider meets the setting, so every call fails: the error stops the loop and names the setting."""
    refusal = {"error": {"message": "No endpoints found matching your data policy (Free model training)"}}

    with pytest.raises(UnpricedOperationError, match="model data privacy setting"):
        _managed_client(lambda request: httpx.Response(404, json=refusal))("hello", None)


def test_a_short_model_name_resolves_to_its_full_slug() -> None:
    """An older run names its model with no provider; the catalog gives the one full slug that the price needs."""
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Record the call and answer it.

        Args:
            request: The request.

        Returns:
            The reply.
        """
        requests.append(request)
        return _reply(request)

    _managed_client(provider, name="gpt-4o")("hello", None)

    assert json.loads(requests[0].content)["model"] == "openai/gpt-4o"


def test_an_expensive_model_gets_the_largest_output_limit_that_fits_one_call() -> None:
    """A call that can cost more than one call's bound stops the loop, so the output limit keeps the bound."""
    expensive = {
        **_CATALOG,
        "endpoints": [{**_CATALOG["endpoints"][0], "pricing": {"prompt": "0.0000025", "completion": "0.001"}}],
    }
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Record the call and answer it.

        Args:
            request: The request.

        Returns:
            The reply.
        """
        requests.append(request)
        return _reply(request)

    _managed_client(provider, catalog=expensive)("hello", None)

    body = json.loads(requests[0].content)
    tokens = body["max_tokens"]
    assert 1 <= tokens < DEFAULT_MAX_TOKENS
    policy = ChargePolicy("managed_model")
    request = {"model": body["model"], "messages": body["messages"], "provider": {"data_collection": "deny"}}
    fits = price_text_request({**request, "max_tokens": tokens}, expensive, policy).quote.maximum.total
    above = price_text_request({**request, "max_tokens": tokens + 1}, expensive, policy).quote.maximum.total
    assert fits <= loop_host.MODEL_MAX_CENTS < above


def test_a_prompt_that_alone_passes_the_bound_stops_the_loop() -> None:
    """When even one output token passes one call's bound, no call can run."""
    costly = {
        **_CATALOG,
        "endpoints": [{**_CATALOG["endpoints"][0], "pricing": {"prompt": "1", "completion": "0.00001"}}],
    }

    with pytest.raises(UnpricedOperationError, match="prompt alone"):
        _managed_client(_reply, catalog=costly)("hello " * 50, None)


def test_a_managed_reply_with_no_cost_fails_the_call() -> None:
    """A reply that gives no complete cost cannot be billed, so the call fails."""
    reply = {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    with pytest.raises(ServiceError, match="no complete cost"):
        _managed_client(lambda request: httpx.Response(200, json=reply))("hello", None)


def test_a_billed_reply_that_cannot_be_read_gives_its_cost() -> None:
    """The provider billed the call, so the lease must pay its cost even when the reply has no message."""
    reply = {"choices": [], "usage": {"cost": 0.002}}

    with pytest.raises(ModelCallBilledError) as raised:
        _managed_client(lambda request: httpx.Response(200, json=reply))("hello", None)
    assert raised.value.cost_cents == ChargePolicy("managed_model").convert(Decimal("0.002")).total
    assert raised.value.cost_cents > 0


def test_a_body_with_no_json_error_message_adds_no_provider_text() -> None:
    """Only a JSON ``error.message`` gives provider text, so a plain body leaves the error with its type and status."""
    with pytest.raises(ServiceError, match=r"^The model call failed: HTTPStatusError \(HTTP 500\)$") as raised:
        _managed_client(lambda request: httpx.Response(500, text=f"key {_FAKE_KEY}"))("hello", None)
    assert _FAKE_KEY not in str(raised.value)


def test_a_string_error_body_gives_the_provider_text_with_no_key() -> None:
    """Some providers send ``{"error": "<text>"}``, so that string is the provider's message, with no key."""
    body = json.dumps({"error": f"Too  many\nrequests for {_FAKE_KEY}"}).encode()

    assert loop_host.provider_message(body, _FAKE_KEY) == "Too many requests for [key]"


def _waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record each retry wait of the model client, with no real sleep.

    Args:
        monkeypatch: The test's patcher.

    Returns:
        The list that gets each wait in seconds.
    """
    waits: list[float] = []
    monkeypatch.setattr(loop_host, "_sleep", waits.append)
    return waits


def _answers(*replies: httpx.Response | Exception) -> tuple[Any, list[httpx.Request]]:
    """Build a fake chat provider that gives each reply in turn.

    Args:
        *replies: The answers, or the transport errors to raise, in order.

    Returns:
        The provider and the list of the chat requests that it got.
    """
    requests: list[httpx.Request] = []
    queue_ = list(replies)

    def provider(request: httpx.Request) -> httpx.Response:
        """Record the call and give the next reply.

        Args:
            request: The request.

        Returns:
            The next reply.

        Raises:
            Exception: The next transport error.
        """
        requests.append(request)
        reply = queue_.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return provider, requests


def _rate_limited(retry_after: str | None = None) -> httpx.Response:
    """Answer as OpenRouter does when it rate limits a call.

    Args:
        retry_after: The ``Retry-After`` header, or ``None`` for no header.

    Returns:
        The 429 answer, whose message also echoes the key.
    """
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    error = {"error": {"message": f"Rate limit exceeded: free-models-per-min ({_FAKE_KEY})", "code": 429}}
    return httpx.Response(429, json=error, headers=headers)


def test_a_rate_limited_managed_call_tries_again_and_costs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """One 429 does not fail the call: a new try gets the reply, and the call costs one reply (SKY-924).

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, requests = _answers(_rate_limited(), _reply(cast(httpx.Request, None)))

    reply = _managed_client(provider)("hello", None)

    assert reply.text == "hi"
    assert reply.cost_cents == ChargePolicy("managed_model").convert(Decimal("0.002")).total
    assert len(requests) == 2
    assert waits == [loop_host._MODEL_BACKOFF_SECONDS[0]]


def test_a_managed_call_rate_limited_on_every_try_fails_with_the_provider_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the last try the error keeps the status and OpenRouter's message, with no key.

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, requests = _answers(*[_rate_limited() for _ in range(loop_host._MODEL_RETRIES + 1)])

    with pytest.raises(ServiceError, match=r"\(HTTP 429\): Rate limit exceeded: free-models-per-min") as raised:
        _managed_client(provider)("hello", None)

    assert _FAKE_KEY not in str(raised.value)
    assert len(requests) == loop_host._MODEL_RETRIES + 1
    assert waits == [5.0, 15.0, 40.0]


@pytest.mark.parametrize(
    ("retry_after", "wait"),
    [("7", 7.0), ("3600", loop_host._MODEL_RETRY_AFTER_CAP_SECONDS), ("Wed, 21 Oct 2026 07:28:00 GMT", 5.0)],
    ids=["seconds", "capped", "date-uses-backoff"],
)
def test_a_retry_honors_a_numeric_retry_after_up_to_its_cap(
    retry_after: str, wait: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait before a new try is the provider's own ``Retry-After``, but never longer than the cap.

    Args:
        retry_after: The provider's header.
        wait: The expected wait in seconds.
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, _ = _answers(_rate_limited(retry_after), _reply(cast(httpx.Request, None)))

    _managed_client(provider)("hello", None)

    assert waits == [wait]


_NOW = 1_732_400_000.0


def _openrouter_rate_limited(reset: object, retry_after: str | None = None) -> httpx.Response:
    """Answer as OpenRouter does when a free model's per-minute limit refuses a call.

    Args:
        reset: The ``X-RateLimit-Reset`` value in the body's ``error.metadata.headers``.
        retry_after: The ``Retry-After`` header, or ``None`` for no header.

    Returns:
        The 429 answer.
    """
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    limits = {"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}
    error = {"code": 429, "message": "Rate limit exceeded: free-models-per-min.", "metadata": {"headers": limits}}
    return httpx.Response(429, json={"error": error}, headers=headers)


@pytest.mark.parametrize(
    ("reset", "retry_after", "wait"),
    [
        (str(int((_NOW + 12) * 1000)), None, 12.0),
        (int((_NOW + 12) * 1000), None, 12.0),
        (str(int(_NOW + 12)), None, 12.0),
        (str(int((_NOW + 3600) * 1000)), None, loop_host._MODEL_RETRY_AFTER_CAP_SECONDS),
        (str(int((_NOW - 10) * 1000)), None, 5.0),
        ("soon", None, 5.0),
        ("nan", None, 5.0),
        (None, None, 5.0),
        (str(int((_NOW + 12) * 1000)), "7", 7.0),
    ],
    ids=[
        "future-ms",
        "future-ms-number",
        "future-seconds",
        "capped",
        "past-uses-backoff",
        "garbage-uses-backoff",
        "nan-uses-backoff",
        "null-uses-backoff",
        "retry-after-wins",
    ],
)
def test_a_retry_waits_for_the_rate_limit_reset_in_an_openrouter_body(
    reset: object, retry_after: str | None, wait: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``Retry-After``, the wait lasts until the body's reset, up to the cap; a bad or past reset uses backoff.

    Args:
        reset: The body's ``X-RateLimit-Reset`` value.
        retry_after: The provider's ``Retry-After`` header, or ``None``.
        wait: The expected wait in seconds.
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    monkeypatch.setattr(loop_host, "_wall", lambda: _NOW)
    provider, requests = _answers(_openrouter_rate_limited(reset, retry_after), _reply(cast(httpx.Request, None)))

    assert _managed_client(provider)("hello", None).text == "hi"
    assert (len(requests), waits) == (2, [wait])


def test_a_retryable_status_with_an_unreadable_event_stream_is_not_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """An event stream that is not UTF-8 can hide a bill, so the answer counts as billed and gets no new try.

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    busy = httpx.Response(503, content=b"data: \xff\xfe\n\n", headers={"content-type": "text/event-stream"})
    provider, requests = _answers(busy, _reply(cast(httpx.Request, None)))

    with pytest.raises(ServiceError, match=r"\(HTTP 503\)$"):
        _managed_client(provider)("hello", None)

    assert (len(requests), waits) == (1, [])


def test_a_client_error_is_not_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 gets the same answer on each try, so the call fails at once with the provider's reason.

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    refusal = httpx.Response(400, json={"error": {"message": "max_tokens is too large"}})
    provider, requests = _answers(refusal)

    with pytest.raises(ServiceError, match=r"\(HTTP 400\): max_tokens is too large$"):
        _managed_client(provider)("hello", None)

    assert (len(requests), waits) == (1, [])


def test_a_connection_error_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """No answer came back, so the provider billed nothing, and a new try is safe.

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, requests = _answers(httpx.ConnectError("refused"), _reply(cast(httpx.Request, None)))

    assert _managed_client(provider)("hello", None).text == "hi"
    assert (len(requests), waits) == (2, [5.0])


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("cut"),
        httpx.WriteError("broken pipe"),
        httpx.ReadError("reset"),
    ],
    ids=["read-timeout", "remote-protocol", "write", "read"],
)
def test_an_error_after_the_request_left_is_not_tried_again(
    error: httpx.HTTPError, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider may have got the request and billed it, so a new try could bill the reply twice.

    Args:
        error: The transport error after the request left.
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, requests = _answers(error, _reply(cast(httpx.Request, None)))

    with pytest.raises(ServiceError, match=rf"The model call failed: {type(error).__name__}$"):
        _managed_client(provider)("hello", None)

    assert (len(requests), waits) == (1, [])


@pytest.mark.parametrize("status", [502, 504])
def test_a_gateway_answer_that_can_follow_a_billed_call_is_not_tried_again(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 502 or 504 can come after the model ran, so the call fails at once with the provider's reason.

    Args:
        status: The gateway's status.
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, requests = _answers(httpx.Response(status, json={"error": {"message": "upstream   timed\nout"}}))

    with pytest.raises(ServiceError, match=rf"\(HTTP {status}\): upstream timed out$"):
        _managed_client(provider)("hello", None)

    assert (len(requests), waits) == (1, [])


def test_a_busy_service_answer_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 refuses the call before the model runs, so a new try is safe.

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    busy = httpx.Response(503, json={"error": {"message": "overloaded"}})
    provider, requests = _answers(busy, _reply(cast(httpx.Request, None)))

    assert _managed_client(provider)("hello", None).text == "hi"
    assert (len(requests), waits) == (2, [5.0])


def test_a_failed_call_logs_the_provider_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The engine loop keeps only the error's class, so the worker process log shows the provider's reason, with no key.

    Args:
        monkeypatch: The test's patcher.
        caplog: The log capture.
    """
    _waits(monkeypatch)
    provider, _ = _answers(*[_rate_limited() for _ in range(loop_host._MODEL_RETRIES + 1)])

    with caplog.at_level(logging.WARNING, logger=loop_host.logger.name), pytest.raises(ServiceError):
        _managed_client(provider)("hello", None)

    assert "(HTTP 429): Rate limit exceeded: free-models-per-min ([key])" in caplog.text
    assert _FAKE_KEY not in caplog.text


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("full")],
    ids=["connect", "connect-timeout", "pool-timeout"],
)
def test_a_call_with_no_answer_on_every_try_fails(error: httpx.HTTPError, monkeypatch: pytest.MonkeyPatch) -> None:
    """After the last try with no answer the call fails with the error's type, and makes no extra request.

    Args:
        error: The transport error before the request reached the provider.
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, requests = _answers(*[error for _ in range(loop_host._MODEL_RETRIES + 1)])

    with pytest.raises(ServiceError, match=rf"^The model call failed: {type(error).__name__}$"):
        _managed_client(provider)("hello", None)

    assert len(requests) == loop_host._MODEL_RETRIES + 1
    assert waits == [5.0, 15.0, 40.0]


@pytest.mark.parametrize(
    "body",
    [
        {"id": "gen-123", "error": {"message": "overloaded"}},
        {"error": {"message": "overloaded"}, "usage": {"prompt_tokens": 10, "cost": 0.001}},
    ],
    ids=["generation-id", "usage"],
)
def test_a_retryable_status_whose_body_shows_a_bill_is_not_tried_again(
    body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 or 503 that names a generation or reports usage can follow a billed call, so it gets no new try.

    Args:
        body: The provider's answer body.
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, requests = _answers(httpx.Response(503, json=body), _reply(cast(httpx.Request, None)))

    with pytest.raises(ServiceError, match=r"\(HTTP 503\): overloaded$"):
        _managed_client(provider)("hello", None)

    assert (len(requests), waits) == (1, [])


def test_no_new_try_starts_that_could_end_after_the_stall_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new try starts only when it can end, at its full timeout, within a fraction of the stall timeout.

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    monkeypatch.setattr(loop_host.settings, "job_stall_timeout_seconds", 1800.0)
    deadline = 1800.0 * loop_host._MODEL_RETRY_STALL_FRACTION
    # At the second check, a try after the 15 s wait could end 5 s after the deadline.
    ticks = iter([0.0, 10.0, deadline - loop_host.MODEL_TIMEOUT_SECONDS - 10.0])
    monkeypatch.setattr(loop_host, "_clock", lambda: next(ticks))
    provider, requests = _answers(*[_rate_limited() for _ in range(loop_host._MODEL_RETRIES + 1)])

    with pytest.raises(ServiceError, match=r"HTTP 429"):
        _managed_client(provider)("hello", None)

    assert (len(requests), waits) == (2, [5.0])


def test_a_call_with_the_stall_watchdog_off_has_no_retry_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the watchdog off, only the try count stops the retries, even after a long time.

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    monkeypatch.setattr(loop_host.settings, "job_stall_timeout_seconds", 0.0)
    monkeypatch.setattr(loop_host, "_clock", iter([0.0, 5000.0, 10000.0, 15000.0]).__next__)
    provider, requests = _answers(_rate_limited(), _rate_limited(), _rate_limited(), _reply(cast(httpx.Request, None)))

    assert _managed_client(provider)("hello", None).text == "hi"
    assert (len(requests), waits) == (4, [5.0, 15.0, 40.0])


@pytest.mark.parametrize(
    ("config", "source", "key", "message"),
    [
        ({"name": "m", "token_source": "subscription"}, None, _FAKE_KEY, "cannot call"),
        ({"name": "m"}, "managed", "", "no provider credential"),
        ({"name": "m", "token_source": "byok"}, None, _FAKE_KEY, "no provider credential"),
    ],
    ids=["subscription", "managed-no-key", "byok-no-key"],
)
def test_a_model_the_loop_cannot_bill_is_refused(
    config: dict[str, Any], source: str | None, key: str, message: str
) -> None:
    """Only a managed model with the worker's key, or a BYOK model with the user's key, runs on the loop.

    Args:
        config: The model config.
        source: The run's token source.
        key: The worker's managed key.
        message: The expected refusal.
    """
    with pytest.raises(ValueError, match=message):
        LeaseModelClient(config, payload_token_source=source, managed_key=key)


def test_a_byok_call_uses_the_user_key_and_costs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's provider bills a BYOK call, so the lease pays nothing for it.

    Args:
        monkeypatch: The test's patcher.
    """
    calls: list[dict[str, Any]] = []

    def completion(**request: Any) -> Any:
        """Answer as LiteLLM does.

        Args:
            **request: The call's arguments.

        Returns:
            The reply.
        """
        calls.append(request)
        message = SimpleNamespace(model_dump=lambda: {"role": "assistant", "content": "byok hi"})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(loop_host.litellm, "completion", completion)
    client = LeaseModelClient(
        {
            "name": "openrouter/openai/gpt-4o",
            "token_source": "byok",
            "extra": {"api_key": "user-key", "reasoning_effort": "medium"},
        },
        payload_token_source=None,
        managed_key="",
    )

    reply = client([{"role": "user", "content": "hello"}], None)

    assert (reply.text, reply.cost_cents) == ("byok hi", Decimal(0))
    [call] = calls
    assert call["api_key"] == "user-key"
    assert call["model"] == "openrouter/openai/gpt-4o"
    assert call["reasoning_effort"] == "medium"


def _custom_byok(**extra: Any) -> LeaseModelClient:
    """Build a BYOK client for the user's own OpenAI-style endpoint.

    Args:
        **extra: The model config's extra fields to add.

    Returns:
        The client.
    """
    return LeaseModelClient(
        {
            "name": "openai/my-model",
            "token_source": "byok",
            "extra": {"api_key": "user-key", "api_base": "https://llm.example.com/v1", **extra},
        },
        payload_token_source=None,
        managed_key="",
    )


def test_a_custom_byok_endpoint_on_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user names the host, so a name that resolves into the worker's network must not get a call.

    Args:
        monkeypatch: The test's patcher.
    """
    monkeypatch.setattr(
        mcp_broker.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("10.0.0.5", 443))]
    )

    with pytest.raises(ValueError, match="private custom provider endpoint"):
        _custom_byok()


def test_a_custom_byok_call_goes_to_the_checked_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each call uses the one address that the check accepted, so a new DNS answer cannot move it.

    Args:
        monkeypatch: The test's patcher.
    """
    pins: list[tuple[str, str]] = []
    requests: list[httpx.Request] = []

    def pinned(url: str, address: str) -> httpx.MockTransport:
        """Record the pin and answer as the endpoint does.

        Args:
            url: The endpoint.
            address: The checked address.

        Returns:
            The fake transport.
        """
        pins.append((url, address))

        def answer(request: httpx.Request) -> httpx.Response:
            """Record the call and answer it.

            Args:
                request: The request.

            Returns:
                The reply.
            """
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "own"}}]})

        return httpx.MockTransport(answer)

    monkeypatch.setattr(
        mcp_broker.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("93.184.216.34", 443))]
    )
    monkeypatch.setattr(loop_host, "_PinnedTransport", pinned)
    monkeypatch.setattr(loop_host.litellm, "completion", lambda **request: pytest.fail("LiteLLM got the call"))

    reply = _custom_byok(reasoning_effort="low")([{"role": "user", "content": "hello"}], None)

    assert (reply.text, reply.cost_cents) == ("own", Decimal(0))
    assert pins == [("https://llm.example.com/v1", "93.184.216.34")]
    [request] = requests
    assert str(request.url) == "https://llm.example.com/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer user-key"
    assert json.loads(request.content) == {
        "model": "my-model",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": "low",
    }


def test_a_custom_byok_call_tries_again_after_a_busy_service_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's own endpoint gets the same retries as OpenRouter (SKY-924).

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    reply = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "own"}}]})
    provider, requests = _answers(httpx.Response(503, headers={"Retry-After": "1"}), reply)
    monkeypatch.setattr(
        mcp_broker.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("93.184.216.34", 443))]
    )
    monkeypatch.setattr(loop_host, "_PinnedTransport", lambda url, address: httpx.MockTransport(provider))

    assert _custom_byok()([{"role": "user", "content": "hello"}], None).text == "own"
    assert (len(requests), waits) == (2, [1.0])


def test_a_custom_byok_request_that_cannot_be_encoded_fails_as_a_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request httpx cannot encode, such as a NaN value, fails as the documented ServiceError with no request.

    Args:
        monkeypatch: The test's patcher.
    """
    waits = _waits(monkeypatch)
    provider, requests = _answers(httpx.Response(200, json={}))
    monkeypatch.setattr(
        mcp_broker.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("93.184.216.34", 443))]
    )
    monkeypatch.setattr(loop_host, "_PinnedTransport", lambda url, address: httpx.MockTransport(provider))

    with pytest.raises(ServiceError, match="ValueError"):
        _custom_byok()._post("https://llm.example.com/v1/chat/completions", {"temperature": float("nan")})
    assert (len(requests), waits) == (0, [])


def _record_clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Client]:
    """Record every HTTP client that the loop host opens.

    Args:
        monkeypatch: The test's patcher.

    Returns:
        The list that gets each opened client.
    """
    opened: list[httpx.Client] = []
    real_client = httpx.Client

    def record(*args: Any, **kwargs: Any) -> httpx.Client:
        """Open a real client and remember it.

        Args:
            *args: The client's arguments.
            **kwargs: The client's keyword arguments.

        Returns:
            The client.
        """
        opened.append(real_client(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(loop_host.httpx, "Client", record)
    return opened


def test_close_closes_the_pinned_http_client_that_the_model_client_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom BYOK client opens only its pinned client, and close closes it.

    Args:
        monkeypatch: The test's patcher.
    """
    opened = _record_clients(monkeypatch)
    monkeypatch.setattr(
        mcp_broker.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("93.184.216.34", 443))]
    )

    _custom_byok().close()

    assert len(opened) == 1
    assert opened[0].is_closed


def test_a_refused_custom_byok_endpoint_leaves_no_http_client_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller gets no model client to close, so a refused endpoint must not leave an open client.

    Args:
        monkeypatch: The test's patcher.
    """
    opened = _record_clients(monkeypatch)
    monkeypatch.setattr(
        mcp_broker.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("10.0.0.5", 443))]
    )

    with pytest.raises(ValueError, match="private custom provider endpoint"):
        _custom_byok()

    assert all(client.is_closed for client in opened)


def test_a_failed_managed_slug_lookup_closes_the_http_client_that_it_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller gets no model client to close, so a failed slug lookup must close the client it opened.

    Args:
        monkeypatch: The test's patcher.
    """
    opened = _record_clients(monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> str:
        """Fail the slug lookup as an unreachable catalog does.

        Args:
            *args: The lookup's arguments.
            **kwargs: The lookup's keyword arguments.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("catalog down")

    monkeypatch.setattr(loop_host, "resolve_model_slug", fail)

    with pytest.raises(RuntimeError, match="catalog down"):
        LeaseModelClient({"name": "openai/gpt-4o"}, payload_token_source=None, managed_key=_FAKE_KEY)

    assert len(opened) == 1
    assert opened[0].is_closed


@pytest.mark.parametrize("fails", [False, True], ids=["finished", "host-raised"])
def test_the_loop_job_closes_its_model_client(monkeypatch: pytest.MonkeyPatch, fails: bool) -> None:
    """The child process closes the model client when the loop ends, also when the loop raises.

    Args:
        monkeypatch: The test's patcher.
        fails: Whether the loop raises.
    """
    closed: list[bool] = []

    class _Model:
        """A model client that records its close."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Take the real client's arguments.

            Args:
                *args: The real client's positional values.
                **kwargs: The real client's named values.
            """

        def close(self) -> None:
            """Record the close."""
            closed.append(True)

    def host(*args: Any, **kwargs: Any) -> None:
        """Stand in for the loop; raise when the case asks for it.

        Args:
            *args: The loop's arguments.
            **kwargs: The loop's keyword arguments.

        Raises:
            RuntimeError: When the case fails.
        """
        if fails:
            raise RuntimeError("the loop broke")

    monkeypatch.setattr(loop_host, "LeaseModelClient", _Model)
    monkeypatch.setattr(loop_host, "RemoteSandboxRuntime", lambda *args: object())
    monkeypatch.setattr(loop_host, "get_job_store", lambda: SimpleNamespace(engine=None))
    monkeypatch.setattr(loop_host, "S3ObjectStore", object)
    monkeypatch.setattr(loop_host, "host_loop_job", host)
    payload = {
        LOOP_HOST_KEY: {"reflection_model": {}},
        "_budget_gateway_descriptor": {"url": "http://127.0.0.1:9", "control_token": "control"},
    }

    if fails:
        with pytest.raises(RuntimeError, match="the loop broke"):
            run_loop_job(payload, "artifact", queue.Queue(), "spawn")
    else:
        run_loop_job(payload, "artifact", queue.Queue(), "spawn")

    assert closed == [True]


def test_close_keeps_a_given_http_client_open() -> None:
    """The caller owns a client that it gives, so the model client does not close it."""
    given = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    LeaseModelClient(
        {"name": "openai/gpt-4o", "token_source": "byok", "extra": {"api_key": "user-key"}},
        payload_token_source=None,
        managed_key="",
        client=given,
    ).close()

    assert not given.is_closed
    given.close()


def test_the_host_removes_the_run_folder_that_it_made(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host with no run folder makes a temporary one, and removes it also when the job fails to start.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    made = tmp_path / "made"

    def mkdtemp(prefix: str) -> str:
        """Make the run folder in the test's folder.

        Args:
            prefix: The folder's name prefix.

        Returns:
            The folder.
        """
        made.mkdir()
        (made / "file").write_text("x")
        return str(made)

    monkeypatch.setattr(loop_host.tempfile, "mkdtemp", mkdtemp)
    _, budget_id, budget_generation = owner
    events: queue.Queue = queue.Queue()

    host_loop_job(
        {},
        f"{RUN_ID}-g1",
        events,
        host={
            "run_id": RUN_ID,
            "username": "alice",
            "budget_id": budget_id,
            "budget_generation": budget_generation,
            "execution_generation": 1,
            "worker_id": "worker-1",
        },
        engine=engine,
        objects=LocalObjectStore(tmp_path / "objects"),
        runtime=LocalSubprocessRuntime(),
        model=_Model(),
    )

    assert events.get_nowait()["type"] == "error"
    assert not made.exists()


def _fake_strategy(strategy: Any, task: Task, server: EvalServer, ctx: EngineContext, *_: Any, **__: Any) -> Any:
    """Act as a loop-routed engine: ask the model, write its reply, score it, then ask the model again.

    Args:
        strategy: The job's strategy.
        task: The task with the empty-patch seed.
        server: The run's eval server.
        ctx: The loop's context.
        *_: The progress sink.
        **__: The capabilities.

    Returns:
        The best version and the lanes.
    """
    assert ctx.call_model is not None
    assert ctx.put_files is not None
    assert ctx.repo_seed is not None
    first = ctx.call_model("write app")
    tree = ctx.put_files({"src/app.py": first.text.encode()}, ctx.repo_seed.tree_id)
    score = server.evaluate(tree)[0]
    ctx.call_model("write more")
    return Result(best_candidate=tree, best_score=score, total_evals=server.used), []


class _FakeRepoScorer:
    """Stand in for the parent's repository scorer: score 1 for any change and 0.5 for the empty patch."""

    calls: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Take the real scorer's arguments.

        Args:
            *args: The URL.
            **kwargs: The relay settings.
        """

    def __call__(self, candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
        """Score one patch.

        Args:
            candidate: The patch.
            case: The case.

        Returns:
            The score and its feedback.
        """
        self.calls.append(candidate)
        return (1.0 if candidate else 0.5), {"feedback": "scored"}

    def close(self) -> None:
        """Close nothing."""


def _host(
    payload: dict[str, Any],
    owner: tuple[User, str, int],
    engine: Engine,
    tmp_path: Path,
    *,
    generation: int,
    model: _Model,
) -> list[dict[str, Any]]:
    """Run the job on a new host and return its events.

    Args:
        payload: The job payload.
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        generation: The job's execution generation.
        model: The fake model.

    Returns:
        The events that the host sent.
    """
    _, budget_id, budget_generation = owner
    events: queue.Queue = queue.Queue()
    host_loop_job(
        payload,
        f"{RUN_ID}-g{generation}",
        events,
        host={
            "run_id": RUN_ID,
            "username": "alice",
            "budget_id": budget_id,
            "budget_generation": budget_generation,
            "execution_generation": generation,
            "worker_id": f"worker-{generation}",
        },
        engine=engine,
        objects=LocalObjectStore(tmp_path / "objects"),
        runtime=LocalSubprocessRuntime(),
        model=model,
        run_dir=tmp_path / f"host-{generation}",
    )
    seen = []
    while not events.empty():
        seen.append(events.get_nowait())
    return seen


def _host_payload(owner: tuple[User, str, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Return a repository job payload for the host that runs the fake engine.

    Args:
        owner: Alice, her budget id and the budget generation.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's patcher.

    Returns:
        The job payload.
    """
    monkeypatch.setattr(service, "run_strategy", _fake_strategy)
    monkeypatch.setattr(service, "RemoteScorer", _FakeRepoScorer)
    monkeypatch.setattr(_FakeRepoScorer, "calls", [])
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(_archive({"src/app.py": "x = 1\n"}))
    return {
        **_repo_request(execution_budget_id=owner[1]),
        # The worker's budget gateway gives every protected run a model route and a spending ceiling.
        "reflection_model_config": {
            "name": "gpt-4o",
            "extra": {ROUTE_KEY: {"url": "http://127.0.0.1:9/model", "token": "model-route", "model": "gpt-4o"}},
        },
        "max_cost_cents": 100,
        "_skynet_evaluator_route": {"url": "http://127.0.0.1:9/evaluate", "token": "route"},
        "_repo_snapshot": {"archive": str(archive), "hidden_hashes": {}, "readonly_paths": []},
        # The parent's box route: the job sees a sandbox runtime, so the engine passes its availability check.
        "_budget_gateway_descriptor": {"url": "http://127.0.0.1:9", "control_token": "control", "image": None},
    }


def test_a_killed_run_resumes_on_the_host_and_repeats_no_evaluation_or_model_call(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the worker in the engine's second model call, then run the job again on generation 2.

    The resume gets the first reply and the first score from the log, and only new calls reach the model and
    the scorer. Every scorer call is one step, so no call happened twice.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    payload = _host_payload(owner, tmp_path, monkeypatch)

    first_model = _Model(die_at=1)
    events = _host(payload, owner, engine, tmp_path, generation=1, model=first_model)
    assert first_model.prompts == ["write app"]
    assert [event["type"] for event in events][-1] == "error"
    first_calls = list(_FakeRepoScorer.calls)
    assert len(first_calls) == 1
    assert "src/app.py" in first_calls[0]

    _new_generation(engine)
    model = _Model()
    events = _host(payload, owner, engine, tmp_path, generation=2, model=model)

    [result] = [event for event in events if event["type"] == "result"]
    assert result["result"]["best_score"] == 1.0
    # The first reply and the first score come back from the log.
    assert model.prompts == ["write more"]
    # The final runs score the starting point and the winner, each as one new step.
    assert _FakeRepoScorer.calls[1:] == ["", first_calls[0]]
    assert _steps(engine) == [("model", 1), ("command", 1), ("model", 2), ("command", 2), ("command", 2)]


class _RepoLane:
    """An Auto lane on the host: one model call, its reply written to the seed, and one score."""

    def __init__(self, name: str) -> None:
        """Set the engine id.

        Args:
            name: The engine id.
        """
        self.name = name

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Write the model's reply to the seed tree and score it.

        Args:
            task: The lane's task; not used.
            server: The lane's eval server.
            ctx: The lane's loop context.

        Returns:
            The written tree and its score.
        """
        assert ctx.call_model is not None
        assert ctx.put_files is not None
        assert ctx.repo_seed is not None
        # The run folder names the phase, so each lane and the continuation write a different patch.
        reply = ctx.call_model(f"write {Path(ctx.run_dir).name}")
        tree = ctx.put_files({"src/app.py": reply.text.encode()}, ctx.repo_seed.tree_id)
        return Result(best_candidate=tree, best_score=server.evaluate(tree)[0], total_evals=server.used)


class _LaneKillModel(_Model):
    """A fake model that kills the worker in the Meta-Harness lane's call."""

    def __call__(self, prompt: str | list[dict[str, Any]], tools: Any) -> ModelReply:
        """Answer one prompt, or kill the worker for the Meta-Harness lane.

        Args:
            prompt: The prompt text.
            tools: The tool definitions; not used.

        Returns:
            The reply and its cost in cents.

        Raises:
            _WorkerDiedError: In the Meta-Harness lane's call.
        """
        if "meta_harness" in str(prompt):
            raise _WorkerDiedError
        return super().__call__(prompt, tools)


def _lane_budgets(events: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """Collect the scorer allowance that each Auto lane started with.

    Args:
        events: The host's job events.

    Returns:
        Each lane's allowance by phase and engine.
    """
    return {
        (event["metrics"]["phase"], event["metrics"]["engine"]): event["metrics"]["budget"]
        for event in events
        if event["type"] == "progress" and event["event"] == PROGRESS_LANE_STARTED
    }


def test_a_killed_auto_repo_run_resumes_on_the_host_and_repeats_no_lane_call(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Auto repository run uses the loop, and a resume repeats no committed model or scorer call (SKY-832).

    The resume gets a larger scorer allowance, so only the committed lane plan keeps the lane allowances.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    payload = {**_host_payload(owner, tmp_path, monkeypatch), "strategy": {"mode": "auto"}}
    monkeypatch.setattr(service, "run_strategy", auto.run_strategy)
    monkeypatch.setattr(auto, "get_engine", lambda name, caps: _RepoLane(name))
    assert service.has_loop_route(BlackboxRunRequest.model_validate(payload))

    first_model = _LaneKillModel()
    events = _host(payload, owner, engine, tmp_path, generation=1, model=first_model)
    assert [event["type"] for event in events][-1] == "error"
    lanes = [name for name in AUTO_ENGINES if name != "meta_harness"]
    assert sorted(first_model.prompts) == sorted(f"write explore-{name}-0" for name in lanes)
    first_calls = list(_FakeRepoScorer.calls)
    assert len(first_calls) == len(lanes)
    # The parent committed its lane plan; the lane results never committed.
    assert _steps(engine) == [("command", 1)]
    per_lane = BlackboxBudget().max_scorer_runs // (len(AUTO_ENGINES) + 1)
    assert _lane_budgets(events) == {("explore", name): per_lane for name in AUTO_ENGINES}

    _new_generation(engine)
    model = _Model()
    resumed = {**payload, "budget": {"max_scorer_runs": 2 * BlackboxBudget().max_scorer_runs}}
    events = _host(resumed, owner, engine, tmp_path, generation=2, model=model)

    [result] = [event for event in events if event["type"] == "result"]
    assert result["result"]["best_score"] == 1.0
    assert _lane_budgets(events) == {
        **{("explore", name): per_lane for name in AUTO_ENGINES},
        ("continue", "gepa"): BlackboxBudget().max_scorer_runs - len(AUTO_ENGINES) * per_lane,
    }
    assert model.prompts == ["write explore-meta_harness-0", "write continue-gepa"]
    assert not set(_FakeRepoScorer.calls[len(first_calls) :]) & set(first_calls)
    assert _steps(engine)[:2] == [("command", 1), ("command", 2)]


def test_the_job_budget_pays_for_the_run_when_the_workspace_points_at_another_budget(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user can move the workspace to a new budget while a run waits, but the run's own budget still pays.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    user, budget_id, _ = owner
    budgets = BudgetService(engine=engine)
    other = budgets.create("alice", 500, idempotency_key="other-budget")
    WorkspaceStore(engine).set_budget(user.workspace().id, other.id)
    payload = _host_payload(owner, tmp_path, monkeypatch)

    events = _host(payload, owner, engine, tmp_path, generation=1, model=_Model())

    assert [event["type"] for event in events][-1] == "result"
    assert budgets.get(budget_id, "alice").run_spent_cents == 2
    assert budgets.get(other.id, "alice").run_spent_cents == 0


def test_a_command_with_a_long_limit_gets_a_box_that_outlives_it(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 30-minute file proposer command gets a box for its limit and the pack, and another command keeps 900 s.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    lifetimes: list[float] = []
    real_open = LocalSubprocessRuntime.open

    def record_open(self: LocalSubprocessRuntime, spec: SandboxSpec) -> Any:
        """Record the box's lifetime, then open it.

        Args:
            self: The runtime.
            spec: The box spec.

        Returns:
            The open box.
        """
        lifetimes.append(spec.lifetime_seconds)
        return real_open(self, spec)

    def strategy(strategy: Any, task: Task, server: EvalServer, ctx: EngineContext, *_: Any, **__: Any) -> Any:
        """Run one command with a 30-minute limit and one with the default limit.

        Args:
            strategy: The job's strategy.
            task: The task.
            server: The run's eval server.
            ctx: The loop's context.
            *_: The progress sink.
            **__: The capabilities.

        Returns:
            The seed as the best version, and no lanes.
        """
        assert ctx.run_command is not None
        assert ctx.repo_seed is not None
        ctx.run_command(ctx.repo_seed.tree_id, "true", timeout_seconds=1800)
        ctx.run_command(ctx.repo_seed.tree_id, "true")
        return Result(best_candidate=ctx.repo_seed.tree_id, best_score=0.5, total_evals=server.used), []

    payload = _host_payload(owner, tmp_path, monkeypatch)
    monkeypatch.setattr(service, "run_strategy", strategy)
    monkeypatch.setattr(LocalSubprocessRuntime, "open", record_open)

    events = _host(payload, owner, engine, tmp_path, generation=1, model=_Model())

    assert [event["type"] for event in events][-1] == "result"
    assert lifetimes[:2] == [1800 + 300, loop_host.STEP_BOX_SECONDS]
    assert loop_host.STEP_BOX_SECONDS == 900
    assert loop_host.command_box_seconds(1800) <= 2700


def test_a_command_box_fits_the_run_deadline_and_its_command_times_out_first(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker refuses a box past the run's deadline, so a 30-minute command in a 1000 s run gets a 1000 s box
    and a 700 s limit, which leaves the pack room to end before the box dies. The command's own environment, such
    as the file proposer's route key, reaches the loop.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    lifetimes: list[float] = []
    timeouts: list[float | None] = []
    envs: list[Any] = []
    real_open = LocalSubprocessRuntime.open
    real_run = EngineLoop.run

    def record_open(self: LocalSubprocessRuntime, spec: SandboxSpec) -> Any:
        """Record the box's lifetime, then open it.

        Args:
            self: The runtime.
            spec: The box spec.

        Returns:
            The open box.
        """
        lifetimes.append(spec.lifetime_seconds)
        return real_open(self, spec)

    def record_run(self: EngineLoop, *args: Any, timeout_seconds: float | None = None, **kwargs: Any) -> Any:
        """Record the command's limit, then run it.

        Args:
            self: The engine loop.
            *args: The command's tree, text and grants.
            timeout_seconds: The command's limit.
            **kwargs: Other options.

        Returns:
            The command's result and tree.
        """
        timeouts.append(timeout_seconds)
        envs.append(kwargs.get("env"))
        return real_run(self, *args, timeout_seconds=timeout_seconds, **kwargs)

    def strategy(strategy: Any, task: Task, server: EvalServer, ctx: EngineContext, *_: Any, **__: Any) -> Any:
        """Run one command with a 30-minute limit.

        Args:
            strategy: The job's strategy.
            task: The task.
            server: The run's eval server.
            ctx: The loop's context.
            *_: The progress sink.
            **__: The capabilities.

        Returns:
            The seed as the best version, and no lanes.
        """
        assert ctx.run_command is not None
        assert ctx.repo_seed is not None
        ctx.run_command(ctx.repo_seed.tree_id, "true", timeout_seconds=1800, env={"ASCENT_MODEL_KEY": "route-key"})
        return Result(best_candidate=ctx.repo_seed.tree_id, best_score=0.5, total_evals=server.used), []

    payload = _host_payload(owner, tmp_path, monkeypatch)
    payload["_budget_gateway_descriptor"]["lifetime_seconds"] = 1000
    monkeypatch.setattr(service, "run_strategy", strategy)
    monkeypatch.setattr(LocalSubprocessRuntime, "open", record_open)
    monkeypatch.setattr(EngineLoop, "run", record_run)

    events = _host(payload, owner, engine, tmp_path, generation=1, model=_Model())

    assert [event["type"] for event in events][-1] == "result"
    assert (lifetimes[0], timeouts[0], envs[0]) == (1000, 700, {"ASCENT_MODEL_KEY": "route-key"})


def test_a_command_box_never_outlives_the_configured_sandbox_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider refuses a longer box, so the configured sandbox lifetime caps a command box.

    Args:
        monkeypatch: The test's patcher.
    """
    monkeypatch.setattr(loop_host.settings, "vercel_sandbox_max_lifetime_seconds", 1000)

    assert loop_host.command_box_seconds(1800) == 1000


def test_the_host_checks_that_the_run_replayed_every_step(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that ends before it replays each committed step lost work, so the host checks the log at the end.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    finishes: list[None] = []
    real_finish = _LoopBinding.finish

    def finish(self: _LoopBinding) -> None:
        """Count the check and run it.

        Args:
            self: The binding.
        """
        finishes.append(None)
        real_finish(self)

    monkeypatch.setattr(_LoopBinding, "finish", finish)
    payload = _host_payload(owner, tmp_path, monkeypatch)

    events = _host(payload, owner, engine, tmp_path, generation=1, model=_Model())

    assert [event["type"] for event in events][-1] == "result"
    assert len(finishes) == 1


@pytest.mark.parametrize("engine_name", ["autoresearch", "meta_harness"])
def test_a_loop_routed_run_with_an_entry_runs_the_contract_files_command(
    engine_name: str, owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real engine on the loop runs the ``[proposer] entry`` command, not its built-in tool loop (SKY-816).

    Args:
        engine_name: The engine that runs on the loop.
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """
    # The real engine, not the fake strategy of the other host tests.
    real_strategy = service.run_strategy
    payload = _host_payload(owner, tmp_path, monkeypatch)
    monkeypatch.setattr(service, "run_strategy", real_strategy)
    payload["strategy"] = {"mode": "single", "engine": engine_name}
    # Repository staging sets these from the contract file's ``[proposer] entry``.
    payload["proposer"] = {"harness": "custom", "run_command": "printf x >> src/app.py"}
    payload["_repo_snapshot"] = {**payload["_repo_snapshot"], "file_proposer": True}
    model = _Model()

    events = _host(payload, owner, engine, tmp_path, generation=1, model=model)

    assert [event["type"] for event in events][-1] == "result"
    assert model.prompts == []
    # The entry appends to the seed's ``x = 1\n``, so the scored patch adds an ``x`` line.
    assert any("\n+x\n" in patch for patch in _FakeRepoScorer.calls)


def test_a_scorer_outage_in_the_final_run_requeues_the_run(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final run scores through the loop too, so its outage must reach the worker as an interruption.

    Args:
        owner: Alice, her budget id and the budget generation.
        engine: The database.
        tmp_path: Pytest's temporary folder.
        monkeypatch: The test's patcher.
    """

    class _DownAtTheEnd(_FakeRepoScorer):
        """Score the engine's versions, then fail as a server error on the starting point."""

        def __call__(self, candidate: Any, case: Any = None) -> tuple[float, dict[str, Any]]:
            """Score one patch, or fail on the empty patch.

            Args:
                candidate: The patch.
                case: The case.

            Returns:
                The score and its feedback.

            Raises:
                ServiceError: On the empty patch.
            """
            if not candidate:
                raise ServiceError("repository scorer failed (HTTP 502): bad gateway")
            return super().__call__(candidate, case)

    payload = _host_payload(owner, tmp_path, monkeypatch)
    monkeypatch.setattr(service, "RemoteScorer", _DownAtTheEnd)

    events = _host(payload, owner, engine, tmp_path, generation=1, model=_Model())

    last = events[-1]
    assert last["type"] == "error"
    assert last["failure_kind"] == INFRASTRUCTURE_INTERRUPTION


def test_the_worker_starts_a_loop_routed_run_on_the_host_with_its_trusted_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed run's child is the loop host, and the claimed job row, not the payload, names its run and budget.

    Args:
        monkeypatch: The test's patcher.
    """
    monkeypatch.setattr(
        process_job_tests,
        "BlackboxRunRequest",
        functools.partial(BlackboxRunRequest, strategy={"mode": "single", "engine": "autoresearch"}),
    )
    reset_worker_for_tests()
    store = FakeJobStore()
    worker = BackgroundWorker(job_store=cast(JobStore, store), num_workers=1, poll_interval=1.0)
    ctx, _ = make_mp_context(exitcode=0, result_events=[make_result_event()])
    try:
        process_job_tests._run_repository_job(
            worker, store, {}, patched={"mp.get_context": lambda method: ctx, "data_policy_for": lambda username: "zdr"}
        )
    finally:
        reset_worker_for_tests()

    assert ctx.Process.call_args.kwargs["target"] is run_loop_job
    host = ctx.Process.call_args.kwargs["args"][0][LOOP_HOST_KEY]
    assert {key: host[key] for key in ("run_id", "username", "budget_id", "budget_generation", "data_policy")} == {
        "run_id": "repo-hardened",
        "username": "alice",
        "budget_id": "budget",
        "budget_generation": 0,
        "data_policy": "zdr",
    }
    assert host["reflection_model"]["name"] == "gpt-4o"


def _crashed_run(tmp_path: Path, *, close: bool) -> StepJobStore:
    """Make two paid model calls on a claimed run's loop, then let the worker die.

    Args:
        tmp_path: Pytest's temporary folder.
        close: Whether the worker closes the loop, and so final-settles its lease, before it dies.

    Returns:
        The job store, with the run's lease run out.
    """
    store = make_sqlite_store(tmp_path)
    _funded_run(store)
    generation = _claim(store, "pod-a")
    budgets = BudgetService(engine=store.engine)
    user = User("owner", engine=store.engine, budgets=budgets)
    WorkspaceStore(store.engine).set_budget(user.workspace().id, store.get_job(RUN_ID)["execution_budget_id"])
    step_log = StepLogStore(store.engine, LocalObjectStore(tmp_path / "objects"), "pod-a", flush_window_seconds=0.02)
    run = user.workspace().start_run(
        RUN_ID,
        step_log=step_log,
        execution_generation=generation,
        budget_generation=0,
        grant_list_id="grants-1",
        chunk_cents=10,
        lease_slices=1,
    )
    loop = EngineLoop(
        run,
        engine=store.engine,
        trees=TreeStore(step_log),
        candidates=RuntimeBoxSource(LocalSubprocessRuntime(), _SPEC),
        metrics=RuntimeBoxSource(LocalSubprocessRuntime(), _SPEC),
        model=_Model(),
        model_max_cents=2,
        run_dir=tmp_path / "loop",
    )
    call_model = loop.context().call_model
    assert call_model is not None
    call_model("write app")
    call_model("write more")
    if close:
        loop.close()
    else:
        # A dead worker settles nothing more: only its step log's flusher must stop.
        step_log.close()
    _kill(store)
    return store


@pytest.mark.parametrize("close", [False, True], ids=["killed", "closed-then-killed"])
def test_a_crashed_loop_requeues_and_bills_only_the_steps_that_the_resume_keeps(tmp_path: Path, close: bool) -> None:
    """The resume replays both model steps and does not pay for them again, so they stay billed once.

    A killed worker leaves its lease slice open with no provider record, so the fence must end it, or the
    recovery waits for it until the run fails.

    Args:
        close: Whether the worker closed the loop before it died.
        tmp_path: Pytest's temporary folder.
    """
    store = _crashed_run(tmp_path, close=close)

    assert store.recover_orphaned_jobs() == 1

    assert store.get_job(RUN_ID)["status"] == "pending"
    budget = BudgetService(engine=store.engine).get(store.get_job(RUN_ID)["execution_budget_id"], "owner")
    assert (budget.billed_cents, budget.pending_operations, budget.reserved_cents) == (2, 0, 0)
    with Session(store.engine) as session:
        assert session.get(BillingCustomerModel, "owner").balance_cents == 48


def _lease_id(store: StepJobStore) -> str:
    """Return the id of the run's only lease slice.

    Args:
        store: The job store.

    Returns:
        The lease slice's operation id.
    """
    with Session(store.engine) as session:
        return session.scalars(
            select(ExecutionOperationModel.id).where(ExecutionOperationModel.cost_kind == AGENT_LEASE_COST_KIND)
        ).one()


def test_a_fenced_worker_that_is_still_alive_charges_nothing_for_its_late_call(tmp_path: Path) -> None:
    """The resume repeats and bills a fenced worker's late call, so its late settle keeps only the evidence.

    The late usage must not count against the run's limit either: it holds no coverage, so settled and
    reserved together could pass the budget total.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    store = _crashed_run(tmp_path, close=False)
    assert store.recover_orphaned_jobs() == 1
    budgets = BudgetService(engine=store.engine)
    budget_id = store.get_job(RUN_ID)["execution_budget_id"]
    lease = _lease_id(store)
    before = budgets.get(budget_id, "owner")

    budgets.settle(lease, "owner", evidence_key="late-call", actual_cents=3, evidence={"late": True}, final=True)

    budget = budgets.get(budget_id, "owner")
    assert (budget.billed_cents, budget.pending_operations, budget.reserved_cents) == (2, 0, 0)
    assert (budget.run_spent_cents, budget.wallet_run_spent_cents) == (
        before.run_spent_cents,
        before.wallet_run_spent_cents,
    )
    with Session(store.engine) as session:
        assert session.get(BillingCustomerModel, "owner").balance_cents == 48
        late = session.scalars(
            select(ExecutionUsageEvidenceModel).where(ExecutionUsageEvidenceModel.evidence_key == "late-call")
        ).one()
        assert (late.billed_cents, late.issue) == (0, None)


def test_the_fence_keeps_a_lease_slice_whose_settle_the_ledger_refused(tmp_path: Path) -> None:
    """A refused settle needs review, so the fence must not end its slice or release its coverage.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    store = _crashed_run(tmp_path, close=False)
    budgets = BudgetService(engine=store.engine)
    budget_id = store.get_job(RUN_ID)["execution_budget_id"]
    lease = _lease_id(store)
    with pytest.raises(BudgetBoundExceededError):
        budgets.settle(lease, "owner", evidence_key="over", actual_cents=1_000, evidence={"over": True}, final=False)
    before = budgets.get(budget_id, "owner")
    assert before.reserved_cents > 0

    budgets.fence_generation(budget_id, "owner", expected_generation=before.generation)

    assert budgets.get(budget_id, "owner").reserved_cents == before.reserved_cents
    with Session(store.engine) as session:
        assert session.get(ExecutionOperationModel, lease).state == "pending"


def test_the_ledger_and_the_worker_name_the_lease_cost_kind_the_same() -> None:
    """The fence finds the worker's lease slices by their cost kind, so the two names must stay equal."""
    assert AGENT_LEASE_COST_KIND == LEASE_COST_KIND

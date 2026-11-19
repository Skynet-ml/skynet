"""Tests for the production engine-loop host of a repository run (SKY-829).

The tests use a SQLite file, the local-filesystem object store, local
subprocess boxes, fake scorers and fake model replies: no network and no paid
call.
"""

from __future__ import annotations

import functools
import io
import json
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
from core.billing.openrouter_quotes import price_text_request
from core.billing.operation_pricing import ChargePolicy, UnpricedOperationError
from core.billing.runtime import UsagePendingError
from core.exceptions import (
    DETERMINISTIC_FAILURE,
    INFRASTRUCTURE_INTERRUPTION,
    InfrastructureInterruptionError,
    ServiceError,
)
from core.models.blackbox import BlackboxRunRequest
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
    """The first generation runs the evaluation; the second gets the committed result and runs nothing."""
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
    """A resume that evaluates another version at the same place gets no committed result."""
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
    """An error of the evaluation is no result: the loop stops, keeps the stop and has no step."""
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
    """A 422 from the repository scorer is a final answer, so the step keeps it and a resume does not ask again."""
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
    """An error that is not the scorer's answer is a bug: no step records it, and the run ends."""
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
    """The replay reads the side info from JSON, so the live engine sees the same values and makes the same steps."""

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
    owner: tuple[User, str, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """The engine's tree id becomes the patch that the repository scorer takes, and the score comes back."""
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
    """Two trees with the same files give no patch."""
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


def test_a_provider_error_fails_the_call_with_no_provider_text() -> None:
    """The provider's error text can hold request data, so the error names only its type."""
    with pytest.raises(ServiceError, match=r"^The model call failed: HTTPStatusError$"):
        _managed_client(lambda request: httpx.Response(500, text=f"key {_FAKE_KEY}"))("hello", None)


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
    """The user's provider bills a BYOK call, so the lease pays nothing for it."""
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
    """The user names the host, so a name that resolves into the worker's network must not get a call."""
    monkeypatch.setattr(
        mcp_broker.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("10.0.0.5", 443))]
    )

    with pytest.raises(ValueError, match="private custom provider endpoint"):
        _custom_byok()


def test_a_custom_byok_call_goes_to_the_checked_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each call uses the one address that the check accepted, so a new DNS answer cannot move it."""
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


def test_close_closes_every_http_client_that_the_model_client_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom BYOK client opens a default client and then its pinned client; close must close both."""
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

    monkeypatch.setattr(
        mcp_broker.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("93.184.216.34", 443))]
    )
    monkeypatch.setattr(loop_host.httpx, "Client", record)

    _custom_byok().close()

    assert len(opened) == 2
    assert all(client.is_closed for client in opened)


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
    """A host with no run folder makes a temporary one, and removes it also when the job fails to start."""
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
    """Route the fake engine to the loop and return a repository job payload for the host.

    Args:
        owner: Alice, her budget id and the budget generation.
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's patcher.

    Returns:
        The job payload.
    """
    monkeypatch.setattr(service, "LOOP_ROUTES", frozenset({("autoresearch", "repo")}))
    monkeypatch.setattr(service, "run_strategy", _fake_strategy)
    monkeypatch.setattr(service, "RemoteScorer", _FakeRepoScorer)
    monkeypatch.setattr(_FakeRepoScorer, "calls", [])
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(_archive({"src/app.py": "x = 1\n"}))
    return {
        **_repo_request(execution_budget_id=owner[1]),
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


def test_a_killed_auto_repo_run_resumes_on_the_host_and_repeats_no_lane_call(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Auto repository run uses the loop, and a resume repeats no committed model or scorer call (SKY-832)."""
    payload = {**_host_payload(owner, tmp_path, monkeypatch), "strategy": {"mode": "auto"}}
    monkeypatch.setattr(service, "LOOP_ROUTES", frozenset((name, "repo") for name in AUTO_ENGINES))
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

    _new_generation(engine)
    model = _Model()
    events = _host(payload, owner, engine, tmp_path, generation=2, model=model)

    [result] = [event for event in events if event["type"] == "result"]
    assert result["result"]["best_score"] == 1.0
    assert model.prompts == ["write explore-meta_harness-0", "write continue-gepa"]
    assert not set(_FakeRepoScorer.calls[len(first_calls) :]) & set(first_calls)
    assert _steps(engine)[:2] == [("command", 1), ("command", 2)]


def test_the_job_budget_pays_for_the_run_when_the_workspace_points_at_another_budget(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user can move the workspace to a new budget while a run waits, but the run's own budget still pays."""
    user, budget_id, _ = owner
    budgets = BudgetService(engine=engine)
    other = budgets.create("alice", 500, idempotency_key="other-budget")
    WorkspaceStore(engine).set_budget(user.workspace().id, other.id)
    payload = _host_payload(owner, tmp_path, monkeypatch)

    events = _host(payload, owner, engine, tmp_path, generation=1, model=_Model())

    assert [event["type"] for event in events][-1] == "result"
    assert budgets.get(budget_id, "alice").run_spent_cents == 2
    assert budgets.get(other.id, "alice").run_spent_cents == 0


def test_the_host_checks_that_the_run_replayed_every_step(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that ends before it replays each committed step lost work, so the host checks the log at the end."""
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


def test_a_scorer_outage_in_the_final_run_requeues_the_run(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final run scores through the loop too, so its outage must reach the worker as an interruption."""

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
    """A routed run's child is the loop host, and the claimed job row, not the payload, names its run and budget."""
    monkeypatch.setattr(service, "LOOP_ROUTES", frozenset({("autoresearch", "repo")}))
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
    """A refused settle needs review, so the fence must not end its slice or release its coverage."""
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

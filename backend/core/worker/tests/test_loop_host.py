"""Tests for the production engine-loop host of a repository run (SKY-829).

The tests use a SQLite file, the local-filesystem object store, local
subprocess boxes, fake scorers and fake model replies: no network and no paid
call.
"""

from __future__ import annotations

import io
import json
import queue
import tarfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.operation_pricing import ChargePolicy
from core.exceptions import ServiceError
from core.models.tests.test_blackbox import _repo_request
from core.sandbox.trees import tar_to_tree
from core.service_gateway.optimization.blackbox import service
from core.service_gateway.optimization.blackbox.protocol import (
    EngineContext,
    EvalServer,
    ModelReply,
    RepoSeed,
    Result,
    Task,
)
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxSource
from core.storage.models import AgentStepModel, Base, BillingCustomerModel, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeStore
from core.storage.workspace_store import WorkspaceStore
from core.worker import loop_host
from core.worker.actors import User
from core.worker.engine_loop import ROOT_AGENT_ID, EngineLoop, LoopStoppedError, StepDivergedError
from core.worker.loop_host import LeaseModelClient, _LoopBinding, host_loop_job, tree_patch

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


@pytest.mark.parametrize(
    "error",
    [
        ServiceError("repository scorer failed (HTTP 502): bad gateway"),
        ServiceError("repository scorer failed (HTTP 429): slow down"),
    ],
    ids=["server-error", "rate-limit"],
)
def test_a_scorer_error_that_can_pass_on_a_new_try_stops_the_loop_and_commits_nothing(
    error: ServiceError,
    owner: tuple[User, str, int],
    engine: Engine,
    step_log: StepLogStore,
    trees: TreeStore,
    tmp_path: Path,
) -> None:
    """A server error or a rate limit is not the version's score, so no step records it.

    Args:
        error: The scorer's error.
    """

    def fails(candidate: Any, case: Any = None) -> tuple[float | None, dict[str, Any]]:
        """Fail with the parametrized error.

        Args:
            candidate: The patch.
            case: The case.

        Raises:
            ServiceError: Always.
        """
        raise error

    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    scorer = _LoopBinding(loop, trees, RUN_ID, _seed(trees)).scorer(fails)
    with pytest.raises(LoopStoppedError):
        scorer("patch")
    assert _steps(engine) == []


def test_a_transport_error_of_the_scorer_stops_the_loop_and_commits_nothing(
    owner: tuple[User, str, int], engine: Engine, step_log: StepLogStore, trees: TreeStore, tmp_path: Path
) -> None:
    """A dropped relay connection says nothing about the version, so the eval server must not see a score."""

    def drops(candidate: Any, case: Any = None) -> tuple[float | None, dict[str, Any]]:
        """Fail as the relay client does when the connection drops.

        Args:
            candidate: The patch.
            case: The case.

        Raises:
            ServiceError: Always, from an ``httpx`` transport error.
        """
        raise ServiceError("repository scorer is unreachable") from httpx.ConnectError("dropped")

    loop = _loop(owner, engine, step_log, trees, tmp_path, generation=1)
    server = EvalServer(_LoopBinding(loop, trees, RUN_ID, _seed(trees)).scorer(drops), max_evals=4)
    with pytest.raises(LoopStoppedError):
        server.evaluate("patch")
    assert _steps(engine) == []


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


def _managed_client(handler: Any, **config: Any) -> LeaseModelClient:
    """Build a managed client over a fake transport.

    Args:
        handler: The fake provider.
        **config: The model config fields to add.

    Returns:
        The client.
    """
    return LeaseModelClient(
        {"name": "openrouter/openai/gpt-4o", **config},
        payload_token_source=None,
        managed_key=_FAKE_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_a_managed_call_sends_the_request_and_gives_the_provider_cost() -> None:
    """A managed call goes to OpenRouter with the worker's key and its cost comes from the usage receipt."""
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Answer as OpenRouter does, with a usage receipt.

        Args:
            request: The request.

        Returns:
            The reply.
        """
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"cost": 0.002, "cost_details": {}},
            },
        )

    reply = _managed_client(provider, temperature=0.2)("hello", None)

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
        "usage": {"include": True},
    }


def test_a_managed_reply_with_no_cost_fails_the_call() -> None:
    """A reply that gives no complete cost cannot be billed, so the call fails."""
    reply = {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    with pytest.raises(ServiceError, match="no complete cost"):
        _managed_client(lambda request: httpx.Response(200, json=reply))("hello", None)


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
        {"name": "openrouter/openai/gpt-4o", "token_source": "byok", "extra": {"api_key": "user-key"}},
        payload_token_source=None,
        managed_key="",
    )

    reply = client([{"role": "user", "content": "hello"}], None)

    assert (reply.text, reply.cost_cents) == ("byok hi", Decimal(0))
    [call] = calls
    assert call["api_key"] == "user-key"
    assert call["model"] == "openrouter/openai/gpt-4o"


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


def test_a_killed_run_resumes_on_the_host_and_repeats_no_evaluation_or_model_call(
    owner: tuple[User, str, int], engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the worker in the engine's second model call, then run the job again on generation 2.

    The resume gets the first reply and the first score from the log, and only new calls reach the model and
    the scorer. Every scorer call is one step, so no call happened twice.
    """
    monkeypatch.setattr(service, "LOOP_ROUTES", frozenset({("autoresearch", "repo")}))
    monkeypatch.setattr(service, "run_strategy", _fake_strategy)
    monkeypatch.setattr(service, "RemoteScorer", _FakeRepoScorer)
    monkeypatch.setattr(_FakeRepoScorer, "calls", [])
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(_archive({"src/app.py": "x = 1\n"}))
    payload = {
        **_repo_request(execution_budget_id=owner[1]),
        "_skynet_evaluator_route": {"url": "http://127.0.0.1:9/evaluate", "token": "route"},
        "_repo_snapshot": {"archive": str(archive), "hidden_hashes": {}, "readonly_paths": []},
        # The parent's box route: the job sees a sandbox runtime, so the engine passes its availability check.
        "_budget_gateway_descriptor": {"url": "http://127.0.0.1:9", "control_token": "control", "image": None},
    }

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

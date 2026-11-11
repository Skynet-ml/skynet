"""Tests for the per-run deadline override on a repository run (SKY-572)."""

from __future__ import annotations

import contextlib
import json
import queue
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from core.api.routers.submissions import _persistable_payload
from core.billing.protected_execution import bind_protected_sandbox
from core.billing.tests.test_protected_execution_local import _Gateway, local_settings  # noqa: F401
from core.config import Settings
from core.models.blackbox import BlackboxRunRequest
from core.service_gateway.optimization.blackbox.remote_sandbox import RemoteSandboxRuntime
from core.service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY
from core.service_gateway.optimization.blackbox.sandbox_broker import SandboxBroker
from core.service_gateway.optimization.blackbox.sandbox_contract import CommandResult, SandboxSpec
from core.service_gateway.optimization.blackbox.sandbox_runtime import current_sandbox_runtime
from core.storage.base import JobStore
from core.storage.models import ContinuousPlanModel

from .. import continuous
from .. import engine as engine_module
from ..constants import EVENT_RESULT
from ..engine import BackgroundWorker, reset_worker_for_tests
from ..subprocess_runner import run_service_in_subprocess
from .conftest import FakeJobStore
from .mocks import REAL_RUN_PAYLOAD, fake_dspy_service, fake_service_registry, make_mp_context, make_result_event

_OVERVIEW = {"optimization_type": "blackbox", "username": "alice"}


@pytest.fixture(autouse=True)
def _reset_global_worker() -> Iterator[None]:
    """Reset the module-level worker singleton around each test."""
    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


def _request(**extra: Any) -> BlackboxRunRequest:
    """Build a minimal repository run request.

    Args:
        **extra: More request fields.

    Returns:
        The validated request.
    """
    return BlackboxRunRequest(
        username="alice",
        recipe="repo",
        scorer={"kind": "python", "metric_code": "def score(candidate, case=None): return 1.0"},
        target={"kind": "repo", "repo": {"repository": "acme/app", "editable_paths": ["src"]}},
        reflection_model_config={"name": "gpt-4o"},
        **extra,
    )


def _bound_lifetime(payload: dict[str, Any]) -> Any:
    """Run one stored repository job and return the lifetime it binds its sandbox with.

    Args:
        payload: The stored run request.

    Returns:
        The ``lifetime_seconds`` argument the engine passed to the sandbox bind.
    """
    store = FakeJobStore()
    store.engine = MagicMock()
    store.seed_job(
        "repo-deadline",
        payload=payload,
        payload_overview=_OVERVIEW,
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker = BackgroundWorker(
        job_store=cast(JobStore, store),
        num_workers=1,
        poll_interval=1.0,
        _allow_unprotected_test_execution=True,
    )
    worker.enqueue_job("repo-deadline")
    gateway = MagicMock()
    gateway.protect_payload.side_effect = lambda payload, **kwargs: payload
    gateway.cost_ceiling_cents.return_value = 100

    def stage(payload: dict, **kwargs: object) -> tuple[dict, MagicMock]:
        """Stage the repository the way the real step leaves the payload."""
        payload["target"]["repo"][engine_module.ENVIRONMENT_KEY] = {}
        payload[REPO_SNAPSHOT_KEY] = {}
        return payload, MagicMock()

    ctx, _proc = make_mp_context(exitcode=0, result_events=[make_result_event()])
    with contextlib.ExitStack() as stack:
        for target, kwargs in (
            ("notify_job_completed", {}),
            ("record_server_event", {}),
            ("validate_blackbox_payload", {}),
            ("payload_uses_token_source", {"return_value": False}),
            ("ModelGateway", {"return_value": gateway}),
            ("BudgetService", {}),
            ("BudgetRuntime", {}),
            ("protected_image", {"return_value": None}),
            ("resolve_execution_credentials", {"side_effect": lambda payload, **kwargs: payload}),
            ("host_proposer", {"return_value": None}),
            ("stage_repository", {"side_effect": stage}),
            ("check_repository", {"return_value": []}),
            ("infer_repo_setup", {"return_value": None}),
            ("bind_repo_scorer", {}),
            ("has_exposed_execution_credentials", {"return_value": False}),
            ("publish_improvement", {}),
        ):
            stack.enter_context(patch(f"core.worker.engine.{target}", **kwargs))
        stack.enter_context(patch("core.worker.engine.mp.get_context", return_value=ctx))
        bind = stack.enter_context(patch("core.worker.engine.bind_protected_sandbox"))
        stack.enter_context(patch.object(worker, "_get_service"))
        stack.enter_context(patch.object(worker, "_record_repo_commit"))
        stack.enter_context(patch.object(worker, "_record_repo_environment", return_value=None))
        stack.enter_context(patch.object(worker, "_close_budget_gateway", return_value=(None, None)))
        worker._process_job("repo-deadline", 0)
    bind.assert_called_once()
    return bind.call_args.kwargs["lifetime_seconds"]


def test_stored_run_binds_its_deadline_override() -> None:
    """A run that asks for a 120 s deadline binds its sandbox with 120 s."""
    payload = _request(deadline_seconds=120).model_dump(mode="json", by_alias=True)

    assert payload["deadline_seconds"] == 120
    assert _bound_lifetime(payload) == 120


def test_run_without_override_keeps_the_configured_deadline() -> None:
    """A run without the field passes no override, so the configured lifetime applies."""
    assert _bound_lifetime(_request().model_dump(mode="json", by_alias=True)) is None


def test_override_below_one_minute_is_refused() -> None:
    """The request refuses a deadline shorter than 60 s."""
    with pytest.raises(ValidationError):
        _request(deadline_seconds=59)


def test_override_only_shortens_the_configured_deadline(
    local_settings: tuple[Settings, Path],  # noqa: F811
) -> None:
    """The bind uses a shorter override and caps a longer one at the configured lifetime."""
    settings, _ = local_settings
    configured = settings.vercel_sandbox_max_lifetime_seconds

    short = bind_protected_sandbox(_Gateway(), settings, workflow="anything", owner_id="job-1", lifetime_seconds=120)
    long = bind_protected_sandbox(
        _Gateway(), settings, workflow="anything", owner_id="job-2", lifetime_seconds=configured + 1000
    )
    default = bind_protected_sandbox(_Gateway(), settings, workflow="anything", owner_id="job-3")

    assert short["lifetime_seconds"] == 120
    assert long["lifetime_seconds"] == default["lifetime_seconds"] == configured


def test_stored_payload_keeps_the_override() -> None:
    """The stored run payload keeps the field, so a resumed run gets the same deadline."""
    stored = _persistable_payload(_request(deadline_seconds=120), job_store=None)

    assert stored["deadline_seconds"] == 120


def test_continuous_cycle_drops_the_override() -> None:
    """A continuous check copies the incumbent's request without its deadline override."""
    incumbent = {
        "payload": _request(deadline_seconds=120).model_dump(mode="json", by_alias=True),
        "payload_overview": {},
        "result": {},
    }
    plan = cast(ContinuousPlanModel, SimpleNamespace(cycle_budget_cents=500))

    copy = continuous._cycle_payload(plan, incumbent, {}, 1)

    assert copy.deadline_seconds is None


def _parent_transport(broker: SandboxBroker) -> httpx.Client:
    """Serve the child's sandbox control requests from a real parent broker.

    Args:
        broker: The parent-owned broker.

    Returns:
        A client whose transport maps each request to the broker, with NDJSON for commands.
    """

    def parent(request: httpx.Request) -> httpx.Response:
        """Answer one control request the way the parent's gateway route does."""
        body = json.loads(request.content)
        if body["action"] != "run":
            return httpx.Response(200, json=broker.handle(body["action"], body["payload"]))
        result = broker.run(body["payload"])
        return httpx.Response(200, content=json.dumps({"type": "result", **asdict(result)}))

    return httpx.Client(transport=httpx.MockTransport(parent))


class _RecordingRuntime:
    """Stand in for the parent's provider runtime and record what it opens."""

    injects_headers = False
    billable = False

    def __init__(self) -> None:
        """Start with no opened boxes and no commands."""
        self.lifetimes: list[float] = []
        self.timeouts: list[float | None] = []

    def open(self, spec: SandboxSpec) -> _RecordingRuntime:
        """Record the box lifetime and act as its session.

        Args:
            spec: The parent-checked box spec.

        Returns:
            This recorder, as the box session.
        """
        self.lifetimes.append(spec.lifetime_seconds)
        return self

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Record the command timeout and echo the command.

        Args:
            command: Guest shell command.
            **kwargs: Broker command options.

        Returns:
            A successful result with the command as its output.
        """
        self.timeouts.append(kwargs.get("timeout_seconds"))
        return CommandResult(exit_code=0, stdout=command)

    def close(self) -> None:
        """Accept box cleanup."""


def test_child_clamps_its_box_to_the_broker_lifetime() -> None:
    """The child sends the smaller lifetime and cuts a command timeout to the box lifetime."""
    runtime = _RecordingRuntime()
    broker = SandboxBroker(runtime, image="sha256:" + "d" * 64, max_lifetime_seconds=120)
    with _parent_transport(broker) as client:
        remote = RemoteSandboxRuntime("http://127.0.0.1:9000/v1", "opaque", client=client, lifetime_ceiling_seconds=120)
        session = remote.open(SandboxSpec(lifetime_seconds=2700))
        result = session.run("echo ok", timeout_seconds=2699)
        shorter = session.run("echo ok", timeout_seconds=30)
        session.close()
        unclamped = RemoteSandboxRuntime("http://127.0.0.1:9000/v1", "opaque", client=client)
        assert unclamped.open(SandboxSpec(lifetime_seconds=60, operation_key="small"))._lifetime == 60
    broker.close()

    assert runtime.lifetimes == [120, 60]
    assert runtime.timeouts == [120, 30]
    assert result.stdout == shorter.stdout == "echo ok"


def test_protected_child_opens_a_box_under_a_short_run_deadline(
    local_settings: tuple[Settings, Path],  # noqa: F811
) -> None:
    """A real deadline broker admits the child's default-lifetime box and runs a command in it."""
    settings, log = local_settings
    configured = settings.vercel_sandbox_max_lifetime_seconds
    assert configured > 120
    gateway = _Gateway()
    bind_protected_sandbox(gateway, settings, workflow="anything", owner_id="job-deadline", lifetime_seconds=120)
    broker = gateway.broker
    descriptor = {"url": "http://127.0.0.1:9000/v1", "control_token": "opaque", **gateway.bound}
    with _parent_transport(broker) as client:
        rejected = RemoteSandboxRuntime(descriptor["url"], descriptor["control_token"], client=client)
        with pytest.raises(Exception, match="Sandbox duration exceeds the trusted runtime profile"):
            rejected.open(SandboxSpec(lifetime_seconds=configured, operation_key="without-ceiling"))

        opened: list[Any] = []

        service = fake_dspy_service()

        def optimize(*_args: Any, **_kwargs: Any) -> Any:
            """Open a box the way a child opener does, with the worker's lifetime setting."""
            session = current_sandbox_runtime().open(SandboxSpec(lifetime_seconds=configured))
            opened.append((session._lifetime, session.run("echo ok", timeout_seconds=configured - 1)))
            session.close()
            return service.run.return_value

        service.run.side_effect = optimize
        events: queue.Queue[dict] = queue.Queue()
        with (
            patch("core.worker.subprocess_runner.DspyService", return_value=service),
            patch("core.worker.subprocess_runner.ServiceRegistry", return_value=fake_service_registry()),
            patch(
                "core.worker.subprocess_runner.RemoteSandboxRuntime",
                side_effect=lambda *args, **kwargs: RemoteSandboxRuntime(*args, client=client, **kwargs),
            ),
        ):
            run_service_in_subprocess(
                {**REAL_RUN_PAYLOAD, "_budget_gateway_descriptor": descriptor}, "art-deadline", events, "spawn"
            )
    broker.close()

    kinds = [events.get_nowait()["type"] for _ in range(events.qsize())]
    assert EVENT_RESULT in kinds
    ((lifetime, result),) = opened
    assert lifetime == 120
    assert result.exit_code == 0
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call[0] for call in calls].count("run") == 1
    assert any(call[0] == "exec" for call in calls)

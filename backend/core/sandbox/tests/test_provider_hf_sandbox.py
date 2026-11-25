"""Tests for the HF Sandbox provider, against an in-memory fake of the SDK.

No test reaches Hugging Face: an autouse fixture swaps the SDK names in the
provider module for fakes, so the suite needs no network, no token and no
``hf_sandbox`` extra.
"""

from __future__ import annotations

import asyncio
import itertools
import shlex
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.hf_sandbox_usage import USAGE
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG
from core.sandbox.errors import SandboxFatalError, SandboxInfraError
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import hf_sandbox
from core.sandbox.providers.hf_sandbox import BOX_LABEL, HfSandboxEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.timeouts import PhaseTimeouts
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

from .test_environment import SandboxEnvironmentContract

CREATED = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


class FakeHttpError(Exception):
    """Stand in for ``HfHubHTTPError``, which carries the HTTP response."""

    def __init__(self, status_code: int) -> None:
        """Keep the status on a fake response.

        Args:
            status_code: The HTTP status that HF answered.
        """
        super().__init__(f"HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code)


class FakeSdkError(Exception):
    """Stand in for the SDK's ``SandboxError``, which carries a status code."""

    def __init__(self, status_code: int | None) -> None:
        """Keep the status.

        Args:
            status_code: The HTTP status of the failed call.
        """
        super().__init__(f"sandbox error {status_code}")
        self.status_code = status_code


@dataclass
class Job:
    """Hold one fake HF job, which is one sandbox."""

    id: str
    labels: dict[str, str]
    params: dict[str, Any]
    stage: str = "RUNNING"
    created_at: datetime = CREATED
    finished_at: datetime | None = None
    files: dict[str, bytes] = field(default_factory=dict)

    @property
    def status(self) -> SimpleNamespace:
        """Return the job status as the SDK's ``JobInfo`` does.

        Returns:
            An object with the stage name.
        """
        return SimpleNamespace(stage=self.stage)


@dataclass
class FakeHub:
    """Hold the shared state of the fake HF account."""

    jobs: list[Job] = field(default_factory=list)
    create_calls: int = 0
    create_error: BaseException | None = None
    create_makes_job: bool = True
    create_started: threading.Event = field(default_factory=threading.Event)
    create_gate: threading.Event | None = None
    list_error: BaseException | None = None
    stuck: set[str] = field(default_factory=set)
    cancels: list[str] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    exec_times_out: bool = False
    exec_gate: threading.Event | None = None
    read_error: BaseException | None = None
    token: str | None = "hf_fake"
    job_minutes: float = 3.0
    ids: Any = field(default_factory=lambda: itertools.count(1))

    def job(self, job_id: str) -> Job:
        """Return one job by id.

        Args:
            job_id: The job id.

        Returns:
            The job.
        """
        return next(job for job in self.jobs if job.id == job_id)


HUB = FakeHub()


def _shell(job: Job, command: str, env: dict[str, str]) -> tuple[int, str, str]:
    """Run a command in a tiny shell model.

    Args:
        job: The job whose files the command sees.
        command: The ``bash -c`` command.
        env: The command environment.

    Returns:
        The exit code, stdout and stderr.
    """
    words = shlex.split(command)
    verb, args = words[0], words[1:]
    if verb == "true":
        return 0, "", ""
    if verb == "false":
        return 1, "", ""
    if verb == "echo":
        return 0, " ".join(args) + "\n", ""
    if verb == "exit":
        return int(args[0]), "", ""
    if verb == "printenv":
        return (0, env[args[0]] + "\n", "") if args[0] in env else (1, "", "")
    if verb == "cat":
        data = job.files.get(args[0])
        return (0, data.decode("utf-8"), "") if data is not None else (1, "", "no such file\n")
    raise AssertionError(f"The fake shell cannot run {command!r}.")


class FakeFiles:
    """Stand in for ``Sandbox.files``."""

    def __init__(self, job: Job) -> None:
        """Keep the job whose files these are.

        Args:
            job: The fake job.
        """
        self._job = job

    def write(self, path: str, data: bytes) -> None:
        """Store the bytes of one file.

        Args:
            path: The absolute path.
            data: The file bytes.
        """
        self._job.files[path] = data

    def read(self, path: str) -> bytes:
        """Return the bytes of one file.

        Args:
            path: The absolute path.

        Returns:
            The file bytes.

        Raises:
            FakeSdkError: A 404 for a missing file, or the error the test sets.
        """
        if HUB.read_error is not None:
            raise HUB.read_error
        if path not in self._job.files:
            raise FakeSdkError(404)
        return self._job.files[path]


class FakeSandbox:
    """Stand in for ``huggingface_hub.Sandbox``."""

    def __init__(self, job: Job) -> None:
        """Wrap one fake job.

        Args:
            job: The fake job.
        """
        self.id = job.id
        self.job = job
        self.files = FakeFiles(job)
        self.closed = False

    @classmethod
    def create(cls, image: str, **params: Any) -> FakeSandbox:
        """Make a job, or fail as the test sets.

        Args:
            image: The image.
            **params: The other create options.

        Returns:
            The handle of the new job.

        Raises:
            BaseException: The create error that the test sets.
        """
        HUB.create_calls += 1
        HUB.create_started.set()
        if HUB.create_gate is not None:
            HUB.create_gate.wait(5)
        job = None
        if HUB.create_error is None or HUB.create_makes_job:
            job = Job(id=f"job-{next(HUB.ids)}", labels=dict(params["labels"]), params={"image": image, **params})
            HUB.jobs.append(job)
        if HUB.create_error is not None:
            raise HUB.create_error
        return cls(job)

    def run(self, argv: list[str], **options: Any) -> SimpleNamespace:
        """Run a command and stream its output.

        Args:
            argv: The command argv.
            **options: The SDK run options.

        Returns:
            A result like ``SandboxCommandResult``.
        """
        assert argv[:2] == ["/bin/bash", "-c"]
        assert options["check"] is False
        HUB.runs.append({"argv": argv, **options})
        if HUB.exec_gate is not None:
            HUB.exec_gate.wait(5)
        if HUB.exec_times_out:
            return SimpleNamespace(exit_code=None, stdout="partial", stderr="", timed_out=True)
        code, stdout, stderr = _shell(self.job, argv[2], options["env"] or {})
        if stdout and options["on_stdout"] is not None:
            options["on_stdout"](stdout)
        if stderr and options["on_stderr"] is not None:
            options["on_stderr"](stderr)
        return SimpleNamespace(exit_code=code, stdout=stdout, stderr=stderr, timed_out=False)

    def close(self) -> None:
        """Mark the HTTP client as closed."""
        self.closed = True


class FakeApi:
    """Stand in for ``huggingface_hub.HfApi``."""

    def list_jobs(self, *, labels: dict[str, str], namespace: str | None) -> list[Job]:
        """Return the jobs with all of these labels.

        Args:
            labels: The labels to match.
            namespace: The HF namespace.

        Returns:
            The matching jobs.

        Raises:
            BaseException: The list error that the test sets.
        """
        if HUB.list_error is not None:
            raise HUB.list_error
        return [job for job in HUB.jobs if labels.items() <= job.labels.items()]

    def cancel_job(self, *, job_id: str, namespace: str | None) -> None:
        """Cancel a job, unless the test keeps it stuck.

        Args:
            job_id: The job id.
            namespace: The HF namespace.
        """
        HUB.cancels.append(job_id)
        job = HUB.job(job_id)
        if job_id not in HUB.stuck:
            job.stage = "CANCELED"
            job.finished_at = job.created_at + timedelta(minutes=HUB.job_minutes)

    def inspect_job(self, *, job_id: str, namespace: str | None) -> Job:
        """Return one job.

        Args:
            job_id: The job id.
            namespace: The HF namespace.

        Returns:
            The job.
        """
        return HUB.job(job_id)


class FakeUsage:
    """Record the reservation calls of a protected box."""

    def __init__(self, request: dict[str, Any], operation_key: str) -> None:
        """Keep the priced request and the operation key.

        Args:
            request: The request that the provider priced.
            operation_key: The box's operation key.
        """
        self.request = request
        self.operation_key = operation_key
        self.calls: list[tuple[str, Any]] = [("reserve", HUB.create_calls)]
        self.confirm_error: BaseException | None = None

    def sent(self, label: str) -> None:
        """Record the create call.

        Args:
            label: The box label.
        """
        self.calls.append(("sent", label))

    def confirm_created(self, job: Any) -> None:
        """Record the job id.

        Args:
            job: The handle or ``JobInfo``.

        Raises:
            BaseException: The error that the test sets.
        """
        self.calls.append(("confirm", job.id))
        if self.confirm_error is not None:
            raise self.confirm_error

    def pending(self) -> None:
        """Record a pending hold."""
        self.calls.append(("pending", None))

    def fail(self, absent: bool = False) -> None:
        """Record a failure, and whether no job can exist.

        Args:
            absent: True when HF refused the create and the lookup found no job.
        """
        self.calls.append(("fail", absent))

    def stopped(self, created_at: Any = None, finished_at: Any = None) -> None:
        """Record the job's own times.

        Args:
            created_at: The job's creation time.
            finished_at: The job's end time.
        """
        self.calls.append(("stopped", (created_at, finished_at)))

    def settle(self) -> None:
        """Record a settle."""
        self.calls.append(("settle", None))

    def names(self) -> list[str]:
        """Return the call names in order.

        Returns:
            The call names.
        """
        return [name for name, _ in self.calls]


class UsageFactory:
    """Make one ``FakeUsage`` for each protected box, as ``USAGE.reserve`` would."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        """Start with no reservations.

        Args:
            gate: When set, each reserve waits for it, so a test can cancel during the reserve.
        """
        self.made: list[FakeUsage] = []
        self.gate = gate
        self.entered = threading.Event()

    def __call__(self, budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Reserve one fake hold.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        usage = FakeUsage(request, operation_key)
        self.made.append(usage)
        return usage


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHub]:
    """Swap the HF SDK for the fake and reset the fake account.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Yields:
        The fresh fake account.
    """
    global HUB
    HUB = FakeHub()
    monkeypatch.setattr(hf_sandbox, "Sandbox", FakeSandbox)
    monkeypatch.setattr(hf_sandbox, "HfApi", FakeApi)
    monkeypatch.setattr(hf_sandbox, "get_token", lambda: HUB.token)
    monkeypatch.setattr(hf_sandbox, "HfHubHTTPError", FakeHttpError)
    monkeypatch.setattr(hf_sandbox, "HfSandboxError", FakeSdkError)
    monkeypatch.setattr(hf_sandbox, "STOP_TIMEOUT", 0.2)
    monkeypatch.setattr(hf_sandbox, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(hf_sandbox, "_JOB_BOXES", {})
    monkeypatch.setattr(hf_sandbox, "_TASKS", set())
    yield HUB
    for gate in (HUB.create_gate, HUB.exec_gate):
        if gate is not None:
            gate.set()


async def _background() -> None:
    """Wait for the background tasks that the provider started, except the lifetime watchdogs."""
    while True:
        tasks = {task for task in hf_sandbox._TASKS if task.get_coro().__name__ != "_expire"}
        if not tasks:
            return
        await asyncio.wait(tasks)


class TestHfSandboxEnvironmentContract(SandboxEnvironmentContract):
    """Run the shared provider contract against the fake HF account."""

    provider_cls = HfSandboxEnvironment


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), **overrides})


def _protected(factory_: UsageFactory, **overrides: Any) -> HfSandboxEnvironment:
    """Return a protected box with a fake budget.

    Args:
        factory_: The fake usage factory.
        **overrides: Spec fields to replace.

    Returns:
        The unstarted box.
    """
    spec = _spec(operation_key="box-one", tags={JOB_TAG: "job-tag"}, **overrides)
    return HfSandboxEnvironment(spec, budget=object(), usage_factory=factory_)


def test_capabilities_are_billable_and_the_adapter_exists() -> None:
    """Mark the box billable, so the bridge funds it, and find its usage adapter."""
    assert HfSandboxEnvironment.capabilities.billable is True
    assert HfSandboxEnvironment.capabilities.name == "hf_sandbox"
    assert usage_adapter("hf_sandbox") is not None


def test_hf_sandbox_is_not_allowed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the provider in production, and load it outside production.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    assert factory.PROVIDERS["hf_sandbox"].production_allowed is False
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("hf_sandbox") is HfSandboxEnvironment
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="hf_sandbox"):
        factory.load_provider("hf_sandbox")


@pytest.mark.parametrize(
    ("kwargs", "spec", "message"),
    [
        ({"flavor": "t4-small"}, {}, "no pinned rate"),
        ({}, {"lifetime_seconds": 24 * 3600 + 1}, "at most 24 hours"),
        ({}, {"resources": ResourceRequest(cpus=4)}, "2 vCPUs"),
        ({}, {"resources": ResourceRequest(cpus=1, memory_mb=32 * 1024)}, "MB of memory"),
        ({}, {"resources": ResourceRequest(cpus=1, disk_mb=1024)}, "disk size"),
        ({}, {"resources": ResourceRequest(cpus=1, gpu=1)}, "0 GPUs"),
        ({"flavor": "a10g-small"}, {"resources": ResourceRequest(cpus=1, gpu=2)}, "1 GPUs"),
        ({"budget": object()}, {}, "operation key"),
    ],
)
def test_spec_that_has_no_price_or_does_not_fit_is_refused(
    kwargs: dict[str, Any], spec: dict[str, Any], message: str
) -> None:
    """Refuse a box before any HF call when its flavor, size or lifetime has no funded price.

    Args:
        kwargs: Provider options.
        spec: Spec fields to replace.
        message: Part of the expected error.
    """
    with pytest.raises(SandboxSpecError, match=message):
        HfSandboxEnvironment(_spec(**spec), **kwargs)
    assert HUB.create_calls == 0


def test_ignored_size_still_refuses_a_gpu_that_the_flavor_lacks() -> None:
    """Skip the CPU check in IGNORE mode, but never start a GPU box on a CPU flavor."""
    HfSandboxEnvironment(_spec(resources=ResourceRequest(cpus=4, mode=ResourceMode.IGNORE)))
    with pytest.raises(SandboxSpecError, match="GPUs"):
        HfSandboxEnvironment(_spec(resources=ResourceRequest(cpus=1, gpu=1, mode=ResourceMode.IGNORE)))


async def test_preflight_needs_the_sdk_and_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast with a fatal error when the SDK or the token is missing.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    HUB.token = None
    with pytest.raises(SandboxFatalError, match="no HF token"):
        await HfSandboxEnvironment(_spec()).start()
    monkeypatch.setattr(hf_sandbox, "Sandbox", None)
    with pytest.raises(SandboxFatalError, match="hf_sandbox"):
        await HfSandboxEnvironment(_spec()).start()
    assert HUB.create_calls == 0


async def test_create_sets_the_lifetime_labels_and_keeps_the_token_out() -> None:
    """Pass the spec lifetime as the idle timeout, a unique label, and no HF token."""
    timeouts = PhaseTimeouts(start=45, setup=30, run=30, score=30)
    env = HfSandboxEnvironment(
        _spec(tags={JOB_TAG: "job-tag"}, env={"A": "1"}), flavor="cpu-upgrade", timeouts=timeouts
    )
    async with env:
        params = HUB.jobs[0].params
    assert params["image"] == "python:3.12"
    assert params["flavor"] == "cpu-upgrade"
    assert params["idle_timeout"] == 300
    assert params["forward_hf_token"] is False
    assert params["start_timeout"] == 45
    assert params["env"] == {"A": "1"}
    assert params["labels"][JOB_TAG] == "job-tag"
    assert params["labels"][BOX_LABEL].startswith("skynet-")
    assert HfSandboxEnvironment(_spec())._label != env._label


async def test_reserve_then_settle_after_a_confirmed_cancel() -> None:
    """Reserve before the create, then settle once with the job's own times after HF confirms the cancel."""
    usages = UsageFactory()
    env = _protected(usages)
    await env.start()
    assert (await env.exec("echo hi")).stdout == "hi\n"
    await env.stop()
    await env.stop()
    (usage,) = usages.made
    assert usage.calls[0] == ("reserve", 0)
    assert usage.names() == ["reserve", "sent", "confirm", "stopped", "settle"]
    assert usage.request == {"flavor": "cpu-basic", "lifetime_ms": 300_000, "exposed_ports": 1, "image": "python:3.12"}
    assert usage.calls[3][1] == (CREATED, CREATED + timedelta(minutes=3))
    assert HUB.cancels == ["job-1"]
    assert hf_sandbox._JOB_BOXES == {}


async def test_failed_create_that_left_a_job_cancels_and_settles_it() -> None:
    """Find the job of a failed create by its label, then cancel and settle it."""
    HUB.create_error = FakeHttpError(500)
    usages = UsageFactory()
    env = _protected(usages)
    with pytest.raises(SandboxInfraError, match="could not create"):
        await env.start()
    (usage,) = usages.made
    assert usage.names() == ["reserve", "sent", "confirm", "stopped", "settle"]
    assert HUB.job("job-1").stage == "CANCELED"


async def test_refused_create_that_still_left_a_job_settles_and_does_not_release() -> None:
    """Settle a refused create whose label lookup still finds a job, because a box existed."""
    HUB.create_error = FakeHttpError(422)
    usages = UsageFactory()
    with pytest.raises(SandboxInfraError):
        await _protected(usages).start()
    assert ("fail", True) not in usages.made[0].calls
    assert usages.made[0].names()[-1] == "settle"


@pytest.mark.parametrize("error", [FakeHttpError(422), FakeHttpError(402), ValueError("bad label")])
async def test_refused_create_with_no_job_releases(error: BaseException) -> None:
    """Release the hold only when HF refused the create and the label lookup finds no job.

    Args:
        error: The create error.
    """
    HUB.create_error = error
    HUB.create_makes_job = False
    usages = UsageFactory()
    with pytest.raises(SandboxInfraError):
        await _protected(usages).start()
    assert usages.made[0].names() == ["reserve", "sent", "fail"]
    assert usages.made[0].calls[-1] == ("fail", True)


@pytest.mark.parametrize("error", [FakeHttpError(500), FakeHttpError(429), TimeoutError("slow"), RuntimeError("?")])
async def test_unknown_create_with_no_job_does_not_release(error: BaseException) -> None:
    """Keep the hold when the create failure does not prove that HF made no job.

    Args:
        error: The create error.
    """
    HUB.create_error = error
    HUB.create_makes_job = False
    usages = UsageFactory()
    with pytest.raises(SandboxInfraError):
        await _protected(usages).start()
    assert usages.made[0].calls[-1] == ("fail", False)


async def test_failed_lookup_keeps_the_hold_pending() -> None:
    """Keep the hold pending when the label lookup after a failed create fails."""
    HUB.create_error = FakeHttpError(422)
    HUB.create_makes_job = False
    HUB.list_error = RuntimeError("down")
    usages = UsageFactory()
    with pytest.raises(SandboxInfraError):
        await _protected(usages).start()
    assert usages.made[0].names() == ["reserve", "sent", "pending"]


async def test_cancelled_start_hands_the_create_to_one_background_task() -> None:
    """Keep the cancel, and let one tracked task cancel and settle the job that the create made later."""
    HUB.create_gate = threading.Event()
    usages = UsageFactory()
    env = _protected(usages)
    start = asyncio.ensure_future(env.start())
    await asyncio.to_thread(HUB.create_started.wait, 5)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert len(hf_sandbox._TASKS) == 1
    await env.stop()
    assert usages.made[0].names() == ["reserve", "sent"]
    HUB.create_gate.set()
    await _background()
    assert usages.made[0].names() == ["reserve", "sent", "confirm", "stopped", "settle"]
    assert HUB.job("job-1").stage == "CANCELED"
    assert not hf_sandbox._TASKS


async def test_stop_during_the_start_ends_the_start_and_the_late_task_settles() -> None:
    """Turn a stop during the start into an infra error, and settle the late job once."""
    HUB.create_gate = threading.Event()
    usages = UsageFactory()
    env = _protected(usages)
    start = asyncio.ensure_future(env.start())
    await asyncio.to_thread(HUB.create_started.wait, 5)
    await env.stop()
    with pytest.raises(SandboxInfraError, match="stopped during its start"):
        await start
    HUB.create_gate.set()
    await _background()
    assert usages.made[0].names().count("settle") == 1


async def test_cancel_during_the_reserve_releases_the_late_hold() -> None:
    """Release a hold that a cancelled reserve made later, because no create was sent."""
    usages = UsageFactory(gate=threading.Event())
    env = _protected(usages)
    start = asyncio.ensure_future(env.start())
    await asyncio.to_thread(usages.entered.wait, 5)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    usages.gate.set()
    await _background()
    assert usages.made[0].names() == ["reserve", "fail"]
    assert HUB.create_calls == 0


async def test_unconfirmed_cancel_stays_pending_and_the_job_sweep_retries() -> None:
    """Keep the box listed and the hold pending until HF confirms the end, then settle once."""
    usages = UsageFactory()
    env = _protected(usages)
    await env.start()
    HUB.stuck.add("job-1")
    with pytest.raises(UsagePendingError, match="job-1"):
        await env.stop()
    assert usages.made[0].names()[-1] == "pending"
    assert env in hf_sandbox._JOB_BOXES["job-tag"]
    assert await HfSandboxEnvironment.stop_job_sandboxes("job-tag") == 0
    HUB.stuck.clear()
    assert await HfSandboxEnvironment.stop_job_sandboxes("job-tag") == 1
    assert usages.made[0].names().count("settle") == 1
    assert hf_sandbox._JOB_BOXES == {}


async def test_lifetime_cap_cancels_and_settles_the_job() -> None:
    """Cancel the job at the spec lifetime, because HF's own job timeout is 24 hours."""
    usages = UsageFactory()
    env = _protected(usages, lifetime_seconds=0.05)
    await env.start()
    for _ in range(200):
        if env._ended:
            break
        await asyncio.sleep(0.01)
    assert HUB.job("job-1").stage == "CANCELED"
    assert usages.made[0].names()[-1] == "settle"
    with pytest.raises(SandboxInfraError, match="not running"):
        await env.exec("true")
    await env.stop()
    assert usages.made[0].names().count("settle") == 1


async def test_unbound_job_is_stopped_and_the_error_kept() -> None:
    """Stop a created box whose job could not bind to the hold, and raise the bind error."""
    usages = UsageFactory()
    env = _protected(usages)
    original = UsageFactory.__call__

    def failing(self: UsageFactory, budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a reservation whose confirm fails.

        Args:
            self: The factory.
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        usage = original(self, budget, request, operation_key=operation_key)
        usage.confirm_error = UsagePendingError("no bind")
        return usage

    env._usage_factory = failing.__get__(usages)
    with pytest.raises(UsagePendingError, match="no bind"):
        await env.start()
    assert HUB.job("job-1").stage == "CANCELED"
    assert usages.made[0].names()[-1] == "settle"


async def test_exec_reports_a_timeout_as_exit_code_124(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return exit code 124 for an SDK timeout and for a hung SDK call.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    async with HfSandboxEnvironment(_spec()) as env:
        HUB.exec_times_out = True
        result = await env.exec("true", timeout_sec=5)
        assert (result.exit_code, result.timed_out, result.stdout) == (124, True, "partial")
        assert HUB.runs[-1]["timeout"] == 5
        HUB.exec_times_out = False
        HUB.exec_gate = threading.Event()
        monkeypatch.setattr(hf_sandbox, "EXEC_GRACE", 0.05)
        result = await env.exec("true", timeout_sec=0.01)
        assert (result.exit_code, result.timed_out) == (124, True)
        HUB.exec_gate.set()


async def test_download_maps_sdk_errors() -> None:
    """Return None for a missing file, and raise an infra error for other read failures."""
    async with HfSandboxEnvironment(_spec()) as env:
        assert await env.download_file("/tmp/missing") is None
        HUB.read_error = FakeSdkError(500)
        with pytest.raises(SandboxInfraError, match="read a file"):
            await env.download_file("/tmp/missing")
        HUB.read_error = None
        HUB.jobs[0].files["/tmp/bin"] = b"\xff\xfe"
        with pytest.raises(SandboxInfraError, match="not UTF-8"):
            await env.download_file("/tmp/bin")


async def test_stop_closes_the_client() -> None:
    """Close the SDK client once HF confirmed the end of the job."""
    env = HfSandboxEnvironment(_spec())
    await env.start()
    sandbox = env._sandbox
    await env.stop()
    assert sandbox.closed is True


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Create a private funded wallet and ledger at cost.

    Args:
        tmp_path: Per-test directory for the SQLite file.
        monkeypatch: The pytest monkeypatch fixture.

    Yields:
        The engine bound to the funded ledger.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    engine = create_engine(f"sqlite:///{tmp_path / 'hf.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=1000, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


async def test_real_ledger_settles_the_job_minutes(database: Engine) -> None:
    """Settle a real hold at the job's billed minutes, below the hold, with the job id on the row.

    Args:
        database: The SQLite ledger.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 500, idempotency_key="draft")
    runtime = BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)
    env = HfSandboxEnvironment(_spec(operation_key="box-one"), budget=runtime, usage_factory=USAGE.reserve)
    await env.start()
    await env.stop()
    with Session(database) as session:
        row = session.scalars(select(ExecutionOperationModel)).one()
    assert row.state == "settled"
    assert row.provider_request_id == "job-1"
    assert 0 < row.actual_units < row.max_units
    assert runtime._pinned == {}

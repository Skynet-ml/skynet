"""Tests for the Hyperbrowser sandbox provider, against an in-memory fake of the SDK.

No test reaches Hyperbrowser: an autouse fixture swaps the SDK names in the
provider module for fakes, so the suite needs no network, no API key and no
``hyperbrowser`` extra. The lifetime cap sleeps on a fake clock.
"""

from __future__ import annotations

import asyncio
import itertools
import shlex
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.hyperbrowser_usage import HyperbrowserUsageReservation
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import SandboxFatalError, SandboxInfraError
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import hyperbrowser
from core.sandbox.providers.hyperbrowser import HyperbrowserEnvironment
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.telemetry import use_output_sink
from core.sandbox.timeouts import PhaseTimeouts
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

from .test_environment import SandboxEnvironmentContract


class FakeHyperbrowserError(Exception):
    """Stand in for ``hyperbrowser.HyperbrowserError``, with the HTTP status of the call."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Keep the status.

        Args:
            message: The error text.
            status_code: The HTTP status that Hyperbrowser answered, or ``None``.
        """
        super().__init__(message)
        self.status_code = status_code


@dataclass
class BoxState:
    """Hold one fake sandbox in the fake cloud."""

    id: str
    params: dict[str, Any]
    stopped: bool = False
    network: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    execs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeCloud:
    """Hold the shared state of the fake Hyperbrowser account."""

    boxes: list[BoxState] = field(default_factory=list)
    clients: list[FakeClient] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    create_calls: int = 0
    create_error: BaseException | None = None
    create_gate: asyncio.Event | None = None
    create_started: asyncio.Event = field(default_factory=asyncio.Event)
    params_error: BaseException | None = None
    granted_timeout: Any = None
    fixup_exit: int = 0
    mkdir_exit: int = 0
    exec_result: dict[str, Any] | None = None
    exec_hangs: bool = False
    stop_error: BaseException | None = None
    ids: Any = field(default_factory=lambda: itertools.count(1))


CLOUD = FakeCloud()


def _shell(box: BoxState, command: str, env: dict[str, str]) -> tuple[int, str, str]:
    """Run one command of a tiny shell model.

    Args:
        box: The sandbox.
        command: The command inside ``bash -c``.
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
    if verb == "exit":
        return int(args[0]), "", ""
    if verb == "echo":
        return 0, " ".join(args) + "\n", ""
    if verb == "printenv":
        return (0, env[args[0]] + "\n", "") if args[0] in env else (1, "", "")
    if verb == "cat":
        if args[0] in box.files:
            return 0, box.files[args[0]].decode("utf-8"), ""
        return 1, "", "No such file\n"
    return 127, "", f"{verb}: command not found\n"


class FakeFiles:
    """Stand in for the SDK file API of one sandbox."""

    def __init__(self, box: BoxState, user: str | None = None) -> None:
        """Keep the sandbox and the user.

        Args:
            box: The sandbox.
            user: The user that the calls run as.
        """
        self.box = box
        self.user = user

    def with_run_as(self, user: str) -> FakeFiles:
        """Return the file API that runs as this user.

        Args:
            user: The user.

        Returns:
            The file API.
        """
        return FakeFiles(self.box, user)

    async def upload(self, path: str, data: bytes) -> None:
        """Write one file.

        Args:
            path: The sandbox path.
            data: The file bytes.
        """
        assert self.user == "root"
        self.box.files[path] = data

    async def download(self, path: str) -> bytes:
        """Read one file.

        Args:
            path: The sandbox path.

        Returns:
            The file bytes.
        """
        return self.box.files[path]

    async def exists(self, path: str) -> bool:
        """Return True when the file exists.

        Args:
            path: The sandbox path.

        Returns:
            True when the file exists.
        """
        return path in self.box.files


class FakeSandbox:
    """Stand in for the SDK sandbox handle."""

    def __init__(self, box: BoxState) -> None:
        """Wrap one sandbox.

        Args:
            box: The sandbox.
        """
        self.box = box
        self.id = box.id
        self.timeout_minutes = (
            CLOUD.granted_timeout if CLOUD.granted_timeout is not None else box.params["timeout_minutes"]
        )
        self.files = FakeFiles(box)

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        run_as: str | None = None,
    ) -> SimpleNamespace:
        """Run one command.

        Args:
            command: The command string.
            cwd: The working directory.
            env: The command environment.
            timeout_sec: The limit.
            run_as: The user.

        Returns:
            The process result.
        """
        assert not self.box.stopped
        self.box.execs.append(
            {"command": command, "cwd": cwd, "env": env, "timeout_sec": timeout_sec, "run_as": run_as}
        )
        if command == hyperbrowser._FIX_RUNTIME_COMMAND:
            return SimpleNamespace(status="exited", exit_code=CLOUD.fixup_exit, stdout="", stderr="no /dev")
        if command.startswith("mkdir -p "):
            return SimpleNamespace(status="exited", exit_code=CLOUD.mkdir_exit, stdout="", stderr="")
        if CLOUD.exec_hangs:
            await asyncio.Event().wait()
        if CLOUD.exec_result is not None:
            return SimpleNamespace(**{"stdout": "", "stderr": "", **CLOUD.exec_result})
        argv = shlex.split(command)
        assert argv[:2] == ["bash", "-c"]
        code, stdout, stderr = _shell(self.box, argv[2], env or {})
        return SimpleNamespace(status="exited", exit_code=code, stdout=stdout, stderr=stderr)

    async def stop(self) -> None:
        """Stop the sandbox.

        Raises:
            BaseException: The stop error that the test sets.
        """
        CLOUD.events.append(f"stop:{self.id}")
        if CLOUD.stop_error is not None:
            raise CLOUD.stop_error
        self.box.stopped = True

    async def info(self) -> SimpleNamespace:
        """Return the sandbox detail.

        Returns:
            The detail with the reported usage.
        """
        return SimpleNamespace(
            credits_used=7, duration=3, start_time=1, end_time=4, close_reason="stop", status="closed"
        )

    async def update_network(self, params: dict[str, Any]) -> None:
        """Replace the egress policy.

        Args:
            params: The network parameters.
        """
        self.box.network.append(params)


class FakeSandboxes:
    """Stand in for ``client.sandboxes``."""

    async def create(self, params: Any) -> FakeSandbox:
        """Create one sandbox, or fail as the test sets.

        Args:
            params: The create parameters.

        Returns:
            The sandbox handle.

        Raises:
            BaseException: The create error that the test sets.
        """
        CLOUD.create_calls += 1
        CLOUD.events.append("create")
        CLOUD.create_started.set()
        if CLOUD.create_gate is not None:
            await CLOUD.create_gate.wait()
        if CLOUD.create_error is not None:
            raise CLOUD.create_error
        box = BoxState(id=f"hb-{next(CLOUD.ids)}", params=dict(params))
        CLOUD.boxes.append(box)
        return FakeSandbox(box)


class FakeClient:
    """Stand in for ``hyperbrowser.AsyncHyperbrowser``."""

    def __init__(self, api_key: str | None = None, timeout: int = 30) -> None:
        """Record the request timeout.

        Args:
            api_key: The API key, unused.
            timeout: The request timeout.
        """
        self.timeout = timeout
        self.closed = False
        self.sandboxes = FakeSandboxes()
        CLOUD.clients.append(self)

    async def close(self) -> None:
        """Close the client."""
        self.closed = True


class FakeParams(dict):
    """Stand in for ``CreateSandboxParams``: a dict that the fake checks before any request."""

    def __init__(self, **params: Any) -> None:
        """Check and keep the parameters.

        Args:
            **params: The create parameters.

        Raises:
            BaseException: The validation error that the test sets.
        """
        if CLOUD.params_error is not None:
            raise CLOUD.params_error
        super().__init__(**params)


async def _clock(seconds: float) -> None:
    """Sleep forever on the fake clock, so the lifetime cap only fires when a test fires it.

    Args:
        seconds: The requested sleep.
    """
    await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    """Swap the Hyperbrowser SDK for the fake and reset the fake cloud.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fresh fake cloud.
    """
    global CLOUD
    CLOUD = FakeCloud()
    monkeypatch.setattr(hyperbrowser, "AsyncHyperbrowser", FakeClient)
    monkeypatch.setattr(hyperbrowser, "HyperbrowserError", FakeHyperbrowserError)
    monkeypatch.setattr(hyperbrowser, "CreateSandboxParams", FakeParams)
    monkeypatch.setattr(hyperbrowser, "_sleep", _clock)
    monkeypatch.setattr(hyperbrowser, "_JOB_BOXES", {})
    monkeypatch.setattr(hyperbrowser, "_LATE_TASKS", set())
    return CLOUD


class TestHyperbrowserEnvironmentContract(SandboxEnvironmentContract):
    """Run the shared provider contract against the fake Hyperbrowser cloud."""

    provider_cls = HyperbrowserEnvironment


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), **overrides})


async def _late() -> None:
    """Wait for the background stops and releases."""
    while hyperbrowser._LATE_TASKS:
        await asyncio.wait(set(hyperbrowser._LATE_TASKS))


def test_hyperbrowser_is_billable_dev_only_and_has_a_usage_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Meter Hyperbrowser, find its usage adapter, and refuse it in production.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    assert hyperbrowser.HYPERBROWSER_CAPABILITIES.billable is True
    assert usage_adapter("hyperbrowser") is not None
    assert factory.PROVIDERS["hyperbrowser"].production_allowed is False
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("hyperbrowser") is HyperbrowserEnvironment
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="hyperbrowser"):
        factory.load_provider("hyperbrowser")


def test_preflight_needs_the_sdk_and_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the missing extra or key before any call.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.delenv("HYPERBROWSER_API_KEY", raising=False)
    with pytest.raises(SandboxFatalError, match="HYPERBROWSER_API_KEY"):
        HyperbrowserEnvironment.preflight()
    monkeypatch.setenv("HYPERBROWSER_API_KEY", "fake")
    HyperbrowserEnvironment.preflight()
    monkeypatch.setattr(hyperbrowser, "AsyncHyperbrowser", None)
    with pytest.raises(SandboxFatalError, match="hyperbrowser package"):
        HyperbrowserEnvironment.preflight()


def test_a_lifetime_above_the_ceiling_is_refused() -> None:
    """Refuse a lifetime longer than 24 hours, because no hold can fund an unknown end."""
    with pytest.raises(SandboxSpecError, match="at most"):
        HyperbrowserEnvironment(_spec(lifetime_seconds=24 * 3600 + 1))


async def test_create_sends_the_timeout_size_disk_floor_and_open_egress(fake_sdk: FakeCloud) -> None:
    """Send the lifetime as whole minutes, the size, the 2 GiB disk floor and open egress, then fix the runtime.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    spec = _spec(lifetime_seconds=300.5, resources=ResourceRequest(cpus=2, memory_mb=3000, disk_mb=100))
    async with HyperbrowserEnvironment(spec):
        pass
    (box,) = fake_sdk.boxes
    assert box.params == {
        "image_name": "default",
        "cpu": 2,
        "memory_mib": 3000,
        "timeout_minutes": 6,
        "disk_mib": 2048,
        "allow_internet_access": True,
    }
    assert box.execs[0] == {
        "command": hyperbrowser._FIX_RUNTIME_COMMAND,
        "cwd": "/",
        "env": None,
        "timeout_sec": hyperbrowser._FIX_RUNTIME_TIMEOUT_SEC,
        "run_as": "root",
    }
    assert box.stopped
    (client,) = fake_sdk.clients
    assert client.timeout == 120
    assert client.closed


async def test_memory_defaults_to_2_gib_per_core_and_the_spec_image_is_sent(fake_sdk: FakeCloud) -> None:
    """Give a box with no memory 2 GiB for each core, so its bill has a ceiling.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    async with HyperbrowserEnvironment(_spec(image="node", resources=ResourceRequest(cpus=3))):
        pass
    assert fake_sdk.boxes[0].params["memory_mib"] == 6144
    assert fake_sdk.boxes[0].params["image_name"] == "node"
    assert "disk_mib" not in fake_sdk.boxes[0].params


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"network_disabled": True}, {"allow_internet_access": False}),
        ({"allowed_hosts": ("api.example.com",)}, {"allow_internet_access": False, "allow_out": ["api.example.com"]}),
    ],
)
async def test_the_spec_network_maps_to_the_create(
    fake_sdk: FakeCloud, spec: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Map a deny-all or allowlist spec to the create parameters.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        spec: The spec network fields.
        expected: The network parameters that the create gets.
    """
    async with HyperbrowserEnvironment(_spec(**spec)):
        pass
    params = fake_sdk.boxes[0].params
    assert {key: params[key] for key in expected} == expected
    assert "deny_out" not in params


async def test_set_network_policy_replaces_the_whole_policy(fake_sdk: FakeCloud) -> None:
    """Clear the lists that a mode leaves unset, so an update replaces the earlier policy.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    async with HyperbrowserEnvironment(_spec()) as env:
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("a.example",)))
        await env.disable_network()
        await env.set_network_policy(NetworkPolicy())
    assert fake_sdk.boxes[0].network == [
        {"allow_internet_access": False, "allow_out": ["a.example"], "deny_out": []},
        {"allow_internet_access": False, "allow_out": [], "deny_out": []},
        {"allow_internet_access": True, "allow_out": [], "deny_out": []},
    ]


async def test_exec_sends_bash_env_user_timeout_and_cwd(fake_sdk: FakeCloud) -> None:
    """Wrap the command in bash, merge the spec env, run as root on sudo and round the limit up.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    async with HyperbrowserEnvironment(_spec(env={"A": "1", "B": "1"})) as env:
        result = await env.exec("printenv B", cwd="/work", env={"B": "2"}, timeout_sec=1.2, sudo=True)
    assert result.stdout == "2\n"
    call = fake_sdk.boxes[0].execs[-1]
    assert call == {
        "command": "bash -c 'printenv B'",
        "cwd": "/work",
        "env": {"A": "1", "B": "2"},
        "timeout_sec": 2,
        "run_as": "root",
    }


async def test_exec_uses_the_run_phase_timeout_and_the_current_sink(fake_sdk: FakeCloud) -> None:
    """Use the run phase limit by default, and send the output to the sink of the current task.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    chunks: list[tuple[str, str]] = []
    timeouts = PhaseTimeouts(start=30, setup=30, run=45, score=30)
    async with HyperbrowserEnvironment(_spec(), timeouts=timeouts) as env:
        with use_output_sink(lambda stream, text: chunks.append((stream, text))):
            await env.exec("echo hi")
    assert fake_sdk.boxes[0].execs[-1]["timeout_sec"] == 45
    assert fake_sdk.boxes[0].execs[-1]["run_as"] is None
    assert chunks == [("stdout", "hi\n")]


@pytest.mark.parametrize(
    ("result", "timed_out", "exit_code"),
    [
        ({"status": "timed_out", "exit_code": None}, True, 124),
        ({"status": "killed", "exit_code": 137, "started_at": 0, "completed_at": 5000}, True, 124),
        ({"status": "killed", "exit_code": 137, "started_at": 0, "completed_at": 4999}, False, 137),
        ({"status": "failed", "exit_code": None}, False, 1),
    ],
)
async def test_sdk_results_map_to_the_contract(
    fake_sdk: FakeCloud, result: dict[str, Any], timed_out: bool, exit_code: int
) -> None:
    """Report Hyperbrowser's own limit as exit code 124, and a missing exit code as 1.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        result: The SDK result fields.
        timed_out: Whether the contract result is a timeout.
        exit_code: The contract exit code.
    """
    async with HyperbrowserEnvironment(_spec()) as env:
        fake_sdk.exec_result = result
        got = await env.exec("work", timeout_sec=5)
    assert got.timed_out is timed_out
    assert got.exit_code == exit_code


async def test_a_hung_exec_call_returns_a_timed_out_result(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End an exec call that never returns at the limit plus the grace.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(hyperbrowser, "_TIMEOUT_GRACE_SEC", 0.01)
    async with HyperbrowserEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        result = await env.exec("work", timeout_sec=0.01)
    assert result.timed_out
    assert result.exit_code == 124


async def test_upload_creates_the_parent_as_root(fake_sdk: FakeCloud) -> None:
    """Create the parent directory as root before the upload, and refuse a failed mkdir.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    async with HyperbrowserEnvironment(_spec()) as env:
        await env.upload_files({"/srv/app/a.txt": "x"})
        assert fake_sdk.boxes[0].execs[-1]["command"] == "mkdir -p /srv/app"
        assert fake_sdk.boxes[0].execs[-1]["run_as"] == "root"
        assert fake_sdk.boxes[0].execs[-1]["timeout_sec"] == 10
        fake_sdk.mkdir_exit = 1
        with pytest.raises(SandboxInfraError, match="/srv/b"):
            await env.upload_files({"/srv/b/a.txt": "x"})


async def test_a_longer_granted_timeout_stops_the_box(fake_sdk: FakeCloud) -> None:
    """Refuse and stop a box that came back with a longer timeout than the funded one.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    fake_sdk.granted_timeout = 6
    env = HyperbrowserEnvironment(_spec())
    with pytest.raises(SandboxInfraError, match="timeout_minutes=6"):
        await env.start()
    assert fake_sdk.boxes[0].stopped
    assert fake_sdk.clients[0].closed


async def test_a_failed_runtime_fix_stops_the_box(fake_sdk: FakeCloud) -> None:
    """Stop the box when the ``/dev`` fix fails.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    fake_sdk.fixup_exit = 1
    with pytest.raises(SandboxInfraError, match="no /dev"):
        await HyperbrowserEnvironment(_spec()).start()
    assert fake_sdk.boxes[0].stopped


@pytest.mark.parametrize(("cap_at", "left"), [(1030.0, 60.0), (1100.0, 0.0)])
async def test_the_lifetime_cap_stops_the_box(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch, cap_at: float, left: float
) -> None:
    """Stop the box when the spec lifetime, counted from the create request, ends on the fake clock.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        monkeypatch: The pytest monkeypatch fixture.
        cap_at: The fake time when the cap starts; the create was sent at 1000.
        left: The lifetime that the cap must still wait.
    """
    monkeypatch.setattr(hyperbrowser, "_monotonic", iter([1000.0, cap_at]).__next__)
    slept: list[float] = []
    ring = asyncio.Event()

    async def clock(seconds: float) -> None:
        """Record the cap and wait until the test rings it.

        Args:
            seconds: The requested sleep.
        """
        slept.append(seconds)
        await ring.wait()

    monkeypatch.setattr(hyperbrowser, "_sleep", clock)
    env, made = _protected(lifetime_seconds=90)
    await env.start()
    await asyncio.sleep(0)
    assert slept == [left]
    ring.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert fake_sdk.boxes[0].stopped
    assert made[0].names()[-3:] == ["stopped", "report", "settle"]
    with pytest.raises(RuntimeError, match="not running"):
        await env.exec("true")
    await env.stop()
    assert made[0].names().count("settle") == 1


async def test_stop_job_sandboxes_goes_on_after_a_pending_hold(fake_sdk: FakeCloud) -> None:
    """Count a box whose settle stays pending, and stop the other boxes of the job.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    first, made = _protected(tags={JOB_TAG: "job"})
    second = HyperbrowserEnvironment(_spec(tags={JOB_TAG: "job"}))
    await first.start()
    await second.start()
    made[0].settle_error = UsagePendingError("ledger down")
    assert await HyperbrowserEnvironment.stop_job_sandboxes("job") == 2
    assert all(box.stopped for box in fake_sdk.boxes)
    assert hyperbrowser._JOB_BOXES == {}


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
        self.calls: list[tuple[str, Any]] = []
        self.settle_error: BaseException | None = None

    def sent(self) -> None:
        """Record the send, with the creates that happened before it."""
        self.calls.append(("sent", CLOUD.create_calls))

    def confirm_created(self, sandbox: Any) -> None:
        """Record the created box.

        Args:
            sandbox: The sandbox handle.
        """
        self.calls.append(("confirm", sandbox.id))

    def stopped(self) -> None:
        """Record the confirmed stop."""
        self.calls.append(("stopped", [box.stopped for box in CLOUD.boxes]))

    def report(self, vendor: dict[str, Any] | None) -> None:
        """Record the vendor usage.

        Args:
            vendor: The reported usage.
        """
        self.calls.append(("report", vendor))

    def settle(self) -> None:
        """Record a settle, or fail as the test sets.

        Raises:
            BaseException: The settle error that the test sets.
        """
        self.calls.append(("settle", [box.stopped for box in CLOUD.boxes]))
        if self.settle_error is not None:
            raise self.settle_error

    def fail(self, absent: bool = False) -> None:
        """Record a failed create, and whether Hyperbrowser refused it.

        Args:
            absent: True when the provider saw a refused create.
        """
        self.calls.append(("fail", absent))

    def names(self) -> list[str]:
        """Return the names of the recorded calls.

        Returns:
            The call names in order.
        """
        return [name for name, _ in self.calls]


def _protected(**spec: Any) -> tuple[HyperbrowserEnvironment, list[FakeUsage]]:
    """Open a protected box whose reservations are fakes.

    Args:
        **spec: Spec fields to replace.

    Returns:
        The box and the list that gets its reservation.
    """
    made: list[FakeUsage] = []

    def factory_(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make and keep one fake reservation, and record the reserve before any create.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        assert budget == "budget"
        made.append(FakeUsage(request, operation_key))
        made[-1].calls.append(("reserve", CLOUD.create_calls))
        return made[-1]

    env = HyperbrowserEnvironment(_spec(operation_key="box-1", **spec), budget="budget", usage_factory=factory_)
    return env, made


def test_a_protected_box_needs_an_operation_key() -> None:
    """Refuse a budget without an operation key, before any call."""
    with pytest.raises(SandboxSpecError, match="operation key"):
        HyperbrowserEnvironment(_spec(), budget="budget")


async def test_a_protected_box_reserves_before_the_create_and_settles_after_the_stop(fake_sdk: FakeCloud) -> None:
    """Reserve the whole lifetime first, confirm the created box, and settle once it stopped.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    env, made = _protected(lifetime_seconds=61)
    async with env:
        pass
    usage = made[0]
    assert usage.operation_key == "box-1"
    assert usage.request == {"image": "default", "timeout_minutes": 2, "vcpus": 1, "memory_mib": 2048}
    assert fake_sdk.boxes[0].params["timeout_minutes"] == 2
    assert usage.calls == [
        ("reserve", 0),
        ("sent", 0),
        ("confirm", "hb-1"),
        ("stopped", [True]),
        (
            "report",
            {
                "credits_used": 7,
                "duration": 3,
                "start_time": 1,
                "end_time": 4,
                "close_reason": "stop",
                "status": "closed",
            },
        ),
        ("settle", [True]),
    ]


async def test_a_failed_stop_still_settles_and_raises(fake_sdk: FakeCloud) -> None:
    """Settle at the funded time when the stop fails, and raise the stop error.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    env, made = _protected()
    await env.start()
    fake_sdk.stop_error = FakeHyperbrowserError("gone", status_code=500)
    with pytest.raises(FakeHyperbrowserError):
        await env.stop()
    assert made[0].names() == ["reserve", "sent", "confirm", "settle"]
    assert fake_sdk.clients[0].closed


async def test_a_failed_start_after_the_create_settles(fake_sdk: FakeCloud) -> None:
    """Stop and settle a box whose start failed after the create.

    The last ``fail`` call finds the hold settled, so the real reservation
    only unpins it.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    fake_sdk.granted_timeout = 60
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert made[0].calls[-1] == ("fail", False)
    assert made[0].names() == ["reserve", "sent", "confirm", "stopped", "report", "settle", "fail"]
    assert fake_sdk.boxes[0].stopped


@pytest.mark.parametrize("status", [400, 402, 403, 422, 429])
async def test_a_refused_create_reports_that_no_box_exists(fake_sdk: FakeCloud, status: int) -> None:
    """Tell the reservation that no box exists when Hyperbrowser refused the create.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        status: The refusal status.
    """
    fake_sdk.create_error = FakeHyperbrowserError("refused", status_code=status)
    env, made = _protected()
    with pytest.raises(FakeHyperbrowserError):
        await env.start()
    assert made[0].calls[-1] == ("fail", True)
    assert fake_sdk.clients[0].closed


@pytest.mark.parametrize(
    "error",
    [
        FakeHyperbrowserError("server", status_code=500),
        FakeHyperbrowserError("timeout", status_code=None),
        ConnectionError("lost"),
        TimeoutError("slow"),
    ],
)
async def test_an_unclear_create_error_keeps_the_hold(fake_sdk: FakeCloud, error: BaseException) -> None:
    """Keep the hold when the create may have made a box.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        error: The create error.
    """
    fake_sdk.create_error = error
    env, made = _protected()
    with pytest.raises(type(error)):
        await env.start()
    assert made[0].calls[-1] == ("fail", False)


async def test_a_local_check_error_is_never_sent(fake_sdk: FakeCloud) -> None:
    """Fail before the send when the SDK model refuses the parameters, so the hold can be released.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    fake_sdk.params_error = ValueError("bad params")
    env, made = _protected()
    with pytest.raises(ValueError, match="bad params"):
        await env.start()
    assert made[0].names() == ["reserve", "fail"]
    assert fake_sdk.create_calls == 0


async def test_a_cancelled_protected_start_stops_and_settles_the_box(fake_sdk: FakeCloud) -> None:
    """Stop and settle a box whose create returned in the grace after a cancel, and raise the cancel.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    fake_sdk.create_gate = asyncio.Event()
    env, made = _protected()
    start = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    start.cancel()
    await asyncio.sleep(0)
    fake_sdk.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    await _late()
    assert made[0].names() == ["reserve", "sent", "confirm", "stopped", "report", "settle"]
    assert fake_sdk.boxes[0].stopped


async def test_a_cancel_during_a_refused_create_releases_and_raises(fake_sdk: FakeCloud) -> None:
    """Raise the cancel, and still tell the reservation that the refused create made no box.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    fake_sdk.create_gate = asyncio.Event()
    fake_sdk.create_error = FakeHyperbrowserError("refused", status_code=429)
    env, made = _protected()
    start = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    start.cancel()
    await asyncio.sleep(0)
    fake_sdk.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert made[0].calls[-1] == ("fail", True)


async def test_a_create_past_the_grace_is_stopped_when_it_returns(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop and settle the box of a create that returns after the cancel grace.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(hyperbrowser, "_CANCEL_GRACE_SEC", 0.01)
    fake_sdk.create_gate = asyncio.Event()
    env, made = _protected()
    start = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert made[0].names() == ["reserve", "sent"]
    fake_sdk.create_gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    await _late()
    assert made[0].names() == ["reserve", "sent", "confirm", "stopped", "report", "settle"]
    assert fake_sdk.boxes[0].stopped


async def test_a_create_past_the_grace_that_fails_keeps_the_hold(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the hold of a create that fails with a server error after the cancel grace.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(hyperbrowser, "_CANCEL_GRACE_SEC", 0.01)
    fake_sdk.create_gate = asyncio.Event()
    env, made = _protected()
    start = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    fake_sdk.create_error = FakeHyperbrowserError("server", status_code=503)
    fake_sdk.create_gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    await _late()
    assert made[0].names() == ["reserve", "sent", "fail"]
    assert made[0].calls[-1] == ("fail", False)
    assert fake_sdk.boxes == []


async def test_a_reservation_past_the_cancel_grace_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """Release a reservation that the factory returns after the start was cancelled, and send no create.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(hyperbrowser, "_CANCEL_GRACE_SEC", 0.01)
    gate = threading.Event()
    entered = threading.Event()
    made: list[FakeUsage] = []

    def factory_(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Block until the test opens the gate, then make the fake reservation.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        entered.set()
        gate.wait(5)
        made.append(FakeUsage(request, operation_key))
        return made[-1]

    env = HyperbrowserEnvironment(_spec(operation_key="box-1"), budget="budget", usage_factory=factory_)
    start = asyncio.ensure_future(env.start())
    await asyncio.to_thread(entered.wait, 5)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert made == []
    gate.set()
    for _ in range(500):
        if made and made[0].calls:
            break
        await asyncio.sleep(0.01)
    await _late()
    assert made[0].calls == [("fail", False)]
    assert CLOUD.create_calls == 0


async def test_the_real_reservation_stays_pending_after_a_late_failed_create(
    fake_sdk: FakeCloud, ledger: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the real hold pending when a create past the cancel grace fails with a server error.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        ledger: The funded ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(hyperbrowser, "_CANCEL_GRACE_SEC", 0.01)
    fake_sdk.create_gate = asyncio.Event()
    start = asyncio.ensure_future(_ledger_env(ledger).start())
    await fake_sdk.create_started.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    fake_sdk.create_error = FakeHyperbrowserError("server", status_code=503)
    fake_sdk.create_gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    await _late()
    assert _state(ledger) == "pending"


async def test_a_cancel_during_the_stop_still_settles(fake_sdk: FakeCloud) -> None:
    """Finish the stop and the settle when a cancel arrives during the stop, and raise the cancel.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    env, made = _protected()
    await env.start()
    stop = asyncio.ensure_future(env.stop())
    await asyncio.sleep(0)
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    await _late()
    assert made[0].names().count("settle") == 1
    assert fake_sdk.boxes[0].stopped


async def test_a_stopped_protected_box_cannot_start_again(fake_sdk: FakeCloud) -> None:
    """Refuse a second start, because the hold of the first box is settled.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
    """
    env, _ = _protected()
    await env.start()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Create a private funded wallet and ledger for the real reservation.

    Args:
        tmp_path: Per-test directory for the SQLite file.
        monkeypatch: Pins the usage markup to 1.0.

    Yields:
        The engine bound to the funded ledger.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    engine = create_engine(f"sqlite:///{tmp_path / 'hyperbrowser.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _ledger_env(ledger: Engine) -> HyperbrowserEnvironment:
    """Open a box that the real reservation funds from the local ledger.

    Args:
        ledger: The funded ledger.

    Returns:
        The protected box.
    """
    service = BudgetService(engine=ledger)
    budget = service.create("alice", 50, idempotency_key="draft")
    runtime = BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)
    return HyperbrowserEnvironment(_spec(lifetime_seconds=60, operation_key="box-1"), budget=runtime)


def _state(ledger: Engine) -> str:
    """Return the state of the only operation.

    Args:
        ledger: The ledger.

    Returns:
        The operation state.
    """
    with Session(ledger) as session:
        return session.scalars(select(ExecutionOperationModel)).one().state


async def test_the_real_reservation_settles_a_stopped_box(fake_sdk: FakeCloud, ledger: Engine) -> None:
    """Settle the real hold below its maximum after a normal stop.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        ledger: The funded ledger.
    """
    env = _ledger_env(ledger)
    async with env:
        assert isinstance(env.usage, HyperbrowserUsageReservation)
        assert _state(ledger) == "pending"
    with Session(ledger) as session:
        row = session.scalars(select(ExecutionOperationModel)).one()
    assert row.state == "settled"
    assert row.actual_units <= row.max_units


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (FakeHyperbrowserError("refused", status_code=402), "released"),
        (FakeHyperbrowserError("server", status_code=503), "pending"),
        (ConnectionError("lost"), "pending"),
    ],
)
async def test_the_real_reservation_releases_only_a_refused_create(
    fake_sdk: FakeCloud, ledger: Engine, error: BaseException, state: str
) -> None:
    """Release the real hold only when Hyperbrowser refused the create; keep it pending otherwise.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        ledger: The funded ledger.
        error: The create error.
        state: The expected operation state.
    """
    fake_sdk.create_error = error
    with pytest.raises(type(error)):
        await _ledger_env(ledger).start()
    assert _state(ledger) == state


async def test_the_real_reservation_settles_a_box_whose_start_failed(fake_sdk: FakeCloud, ledger: Engine) -> None:
    """Settle, not release, the real hold of a box that existed when the start failed.

    Args:
        fake_sdk: The fake Hyperbrowser cloud.
        ledger: The funded ledger.
    """
    fake_sdk.granted_timeout = 60
    with pytest.raises(SandboxInfraError):
        await _ledger_env(ledger).start()
    assert _state(ledger) == "settled"

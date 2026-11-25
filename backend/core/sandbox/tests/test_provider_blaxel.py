"""Tests for the Blaxel sandbox provider, against an in-memory fake of the Blaxel SDK.

No test calls Blaxel: an autouse fixture replaces every SDK name that the
provider uses with the fake below. The ledger tests also replace the usage
clock, so the measured runtime is exact.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import blaxel_usage
from core.billing.budget_amounts import cent_units
from core.billing.budgets import BudgetService
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox import (
    JOB_TAG,
    ResourceMode,
    ResourceRequest,
    SandboxEnvironment,
    SandboxSpec,
    SandboxSpecError,
    factory,
)
from core.sandbox.capabilities import NetworkMode
from core.sandbox.errors import SandboxFatalError, SandboxInfraError
from core.sandbox.factory import ProductionNotAllowedError
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import blaxel as blaxel_provider
from core.sandbox.providers.blaxel import BlaxelEnvironment
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

_SPEC = SandboxSpec(lifetime_seconds=300)


class FakeSandboxAPIError(Exception):
    """Stand in for ``blaxel.core.SandboxAPIError``."""

    def __init__(self, status_code: int, code: str | None = None) -> None:
        """Keep the HTTP status and the error code.

        Args:
            status_code: The HTTP status.
            code: The Blaxel error code, or ``None``.
        """
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.code = code


class FakeCreationTimeoutError(FakeSandboxAPIError):
    """Stand in for ``blaxel.core.SandboxCreationTimeoutError``."""

    def __init__(self) -> None:
        """Use the 408 status of the real error."""
        super().__init__(408)


class FakeUnexpectedStatusError(Exception):
    """Stand in for ``blaxel.core.client.errors.UnexpectedStatus``, which is not a ``SandboxAPIError``."""

    def __init__(self, status_code: int, code: str | None = None) -> None:
        """Keep the HTTP status and the code that the SDK parses from the body.

        Args:
            status_code: The HTTP status that the create endpoint does not model.
            code: The Blaxel error code from the body, or ``None``.
        """
        super().__init__(f"Unexpected HTTP status {status_code}")
        self.status_code = status_code
        self.code = code


class FakeResponseError(Exception):
    """Stand in for ``blaxel.core.ResponseError``."""

    def __init__(self, status: int) -> None:
        """Keep the HTTP status.

        Args:
            status: The HTTP status.
        """
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeProcess:
    """Run a few shell commands in memory, like the Blaxel process API."""

    def __init__(self, box: FakeBox) -> None:
        """Bind the process API to its box.

        Args:
            box: The fake sandbox.
        """
        self.box = box
        self.execs: list[dict[str, Any]] = []
        self.killed: list[str] = []
        self.results: dict[str, SimpleNamespace] = {}
        self.durations: dict[str, float] = {}

    async def exec(self, config: dict[str, Any]) -> SimpleNamespace:
        """Start one command and keep its result for ``wait``.

        Args:
            config: The process config.

        Returns:
            The running process.
        """
        self.execs.append(config)
        assert not self.box.deleted
        argv = shlex.split(config["command"])
        assert argv[:2] == ["bash", "-c"]
        pid = f"p{len(self.execs)}"
        self.results[pid], self.durations[pid] = self._run(argv[2], config["env"])
        return SimpleNamespace(pid=pid, name=pid, status="running", exit_code=None, stdout="", stderr="")

    def _run(self, command: str, env: dict[str, str]) -> tuple[SimpleNamespace, float]:
        """Interpret one command.

        Args:
            command: The shell command.
            env: The command environment.

        Returns:
            The finished process and its run time in seconds.
        """
        word, _, rest = command.partition(" ")
        code, out, duration = 0, "", 0.0
        if word == "echo":
            out = f"{rest}\n"
        elif word == "exit":
            code = int(rest)
        elif word == "printenv":
            code, out = (0, f"{env[rest]}\n") if rest in env else (1, "")
        elif word == "cat":
            code, out = (0, self.box.files[rest]) if rest in self.box.files else (1, "")
        elif word == "mkdir":
            code = self.box.mkdir_code
        elif word == "sleep":
            duration = float(rest)
        elif word == "false":
            code = 1
        status = "completed" if code == 0 else "failed"
        return SimpleNamespace(pid="p", status=status, exit_code=code, stdout=out, stderr=""), duration

    async def wait(self, pid: str, *, max_wait: int, interval: int) -> SimpleNamespace:
        """Return the finished process, or raise when it runs past ``max_wait``.

        Args:
            pid: The process id.
            max_wait: The longest wait in milliseconds; -1 waits forever.
            interval: The poll interval in milliseconds.

        Returns:
            The finished process.

        Raises:
            TimeoutError: The process runs longer than ``max_wait``.
        """
        if max_wait != -1 and self.durations[pid] * 1000 > max_wait:
            raise TimeoutError("still running")
        return self.results[pid]

    async def kill(self, pid: str) -> None:
        """Record a kill.

        Args:
            pid: The process id.
        """
        self.killed.append(pid)


class FakeFs:
    """Keep the files of a fake sandbox in memory."""

    def __init__(self, box: FakeBox) -> None:
        """Bind the file API to its box.

        Args:
            box: The fake sandbox.
        """
        self.box = box
        self.read_error: Exception | None = None

    async def write_binary(self, path: str, data: bytes) -> None:
        """Store one file.

        Args:
            path: The sandbox path.
            data: The file bytes.
        """
        self.box.files[path] = data.decode("utf-8")

    async def read_binary(self, path: str) -> bytes:
        """Read one file.

        Args:
            path: The sandbox path.

        Returns:
            The file bytes.

        Raises:
            FakeResponseError: The file does not exist (404).
        """
        if self.read_error is not None:
            raise self.read_error
        if path not in self.box.files:
            raise FakeResponseError(404)
        return self.box.files[path].encode("utf-8")


class FakeBox:
    """Stand in for a ``SandboxInstance``."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Make a running box from its create config.

        Args:
            config: The create config.
        """
        self.config = config
        self.metadata = SimpleNamespace(name=config["name"])
        self.status = "DEPLOYED"
        self.deleted = False
        self.files: dict[str, str] = {}
        self.mkdir_code = 0
        self.process = FakeProcess(self)
        self.fs = FakeFs(self)

    def release(self) -> None:
        """Delete the box the way Blaxel does: its record stays, with status TERMINATED."""
        self.deleted = True
        self.status = "TERMINATED"


class FakeBlaxel:
    """Stand in for the ``SandboxInstance`` class methods, with hooks for failures and waits."""

    def __init__(self) -> None:
        """Start with no boxes and no failures."""
        self.boxes: dict[str, FakeBox] = {}
        self.create_error: Exception | None = None
        self.create_error_makes_box = False
        self.create_error_box_released = False
        self.create_timeout: int | None = None
        self.create_gate: asyncio.Event | None = None
        self.create_entered = asyncio.Event()
        self.returned_name: str | None = None
        self.get_error: Exception | None = None
        self.get_gate: asyncio.Event | None = None
        self.get_entered = asyncio.Event()
        self.get_calls = 0
        self.delete_error: Exception | None = None
        self.delete_gate: asyncio.Event | None = None
        self.delete_entered = asyncio.Event()
        self.delete_calls: list[str] = []
        self.on_delete: Callable[[], None] | None = None

    # The fake keeps the SDK's ``timeout`` parameter name.
    async def create(
        self,
        config: dict[str, Any],
        safe: bool = True,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> FakeBox:
        """Create a box, or fail as the test asks.

        Args:
            config: The create config.
            safe: The SDK's ready check; the provider always sets it.
            timeout: The create deadline in seconds.

        Returns:
            The created box.
        """
        assert safe is True
        self.create_timeout = timeout
        self.create_entered.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        if self.create_error is not None:
            if self.create_error_makes_box:
                self.boxes[config["name"]] = FakeBox(config)
                if self.create_error_box_released:
                    self.boxes[config["name"]].release()
            raise self.create_error
        box = FakeBox(config)
        self.boxes[config["name"]] = box
        if self.returned_name is not None:
            box.metadata = SimpleNamespace(name=self.returned_name)
        return box

    async def get(self, name: str) -> FakeBox:
        """Look up a box by name.

        Args:
            name: The box name.

        Returns:
            The box; a deleted box keeps its record with status TERMINATED, as in the SDK.

        Raises:
            FakeSandboxAPIError: No box ever had the name (404).
        """
        self.get_calls += 1
        self.get_entered.set()
        if self.get_gate is not None:
            await self.get_gate.wait()
        if self.get_error is not None:
            raise self.get_error
        box = self.boxes.get(name)
        if box is None:
            raise FakeSandboxAPIError(404)
        return box

    async def delete(self, name: str) -> None:
        """Delete a box by name.

        Args:
            name: The box name.

        Raises:
            FakeSandboxAPIError: No box has the name (404).
        """
        self.delete_calls.append(name)
        self.delete_entered.set()
        if self.delete_gate is not None:
            await self.delete_gate.wait()
        if self.on_delete is not None:
            self.on_delete()
        if self.delete_error is not None:
            raise self.delete_error
        box = self.boxes.get(name)
        if box is None or box.deleted:
            raise FakeSandboxAPIError(404)
        box.release()

    def only_box(self) -> FakeBox:
        """Return the only box that a test created.

        Returns:
            The box.
        """
        (box,) = self.boxes.values()
        return box


@pytest.fixture(autouse=True)
def fake_blaxel(monkeypatch: pytest.MonkeyPatch) -> FakeBlaxel:
    """Replace every Blaxel SDK name that the provider uses with the fake.

    Args:
        monkeypatch: Pytest fixture that restores the module after the test.

    Returns:
        The fake SDK.
    """
    sdk = FakeBlaxel()
    monkeypatch.setattr(blaxel_provider, "SandboxInstance", sdk)
    monkeypatch.setattr(blaxel_provider, "SandboxAPIError", FakeSandboxAPIError)
    monkeypatch.setattr(blaxel_provider, "SandboxCreationTimeoutError", FakeCreationTimeoutError)
    monkeypatch.setattr(blaxel_provider, "ResponseError", FakeResponseError)
    monkeypatch.setattr(blaxel_provider, "UnexpectedStatus", FakeUnexpectedStatusError)
    monkeypatch.setattr(blaxel_provider, "get_credentials", lambda: SimpleNamespace(workspace="ws"))
    monkeypatch.setattr(blaxel_provider, "_JOB_BOXES", {})
    return sdk


class TestBlaxelEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake Blaxel SDK."""

    provider_cls = BlaxelEnvironment


async def _until(predicate: Callable[[], bool]) -> None:
    """Let the loop run until ``predicate`` holds, for at most two seconds.

    Args:
        predicate: The condition to wait for.

    Raises:
        AssertionError: The condition did not hold in time.
    """
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("The condition did not hold in time.")


async def test_create_sends_the_name_image_memory_and_ttl(fake_blaxel: FakeBlaxel) -> None:
    """Send a unique name, the default image, 2048 MB for each vCPU, the ttl of the lifetime and a deadline."""
    async with BlaxelEnvironment(SandboxSpec(lifetime_seconds=600, resources=ResourceRequest(cpus=2))):
        config = fake_blaxel.only_box().config
    assert config["name"].startswith("skynet-")
    assert len(config["name"]) <= 49
    assert config["image"] == blaxel_provider.DEFAULT_IMAGE
    assert config["memory"] == 4096
    assert config["ttl"] == "600s"
    assert "network" not in config
    # The SDK maximum, so the boot stays inside the 60 s margin of the hold.
    assert fake_blaxel.create_timeout == 50


@pytest.mark.parametrize(("lifetime", "ttl"), [(10, "300s"), (300, "300s"), (300.2, "301s"), (86_400, "86400s")])
def test_ttl_is_the_funded_lifetime(lifetime: float, ttl: str) -> None:
    """Set the ttl to the lifetime, at least Blaxel's 5 minutes, and price the same ttl.

    Args:
        lifetime: The spec lifetime in seconds.
        ttl: The expected Blaxel ttl.
    """
    env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=lifetime))
    assert env._create_config("skynet-x")["ttl"] == ttl
    assert env._usage_request()["lifetime_ms"] == int(ttl[:-1]) * 1000


def test_ttl_fits_inside_the_funded_hold() -> None:
    """Give Blaxel a ttl no longer than the funded time, so Blaxel deletes a box that Skynet never stops."""
    env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, operation_key="op-1"), budget=object())
    request = env._usage_request()
    funded_ms = request["lifetime_ms"] + blaxel_usage._LIFETIME_MARGIN_MS
    assert int(env._create_config("skynet-x")["ttl"][:-1]) * 1000 <= funded_ms
    assert blaxel_usage.quote_blaxel_sandbox(request).maximum.total > 0


@pytest.mark.parametrize(
    "spec",
    [
        SandboxSpec(lifetime_seconds=30 * 24 * 60 * 60 + 1),
        SandboxSpec(lifetime_seconds=300, resources=ResourceRequest(cpus=1, disk_mb=1024)),
    ],
)
def test_specs_blaxel_cannot_apply_are_refused(spec: SandboxSpec) -> None:
    """Refuse a lifetime over 30 days and a disk size.

    Args:
        spec: The spec that Blaxel cannot apply.
    """
    with pytest.raises(SandboxSpecError):
        BlaxelEnvironment(spec)


@pytest.mark.parametrize(
    ("resources", "memory"),
    [
        (ResourceRequest(cpus=1), 2048),
        (ResourceRequest(cpus=4), 8192),
        (ResourceRequest(mode=ResourceMode.IGNORE, memory_mb=3000), 4096),
        (ResourceRequest(mode=ResourceMode.IGNORE), 4096),
    ],
)
def test_memory_follows_the_vcpus(resources: ResourceRequest, memory: int) -> None:
    """Send and price 2048 MB per vCPU, round an ignored size up to whole vCPUs, or use the SDK default.

    Args:
        resources: The requested sizes.
        memory: The expected memory size in MB.
    """
    env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, resources=resources))
    assert env._create_config("skynet-x")["memory"] == memory
    assert env._usage_request()["memory_mb"] == memory


@pytest.mark.parametrize(
    "spec",
    [
        SandboxSpec(lifetime_seconds=300, operation_key="op-1", network_disabled=True),
        SandboxSpec(lifetime_seconds=300, operation_key="op-1", allowed_hosts=("pypi.org",)),
    ],
)
def test_egress_limits_are_refused_before_the_reserve(fake_blaxel: FakeBlaxel, spec: SandboxSpec) -> None:
    """Refuse a spec with no network or an allowlist before any reserve or create.

    Args:
        fake_blaxel: The fake SDK.
        spec: The spec that needs egress isolation.
    """
    budget = SimpleNamespace(reservations=[])
    with pytest.raises(SandboxSpecError, match="network mode"):
        BlaxelEnvironment(spec, budget=budget, usage_factory=FakeUsage)
    assert budget.reservations == []
    assert not fake_blaxel.create_entered.is_set()


async def test_exec_runs_bash_in_the_cwd_with_the_merged_env(fake_blaxel: FakeBlaxel) -> None:
    """Run the command through ``bash -c`` with the spec env under the call env."""
    spec = SandboxSpec(lifetime_seconds=300, env={"A": "spec", "B": "spec"})
    async with BlaxelEnvironment(spec) as env:
        result = await env.exec("printenv A", cwd="/work", env={"A": "call"})
        (config,) = fake_blaxel.only_box().process.execs
    assert result.stdout == "call\n"
    assert config["working_dir"] == "/work"
    assert config["env"] == {"A": "call", "B": "spec"}
    assert config["timeout"] == 0


async def test_exec_past_its_time_limit_is_killed(fake_blaxel: FakeBlaxel) -> None:
    """Kill a command that runs past its limit and report exit code 124."""
    async with BlaxelEnvironment(_SPEC) as env:
        result = await env.exec("sleep 5", timeout_sec=1)
        process = fake_blaxel.only_box().process
    assert result.timed_out
    assert result.exit_code == 124
    assert process.killed == ["p1"]
    assert process.execs[0]["timeout"] == 1 + blaxel_provider._TIMEOUT_GRACE_SEC


async def test_upload_fails_when_the_directories_cannot_be_made(fake_blaxel: FakeBlaxel) -> None:
    """Raise an infrastructure error when ``mkdir -p`` fails."""
    async with BlaxelEnvironment(_SPEC) as env:
        fake_blaxel.only_box().mkdir_code = 1
        with pytest.raises(SandboxInfraError, match="upload directories"):
            await env.upload_files({"/a/b.txt": "x"})


async def test_download_error_other_than_404_is_raised(fake_blaxel: FakeBlaxel) -> None:
    """Raise a read error that is not a missing file."""
    async with BlaxelEnvironment(_SPEC) as env:
        fake_blaxel.only_box().fs.read_error = FakeResponseError(500)
        with pytest.raises(FakeResponseError):
            await env.download_file("/a.txt")


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to run without the blaxel package."""
    monkeypatch.setattr(blaxel_provider, "SandboxInstance", None)
    with pytest.raises(SandboxFatalError, match="blaxel package"):
        BlaxelEnvironment.preflight()


@pytest.mark.parametrize("credentials", [None, SimpleNamespace(workspace="")])
def test_preflight_needs_credentials(monkeypatch: pytest.MonkeyPatch, credentials: Any) -> None:
    """Refuse to run without a Blaxel workspace.

    Args:
        monkeypatch: Pytest fixture that replaces the credentials lookup.
        credentials: The fake credentials.
    """
    monkeypatch.setattr(blaxel_provider, "get_credentials", lambda: credentials)
    with pytest.raises(SandboxFatalError, match="BL_WORKSPACE"):
        BlaxelEnvironment.preflight()


def test_factory_creates_the_blaxel_provider_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a BlaxelEnvironment through the factory in dev, and run its preflight."""
    monkeypatch.setattr(factory.settings, "app_environment", "development")
    environment = factory.create_environment("blaxel", _SPEC)
    assert isinstance(environment, BlaxelEnvironment)
    assert isinstance(environment, SandboxEnvironment)
    factory.preflight("blaxel")


def test_factory_refuses_the_blaxel_provider_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the dev-only Blaxel provider in production, for create and preflight."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        factory.create_environment("blaxel", _SPEC)
    with pytest.raises(ProductionNotAllowedError):
        factory.preflight("blaxel")
    assert factory.PROVIDERS["blaxel"].production_allowed is False


def test_capabilities_are_billable_with_open_egress_only() -> None:
    """Report billing, only open egress and a fixed egress policy."""
    caps = BlaxelEnvironment.capabilities
    assert caps.name == "blaxel"
    assert caps.billable is True
    assert not caps.dynamic_network_policy
    assert caps.network_modes == frozenset({NetworkMode.OPEN})
    assert caps.memory_mb_per_vcpu == 2048
    assert usage_adapter("blaxel") is not None


class FakeUsage:
    """Record the calls a protected box makes on its usage reservation."""

    def __init__(self, budget: Any, request: dict[str, Any], *, operation_key: str) -> None:
        """Record the reservation.

        Args:
            budget: The fake budget.
            request: The priced request.
            operation_key: The box's logical identity.
        """
        self.request = request
        self.operation_key = operation_key
        self.calls: list[str] = []
        self.sandbox_name: str | None = None
        self.sandbox_id: str | None = None
        self.sent_at: float | None = None
        self.stopped_at: float | None = None
        self.absent: bool | None = None
        budget.reservations.append(self)

    def sent(self, name: str) -> None:
        """Record that the create was sent.

        Args:
            name: The box name.
        """
        self.calls.append("sent")
        self.sandbox_name = name
        self.sent_at = 1.0

    def confirm_created(self, sandbox: Any) -> None:
        """Record the created box, and refuse another name like the real reservation.

        Args:
            sandbox: The fake box.

        Raises:
            UsagePendingError: The box has another name than the create sent.
        """
        self.calls.append("confirm")
        if sandbox.metadata.name != self.sandbox_name:
            raise UsagePendingError("another name")
        self.sandbox_id = sandbox.metadata.name

    def pending(self) -> None:
        """Record that the hold stays pending."""
        self.calls.append("pending")

    def stopped(self) -> None:
        """Record a confirmed delete."""
        self.calls.append("stopped")
        self.stopped_at = 2.0

    def settle(self) -> None:
        """Record the settle."""
        self.calls.append("settle")

    def fail(self, absent: bool = False) -> None:
        """Record a failure with no confirmed box.

        Args:
            absent: True when Blaxel refused the create and no box has its name.
        """
        self.calls.append("fail")
        self.absent = absent


def _protected(spec: SandboxSpec | None = None) -> tuple[BlaxelEnvironment, SimpleNamespace]:
    """Build a protected box with a fake budget and a fake usage factory.

    Args:
        spec: The spec, or a default spec with an operation key.

    Returns:
        The box and the fake budget whose ``reservations`` collects each reservation.
    """
    budget = SimpleNamespace(reservations=[])
    spec = spec or SandboxSpec(lifetime_seconds=300, operation_key="op-1")
    return BlaxelEnvironment(spec, budget=budget, usage_factory=FakeUsage), budget


def test_protected_box_needs_an_operation_key() -> None:
    """Refuse a protected box with no operation key."""
    with pytest.raises(SandboxSpecError, match="operation key"):
        BlaxelEnvironment(_SPEC, budget=SimpleNamespace(reservations=[]), usage_factory=FakeUsage)


async def test_protected_box_reserves_before_the_create_and_settles_after_the_delete(fake_blaxel: FakeBlaxel) -> None:
    """Reserve the hold, send the create, confirm it, then delete and settle once."""
    env, budget = _protected()
    await env.start()
    (usage,) = budget.reservations
    assert usage.request == {"image": blaxel_provider.DEFAULT_IMAGE, "lifetime_ms": 300_000, "memory_mb": 4096}
    assert usage.calls == ["sent", "confirm"]
    await env.stop()
    await env.stop()
    assert fake_blaxel.only_box().deleted
    assert usage.calls == ["sent", "confirm", "stopped", "settle"]
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()


@pytest.mark.parametrize(
    ("error", "get_error", "absent"),
    [
        (FakeSandboxAPIError(400), None, True),
        (FakeSandboxAPIError(401), None, True),
        (FakeSandboxAPIError(403), None, True),
        (FakeUnexpectedStatusError(402), None, True),
        (FakeUnexpectedStatusError(404), None, True),
        (FakeUnexpectedStatusError(413), None, True),
        (FakeUnexpectedStatusError(422), None, True),
        (FakeSandboxAPIError(400), FakeSandboxAPIError(500), False),
        (FakeUnexpectedStatusError(503, "AUTHENTICATION_UNAVAILABLE"), None, True),
        (FakeUnexpectedStatusError(503, "AUTHENTICATION_UNAVAILABLE"), FakeSandboxAPIError(500), False),
        (FakeUnexpectedStatusError(503), None, False),
        (FakeSandboxAPIError(409), None, False),
        (FakeUnexpectedStatusError(429), None, False),
        (FakeSandboxAPIError(500), None, False),
        (FakeCreationTimeoutError(), None, False),
        (RuntimeError("connection reset"), None, False),
    ],
)
async def test_failed_create_releases_only_when_no_box_can_exist(
    fake_blaxel: FakeBlaxel, error: Exception, get_error: Exception | None, absent: bool
) -> None:
    """Release only after a refused create whose lookup answered 404; keep any other hold pending.

    Args:
        fake_blaxel: The fake SDK.
        error: The create error.
        get_error: The lookup error, or ``None`` for a 404 answer.
        absent: The expected ``absent`` flag of the fail call.
    """
    fake_blaxel.create_error = error
    fake_blaxel.get_error = get_error
    env, budget = _protected()
    with pytest.raises(type(error)):
        await env.start()
    (usage,) = budget.reservations
    assert usage.calls == ["sent", "fail"]
    assert usage.absent is absent
    assert fake_blaxel.get_calls == 1
    assert env.usage is None


@pytest.mark.parametrize(
    "error", [FakeSandboxAPIError(400), FakeUnexpectedStatusError(503, "AUTHENTICATION_UNAVAILABLE")]
)
async def test_box_that_a_failed_create_made_is_deleted_and_settled(fake_blaxel: FakeBlaxel, error: Exception) -> None:
    """Delete and settle, not release, a box that the lookup finds after a refused create.

    Args:
        fake_blaxel: The fake SDK.
        error: The refused create error.
    """
    fake_blaxel.create_error = error
    fake_blaxel.create_error_makes_box = True
    env, budget = _protected()
    with pytest.raises(type(error)):
        await env.start()
    (usage,) = budget.reservations
    assert fake_blaxel.only_box().deleted
    assert usage.calls == ["sent", "confirm", "stopped", "settle"]


@pytest.mark.parametrize("error", [FakeCreationTimeoutError(), FakeSandboxAPIError(400)])
async def test_box_that_blaxel_already_deleted_stays_pending(fake_blaxel: FakeBlaxel, error: Exception) -> None:
    """Keep the hold pending, with no confirm, delete or settle, when the lookup finds a TERMINATED box.

    Args:
        fake_blaxel: The fake SDK.
        error: The create error.
    """
    fake_blaxel.create_error = error
    fake_blaxel.create_error_makes_box = True
    fake_blaxel.create_error_box_released = True
    env, budget = _protected()
    with pytest.raises(type(error)):
        await env.start()
    (usage,) = budget.reservations
    assert usage.calls == ["sent", "fail"]
    assert usage.absent is False
    assert fake_blaxel.delete_calls == []


async def test_confirm_failure_settles_once(fake_blaxel: FakeBlaxel) -> None:
    """Settle a box whose confirm failed once, with no second lookup, delete or settle."""
    fake_blaxel.returned_name = "skynet-other"
    env, budget = _protected()
    with pytest.raises(UsagePendingError):
        await env.start()
    (usage,) = budget.reservations
    assert usage.calls == ["sent", "confirm", "stopped", "settle", "fail"]
    assert fake_blaxel.get_calls == 0
    assert len(fake_blaxel.delete_calls) == 1


async def test_real_hold_of_a_timed_out_create_whose_box_is_gone_stays_pending(
    fake_blaxel: FakeBlaxel, ledger: Engine, clock: FakeClock
) -> None:
    """Keep the real hold pending, not charged, when Blaxel already released the box of a 408.

    Args:
        fake_blaxel: The fake SDK.
        ledger: The SQLite ledger.
        clock: The fake usage clock.
    """
    fake_blaxel.create_error = FakeCreationTimeoutError()
    fake_blaxel.create_error_makes_box = True
    fake_blaxel.create_error_box_released = True
    env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, operation_key="op-1"), budget=_runtime(ledger))
    with pytest.raises(FakeCreationTimeoutError):
        await env.start()
    assert _operation(ledger).state == "pending"


async def test_box_with_another_name_is_deleted_and_the_error_raised(fake_blaxel: FakeBlaxel, tmp_path: Path) -> None:
    """Delete the box and settle the real hold when Blaxel returns another name than the create sent.

    Args:
        fake_blaxel: The fake SDK.
        tmp_path: Per-test directory for the SQLite ledger.
    """
    fake_blaxel.returned_name = "skynet-other"
    with _ledger(tmp_path) as engine:
        runtime = _runtime(engine)
        env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, operation_key="op-1"), budget=runtime)
        with pytest.raises(UsagePendingError, match="another name"):
            await env.start()
        assert fake_blaxel.only_box().deleted
        assert fake_blaxel.get_calls == 0
        assert _operation(engine).state == "settled"
        assert runtime._pinned == {}


async def test_failed_delete_still_settles(fake_blaxel: FakeBlaxel) -> None:
    """Settle the hold when the delete fails, and raise the delete error."""
    env, budget = _protected()
    await env.start()
    fake_blaxel.delete_error = FakeSandboxAPIError(500)
    with pytest.raises(FakeSandboxAPIError):
        await env.stop()
    (usage,) = budget.reservations
    assert usage.calls == ["sent", "confirm", "settle"]


async def test_delete_of_a_box_that_is_already_gone_settles(fake_blaxel: FakeBlaxel) -> None:
    """Treat a 404 on the delete as a confirmed delete."""
    env, budget = _protected()
    await env.start()
    fake_blaxel.only_box().deleted = True
    await env.stop()
    (usage,) = budget.reservations
    assert usage.calls == ["sent", "confirm", "stopped", "settle"]


async def test_cancel_during_the_create_deletes_and_settles_the_box(fake_blaxel: FakeBlaxel) -> None:
    """Delete and settle a box whose create ends inside the cancel grace time, and raise the cancel."""
    fake_blaxel.create_gate = asyncio.Event()
    env, budget = _protected()
    task = asyncio.create_task(env.start())
    await fake_blaxel.create_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    fake_blaxel.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    (usage,) = budget.reservations
    assert fake_blaxel.only_box().deleted
    assert usage.calls == ["sent", "confirm", "stopped", "settle"]


async def test_create_that_ends_after_the_cancel_is_deleted_and_settled(
    fake_blaxel: FakeBlaxel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete and settle in the background a box whose create ends after the cancel passed on.

    Args:
        fake_blaxel: The fake SDK.
        monkeypatch: Pytest fixture that shortens the cancel grace time.
    """
    monkeypatch.setattr(blaxel_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_blaxel.create_gate = asyncio.Event()
    env, budget = _protected()
    task = asyncio.create_task(env.start())
    await fake_blaxel.create_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    (usage,) = budget.reservations
    assert usage.calls == ["sent"]
    fake_blaxel.create_gate.set()
    await _until(lambda: usage.calls == ["sent", "confirm", "stopped", "settle"])
    assert fake_blaxel.only_box().deleted
    await _until(lambda: not blaxel_provider._LATE_TASKS)


@pytest.mark.parametrize(("error", "absent"), [(FakeSandboxAPIError(400), True), (FakeSandboxAPIError(500), False)])
async def test_create_error_after_the_cancel_fails_the_usage(
    fake_blaxel: FakeBlaxel, monkeypatch: pytest.MonkeyPatch, error: Exception, absent: bool
) -> None:
    """Look up the box and fail the usage in the background when the create fails after the cancel passed on.

    Args:
        fake_blaxel: The fake SDK.
        monkeypatch: Pytest fixture that shortens the cancel grace time.
        error: The late create error.
        absent: The expected ``absent`` flag of the fail call.
    """
    monkeypatch.setattr(blaxel_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_blaxel.create_gate = asyncio.Event()
    env, budget = _protected()
    task = asyncio.create_task(env.start())
    await fake_blaxel.create_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    (usage,) = budget.reservations
    fake_blaxel.create_error = error
    fake_blaxel.create_gate.set()
    await _until(lambda: usage.calls == ["sent", "fail"])
    assert usage.absent is absent
    await _until(lambda: not blaxel_provider._LATE_TASKS)


async def test_cancel_during_the_lookup_is_raised_and_the_usage_still_fails(
    fake_blaxel: FakeBlaxel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise a cancel that arrives during the cleanup lookup, and finish the cleanup in the background.

    Args:
        fake_blaxel: The fake SDK.
        monkeypatch: Pytest fixture that shortens the cancel grace time.
    """
    monkeypatch.setattr(blaxel_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_blaxel.create_error = FakeSandboxAPIError(400)
    fake_blaxel.get_gate = asyncio.Event()
    env, budget = _protected()
    task = asyncio.create_task(env.start())
    await fake_blaxel.get_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(task, 2)
    assert isinstance(raised.value.__cause__, FakeSandboxAPIError)
    (usage,) = budget.reservations
    assert usage.calls == ["sent"]
    fake_blaxel.get_gate.set()
    await _until(lambda: usage.calls == ["sent", "fail"])
    assert usage.absent is True
    await _until(lambda: not blaxel_provider._LATE_TASKS)


async def test_cancel_during_the_stop_still_deletes_and_settles(
    fake_blaxel: FakeBlaxel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise the cancel of a stop and finish the delete and the settle in the background.

    Args:
        fake_blaxel: The fake SDK.
        monkeypatch: Pytest fixture that shortens the cancel grace time.
    """
    monkeypatch.setattr(blaxel_provider, "_CANCEL_GRACE_SEC", 0.05)
    env, budget = _protected()
    await env.start()
    fake_blaxel.delete_gate = asyncio.Event()
    task = asyncio.create_task(env.stop())
    await fake_blaxel.delete_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    (usage,) = budget.reservations
    assert usage.calls == ["sent", "confirm"]
    fake_blaxel.delete_gate.set()
    await _until(lambda: usage.calls == ["sent", "confirm", "stopped", "settle"])
    assert fake_blaxel.only_box().deleted
    await _until(lambda: not blaxel_provider._LATE_TASKS)


async def test_job_sweep_goes_on_past_a_pending_usage(fake_blaxel: FakeBlaxel) -> None:
    """Stop every box of the job, and log a usage that stays pending instead of raising it."""
    spec = SandboxSpec(lifetime_seconds=300, operation_key="op-1", tags={JOB_TAG: "job-1"})
    env, budget = _protected(spec)
    plain = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, tags={JOB_TAG: "job-1"}))
    await env.start()
    await plain.start()

    def pending_settle() -> None:
        """Keep the hold pending, as a ledger outage would."""
        raise UsagePendingError("ledger down")

    budget.reservations[0].settle = pending_settle
    assert await BlaxelEnvironment.stop_job_sandboxes("job-1") == 2
    assert all(box.deleted for box in fake_blaxel.boxes.values())
    assert blaxel_provider._JOB_BOXES == {}


class FakeClock:
    """Replace ``time`` in the usage module, so the measured runtime is exact."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the fake monotonic time.

        Returns:
            Seconds since the start.
        """
        return self.now

    def time(self) -> float:
        """Return the fake wall time.

        Returns:
            A fixed epoch plus the fake seconds.
        """
        return 1_800_000_000.0 + self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the usage clock and pin the markup to 1.0.

    Args:
        monkeypatch: Pytest fixture that restores the module after the test.

    Returns:
        The fake clock.
    """
    fake = FakeClock()
    monkeypatch.setattr(blaxel_usage, "time", fake)
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    return fake


@contextmanager
def _ledger(tmp_path: Path) -> Iterator[Engine]:
    """Open a private funded SQLite ledger for one test.

    Args:
        tmp_path: Per-test directory.

    Yields:
        The engine of the ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'blaxel.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    try:
        yield engine
    finally:
        engine.dispose()


def _runtime(engine: Engine) -> BudgetRuntime:
    """Bind a real budget runtime to the ledger.

    Args:
        engine: Engine of the funded ledger.

    Returns:
        A runtime for a fresh 50-cent budget owned by ``alice``.
    """
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _operation(engine: Engine) -> ExecutionOperationModel:
    """Return the only operation row.

    Args:
        engine: Engine of the ledger.

    Returns:
        The operation row.
    """
    with Session(engine) as session:
        return session.scalars(select(ExecutionOperationModel)).one()


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Engine]:
    """Yield a private funded ledger.

    Args:
        tmp_path: Per-test directory.

    Yields:
        The engine of the ledger.
    """
    with _ledger(tmp_path) as engine:
        yield engine


async def test_real_hold_settles_at_the_measured_runtime(
    fake_blaxel: FakeBlaxel, ledger: Engine, clock: FakeClock
) -> None:
    """Settle the real hold at the fake-clock seconds from the create to the delete, rounded up."""
    runtime = _runtime(ledger)
    env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, operation_key="op-1"), budget=runtime)
    await env.start()
    assert _operation(ledger).state == "pending"
    clock.now = 41.3
    await env.stop()
    row = _operation(ledger)
    usd = 42 * 4 * blaxel_usage._GB_RAM_SECOND_USD
    snapshot = blaxel_usage.quote_blaxel_sandbox(env._usage_request()).price_snapshot
    assert row.state == "settled"
    assert row.actual_units == cent_units(blaxel_usage.vercel_charge_policy(snapshot).convert(usd).total)
    assert row.actual_units <= row.max_units
    assert runtime._pinned == {}


async def test_real_hold_over_run_settles_at_the_hold(
    fake_blaxel: FakeBlaxel, ledger: Engine, clock: FakeClock
) -> None:
    """Clamp the settle of a box that ran past its funded time to the hold."""
    runtime = _runtime(ledger)
    env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, operation_key="op-1"), budget=runtime)
    await env.start()
    clock.now = 10_000
    await env.stop()
    row = _operation(ledger)
    assert row.state == "settled"
    assert row.actual_units == row.max_units


@pytest.mark.parametrize(
    ("error", "get_error", "state"),
    [
        (FakeSandboxAPIError(400), None, "released"),
        (FakeSandboxAPIError(400), FakeUnexpectedStatusError(503), "pending"),
        (FakeUnexpectedStatusError(422), None, "released"),
        (FakeSandboxAPIError(500), None, "pending"),
        (FakeCreationTimeoutError(), None, "pending"),
    ],
)
async def test_real_hold_after_a_failed_create(
    fake_blaxel: FakeBlaxel,
    ledger: Engine,
    clock: FakeClock,
    error: Exception,
    get_error: Exception | None,
    state: str,
) -> None:
    """Release the real hold only when no box can exist, and keep it pending with the box name otherwise.

    Args:
        fake_blaxel: The fake SDK.
        ledger: The SQLite ledger.
        clock: The fake usage clock.
        error: The create error.
        get_error: The lookup error, or ``None`` for a 404 answer.
        state: The expected ledger state.
    """
    fake_blaxel.create_error = error
    fake_blaxel.get_error = get_error
    runtime = _runtime(ledger)
    env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, operation_key="op-1"), budget=runtime)
    with pytest.raises(type(error)):
        await env.start()
    row = _operation(ledger)
    assert row.state == state
    assert (runtime._pinned == {}) is (state == "released")


async def test_real_hold_settles_when_the_start_is_cancelled(
    fake_blaxel: FakeBlaxel, ledger: Engine, clock: FakeClock
) -> None:
    """Settle the real hold of a box whose start was cancelled during the create."""
    fake_blaxel.create_gate = asyncio.Event()
    runtime = _runtime(ledger)
    env = BlaxelEnvironment(SandboxSpec(lifetime_seconds=300, operation_key="op-1"), budget=runtime)
    task = asyncio.create_task(env.start())
    await fake_blaxel.create_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    clock.now = 3
    fake_blaxel.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert fake_blaxel.only_box().deleted
    assert _operation(ledger).state == "settled"
    assert runtime._pinned == {}

"""Tests for the Superserve sandbox provider, against an in-memory fake of the Superserve SDK.

No test calls Superserve: an autouse fixture replaces every SDK name that the
provider uses with the fake below. The lifetime cap sleeps on a fake sleep.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable, Iterator
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.superserve_usage import USAGE
from core.billing.usage_tags import BOX_KEY, box_tag
from core.config import settings
from core.sandbox import JOB_TAG, NetworkPolicy, ResourceRequest, SandboxSpec, SandboxSpecError, factory
from core.sandbox.errors import SandboxFatalError, SandboxInfraError
from core.sandbox.factory import ProductionNotAllowedError
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import superserve as provider
from core.sandbox.providers.superserve import SUPERSERVE_CAPABILITIES, SuperserveEnvironment
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.sandbox.timeouts import PhaseTimeouts
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

_SPEC = SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=2, memory_mb=4096))


# The SDK declares ``class SandboxStatus(str, Enum)``; a StrEnum would change str() and hide a missing unwrap.
class FakeStatus(str, Enum):  # noqa: UP042
    """Act as the SDK's ``SandboxStatus`` and ``TemplateStatus``: a ``str`` enum hashed by name."""

    ACTIVE = "active"
    FAILED = "failed"
    DELETED = "deleted"
    BUILDING = "building"
    READY = "ready"


class FakeSDKError(Exception):
    """Stand in for ``superserve.SandboxError``, with its status code."""

    def __init__(self, message: str = "error", status_code: int | None = None) -> None:
        """Record the status code.

        Args:
            message: The error text.
            status_code: The HTTP status, or ``None`` when no answer arrived.
        """
        super().__init__(message)
        self.status_code = status_code


class FakeTimeoutError(FakeSDKError):
    """Stand in for ``superserve.SandboxTimeoutError``."""


class FakeNotFoundError(FakeSDKError):
    """Stand in for ``superserve.NotFoundError``."""

    def __init__(self, message: str = "not found") -> None:
        """Set the 404 status.

        Args:
            message: The error text.
        """
        super().__init__(message, 404)


class FakeConflictError(FakeSDKError):
    """Stand in for ``superserve.ConflictError``."""

    def __init__(self, message: str = "conflict") -> None:
        """Set the 409 status.

        Args:
            message: The error text.
        """
        super().__init__(message, 409)


class FakeBox:
    """Act as one Superserve sandbox with a dict file system and a tiny command set."""

    def __init__(self, sdk: FakeSuperserve, kwargs: dict[str, Any]) -> None:
        """Record the create arguments.

        Args:
            sdk: The fake SDK that owns the box.
            kwargs: The create keyword arguments.
        """
        self.sdk = sdk
        self.id = f"sbx-{len(sdk.boxes) + 1}"
        self.kwargs = kwargs
        self.metadata: dict[str, str] = dict(kwargs.get("metadata") or {})
        self.status = FakeStatus.ACTIVE
        self.data: dict[str, bytes] = {}
        self.run_calls: list[dict[str, Any]] = []
        self.updates: list[Any] = []
        self.run_error: Exception | None = None
        self.run_hook: Callable[[], Awaitable[None]] | None = None
        self.exit_override: int | None = None
        self.closed = False
        self.transfers: list[dict[str, Any]] = []
        self.info = SimpleNamespace(
            id=self.id,
            vcpu_count=sdk.size["vcpu"],
            memory_mib=sdk.size["memory_mib"],
            vcpu_max=None,
            memory_max_mib=None,
        )
        self.commands = SimpleNamespace(run=self._run)
        self.files = SimpleNamespace(write=self._write, read=self._read)

    async def get_info(self) -> Any:
        """Return the box info.

        Returns:
            The fake ``SandboxInfo``.
        """
        return self.info

    async def kill(self) -> None:
        """Delete the box through its handle, then close the handle's client, like the SDK does."""
        self.sdk.handle_kills.append(self.id)
        try:
            await self.sdk._kill(self.id)
        finally:
            self.closed = True

    async def update(self, *, network: Any) -> None:
        """Record a network update.

        Args:
            network: The new egress rules.
        """
        self.updates.append(network)

    async def _run(
        self, command: str, *, cwd: str | None, user: str | None, env: dict[str, str] | None, timeout_seconds: int
    ) -> Any:
        """Run one command from the small command set.

        Args:
            command: The shell command.
            cwd: The working directory.
            user: The user that runs the command.
            env: Extra environment variables.
            timeout_seconds: The SDK timeout.

        Returns:
            A fake ``CommandResult``.
        """
        self.run_calls.append({"command": command, "cwd": cwd, "user": user, "env": env, "timeout": timeout_seconds})
        if self.run_hook is not None:
            await self.run_hook()
        if self.run_error is not None:
            raise self.run_error
        words = shlex.split(command)
        stdout, code = "", 0
        if words[0] == "false":
            code = 1
        elif words[0] == "exit":
            code = int(words[1])
        elif words[0] == "echo":
            stdout = " ".join(words[1:]) + "\n"
        elif words[0] == "cat":
            stdout = self.data[words[1]].decode("utf-8")
        elif words[0] == "printenv":
            merged = {**(self.kwargs.get("env_vars") or {}), **(env or {})}
            stdout = merged.get(words[1], "") + "\n"
        if self.exit_override is not None:
            code = self.exit_override
        return SimpleNamespace(stdout=stdout, stderr="", exit_code=code, truncated=False)

    # The fake keeps the SDK's keyword names, so the provider's calls match the real signature.
    async def _write(self, path: str, content: bytes, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """Store one file.

        Args:
            path: The file path.
            content: The file bytes.
            timeout: The transfer timeout.
        """
        self.transfers.append({"write": path, "timeout": timeout})
        self.data[path] = content

    async def _read(
        self,
        path: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
        max_bytes: int | None = None,
    ) -> bytes:
        """Return one file.

        Args:
            path: The file path.
            timeout: The transfer timeout.
            max_bytes: The download limit.

        Returns:
            The file bytes.

        Raises:
            FakeNotFoundError: The file does not exist.
        """
        self.transfers.append({"read": path, "timeout": timeout, "max_bytes": max_bytes})
        if path not in self.data:
            raise FakeNotFoundError()
        return self.data[path]


class FakeTemplate:
    """Hold the server-side state of one Superserve template and its builds."""

    def __init__(self, sdk: FakeSuperserve, name: str, status: str = "ready", **kwargs: Any) -> None:
        """Record the template with one build.

        Args:
            sdk: The fake SDK.
            name: The template name.
            status: The status of the first build.
            **kwargs: The create keyword arguments.
        """
        self.sdk = sdk
        self.id = f"tpl-{len(sdk.templates) + 1}"
        self.name = name
        self.kwargs = kwargs
        self.rebuilds = 0
        self.builds: dict[str, str] = {"build-1": status}
        self.latest_build_id = "build-1"

    @property
    def status(self) -> FakeStatus:
        """Return the status of the latest build as an enum member, as the SDK does."""
        return FakeStatus(self.builds[self.latest_build_id])

    @status.setter
    def status(self, value: str) -> None:
        """Set the status of the latest build.

        Args:
            value: The new status.
        """
        self.builds[self.latest_build_id] = value


class FakeTemplateHandle:
    """Act as an SDK template handle, which keeps the build id it saw at connect time."""

    def __init__(self, template: FakeTemplate) -> None:
        """Take the template's latest build id, as the SDK does.

        Args:
            template: The server-side template.
        """
        self.template = template
        self.id = template.id
        self.name = template.name
        self.latest_build_id = template.latest_build_id

    async def rebuild(self) -> Any:
        """Queue a new build; like the SDK, leave this handle's build id unchanged.

        Returns:
            The new build info.
        """
        template = self.template
        template.rebuilds += 1
        build_id = f"build-{len(template.builds) + 1}"
        template.builds[build_id] = "building"
        template.latest_build_id = build_id
        return SimpleNamespace(id=build_id)

    async def wait_until_ready(self) -> None:
        """Wait for the build this handle knows, and finish it.

        Raises:
            FakeSDKError: The build failed, or the SDK was told to fail it.
        """
        sdk = self.template.sdk
        if sdk.build_hook is not None:
            await sdk.build_hook()
        if sdk.build_error is not None:
            raise sdk.build_error
        if self.template.builds[self.latest_build_id] == "failed":
            raise FakeSDKError("build failed")
        self.template.builds[self.latest_build_id] = "ready"


class FakeSuperserve:
    """Hold the fake SDK state and its ``AsyncSandbox`` and ``AsyncTemplate`` entry points."""

    def __init__(self) -> None:
        """Start with no boxes and no templates."""
        self.boxes: list[FakeBox] = []
        self.templates: list[FakeTemplate] = []
        self.killed: list[str] = []
        self.handle_kills: list[str] = []
        self.size = {"vcpu": 2, "memory_mib": 4096}
        self.create_error: Exception | None = None
        self.create_makes_box = False
        self.create_hook: Callable[[], Awaitable[None]] | None = None
        self.kill_hook: Callable[[], Awaitable[None]] | None = None
        self.kill_error: Exception | None = None
        self.list_error: Exception | None = None
        self.build_error: Exception | None = None
        self.build_hook: Callable[[], Awaitable[None]] | None = None
        self.AsyncSandbox = SimpleNamespace(create=self._create, list=self._list, kill_by_id=self._kill)
        self.AsyncTemplate = SimpleNamespace(
            list=self._list_templates, create=self._create_template, connect=self._connect
        )

    async def _create(self, **kwargs: Any) -> FakeBox:
        """Create one box, or fail as the test asks.

        Args:
            **kwargs: The create keyword arguments.

        Returns:
            The new box.
        """
        if self.create_hook is not None:
            await self.create_hook()
        box = None
        if self.create_error is None or self.create_makes_box:
            box = FakeBox(self, kwargs)
            self.boxes.append(box)
        if self.create_error is not None:
            raise self.create_error
        return box

    async def _list(self, *, metadata: dict[str, str]) -> list[FakeBox]:
        """List the boxes whose metadata holds every given pair.

        Args:
            metadata: The pairs to match.

        Returns:
            The matching boxes.
        """
        if self.list_error is not None:
            raise self.list_error
        return [box for box in self.boxes if metadata.items() <= box.metadata.items()]

    async def _kill(self, sandbox_id: str) -> None:
        """Delete one box; an unknown id is ignored, like the SDK does.

        Args:
            sandbox_id: The box id.
        """
        if self.kill_hook is not None:
            await self.kill_hook()
        if self.kill_error is not None:
            raise self.kill_error
        self.killed.append(sandbox_id)
        for box in self.boxes:
            if box.id == sandbox_id:
                box.status = FakeStatus.DELETED

    async def _list_templates(self, *, name_prefix: str) -> list[FakeTemplate]:
        """List the templates whose name starts with a prefix.

        Args:
            name_prefix: The prefix.

        Returns:
            The matching templates.
        """
        return [template for template in self.templates if template.name.startswith(name_prefix)]

    async def _create_template(self, *, name: str, **kwargs: Any) -> FakeTemplateHandle:
        """Start a template build.

        Args:
            name: The template name.
            **kwargs: The other create arguments.

        Returns:
            A handle to the building template.
        """
        template = FakeTemplate(self, name, "building", **kwargs)
        self.templates.append(template)
        return FakeTemplateHandle(template)

    async def _connect(self, name_or_id: str) -> FakeTemplateHandle:
        """Return a fresh handle to one template by name or id.

        Args:
            name_or_id: The template name or id.

        Returns:
            The handle.
        """
        return FakeTemplateHandle(next(t for t in self.templates if name_or_id in (t.name, t.id)))

    def box(self) -> FakeBox:
        """Return the only box.

        Returns:
            The box.
        """
        assert len(self.boxes) == 1
        return self.boxes[0]


class FakeUsage:
    """Record the calls that the provider makes on a usage reservation."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[tuple[Any, ...]] = []
        self.box = "box-tag"
        self.sent_at: float | None = None
        self.sandbox_id: str | None = None
        self.attempt: str | None = None
        self.size_error: Exception | None = None

    def sent(self, name: str, attempt: str) -> None:
        """Record the send.

        Args:
            name: The box name.
            attempt: The attempt label.
        """
        self.sent_at = 1.0
        self.attempt = attempt
        self.calls.append(("sent", name))

    def bind(self, sandbox_id: str) -> None:
        """Record the bind.

        Args:
            sandbox_id: The box id.
        """
        self.sandbox_id = sandbox_id
        self.calls.append(("bind", sandbox_id))

    def check_size(self, info: Any) -> None:
        """Record the size check, and fail it when the test asks.

        Args:
            info: The box info.

        Raises:
            Exception: The error that the test set.
        """
        self.calls.append(("check_size", info.id))
        if self.size_error is not None:
            raise self.size_error

    def stopped(self) -> None:
        """Record the confirmed kill."""
        self.calls.append(("stopped",))

    def settle(self) -> None:
        """Record the settle."""
        self.calls.append(("settle",))

    def fail(self, absent: bool = False) -> None:
        """Record the failure.

        Args:
            absent: True when no box can exist.
        """
        self.calls.append(("fail", absent))

    def names(self) -> list[str]:
        """Return the call names in order.

        Returns:
            The names.
        """
        return [call[0] for call in self.calls]


@pytest.fixture(autouse=True)
def sdk(monkeypatch: pytest.MonkeyPatch) -> FakeSuperserve:
    """Replace the Superserve SDK in the provider module with a fresh fake.

    The lifetime cap sleeps forever unless a test replaces ``_cap_sleep``.

    Args:
        monkeypatch: Pytest fixture that restores the module after the test.

    Returns:
        The fake SDK.
    """
    fake = FakeSuperserve()
    monkeypatch.setattr(provider, "AsyncSandbox", fake.AsyncSandbox)
    monkeypatch.setattr(provider, "AsyncTemplate", fake.AsyncTemplate)
    monkeypatch.setattr(provider, "NetworkConfig", lambda **rules: rules)
    monkeypatch.setattr(provider, "RunStep", lambda *, run: {"run": run})
    monkeypatch.setattr(provider, "SuperserveError", FakeSDKError)
    monkeypatch.setattr(provider, "SandboxTimeoutError", FakeTimeoutError)
    monkeypatch.setattr(provider, "NotFoundError", FakeNotFoundError)
    monkeypatch.setattr(provider, "ConflictError", FakeConflictError)
    return fake


def _protected(usage: FakeUsage, spec: SandboxSpec = _SPEC, **options: Any) -> SuperserveEnvironment:
    """Return a protected provider that uses a fake usage.

    Args:
        usage: The fake usage that the factory returns.
        spec: The sandbox spec; it gets an operation key.
        **options: Other provider options.

    Returns:
        The provider.
    """
    spec = SandboxSpec(**{**spec.__dict__, "operation_key": "op-1"})
    return SuperserveEnvironment(spec, budget=object(), usage_factory=lambda *a, **k: usage, **options)


async def _settle_loop() -> None:
    """Let the event loop run the pending callbacks and tasks."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _background() -> None:
    """Wait until every other task of the test's event loop ends.

    A background kill settles in a thread, so a fixed number of loop turns
    does not always reach the settle (SKY-969). The time limit makes a stuck
    background task fail the test instead of hanging the CI job.
    """
    async with asyncio.timeout(10):
        while others := asyncio.all_tasks() - {asyncio.current_task()}:
            await asyncio.wait(others)


class TestSuperserveEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the Superserve provider and the fake SDK."""

    provider_cls = SuperserveEnvironment


def test_capabilities_are_billable_and_the_usage_adapter_exists() -> None:
    """Mark the provider billable, so the bridge passes a budget, and find its usage adapter."""
    assert SUPERSERVE_CAPABILITIES.billable is True
    assert SuperserveEnvironment.capabilities is SUPERSERVE_CAPABILITIES
    assert usage_adapter("superserve") is USAGE


def test_factory_refuses_superserve_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Superserve out of production."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        factory.create_environment("superserve", _SPEC)


async def test_create_sends_the_lifetime_template_metadata_env_and_network(sdk: FakeSuperserve) -> None:
    """Send the lifetime as the timeout, a short auto delete, the attempt label, the tags and the env."""
    spec = SandboxSpec(
        lifetime_seconds=90.5,
        resources=ResourceRequest(cpus=2, memory_mb=4096),
        tags={JOB_TAG: "job-1"},
        env={"A": "1"},
        name="named",
        network_disabled=True,
    )
    env = SuperserveEnvironment(spec)
    await env.start()
    try:
        kwargs = sdk.box().kwargs
        assert kwargs["name"] == "named"
        assert kwargs["timeout_seconds"] == 91
        assert kwargs["auto_delete_seconds"] == provider._AUTO_DELETE_SEC
        assert kwargs["metadata"][JOB_TAG] == "job-1"
        assert len(kwargs["metadata"][provider._ATTEMPT_KEY]) == 32
        assert BOX_KEY not in kwargs["metadata"]
        assert kwargs["env_vars"] == {"A": "1"}
        assert kwargs["network"] == {"allow_out": [], "deny_out": ["0.0.0.0/0"]}
        assert kwargs["from_template"] == sdk.templates[0].name
    finally:
        await env.stop()


async def test_template_has_the_fixed_size_and_is_reused(sdk: FakeSuperserve) -> None:
    """Build one fixed-size template with /tmp on disk, then reuse it for the next box."""
    for _ in range(2):
        env = SuperserveEnvironment(_SPEC)
        await env.start()
        await env.stop()
    (template,) = sdk.templates
    assert template.name.startswith("skynet/")
    assert template.kwargs == {
        "from_": provider.DEFAULT_IMAGE,
        "vcpu": 2,
        "memory_mib": 4096,
        "disk_mib": 10240,
        "steps": [{"run": provider._TMP_ON_DISK}],
    }
    assert template.status == "ready"


async def test_failed_template_is_rebuilt(sdk: FakeSuperserve) -> None:
    """Rebuild a template whose last build failed."""
    env = SuperserveEnvironment(_SPEC)
    await env.start()
    await env.stop()
    sdk.templates[0].status = "failed"
    await env.start()
    await env.stop()
    assert sdk.templates[0].rebuilds == 1
    assert sdk.templates[0].builds == {"build-1": "failed", "build-2": "ready"}


async def test_stuck_template_build_times_out_and_releases_the_unsent_hold(
    sdk: FakeSuperserve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop waiting for a build at the build timeout, send no create and release the hold."""
    monkeypatch.setattr(provider, "_BUILD_TIMEOUT_SEC", 0.01)
    sdk.build_hook = asyncio.Event().wait
    usage = FakeUsage()
    with pytest.raises(SandboxInfraError, match="prepare the template") as raised:
        await _protected(usage).start()
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert not sdk.boxes
    assert usage.names() == ["fail"]


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (NetworkPolicy(), {"allow_out": [], "deny_out": []}),
        (NetworkPolicy(disabled=True), {"allow_out": [], "deny_out": ["0.0.0.0/0"]}),
        (
            NetworkPolicy(allowed_hosts=("10.0.0.0/8",)),
            {"allow_out": ["10.0.0.0/8"], "deny_out": ["0.0.0.0/0"]},
        ),
        (
            NetworkPolicy(allowed_hosts=("pypi.org", "10.0.0.0/8", "pypi.org")),
            {"allow_out": ["pypi.org", "10.0.0.0/8"], "deny_out": ["0.0.0.0/0"]},
        ),
    ],
)
def test_network_config_maps_each_policy(policy: NetworkPolicy, expected: dict[str, list[str]]) -> None:
    """Map open, deny-all and allowlist policies to Superserve egress rules, with no added DNS resolvers.

    Args:
        policy: The egress policy.
        expected: The egress rules.
    """
    assert provider._network_config(policy) == expected


async def test_network_policy_changes_on_a_running_box(sdk: FakeSuperserve) -> None:
    """Send a new egress policy to the running box."""
    async with SuperserveEnvironment(_SPEC) as env:
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("10.0.0.1",)))
        assert sdk.box().updates == [{"allow_out": ["10.0.0.1"], "deny_out": ["0.0.0.0/0"]}]


def test_constructor_refuses_unpriced_or_unbounded_boxes() -> None:
    """Refuse a lifetime above seven days, and a protected box with no operation key."""
    with pytest.raises(SandboxSpecError, match="604800"):
        SuperserveEnvironment(SandboxSpec(lifetime_seconds=604_801))
    with pytest.raises(SandboxSpecError, match="operation key"):
        SuperserveEnvironment(_SPEC, budget=object())


async def test_protected_box_reserves_before_create_and_settles_after_the_kill(sdk: FakeSuperserve) -> None:
    """Reserve, send, bind and check the size, then kill and settle on stop."""
    usage = FakeUsage()
    requests: list[dict[str, Any]] = []

    def factory_(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Record the usage request.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Returns:
            The fake usage.
        """
        assert not sdk.boxes
        requests.append({**request, "operation_key": operation_key})
        return usage

    spec = SandboxSpec(**{**_SPEC.__dict__, "operation_key": "op-1"})
    env = SuperserveEnvironment(spec, budget=object(), usage_factory=factory_)
    await env.start()
    assert requests == [
        {
            "image": provider.DEFAULT_IMAGE,
            "lifetime_ms": 60_000,
            "vcpu": 2,
            "memory_mib": 4096,
            "disk_mib": 10240,
            "gpu": False,
            "operation_key": "op-1",
        }
    ]
    assert sdk.box().metadata[BOX_KEY] == "box-tag"
    assert usage.names() == ["sent", "bind", "check_size"]
    await env.stop()
    await env.stop()
    assert usage.names() == ["sent", "bind", "check_size", "stopped", "settle"]
    assert sdk.killed == ["sbx-1"]
    assert sdk.handle_kills == ["sbx-1"]
    assert sdk.box().closed
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()


async def test_wrong_size_kills_and_settles_the_box(sdk: FakeSuperserve) -> None:
    """Kill and settle a box that is not the funded size through its handle, and raise."""
    usage = FakeUsage()
    usage.size_error = UsagePendingError("size")
    env = _protected(usage)
    with pytest.raises(UsagePendingError):
        await env.start()
    assert sdk.killed == ["sbx-1"]
    assert sdk.handle_kills == ["sbx-1"]
    assert sdk.box().closed
    assert usage.names()[-3:] == ["stopped", "settle", "fail"]


async def test_refused_create_with_no_box_releases_the_hold(sdk: FakeSuperserve) -> None:
    """Release the hold when Superserve refused the create and the lookup finds no box."""
    usage = FakeUsage()
    sdk.create_error = FakeSDKError("no credit", 402)
    with pytest.raises(SandboxInfraError):
        await _protected(usage).start()
    assert usage.calls[-1] == ("fail", True)


async def test_refused_create_that_made_a_box_kills_and_settles_it(sdk: FakeSuperserve) -> None:
    """Find a box that a refused create still made, then kill and settle it."""
    usage = FakeUsage()
    sdk.create_error = FakeSDKError("conflict", 409)
    sdk.create_makes_box = True
    with pytest.raises(SandboxInfraError):
        await _protected(usage).start()
    assert sdk.killed == ["sbx-1"]
    assert usage.names() == ["sent", "bind", "stopped", "settle"]


@pytest.mark.parametrize(
    "error",
    [FakeSDKError("connection", None), FakeSDKError("server", 503), FakeTimeoutError("slow"), OSError("reset")],
)
async def test_unknown_create_keeps_the_hold_pending(sdk: FakeSuperserve, error: Exception) -> None:
    """Keep the hold pending when the create outcome is unknown and the lookup finds no box.

    Args:
        sdk: The fake SDK.
        error: The create error.
    """
    usage = FakeUsage()
    sdk.create_error = error
    with pytest.raises(SandboxInfraError):
        await _protected(usage).start()
    assert usage.calls[-1] == ("fail", False)


@pytest.mark.parametrize("kill_fails", [False, True])
@pytest.mark.parametrize("status", [FakeStatus.FAILED, FakeStatus.DELETED])
@pytest.mark.parametrize(("status_code", "absent"), [(503, False), (409, True)])
async def test_lookup_ignores_boxes_that_are_not_running(
    sdk: FakeSuperserve, status: FakeStatus, status_code: int, absent: bool, kill_fails: bool
) -> None:
    """Never bind a failed or deleted box, and kill only a failed box, best effort.

    A deleted box counts as no box, so a refusal releases the hold. A failed
    box shows that the create made a box, so the hold stays pending, also
    when its kill fails and the outcome is unknown.

    Args:
        sdk: The fake SDK.
        status: The status of the box that the lookup finds.
        status_code: The status of the create error.
        absent: Whether a refusal with no box releases the hold as absent.
        kill_fails: Whether the best-effort kill fails.
    """
    usage = FakeUsage()
    sdk.create_error = FakeSDKError("create", status_code)
    sdk.create_makes_box = True
    listing = sdk.AsyncSandbox.list
    kills: list[str] = []

    async def list_after_the_box_ended(*, metadata: dict[str, str]) -> list[FakeBox]:
        """End the box, then list.

        Args:
            metadata: The pairs to match.

        Returns:
            The matching boxes.
        """
        sdk.box().status = status
        return await listing(metadata=metadata)

    async def record_kill() -> None:
        """Record each kill attempt."""
        kills.append("kill")

    sdk.AsyncSandbox.list = list_after_the_box_ended
    sdk.kill_hook = record_kill
    if kill_fails:
        sdk.kill_error = FakeSDKError("down", 503)
    with pytest.raises(SandboxInfraError):
        await _protected(usage).start()
    expected_kills = ["kill"] if status is FakeStatus.FAILED else []
    assert kills == expected_kills
    assert sdk.killed == ([] if kill_fails else ["sbx-1"] if expected_kills else [])
    assert not sdk.handle_kills
    assert usage.names() == ["sent", "fail"]
    assert usage.calls[-1] == ("fail", absent and status is FakeStatus.DELETED)


@pytest.mark.parametrize("failed_first", [True, False])
async def test_failed_kill_of_a_failed_box_keeps_the_live_box(sdk: FakeSuperserve, failed_first: bool) -> None:
    """Bind, kill and settle the live box of a lookup, also when the kill of its failed box fails.

    Args:
        sdk: The fake SDK.
        failed_first: Whether the lookup lists the failed box before the live box.
    """
    usage = FakeUsage()
    sdk.create_error = FakeSDKError("conflict", 409)
    sdk.create_makes_box = True
    listing = sdk.AsyncSandbox.list
    killing = sdk.AsyncSandbox.kill_by_id

    async def list_two_boxes(*, metadata: dict[str, str]) -> list[FakeBox]:
        """Add a second box with the same attempt label, fail one of them, then list.

        Args:
            metadata: The pairs to match.

        Returns:
            The matching boxes.
        """
        sdk.boxes.append(FakeBox(sdk, {"metadata": dict(sdk.boxes[0].metadata)}))
        sdk.boxes[0 if failed_first else 1].status = FakeStatus.FAILED
        return await listing(metadata=metadata)

    async def kill_all_but_the_failed_box(sandbox_id: str) -> None:
        """Fail the kill of the failed box, and kill other boxes.

        Args:
            sandbox_id: The box id.

        Raises:
            FakeSDKError: When the box is the failed box.
        """
        if sandbox_id == failed_id:
            raise FakeSDKError("down", 503)
        await killing(sandbox_id)

    failed_id, live_id = ("sbx-1", "sbx-2") if failed_first else ("sbx-2", "sbx-1")
    sdk.AsyncSandbox.list = list_two_boxes
    sdk.AsyncSandbox.kill_by_id = kill_all_but_the_failed_box
    with pytest.raises(SandboxInfraError):
        await _protected(usage).start()
    assert sdk.killed == [live_id]
    assert usage.calls[1] == ("bind", live_id)
    assert usage.names() == ["sent", "bind", "stopped", "settle"]


async def test_failed_lookup_keeps_a_refused_hold_pending(sdk: FakeSuperserve) -> None:
    """Keep the hold pending when the lookup after a refused create fails."""
    usage = FakeUsage()
    sdk.create_error = FakeSDKError("bad", 400)
    sdk.list_error = FakeSDKError("down", 503)
    with pytest.raises(SandboxInfraError):
        await _protected(usage).start()
    assert usage.calls[-1] == ("fail", False)


async def test_failed_template_build_releases_the_unsent_hold(sdk: FakeSuperserve) -> None:
    """Fail the usage before any create is sent when the template build fails."""
    usage = FakeUsage()
    sdk.build_error = FakeSDKError("build", None)
    with pytest.raises(SandboxInfraError, match="template"):
        await _protected(usage).start()
    assert usage.calls == [("fail", False)]
    assert usage.sent_at is None
    assert not sdk.boxes


async def test_cancel_during_create_kills_and_settles_the_box(sdk: FakeSuperserve) -> None:
    """Wait for a create that a cancel interrupted, then kill and settle its box, and raise the cancel."""
    usage = FakeUsage()
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate() -> None:
        """Hold the create until the test releases it."""
        entered.set()
        await release.wait()

    sdk.create_hook = gate
    start = asyncio.ensure_future(_protected(usage).start())
    await entered.wait()
    start.cancel()
    await _settle_loop()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert sdk.killed == ["sbx-1"]
    assert usage.names()[-2:] == ["stopped", "settle"]


async def test_cancel_during_a_failed_create_fails_the_usage(sdk: FakeSuperserve) -> None:
    """Fail the usage of a create that failed after a cancel, and raise the cancel."""
    usage = FakeUsage()
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate() -> None:
        """Hold the create until the test releases it."""
        entered.set()
        await release.wait()

    sdk.create_hook = gate
    sdk.create_error = FakeSDKError("no credit", 402)
    start = asyncio.ensure_future(_protected(usage).start())
    await entered.wait()
    start.cancel()
    await _settle_loop()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert usage.calls[-1] == ("fail", True)


async def test_create_that_ends_after_the_grace_is_killed_in_the_background(
    sdk: FakeSuperserve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass the cancel on after the grace time, then kill and settle a box that arrives later."""
    monkeypatch.setattr(provider, "CREATE_GRACE", 0.01)
    usage = FakeUsage()
    release = asyncio.Event()
    sdk.create_hook = release.wait
    start = asyncio.ensure_future(_protected(usage).start())
    await _settle_loop()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert not sdk.boxes
    release.set()
    await _background()
    assert sdk.killed == ["sbx-1"]
    assert usage.names()[-2:] == ["stopped", "settle"]


async def test_failed_kill_still_settles_and_raises(sdk: FakeSuperserve) -> None:
    """Settle the funded lifetime when the kill fails, and raise the kill error as an infra error."""
    usage = FakeUsage()
    env = _protected(usage)
    await env.start()
    sdk.kill_error = FakeSDKError("down", 503)
    with pytest.raises(SandboxInfraError, match="kill the box") as raised:
        await env.stop()
    assert isinstance(raised.value.__cause__, FakeSDKError)
    assert usage.names()[-1] == "settle"
    assert "stopped" not in usage.names()
    assert sdk.box().closed


async def test_kill_timeout_still_settles_and_raises_an_infra_error(
    sdk: FakeSuperserve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Map a kill that runs past ``STOP_TIMEOUT`` to an infra error, and still settle."""
    monkeypatch.setattr(provider, "STOP_TIMEOUT", 0.01)
    usage = FakeUsage()
    env = _protected(usage)
    await env.start()
    sdk.kill_hook = asyncio.Event().wait
    with pytest.raises(SandboxInfraError, match="kill the box") as raised:
        await env.stop()
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert usage.names()[-1] == "settle"
    assert "stopped" not in usage.names()


async def test_short_lifetime_sends_the_timeout_floor_and_funds_it(
    sdk: FakeSuperserve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send at least 60 s as the timeout, fund that bound, and keep the cap at the spec lifetime."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record the cap sleep and never wake.

        Args:
            seconds: The requested sleep.
        """
        slept.append(seconds)
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "_cap_sleep", fake_sleep)
    requests: list[dict[str, Any]] = []
    usage = FakeUsage()
    spec = SandboxSpec(lifetime_seconds=10, resources=ResourceRequest(cpus=2, memory_mb=4096), operation_key="op-1")
    env = SuperserveEnvironment(
        spec, budget=object(), usage_factory=lambda budget, request, **_: requests.append(request) or usage
    )
    await env.start()
    await _settle_loop()
    try:
        assert sdk.box().kwargs["timeout_seconds"] == 60
        assert requests[0]["lifetime_ms"] == 60_000
        assert 9 < slept[0] <= 10
    finally:
        await env.stop()


async def test_create_arguments_are_built_before_the_clock_starts(
    sdk: FakeSuperserve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start no clock when building the create arguments fails, so the hold is released as never sent."""

    def broken(policy: NetworkPolicy) -> Any:
        """Fail as a bad egress map would.

        Args:
            policy: The egress policy.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("bad policy")

    monkeypatch.setattr(provider, "_network_config", broken)
    usage = FakeUsage()
    env = _protected(usage)
    with pytest.raises(RuntimeError, match="bad policy"):
        await env.start()
    assert "sent" not in usage.names()
    assert env._sent_at is None
    assert usage.calls[-1] == ("fail", False)
    assert not sdk.boxes


async def test_cancel_during_stop_still_kills_and_settles(sdk: FakeSuperserve) -> None:
    """Finish the kill and the settle when the stop is cancelled, and raise the cancel."""
    usage = FakeUsage()
    env = _protected(usage)
    await env.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate() -> None:
        """Hold the kill until the test releases it."""
        entered.set()
        await release.wait()

    sdk.kill_hook = gate
    stop = asyncio.ensure_future(env.stop())
    await entered.wait()
    stop.cancel()
    await _settle_loop()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert sdk.killed == ["sbx-1"]
    assert usage.names()[-2:] == ["stopped", "settle"]


async def test_own_cap_kills_and_settles_the_box_at_its_lifetime(
    sdk: FakeSuperserve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sleep for the lifetime from the create, then kill and settle the box."""
    slept: list[float] = []
    fire = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        """Record the sleep and wait until the test fires the cap.

        Args:
            seconds: The requested sleep.
        """
        slept.append(seconds)
        await fire.wait()

    monkeypatch.setattr(provider, "_cap_sleep", fake_sleep)
    usage = FakeUsage()
    env = _protected(usage)
    await env.start()
    await _settle_loop()
    assert 59 < slept[0] <= 60
    assert not sdk.killed
    fire.set()
    await _background()
    assert sdk.killed == ["sbx-1"]
    assert usage.names()[-2:] == ["stopped", "settle"]
    with pytest.raises(SandboxInfraError, match="not running"):
        await env.exec("true")


async def test_stop_cancels_the_cap(sdk: FakeSuperserve) -> None:
    """Cancel the cap task when the owner stops the box first."""
    env = SuperserveEnvironment(_SPEC)
    await env.start()
    cap = env._cap_task
    assert cap is not None
    assert not cap.done()
    await env.stop()
    await _settle_loop()
    assert cap.cancelled()
    assert sdk.killed == ["sbx-1"]


async def test_exec_sends_cwd_env_user_and_the_run_phase_timeout(sdk: FakeSuperserve) -> None:
    """Send the working directory, the merged env, root for sudo and the run phase timeout."""
    spec = SandboxSpec(**{**_SPEC.__dict__, "env": {"A": "1"}})
    async with SuperserveEnvironment(spec, timeouts=PhaseTimeouts(start=1, setup=1, run=12.5, score=1)) as env:
        await env.exec("true", cwd="/work", env={"B": "2"}, sudo=True)
        (call,) = sdk.box().run_calls
    assert call == {"command": "true", "cwd": "/work", "user": "root", "env": {"A": "1", "B": "2"}, "timeout": 13}


async def test_sdk_timeout_becomes_a_run_timeout(sdk: FakeSuperserve) -> None:
    """Report an SDK command timeout as exit code 124."""
    async with SuperserveEnvironment(_SPEC) as env:
        sdk.box().run_error = FakeTimeoutError("slow")
        result = await env.exec("sleep 99", timeout_sec=1)
    assert (result.exit_code, result.timed_out) == (124, True)


async def test_local_deadline_bounds_a_hung_exec(sdk: FakeSuperserve, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report a timeout when the SDK call hangs past the limit and the grace."""
    monkeypatch.setattr(provider, "EXEC_GRACE", 0)
    async with SuperserveEnvironment(_SPEC) as env:
        sdk.box().run_hook = asyncio.Event().wait
        result = await env.exec("sleep 99", timeout_sec=0.01)
    assert (result.exit_code, result.timed_out) == (124, True)


async def test_sdk_error_in_exec_is_an_infra_error(sdk: FakeSuperserve) -> None:
    """Raise a sandbox infrastructure error for any other SDK failure."""
    async with SuperserveEnvironment(_SPEC) as env:
        sdk.box().run_error = FakeSDKError("down", 503)
        with pytest.raises(SandboxInfraError, match="run the command"):
            await env.exec("true")


async def test_upload_makes_parents_and_writes_bytes(sdk: FakeSuperserve) -> None:
    """Make the parent directories once as root, then write UTF-8 bytes; both transfers have bounds."""
    async with SuperserveEnvironment(_SPEC) as env:
        await env.upload_files({"/a/b/c.txt": "שלום\r\n"})
        box = sdk.box()
        assert box.run_calls[0]["command"] == "mkdir -p /a/b"
        assert box.run_calls[0]["user"] == "root"
        assert box.data["/a/b/c.txt"] == "שלום\r\n".encode()
        assert await env.download_file("/a/b/c.txt") == "שלום\r\n"
        assert box.transfers == [
            {"write": "/a/b/c.txt", "timeout": provider._TRANSFER_TIMEOUT_SEC},
            {"read": "/a/b/c.txt", "timeout": provider._TRANSFER_TIMEOUT_SEC, "max_bytes": 1024**3},
        ]


async def test_download_refuses_binary_data(sdk: FakeSuperserve) -> None:
    """Raise when a file is not UTF-8 text."""
    async with SuperserveEnvironment(_SPEC) as env:
        sdk.box().data["/bin.dat"] = b"\xff\xfe"
        with pytest.raises(SandboxInfraError, match="UTF-8"):
            await env.download_file("/bin.dat")


async def test_stop_job_sandboxes_skips_deleted_boxes_and_survives_a_failure(sdk: FakeSuperserve) -> None:
    """Kill every live box of the job, skip deleted ones and go on after a failed kill."""
    envs = [SuperserveEnvironment(SandboxSpec(**{**_SPEC.__dict__, "tags": {JOB_TAG: "job"}})) for _ in range(2)]
    for env in envs:
        await env.start()
    sdk.boxes[0].status = FakeStatus.DELETED
    assert await SuperserveEnvironment.stop_job_sandboxes("job") == 1
    assert sdk.killed == ["sbx-2"]
    assert not sdk.handle_kills
    sdk.kill_error = FakeSDKError("down", 503)
    assert await SuperserveEnvironment.stop_job_sandboxes("job") == 0


def test_preflight_needs_the_sdk_and_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail preflight without the SDK or without the API key."""
    monkeypatch.delenv("SUPERSERVE_API_KEY", raising=False)
    with pytest.raises(SandboxFatalError, match="SUPERSERVE_API_KEY"):
        SuperserveEnvironment.preflight()
    monkeypatch.setenv("SUPERSERVE_API_KEY", "test-value")
    SuperserveEnvironment.preflight()
    monkeypatch.setattr(provider, "AsyncSandbox", None)
    with pytest.raises(SandboxFatalError, match="superserve package"):
        SuperserveEnvironment.preflight()


async def test_cleanup_errors_are_logged_not_raised(sdk: FakeSuperserve, caplog: pytest.LogCaptureFixture) -> None:
    """Log a failed kill of a box that a failed create made, and still raise the create error."""
    usage = FakeUsage()
    sdk.create_error = FakeSDKError("conflict", 409)
    sdk.create_makes_box = True
    sdk.kill_error = FakeSDKError("down", 503)
    with caplog.at_level(logging.ERROR, logger=provider.__name__), pytest.raises(SandboxInfraError):
        await _protected(usage).start()
    assert "failed create made" in caplog.text
    assert usage.names()[-1] == "settle"


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Create a private funded wallet and ledger for real admission tests.

    Args:
        tmp_path: Per-test directory for the SQLite file.
        monkeypatch: Pytest fixture that pins the usage markup.

    Yields:
        The engine bound to the funded ledger.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    engine = create_engine(f"sqlite:///{tmp_path / 'superserve.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


def _runtime(database: Engine) -> BudgetRuntime:
    """Bind a real budget runtime to the local ledger.

    Args:
        database: Engine of the funded ledger.

    Returns:
        A runtime for a fresh 50-cent budget owned by ``alice``.
    """
    service = BudgetService(engine=database)
    budget = service.create("alice", 50, idempotency_key="draft")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _state(database: Engine) -> str:
    """Return the state of the only operation.

    Args:
        database: Engine of the ledger.

    Returns:
        The operation state.
    """
    with Session(database) as session:
        return session.scalars(select(ExecutionOperationModel)).one().state


def _ledger_spec() -> SandboxSpec:
    """Return a protected spec for the real ledger.

    Returns:
        The spec.
    """
    return SandboxSpec(**{**_SPEC.__dict__, "operation_key": "op-1"})


async def test_real_ledger_settles_a_stopped_box(sdk: FakeSuperserve, database: Engine) -> None:
    """Reserve before the create, then settle the measured runtime after the kill."""
    runtime = _runtime(database)
    env = SuperserveEnvironment(_ledger_spec(), budget=runtime)
    await env.start()
    assert _state(database) == "pending"
    assert sdk.box().metadata[BOX_KEY] == box_tag("op-1")
    await env.stop()
    assert _state(database) == "settled"
    assert runtime._pinned == {}


async def test_real_ledger_releases_a_refused_create(sdk: FakeSuperserve, database: Engine) -> None:
    """Release the hold when Superserve refused the create and no box exists."""
    runtime = _runtime(database)
    sdk.create_error = FakeSDKError("no credit", 402)
    with pytest.raises(SandboxInfraError):
        await SuperserveEnvironment(_ledger_spec(), budget=runtime).start()
    assert _state(database) == "released"
    assert runtime._pinned == {}


async def test_real_ledger_keeps_an_unknown_create_pending(sdk: FakeSuperserve, database: Engine) -> None:
    """Keep the hold pending when the create outcome is unknown."""
    sdk.create_error = FakeSDKError("connection", None)
    with pytest.raises(SandboxInfraError):
        await SuperserveEnvironment(_ledger_spec(), budget=_runtime(database)).start()
    assert _state(database) == "pending"

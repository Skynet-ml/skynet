"""Tests for the Modal sandbox provider, against an in-memory fake of the Modal SDK.

No test calls Modal: an autouse fixture replaces every SDK name that the
provider uses with the fake below.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.modal_usage import ModalUsageReservation
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.usage_tags import BOX_KEY
from core.sandbox import (
    JOB_TAG,
    NetworkPolicy,
    ResourceMode,
    ResourceRequest,
    SandboxEnvironment,
    SandboxSpec,
    SandboxSpecError,
    factory,
)
from core.sandbox.capabilities import VERCEL_CAPABILITIES, EnvironmentCapabilities, validate_spec
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.factory import ProductionNotAllowedError
from core.sandbox.providers import modal as modal_provider
from core.sandbox.providers.ec2 import EC2_CAPABILITIES
from core.sandbox.providers.modal import ModalEnvironment, egress_kwargs, resource_kwargs
from core.sandbox.providers.smol import SmolEnvironment
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.sandbox.timeouts import Phase, PhaseTimeouts
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

_SPEC = SandboxSpec(lifetime_seconds=60)


class FakeModalError(Exception):
    """Stand in for ``modal.exception.Error``."""


class FakeModalTimeoutError(FakeModalError):
    """Stand in for ``modal.exception.TimeoutError``, which is not the builtin one."""


class FakeExecTimeoutError(FakeModalTimeoutError):
    """Stand in for ``modal.exception.ExecTimeoutError``."""


def _aio(fn: Callable[..., Any]) -> SimpleNamespace:
    """Expose a function the way Modal exposes its async methods.

    Args:
        fn: The coroutine or async generator function.

    Returns:
        An object whose ``aio`` attribute is ``fn``.
    """
    return SimpleNamespace(aio=fn)


class FakeStream:
    """Act as a Modal stream reader over fixed text chunks."""

    def __init__(self, chunks: list[str]) -> None:
        """Keep the chunks.

        Args:
            chunks: The chunks that iteration yields.
        """
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[str]:
        """Yield each chunk."""
        for chunk in self._chunks:
            yield chunk


class FakeSandbox:
    """Act as one Modal sandbox with a dict file system and a tiny command set."""

    def __init__(self, sdk: FakeModal, args: tuple[str, ...], kwargs: dict[str, Any]) -> None:
        """Record the create arguments.

        Args:
            sdk: The fake SDK that owns the sandbox.
            args: The entrypoint arguments.
            kwargs: The create keyword arguments.
        """
        self.sdk = sdk
        self.object_id: str | None = None if sdk.no_object_id else f"sb-{len(sdk.boxes) + 1}"
        self.args = args
        self.kwargs = kwargs
        self.tags: dict[str, str] = dict(kwargs.get("tags") or {})
        self.files: dict[str, bytes] = {}
        self.policies: list[dict[str, list[str]]] = []
        self.exec_calls: list[dict[str, Any]] = []
        self.terminated = False
        self.poll_code: int | None = None
        self.exec_hook: Callable[[], Awaitable[None]] | None = None
        self.exit_override: int | None = None
        self.fail_terminate = False
        self.poll_error: Exception | None = None
        self.exec = _aio(self._exec)
        self.poll = _aio(self._poll)
        self.terminate = _aio(self._terminate)
        self.wait = _aio(self._wait)
        self._experimental_set_outbound_network_policy = _aio(self._set_policy)
        self.filesystem = SimpleNamespace(
            copy_from_local=_aio(self._copy_from_local), copy_to_local=_aio(self._copy_to_local)
        )

    def _env(self, secrets: list[FakeSecret]) -> dict[str, str]:
        """Merge the sandbox secrets and the per-exec secrets.

        Args:
            secrets: The per-exec secrets.

        Returns:
            The environment the command sees.
        """
        env: dict[str, str] = {}
        for secret in [*(self.kwargs.get("secrets") or []), *secrets]:
            env.update(secret.values)
        return env

    async def _exec(self, *args: str, workdir: str | None, secrets: list[FakeSecret], **options: int | None) -> Any:
        """Run one ``bash -c`` command from the small command set.

        Args:
            *args: ``bash``, ``-c`` and the command.
            workdir: The working directory.
            secrets: The per-exec secrets.
            **options: ``timeout``, the Modal exec timeout.

        Returns:
            A fake process.
        """
        assert args[:2] == ("bash", "-c")
        self.exec_calls.append({"command": args[2], "workdir": workdir, "timeout": options["timeout"]})
        if self.exec_hook is not None:
            await self.exec_hook()
        words = shlex.split(args[2])
        stdout, code = "", 0
        if words[0] == "false":
            code = 1
        elif words[0] == "exit":
            code = int(words[1])
        elif words[0] == "echo":
            stdout = " ".join(words[1:]) + "\n"
        elif words[0] == "cat":
            stdout = self.files[words[1]].decode("utf-8")
        elif words[0] == "printenv":
            stdout = self._env(secrets).get(words[1], "") + "\n"
        if self.exit_override is not None:
            code = self.exit_override

        async def wait() -> int:
            """Return the exit code."""
            return code

        return SimpleNamespace(stdout=FakeStream([stdout] if stdout else []), stderr=FakeStream([]), wait=_aio(wait))

    async def _poll(self) -> int | None:
        """Return the exit code of the sandbox, or None while it runs.

        Raises:
            Exception: ``poll_error`` is set.
        """
        if self.poll_error is not None:
            raise self.poll_error
        return self.poll_code

    async def _terminate(self) -> None:
        """End the sandbox.

        Raises:
            RuntimeError: ``fail_terminate`` is set.
        """
        if self.fail_terminate:
            raise RuntimeError("fake terminate failure")
        self.terminated = True
        self.poll_code = 137

    async def _wait(self, *, raise_on_termination: bool = True) -> None:
        """Return at once: the fake ends right away.

        Args:
            raise_on_termination: Must be False for the provider.
        """
        assert raise_on_termination is False

    async def _set_policy(self, **kwargs: list[str]) -> None:
        """Record a new egress policy.

        Args:
            **kwargs: The allowlists.
        """
        self.policies.append(kwargs)

    async def _copy_from_local(self, local: Path, remote: str) -> None:
        """Copy a local file into the sandbox.

        Args:
            local: The local path.
            remote: The sandbox path.
        """
        self.files[remote] = Path(local).read_bytes()  # noqa: ASYNC240 - a tiny local test file

    async def _copy_to_local(self, remote: str, local: Path) -> None:
        """Copy a sandbox file to a local path.

        Args:
            remote: The sandbox path.
            local: The local path.

        Raises:
            FileNotFoundError: The sandbox file does not exist.
        """
        if remote not in self.files:
            raise FileNotFoundError(remote)
        Path(local).write_bytes(self.files[remote])  # noqa: ASYNC240 - a tiny local test file


class FakeSecret:
    """Act as a Modal secret built from a dict."""

    def __init__(self, values: dict[str, str]) -> None:
        """Keep the values.

        Args:
            values: The environment variables.
        """
        self.values = values


class FakeModal:
    """Act as the parts of the Modal SDK that the provider uses."""

    def __init__(self) -> None:
        """Create an empty fake SDK."""
        self.boxes: list[FakeSandbox] = []
        self.create_calls = 0
        self.create_error: Exception | None = None
        self.create_error_after_box: Exception | None = None
        self.create_gate: asyncio.Event | None = None
        self.create_entered = asyncio.Event()
        self.builds = 0
        self.build_error: Exception | None = None
        self.list_error: Exception | None = None
        self.list_calls = 0
        self.list_gate: asyncio.Event | None = None
        self.list_entered = asyncio.Event()
        self.list_hook: Callable[[], None] | None = None
        self.no_object_id = False
        self.App = SimpleNamespace(lookup=_aio(self._lookup))
        self.Image = SimpleNamespace(from_registry=lambda tag: SimpleNamespace(tag=tag, build=_aio(self._build)))
        self.Secret = SimpleNamespace(from_dict=FakeSecret)
        self.Sandbox = SimpleNamespace(create=_aio(self._create), list=_aio(self._list))

    async def _lookup(self, name: str, *, create_if_missing: bool) -> Any:
        """Return a fake app.

        Args:
            name: The app name.
            create_if_missing: Must be True.

        Returns:
            The fake app.
        """
        assert create_if_missing
        return SimpleNamespace(name=name, app_id=f"ap-{name}")

    async def _build(self, app: Any) -> None:
        """Build the image.

        Args:
            app: The fake app.

        Raises:
            Exception: ``build_error`` is set.
        """
        self.builds += 1
        if self.build_error is not None:
            raise self.build_error

    async def _create(self, *args: str, app: Any, **kwargs: Any) -> FakeSandbox:
        """Create a fake sandbox.

        Args:
            *args: The entrypoint arguments.
            app: The fake app.
            **kwargs: The create keyword arguments.

        Returns:
            The fake sandbox.

        Raises:
            Exception: ``create_error`` is set, or ``create_error_after_box`` is
                set, which raises after the sandbox exists.
        """
        self.create_calls += 1
        self.create_entered.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        if self.create_error is not None:
            raise self.create_error
        box = FakeSandbox(self, args, {"app": app, **kwargs})
        self.boxes.append(box)
        if self.create_error_after_box is not None:
            raise self.create_error_after_box
        return box

    async def _list(self, *, app_id: str, tags: dict[str, str]) -> AsyncIterator[FakeSandbox]:
        """Yield the running sandboxes of the app that carry every tag.

        Args:
            app_id: The app id.
            tags: The tags to match.

        Raises:
            Exception: ``list_error`` is set.
        """
        self.list_calls += 1
        self.list_entered.set()
        if self.list_hook is not None:
            self.list_hook()
        if self.list_gate is not None:
            await self.list_gate.wait()
        if self.list_error is not None:
            raise self.list_error
        for box in list(self.boxes):
            if box.kwargs["app"].app_id == app_id and not box.terminated and tags.items() <= box.tags.items():
                yield box


@pytest.fixture(autouse=True)
def fake_modal(monkeypatch: pytest.MonkeyPatch) -> FakeModal:
    """Replace the Modal SDK in the provider module with a fresh fake.

    The second lookup after a failed create runs at once.

    Args:
        monkeypatch: Pytest fixture that restores the module after the test.
    """
    sdk = FakeModal()
    for name in ("App", "Image", "Secret", "Sandbox"):
        monkeypatch.setattr(modal_provider, name, getattr(sdk, name))
    monkeypatch.setattr(modal_provider, "_EXEC_TIMEOUT_ERRORS", (FakeExecTimeoutError,))
    monkeypatch.setattr(modal_provider, "_RECHECK_DELAY_SEC", 0)
    return sdk


class TestModalEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the Modal provider and the fake SDK."""

    provider_cls = ModalEnvironment


def _box(sdk: FakeModal) -> FakeSandbox:
    """Return the only fake sandbox.

    Args:
        sdk: The fake SDK.

    Returns:
        The fake sandbox.
    """
    assert len(sdk.boxes) == 1
    return sdk.boxes[0]


async def test_create_sends_the_image_lifetime_tags_env_and_name(fake_modal: FakeModal) -> None:
    """Create one sleeping sandbox with the spec image, lifetime, tags, env and name."""
    spec = SandboxSpec(
        lifetime_seconds=90.5,
        image="python:3.12-slim",
        name="box-1",
        tags={JOB_TAG: "job-1"},
        env={"A": "1"},
    )
    async with ModalEnvironment(spec, app_name="skynet-test", idle_timeout_sec=30):
        kwargs = _box(fake_modal).kwargs
    assert _box(fake_modal).args == ("sleep", "infinity")
    assert kwargs["app"].name == "skynet-test"
    assert kwargs["image"].tag == "python:3.12-slim"
    assert kwargs["timeout"] == 91
    assert kwargs["idle_timeout"] == 30
    assert kwargs["tags"] == {JOB_TAG: "job-1"}
    assert kwargs["name"] == "box-1"
    assert [secret.values for secret in kwargs["secrets"]] == [{"A": "1"}]


async def test_create_defaults_omit_optional_arguments(fake_modal: FakeModal) -> None:
    """Use the default image and leave out tags, secrets, name and idle timeout."""
    async with ModalEnvironment(_SPEC):
        kwargs = _box(fake_modal).kwargs
    assert kwargs["image"].tag == modal_provider.DEFAULT_IMAGE
    assert kwargs["app"].name == modal_provider.DEFAULT_APP_NAME
    for name in ("tags", "secrets", "name", "idle_timeout"):
        assert name not in kwargs


@pytest.mark.parametrize(
    ("mode", "cpu", "memory"),
    [
        (ResourceMode.REQUEST, 2, 4096),
        (ResourceMode.LIMIT, (0.125, 2), (128, 4096)),
        (ResourceMode.GUARANTEE, (2, 2), (4096, 4096)),
        (ResourceMode.AUTO, (2, 2), 4096),
    ],
)
def test_resource_kwargs_follow_the_resource_mode(mode: ResourceMode, cpu: Any, memory: Any) -> None:
    """Map each resource mode to the Harbor request and limit values."""
    spec = SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=2, memory_mb=4096, mode=mode))
    assert resource_kwargs(spec) == {"cpu": cpu, "memory": memory}


def test_resource_kwargs_send_a_gpu_and_skip_sizes_when_ignored() -> None:
    """Send the GPU count, and send no sizes in the ignore mode."""
    spec = SandboxSpec(
        lifetime_seconds=60, resources=ResourceRequest(cpus=4, memory_mb=1024, gpu=2, mode=ResourceMode.IGNORE)
    )
    assert resource_kwargs(spec) == {"gpu": "any:2"}


@pytest.mark.parametrize(
    ("gpu_type", "count", "expected"),
    [(" a100 ", 0, "A100"), ("h100", 2, "H100:2"), ("A100-80GB", 1, "A100-80GB:1"), ("a10g", 0, "A10G")],
)
def test_resource_kwargs_send_the_normalized_gpu_type(gpu_type: str, count: int, expected: str) -> None:
    """Send the GPU type as the upper-case Modal name, with the count when the spec sets one."""
    spec = SandboxSpec(lifetime_seconds=60, gpu_type=gpu_type, resources=ResourceRequest(gpu=count))
    assert resource_kwargs(spec)["gpu"] == expected


@pytest.mark.parametrize("gpu_type", ["", "  ", "tpu-v5", "a100:2", 7])
def test_a_gpu_type_modal_does_not_name_is_refused_before_start(fake_modal: FakeModal, gpu_type: Any) -> None:
    """Refuse an empty, unknown or malformed GPU type in the constructor, so no box starts."""
    with pytest.raises(SandboxSpecError, match="GPU"):
        ModalEnvironment(SandboxSpec(lifetime_seconds=60, gpu_type=gpu_type))
    assert fake_modal.create_calls == 0


async def test_create_starts_the_sandbox_with_the_gpu_type(fake_modal: FakeModal) -> None:
    """Send the spec's GPU type to ``Sandbox.create``."""
    async with ModalEnvironment(SandboxSpec(lifetime_seconds=60, gpu_type="a100")):
        assert _box(fake_modal).kwargs["gpu"] == "A100"


@pytest.mark.parametrize(
    "caps",
    [VERCEL_CAPABILITIES, SmolEnvironment.capabilities, EC2_CAPABILITIES],
    ids=["vercel", "smol-with-gpu-counts", "ec2"],
)
def test_every_other_provider_refuses_a_gpu_type(caps: EnvironmentCapabilities) -> None:
    """Refuse a GPU type on a provider without GPU types, also one that takes a GPU count or picks an instance type.

    A type that is not text or is blank gets the name error first, on every provider.

    Args:
        caps: The capabilities of a provider that is not Modal.
    """
    with pytest.raises(SandboxSpecError, match=f"The {caps.name} sandbox cannot start a box with GPU type 'a100'"):
        validate_spec(SandboxSpec(lifetime_seconds=60, gpu_type="a100"), caps)
    for bad in (7, "  "):
        with pytest.raises(SandboxSpecError, match="must be a non-empty name"):
            validate_spec(SandboxSpec(lifetime_seconds=60, gpu_type=bad), caps)  # type: ignore[arg-type]


def test_resource_kwargs_leave_out_memory_when_unset() -> None:
    """Leave the memory to Modal when the spec sets none."""
    assert resource_kwargs(SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=1))) == {"cpu": 1}


@pytest.mark.parametrize(
    "spec",
    [
        SandboxSpec(lifetime_seconds=24 * 60 * 60 + 1),
        SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(disk_mb=1024)),
    ],
)
def test_specs_modal_cannot_apply_are_refused(spec: SandboxSpec) -> None:
    """Refuse a lifetime above 24 hours and any disk size."""
    with pytest.raises(SandboxSpecError):
        ModalEnvironment(spec)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (NetworkPolicy(), {"outbound_domain_allowlist": ["*"], "outbound_cidr_allowlist": ["0.0.0.0/0"]}),
        (NetworkPolicy(disabled=True), {"outbound_domain_allowlist": [], "outbound_cidr_allowlist": []}),
        (
            NetworkPolicy(allowed_hosts=("pypi.org", "10.0.0.0/8", "1.2.3.4", "::1")),
            {
                "outbound_domain_allowlist": ["pypi.org"],
                "outbound_cidr_allowlist": ["10.0.0.0/8", "1.2.3.4/32", "::1/128"],
            },
        ),
    ],
)
def test_egress_kwargs_split_hosts_into_domains_and_cidrs(policy: NetworkPolicy, expected: dict) -> None:
    """Map open, deny-all and allowlist egress to the Modal allowlists."""
    assert egress_kwargs(policy) == expected


async def test_create_applies_the_spec_network_policy(fake_modal: FakeModal) -> None:
    """Send the spec allowlist at create time."""
    spec = SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",))
    async with ModalEnvironment(spec):
        kwargs = _box(fake_modal).kwargs
    assert kwargs["outbound_domain_allowlist"] == ["pypi.org"]
    assert kwargs["outbound_cidr_allowlist"] == []


async def test_network_policy_changes_on_a_running_sandbox(fake_modal: FakeModal) -> None:
    """Replace the egress policy of a running sandbox, and refuse it before start."""
    env = ModalEnvironment(_SPEC)
    with pytest.raises(RuntimeError):
        await env.set_network_policy(NetworkPolicy(disabled=True))
    async with env:
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("github.com",)))
        await env.disable_network()
        assert _box(fake_modal).policies == [
            {"outbound_domain_allowlist": ["github.com"], "outbound_cidr_allowlist": []},
            {"outbound_domain_allowlist": [], "outbound_cidr_allowlist": []},
        ]


async def test_exec_sends_cwd_env_and_the_run_phase_timeout(fake_modal: FakeModal) -> None:
    """Pass the working directory, the env and the rounded run-phase limit to Modal."""
    timeouts = PhaseTimeouts(start=1, setup=1, run=2.5, score=1)
    async with ModalEnvironment(_SPEC, timeouts=timeouts) as env:
        result = await env.exec("printenv X", cwd="/work", env={"X": "y"})
        await env.exec("true", timeout_sec=0.2)
    assert result.stdout == "y\n"
    calls = _box(fake_modal).exec_calls
    assert calls[0] == {"command": "printenv X", "workdir": "/work", "timeout": 3}
    assert calls[1]["timeout"] == 1


async def test_exec_before_start_raises() -> None:
    """Refuse exec on a stopped sandbox."""
    with pytest.raises(RuntimeError):
        await ModalEnvironment(_SPEC).exec("true")


async def test_modal_timeout_exit_code_becomes_a_run_timeout(fake_modal: FakeModal) -> None:
    """Turn Modal's -1 exit code for an expired exec into a timed-out result."""
    async with ModalEnvironment(_SPEC) as env:
        _box(fake_modal).exit_override = -1
        result = await env.exec("true", timeout_sec=5)
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_modal_exec_timeout_error_becomes_a_run_timeout(fake_modal: FakeModal) -> None:
    """Turn Modal's ExecTimeoutError into a timed-out result."""

    async def expire() -> None:
        """Raise the fake Modal timeout."""
        raise FakeExecTimeoutError

    async with ModalEnvironment(_SPEC) as env:
        _box(fake_modal).exec_hook = expire
        result = await env.exec("true", timeout_sec=5)
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_local_deadline_bounds_a_hung_exec(fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a timed-out result when Modal never answers within the limit and the grace."""
    monkeypatch.setattr(modal_provider, "_TIMEOUT_GRACE_SEC", 0.0)

    async def hang() -> None:
        """Never return."""
        await asyncio.Event().wait()

    async with ModalEnvironment(_SPEC) as env:
        _box(fake_modal).exec_hook = hang
        result = await env.exec("true", timeout_sec=0.05)
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_exit_137_after_a_failed_call_is_an_out_of_memory_error(fake_modal: FakeModal) -> None:
    """Report an infra error when the sandbox died with 137 under a failed SDK call."""
    cause = RuntimeError("stream closed")

    async def die() -> None:
        """Fail the way a killed sandbox fails."""
        raise cause

    spec = SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(memory_mb=512))
    async with ModalEnvironment(spec) as env:
        box = _box(fake_modal)
        box.exec_hook = die
        box.poll_code = 137
        with pytest.raises(SandboxInfraError, match="512") as info:
            await env.exec("true")
        assert info.value.__cause__ is cause
        box.poll_code = 1
        with pytest.raises(RuntimeError, match="stream closed"):
            await env.exec("true")
        box.poll_error = RuntimeError("poll failed")
        with pytest.raises(RuntimeError, match="stream closed"):
            await env.exec("true")


async def test_failure_after_a_requested_stop_is_not_out_of_memory(fake_modal: FakeModal) -> None:
    """Keep the original error when our own stop ended the sandbox with 137."""
    env = ModalEnvironment(_SPEC)
    await env.start()

    async def stop_then_fail() -> None:
        """Stop the sandbox, then fail like a closed stream."""
        await env.stop()
        raise RuntimeError("stream closed")

    _box(fake_modal).exec_hook = stop_then_fail
    with pytest.raises(RuntimeError, match="stream closed"):
        await env.exec("true")
    assert _box(fake_modal).poll_code == 137


async def test_failed_create_runs_once_with_no_retry(fake_modal: FakeModal) -> None:
    """Call Modal once and pass the error on, with no retry."""
    fake_modal.create_error = RuntimeError("quota")
    env = ModalEnvironment(_SPEC)
    with pytest.raises(RuntimeError, match="quota"):
        await env.start()
    assert fake_modal.create_calls == 1
    with pytest.raises(RuntimeError):
        await env.exec("true")


async def test_start_is_a_no_op_while_running(fake_modal: FakeModal) -> None:
    """Create one sandbox when start runs twice."""
    async with ModalEnvironment(_SPEC) as env:
        await env.start()
    assert fake_modal.create_calls == 1


async def test_cancel_during_start_terminates_the_new_sandbox(fake_modal: FakeModal) -> None:
    """Wait for a cancelled create to finish, then terminate the sandbox it made."""
    fake_modal.create_gate = asyncio.Event()
    env = ModalEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await fake_modal.create_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    fake_modal.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _box(fake_modal).terminated
    with pytest.raises(RuntimeError):
        await env.exec("true")


async def test_repeat_cancel_during_start_still_terminates_the_new_sandbox(fake_modal: FakeModal) -> None:
    """Keep waiting for the create through a second cancel, then terminate the sandbox it made."""
    fake_modal.create_gate = asyncio.Event()
    env = ModalEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await fake_modal.create_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    fake_modal.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_modal.create_calls == 1
    assert _box(fake_modal).terminated
    with pytest.raises(RuntimeError):
        await env.exec("true")


async def test_cancel_during_a_hung_create_passes_on_after_the_grace_time(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass a cancel on after the grace time, also through a repeat cancel, and stop the late sandbox."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_modal.create_gate = asyncio.Event()
    env = ModalEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await fake_modal.create_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert fake_modal.boxes == []
    fake_modal.create_gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert fake_modal.create_calls == 1
    assert _box(fake_modal).terminated
    assert not modal_provider._LATE_TERMINATES
    with pytest.raises(RuntimeError):
        await env.exec("true")


async def test_cancel_during_start_does_not_wait_past_the_grace_time_for_a_hung_terminate(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass a cancel on after the grace time while the terminate hangs, then finish it in the background."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05)
    gate = asyncio.Event()
    real_terminate = modal_provider._terminate

    async def slow_terminate(sandbox: Any) -> None:
        """Wait for the gate, then terminate.

        Args:
            sandbox: The Modal sandbox.
        """
        await gate.wait()
        await real_terminate(sandbox)

    monkeypatch.setattr(modal_provider, "_terminate", slow_terminate)
    fake_modal.create_gate = asyncio.Event()
    env = ModalEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await fake_modal.create_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    fake_modal.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    box = _box(fake_modal)
    assert not box.terminated
    assert len(modal_provider._LATE_TERMINATES) == 1
    gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert box.terminated
    assert not modal_provider._LATE_TERMINATES
    with pytest.raises(RuntimeError):
        await env.exec("true")


async def test_start_timeout_ends_start_while_the_create_hangs(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise the start timeout soon after its limit while Modal's create hangs, then stop the late sandbox."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_modal.create_gate = asyncio.Event()
    env = ModalEnvironment(_SPEC)
    timeouts = PhaseTimeouts(start=0.05, setup=1, run=1, score=1)

    async def start_under_the_limit() -> None:
        """Start the sandbox under the start phase limit."""
        async with timeouts.limit(Phase.START):
            await env.start()

    with pytest.raises(StartTimeoutError):
        await asyncio.wait_for(start_under_the_limit(), 2)
    fake_modal.create_gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert _box(fake_modal).terminated


async def test_late_terminate_failure_is_logged_not_raised(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Log a failed terminate of a late sandbox, and raise nothing from the callback."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.01)

    async def failing_terminate(sandbox: Any) -> None:
        """Fail like a lost Modal connection.

        Args:
            sandbox: The Modal sandbox.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("terminate lost")

    monkeypatch.setattr(modal_provider, "_terminate", failing_terminate)
    fake_modal.create_gate = asyncio.Event()
    env = ModalEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await fake_modal.create_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with caplog.at_level(logging.ERROR):
        fake_modal.create_gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
    assert "after a cancelled start or stop" in caplog.text
    assert "terminate lost" in caplog.text
    assert not modal_provider._LATE_TERMINATES


async def test_repeat_cancel_during_stop_still_terminates(fake_modal: FakeModal) -> None:
    """Finish the terminate through a second cancel of stop, then pass the cancel on."""
    env = ModalEnvironment(_SPEC)
    await env.start()
    box = _box(fake_modal)
    gate = asyncio.Event()
    terminate = box._terminate

    async def slow_terminate() -> None:
        """Wait for the gate, then terminate."""
        await gate.wait()
        await terminate()

    box.terminate = _aio(slow_terminate)
    task = asyncio.create_task(env.stop())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert box.terminated


async def test_sink_error_cancels_the_other_stream_reader(fake_modal: FakeModal) -> None:
    """Pass a sink error on unchanged and cancel the reader of the other stream."""
    env = ModalEnvironment(_SPEC)
    await env.start()
    box = _box(fake_modal)
    reader_cancelled = asyncio.Event()

    async def hanging_stream() -> AsyncIterator[str]:
        """Yield nothing until cancelled, and record the cancel."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            reader_cancelled.set()
            raise
        yield ""

    async def wait() -> int:
        """Return exit 0."""
        return 0

    async def fake_exec(*args: str, **kwargs: Any) -> Any:
        """Return a process with one stdout chunk and a stderr that hangs.

        Args:
            *args: The command arguments.
            **kwargs: The exec options.

        Returns:
            A fake process.
        """
        return SimpleNamespace(stdout=FakeStream(["boom"]), stderr=hanging_stream(), wait=_aio(wait))

    box.exec = _aio(fake_exec)

    def sink(stream: str, text: str) -> None:
        """Fail on stdout.

        Args:
            stream: The stream name.
            text: The chunk.

        Raises:
            ValueError: The chunk comes from stdout.
        """
        if stream == "stdout":
            raise ValueError("sink failed")

    with pytest.raises(ValueError, match="sink failed"):
        await env.exec("echo hi", on_output=sink)
    assert reader_cancelled.is_set()


async def test_cancel_during_stop_still_terminates(fake_modal: FakeModal) -> None:
    """Finish the terminate when stop is cancelled."""
    env = ModalEnvironment(_SPEC)
    await env.start()
    box = _box(fake_modal)
    gate = asyncio.Event()
    terminate = box._terminate

    async def slow_terminate() -> None:
        """Wait for the gate, then terminate."""
        await gate.wait()
        await terminate()

    box.terminate = _aio(slow_terminate)
    task = asyncio.create_task(env.stop())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert box.terminated


async def test_stop_timeout_ends_stop_while_the_terminate_hangs(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass a caller timeout on after the grace time while the terminate hangs, then finish it in the background."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05)
    env = ModalEnvironment(_SPEC)
    await env.start()
    box = _box(fake_modal)
    gate = asyncio.Event()
    terminate = box._terminate

    async def hung_terminate() -> None:
        """Wait for the gate, then terminate."""
        await gate.wait()
        await terminate()

    box.terminate = _aio(hung_terminate)

    async def stop_under_a_limit() -> None:
        """Stop the sandbox under a short caller timeout."""
        async with asyncio.timeout(0.05):
            await env.stop()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(stop_under_a_limit(), 2)
    assert not box.terminated
    assert len(modal_provider._LATE_TERMINATES) == 1
    gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert box.terminated
    assert not modal_provider._LATE_TERMINATES


async def test_stop_job_sandboxes_continues_after_a_failure(
    fake_modal: FakeModal, caplog: pytest.LogCaptureFixture
) -> None:
    """Log a failed terminate, keep sweeping and count only the stopped sandboxes."""
    envs = [ModalEnvironment(SandboxSpec(lifetime_seconds=60, tags={JOB_TAG: "job-9"})) for _ in range(3)]
    for env in envs:
        await env.start()
    fake_modal.boxes[0].fail_terminate = True
    with caplog.at_level(logging.ERROR):
        assert await ModalEnvironment.stop_job_sandboxes("job-9") == 2
    assert "job-9" in caplog.text
    assert [box.terminated for box in fake_modal.boxes] == [False, True, True]


async def test_upload_writes_bytes_and_download_keeps_crlf(fake_modal: FakeModal) -> None:
    """Upload UTF-8 bytes and read them back without newline changes."""
    async with ModalEnvironment(_SPEC) as env:
        await env.upload_files({"/a/b.txt": "x\r\nבטא", "/c.txt": ""})
        assert _box(fake_modal).files == {"/a/b.txt": "x\r\nבטא".encode(), "/c.txt": b""}
        assert await env.download_file("/a/b.txt") == "x\r\nבטא"


async def test_exec_uses_the_task_output_sink(fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send output to the task sink when exec gets no on_output."""
    chunks: list[tuple[str, str]] = []
    monkeypatch.setattr(
        modal_provider, "current_output_sink", lambda: lambda stream, text: chunks.append((stream, text))
    )
    async with ModalEnvironment(_SPEC) as env:
        await env.exec("echo sink")
    assert chunks == [("stdout", "sink\n")]


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a fatal error that names the extra when the SDK is missing."""
    monkeypatch.setattr(modal_provider, "Sandbox", None)
    with pytest.raises(SandboxFatalError, match="modal"):
        ModalEnvironment.preflight()


def test_preflight_needs_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Raise a fatal error with no token in the env or in ~/.modal.toml, and pass with either."""
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(SandboxFatalError, match="MODAL_TOKEN_ID"):
        ModalEnvironment.preflight()
    monkeypatch.setenv("MODAL_TOKEN_ID", "id")
    with pytest.raises(SandboxFatalError):
        ModalEnvironment.preflight()
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret")
    ModalEnvironment.preflight()
    monkeypatch.delenv("MODAL_TOKEN_ID")
    (tmp_path / ".modal.toml").write_text("")
    ModalEnvironment.preflight()


def test_factory_creates_the_modal_provider_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a ModalEnvironment through the factory in dev, and run its preflight."""
    monkeypatch.setattr(factory.settings, "app_environment", "development")
    monkeypatch.setenv("MODAL_TOKEN_ID", "id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret")
    environment = factory.create_environment("modal", _SPEC)
    assert isinstance(environment, ModalEnvironment)
    assert isinstance(environment, SandboxEnvironment)
    factory.preflight("modal")


def test_factory_refuses_the_modal_provider_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the dev-only Modal provider in production, for create and preflight."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        factory.create_environment("modal", _SPEC)
    with pytest.raises(ProductionNotAllowedError):
        factory.preflight("modal")


def test_capabilities_allow_every_network_mode_and_gpus() -> None:
    """Report dynamic egress for all modes, GPUs, billing, and no protection."""
    caps = ModalEnvironment.capabilities
    assert caps.name == "modal"
    assert caps.dynamic_network_policy
    assert caps.gpus
    assert caps.gpu_types
    assert caps.billable
    assert not caps.protected
    assert len(caps.network_modes) == 3


class FakeUsage:
    """Record the calls a protected box makes on its usage reservation."""

    def __init__(self, budget: Any, request: dict[str, Any], *, operation_key: str) -> None:
        """Record the reservation.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's logical identity.
        """
        self.budget = budget
        self.request = request
        self.operation_key = operation_key
        self.box = f"box-{operation_key}"
        self.calls: list[str] = []
        self.confirmed: Any = None
        self.sent_at: float | None = None
        self.sandbox_id: str | None = None
        self.absent: bool | None = None
        budget.reservations.append(self)

    def sent(self) -> None:
        """Record that the create was sent."""
        self.calls.append("sent")
        self.sent_at = 1.0

    def confirm_created(self, sandbox: Any) -> None:
        """Record the created sandbox.

        Args:
            sandbox: The fake sandbox.
        """
        self.calls.append("confirm")
        self.confirmed = sandbox
        self.sandbox_id = sandbox.object_id

    def stopped(self) -> None:
        """Record a confirmed terminate."""
        self.calls.append("stopped")

    def settle(self) -> None:
        """Record the settle."""
        self.calls.append("settle")

    def fail(self, absent: bool = False) -> None:
        """Record a failure with no sandbox.

        Args:
            absent: True when a lookup by the box tag found no sandbox.
        """
        self.calls.append("fail")
        self.absent = absent


def _protected(spec: SandboxSpec | None = None) -> tuple[ModalEnvironment, SimpleNamespace]:
    """Build a protected box with a fake budget and a fake usage factory.

    Args:
        spec: The spec, or a default spec with an operation key.

    Returns:
        The box and the fake budget whose ``reservations`` collects each reservation.
    """
    budget = SimpleNamespace(reservations=[])
    spec = spec or SandboxSpec(
        lifetime_seconds=60, operation_key="op-1", network_disabled=True, resources=ResourceRequest(cpus=2)
    )
    return ModalEnvironment(spec, budget=budget, usage_factory=FakeUsage), budget


def test_protected_box_needs_an_operation_key() -> None:
    """Refuse a protected box with no operation key."""
    with pytest.raises(SandboxSpecError, match="operation key"):
        ModalEnvironment(_SPEC, budget=SimpleNamespace(), usage_factory=FakeUsage)


def test_protected_box_refuses_a_gpu() -> None:
    """Refuse a protected GPU box, because no sandbox GPU rate is published."""
    spec = SandboxSpec(
        lifetime_seconds=60, operation_key="op-1", network_disabled=True, resources=ResourceRequest(gpu=1)
    )
    with pytest.raises(SandboxSpecError, match="GPU"):
        ModalEnvironment(spec, budget=SimpleNamespace(), usage_factory=FakeUsage)


async def test_protected_box_reserves_before_create_and_settles_after_stop(fake_modal: FakeModal) -> None:
    """Reserve the lifetime, pin the billed size, confirm the sandbox, then settle after the terminate."""
    env, budget = _protected()
    await env.start()
    (usage,) = budget.reservations
    assert usage.operation_key == "op-1"
    assert usage.request == {
        "image": modal_provider.DEFAULT_IMAGE,
        "lifetime_ms": 60_000,
        "cpu_cores": 2,
        "memory_mib": 4096,
        "gpu": False,
    }
    box = _box(fake_modal)
    assert box.kwargs["timeout"] == 60
    assert box.kwargs["outbound_domain_allowlist"] == []
    assert box.kwargs["outbound_cidr_allowlist"] == []
    assert box.kwargs["cpu"] == (2, 2)
    assert box.kwargs["memory"] == (4096, 4096)
    assert box.tags[BOX_KEY] == usage.box
    assert fake_modal.builds == 1
    assert usage.confirmed is box
    assert usage.calls == ["sent", "confirm"]
    await env.stop()
    assert box.terminated
    assert usage.calls == ["sent", "confirm", "stopped", "settle"]


@pytest.mark.parametrize(
    "spec",
    [
        SandboxSpec(lifetime_seconds=60, operation_key="op-1"),
        SandboxSpec(lifetime_seconds=60, operation_key="op-1", allowed_hosts=("pypi.org",)),
    ],
)
def test_protected_box_refuses_egress_at_construction(spec: SandboxSpec) -> None:
    """Refuse open or allowlisted egress, because Modal neither caps nor reports it.

    Args:
        spec: A protected spec whose network is not disabled.
    """
    with pytest.raises(SandboxSpecError, match="egress"):
        ModalEnvironment(spec, budget=SimpleNamespace(), usage_factory=FakeUsage)


@pytest.mark.parametrize("policy", [NetworkPolicy(), NetworkPolicy(allowed_hosts=("pypi.org",))])
async def test_protected_box_refuses_to_open_egress(fake_modal: FakeModal, policy: NetworkPolicy) -> None:
    """Refuse a policy that opens egress on a running protected box, and keep it closed.

    Args:
        fake_modal: The fake SDK.
        policy: A policy that opens egress.
    """
    env, budget = _protected()
    await env.start()
    with pytest.raises(SandboxSpecError, match="egress"):
        await env.set_network_policy(policy)
    assert _box(fake_modal).policies == []
    await env.disable_network()
    assert _box(fake_modal).policies == [{"outbound_domain_allowlist": [], "outbound_cidr_allowlist": []}]
    assert budget.reservations[0].calls == ["sent", "confirm"]
    await env.stop()


async def test_protected_box_fails_usage_when_create_fails(fake_modal: FakeModal) -> None:
    """Hand a failed create to the reservation, which keeps or releases the hold."""
    fake_modal.create_error = RuntimeError("quota")
    env, budget = _protected()
    with pytest.raises(RuntimeError, match="quota"):
        await env.start()
    assert budget.reservations[0].calls == ["sent", "fail"]
    assert budget.reservations[0].absent is True
    assert env.usage is None


class FakeInvalidError(Exception):
    """Stand in for modal.exception.InvalidError."""


class FakeResourceExhaustedError(Exception):
    """Stand in for modal.exception.ResourceExhaustedError, which the V1 create raises after its box exists."""


_CREATE_ERRORS = [
    FakeInvalidError("bad"),
    FakeResourceExhaustedError("Insufficient capacity to create sandbox."),
    RuntimeError("client timeout"),
]


@pytest.mark.parametrize("error", _CREATE_ERRORS)
async def test_protected_box_releases_only_after_two_lookups_find_no_box(
    fake_modal: FakeModal, error: Exception
) -> None:
    """Report the box as absent only when both lookups by the box tag found no sandbox, whatever the error type.

    Args:
        fake_modal: The fake SDK.
        error: The create error.
    """
    fake_modal.create_error = error
    env, budget = _protected()
    with pytest.raises(type(error)):
        await env.start()
    (usage,) = budget.reservations
    assert usage.calls == ["sent", "fail"]
    assert usage.absent is True
    assert fake_modal.list_calls == 2


@pytest.mark.parametrize("error", _CREATE_ERRORS)
async def test_protected_box_settles_a_box_made_before_the_create_error(
    fake_modal: FakeModal, error: Exception
) -> None:
    """Find, terminate and settle a box that a failed create made, whatever the error type.

    Args:
        fake_modal: The fake SDK.
        error: The error the create raises after the sandbox exists.
    """
    fake_modal.create_error_after_box = error
    env, budget = _protected()
    with pytest.raises(type(error)):
        await env.start()
    (usage,) = budget.reservations
    assert _box(fake_modal).terminated
    assert usage.confirmed is _box(fake_modal)
    assert usage.calls == ["sent", "confirm", "stopped", "settle"]
    assert env.usage is None


async def test_protected_box_keeps_the_hold_when_the_lookup_fails(fake_modal: FakeModal) -> None:
    """Keep the hold pending when the lookup by the box tag fails, because the box may exist."""
    fake_modal.create_error = FakeInvalidError("bad")
    fake_modal.list_error = RuntimeError("list down")
    env, budget = _protected()
    with pytest.raises(FakeInvalidError):
        await env.start()
    (usage,) = budget.reservations
    assert usage.calls == ["sent", "fail"]
    assert usage.absent is False


async def test_protected_box_build_failure_is_an_unsent_create(fake_modal: FakeModal) -> None:
    """Fail the usage before ``sent`` when the image build fails, so the hold is released."""
    fake_modal.build_error = RuntimeError("build")
    env, budget = _protected()
    with pytest.raises(RuntimeError, match="build"):
        await env.start()
    (usage,) = budget.reservations
    assert usage.calls == ["fail"]
    assert usage.absent is False
    assert fake_modal.create_calls == 0


async def test_protected_box_cannot_start_again_after_stop(fake_modal: FakeModal) -> None:
    """Refuse a restart of a stopped protected box before any reservation."""
    env, budget = _protected()
    await env.start()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    assert len(budget.reservations) == 1
    assert len(fake_modal.boxes) == 1


async def test_protected_box_settles_when_terminate_fails(fake_modal: FakeModal) -> None:
    """Settle at the funded lifetime when Modal does not confirm the terminate."""
    env, budget = _protected()
    await env.start()
    _box(fake_modal).fail_terminate = True
    with pytest.raises(RuntimeError, match="terminate"):
        await env.stop()
    assert budget.reservations[0].calls == ["sent", "confirm", "settle"]


async def test_protected_box_settles_when_start_is_cancelled(fake_modal: FakeModal) -> None:
    """Terminate and settle the sandbox that a cancelled start created."""
    fake_modal.create_gate = asyncio.Event()
    env, budget = _protected()
    task = asyncio.create_task(env.start())
    await fake_modal.create_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    fake_modal.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _box(fake_modal).terminated
    assert budget.reservations[0].calls == ["sent", "confirm", "stopped", "settle"]


class RefusingUsage(FakeUsage):
    """Refuse every created sandbox, like a create that returns no sandbox id."""

    def confirm_created(self, sandbox: Any) -> None:
        """Refuse the sandbox.

        Args:
            sandbox: The fake sandbox.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("no id")


async def test_protected_box_settles_when_confirm_fails(fake_modal: FakeModal) -> None:
    """Terminate and settle a sandbox whose confirm fails, then pass the error on."""
    budget = SimpleNamespace(reservations=[])
    spec = SandboxSpec(lifetime_seconds=60, operation_key="op-1", network_disabled=True)
    env = ModalEnvironment(spec, budget=budget, usage_factory=RefusingUsage)
    with pytest.raises(RuntimeError, match="no id"):
        await env.start()
    assert _box(fake_modal).terminated
    assert budget.reservations[0].calls == ["sent", "stopped", "settle", "fail"]


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


class BlockingUsage(FakeUsage):
    """Block the reservation in its thread until the test opens the gate."""

    entered = threading.Event()
    gate = threading.Event()

    def __init__(self, budget: Any, request: dict[str, Any], *, operation_key: str) -> None:
        """Wait for the gate, then record the reservation.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's logical identity.
        """
        BlockingUsage.entered.set()
        assert BlockingUsage.gate.wait(5)
        super().__init__(budget, request, operation_key=operation_key)


@pytest.mark.parametrize("late", [False, True])
async def test_cancel_during_reserve_releases_the_hold(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch, late: bool
) -> None:
    """Release a reservation that a cancelled start made, also one that ends after the grace time.

    Args:
        fake_modal: The fake SDK.
        monkeypatch: Shortens the grace time and resets the gate.
        late: True when the reservation ends after the grace time.
    """
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05 if late else 5)
    monkeypatch.setattr(BlockingUsage, "entered", threading.Event())
    monkeypatch.setattr(BlockingUsage, "gate", threading.Event())
    budget = SimpleNamespace(reservations=[])
    spec = SandboxSpec(lifetime_seconds=60, operation_key="op-1", network_disabled=True)
    env = ModalEnvironment(spec, budget=budget, usage_factory=BlockingUsage)
    task = asyncio.create_task(env.start())
    await _until(BlockingUsage.entered.is_set)
    task.cancel()
    if late:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert budget.reservations == []
        BlockingUsage.gate.set()
    else:
        await asyncio.sleep(0.05)
        BlockingUsage.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    await _until(lambda: bool(budget.reservations) and budget.reservations[0].calls == ["fail"])
    assert budget.reservations[0].absent is False
    assert fake_modal.create_calls == 0
    assert env.usage is None


async def test_late_create_after_a_cancelled_start_settles_its_box(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminate and settle the box of a create that ends after the cancel passed on."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_modal.create_gate = asyncio.Event()
    env, budget = _protected()
    task = asyncio.create_task(env.start())
    await fake_modal.create_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    (usage,) = budget.reservations
    assert usage.calls == ["sent"]
    fake_modal.create_gate.set()
    await _until(lambda: usage.calls == ["sent", "confirm", "stopped", "settle"])
    assert _box(fake_modal).terminated
    await _until(lambda: not modal_provider._LATE_TERMINATES)


async def test_late_create_error_after_a_cancelled_start_looks_up_the_box(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Look up the box by its tag when a create fails after the cancel passed on, then fail the usage."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_modal.create_gate = asyncio.Event()
    env, budget = _protected()
    task = asyncio.create_task(env.start())
    await fake_modal.create_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    (usage,) = budget.reservations
    fake_modal.create_error = FakeInvalidError("bad")
    fake_modal.create_gate.set()
    await _until(lambda: usage.calls == ["sent", "fail"])
    assert usage.absent is True
    await _until(lambda: not modal_provider._LATE_TERMINATES)


async def test_confirm_failure_settles_the_real_reservation(fake_modal: FakeModal, tmp_path: Path) -> None:
    """Settle the real reservation of a box whose confirm failed before it had an id.

    Args:
        fake_modal: The fake SDK.
        tmp_path: Per-test directory for the SQLite ledger.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'modal.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    runtime = BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)
    fake_modal.no_object_id = True
    spec = SandboxSpec(lifetime_seconds=60, operation_key="op-1", network_disabled=True)
    env = ModalEnvironment(spec, budget=runtime, usage_factory=ModalUsageReservation)
    try:
        with pytest.raises(UsagePendingError, match="sandbox id"):
            await env.start()
        assert _box(fake_modal).terminated
        with Session(engine) as session:
            row = session.scalars(select(ExecutionOperationModel)).one()
        assert row.state == "settled"
        assert runtime._pinned == {}
    finally:
        engine.dispose()


@pytest.mark.parametrize("error", _CREATE_ERRORS)
async def test_box_that_appears_after_a_create_error_is_settled(fake_modal: FakeModal, error: Exception) -> None:
    """Terminate and settle, not release, a box that only the second lookup finds, whatever the error type.

    Args:
        fake_modal: The fake SDK.
        error: The create error.
    """
    fake_modal.create_error = error
    env, budget = _protected()

    def make_box_late() -> None:
        """Make the box of the failed create once the first lookup ran."""
        if fake_modal.list_calls == 2:
            (usage,) = budget.reservations
            app = SimpleNamespace(app_id=f"ap-{env._app_name}")
            fake_modal.boxes.append(FakeSandbox(fake_modal, ("sleep",), {"app": app, "tags": {BOX_KEY: usage.box}}))

    fake_modal.list_hook = make_box_late
    with pytest.raises(type(error)):
        await env.start()
    (usage,) = budget.reservations
    assert fake_modal.list_calls == 2
    assert _box(fake_modal).terminated
    assert usage.calls == ["sent", "confirm", "stopped", "settle"]


async def test_failed_create_does_not_wait_for_the_second_lookup(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise the create error after the cancel grace time and finish the delayed lookup in the background."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05)
    monkeypatch.setattr(modal_provider, "_RECHECK_DELAY_SEC", 0.5)
    fake_modal.create_error = FakeInvalidError("bad")
    env, budget = _protected()
    with pytest.raises(FakeInvalidError):
        await asyncio.wait_for(env.start(), 0.4)
    (usage,) = budget.reservations
    assert usage.calls == ["sent"]
    await _until(lambda: usage.calls == ["sent", "fail"])
    assert fake_modal.list_calls == 2
    await _until(lambda: not modal_provider._LATE_TERMINATES)


async def test_cancel_during_the_box_lookup_still_fails_the_usage(
    fake_modal: FakeModal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finish the cleanup of a failed create in the background when cancels arrive during the lookup."""
    monkeypatch.setattr(modal_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_modal.create_error = FakeInvalidError("bad")
    fake_modal.list_gate = asyncio.Event()
    env, budget = _protected()
    task = asyncio.create_task(env.start())
    await fake_modal.list_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(task, 2)
    assert isinstance(raised.value.__cause__, FakeInvalidError)
    (usage,) = budget.reservations
    assert usage.calls == ["sent"]
    fake_modal.list_gate.set()
    await _until(lambda: usage.calls == ["sent", "fail"])
    assert usage.absent is True
    await _until(lambda: not modal_provider._LATE_TERMINATES)

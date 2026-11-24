"""Tests for the Daytona sandbox provider, with a fake Daytona SDK.

The tests never import the real SDK and never call Daytona. A module-level
fixture replaces the SDK names in ``core.sandbox.providers.daytona`` with
small in-memory fakes.
"""

from __future__ import annotations

import asyncio
import itertools
import shlex
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.daytona_usage import DaytonaUsageReservation
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.sandbox import JOB_TAG, NetworkPolicy, ResourceRequest, SandboxSpec, factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.errors import (
    SandboxError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.factory import ProductionNotAllowedError
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import daytona
from core.sandbox.providers.daytona import DaytonaEnvironment
from core.sandbox.resources import ResourceMode
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.sandbox.timeouts import PhaseTimeouts
from core.storage.models import Base, BillingCustomerModel

_SPEC = SandboxSpec(lifetime_seconds=60)


class FakeDaytonaError(Exception):
    """Stand in for ``daytona.DaytonaError``."""


class FakeNotFoundError(FakeDaytonaError):
    """Stand in for ``daytona.DaytonaNotFoundError``."""


class FakeAuthenticationError(FakeDaytonaError):
    """Stand in for ``daytona.DaytonaAuthenticationError``."""


class FakeAuthorizationError(FakeDaytonaError):
    """Stand in for ``daytona.DaytonaAuthorizationError``."""


class FakeForbiddenError(FakeDaytonaError):
    """Stand in for ``daytona.DaytonaForbiddenError``."""


class FakeRateLimitError(FakeDaytonaError):
    """Stand in for ``daytona.DaytonaRateLimitError``."""


class FakeTimeoutError(FakeDaytonaError):
    """Stand in for ``daytona.DaytonaTimeoutError``, which is not a ``TimeoutError``."""

    def __init__(self, message: str, sandbox_id: str | None = None) -> None:
        """Store the id of the box that the timed-out create made.

        Args:
            message: The error message.
            sandbox_id: The box id, or ``None``.
        """
        super().__init__(message)
        self.sandbox_id = sandbox_id


@dataclass
class FakeParams:
    """Record the create parameters, as ``CreateSandboxFromImageParams`` does."""

    image: str
    resources: Any = None
    env_vars: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    ephemeral: bool | None = None
    auto_stop_interval: int | None = None
    ttl_minutes: int | None = None
    network_block_all: bool | None = None
    domain_allow_list: str | None = None
    network_allow_list: str | None = None


def _fake_resources(**sizes: int) -> dict[str, int]:
    """Record the resource sizes, as ``Resources`` does.

    Args:
        **sizes: The sizes.

    Returns:
        The sizes.
    """
    return dict(sizes)


def _fake_query(*, labels: dict[str, str]) -> SimpleNamespace:
    """Record the list filter, as ``ListSandboxesQuery`` does.

    Args:
        labels: The label filter.

    Returns:
        The query.
    """
    return SimpleNamespace(labels=labels)


def _fake_request(*, command: str, run_async: bool) -> SimpleNamespace:
    """Record a session request, as ``SessionExecuteRequest`` does.

    Args:
        command: The command line.
        run_async: The async flag.

    Returns:
        The request.
    """
    return SimpleNamespace(command=command, run_async=run_async)


class FakeProcess:
    """Run session commands from a small command set inside one fake box."""

    def __init__(self, box: FakeBox) -> None:
        """Bind the process API to its box.

        Args:
            box: The fake box.
        """
        self._box = box
        self.sessions: set[str] = set()
        self.lines: list[str] = []

    async def create_session(self, session_id: str) -> None:
        """Open a session.

        Args:
            session_id: The session id.
        """
        self._box.check_alive()
        self.sessions.add(session_id)

    async def execute_session_command(self, session_id: str, req: Any, timeout: int | None = None) -> Any:  # noqa: ASYNC109 - the SDK signature
        """Interpret one wrapped command line.

        Args:
            session_id: The session id.
            req: The request.
            timeout: The SDK timeout.

        Returns:
            A response with exit code, stdout and stderr.
        """
        self._box.check_alive()
        assert session_id in self.sessions
        assert req.run_async is False
        self.lines.append(req.command)
        argv = shlex.split(req.command)
        env = dict(self._box.env)
        if argv[:1] == ["cd"]:
            assert argv[2] == "&&"
            argv = argv[3:]
        if argv[:1] == ["timeout"]:
            argv = argv[2:]
        if argv[:1] == ["env"]:
            argv = argv[1:]
            while "=" in argv[0]:
                key, _, value = argv.pop(0).partition("=")
                env[key] = value
        assert argv[:2] == ["bash", "-c"]
        code, out, err = await self._run(argv[2], env)
        return SimpleNamespace(exit_code=code, stdout=out, stderr=err, output=out + err)

    async def _run(self, command: str, env: dict[str, str]) -> tuple[int, str, str]:
        """Interpret one command of the fake command set.

        Args:
            command: The inner shell command.
            env: The command environment.

        Returns:
            The exit code, stdout and stderr.
        """
        argv = shlex.split(command)
        name, args = argv[0], argv[1:]
        files = self._box.files
        if name == "true":
            return 0, "", ""
        if name == "false":
            return 1, "", ""
        if name == "exit":
            return int(args[0]), "", ""
        if name == "echo":
            return 0, " ".join(args) + "\n", ""
        if name == "sleep":
            await asyncio.sleep(float(args[0]))
            return 0, "", ""
        if name == "mkdir":
            if self._box.fail_mkdir:
                return 1, "", "mkdir: denied\n"
            return 0, "", ""
        if name == "cat":
            missing = [path for path in args if path not in files]
            if missing:
                return 1, "", f"cat: {missing[0]}: No such file or directory\n"
            return 0, "".join(files[path].decode("utf-8") for path in args), ""
        if name == "printenv":
            value = env.get(args[0])
            return (1, "", "") if value is None else (0, value + "\n", "")
        return 127, "", f"{name}: command not found\n"


class FakeFs:
    """Store box files as bytes."""

    def __init__(self, box: FakeBox) -> None:
        """Bind the file API to its box.

        Args:
            box: The fake box.
        """
        self._box = box

    async def upload_file(self, file: bytes, remote_path: str) -> None:
        """Store a file.

        Args:
            file: The content; must be bytes.
            remote_path: The box path.
        """
        self._box.check_alive()
        assert isinstance(file, bytes)
        self._box.files[remote_path] = file

    async def download_file(self, remote_path: str) -> bytes:
        """Return a stored file.

        Args:
            remote_path: The box path.

        Returns:
            The content.

        Raises:
            FakeNotFoundError: The file does not exist.
        """
        self._box.check_alive()
        if remote_path not in self._box.files:
            raise FakeNotFoundError(f"file {remote_path} not found")
        return self._box.files[remote_path]


@dataclass(eq=False)
class FakeBox:
    """Act as one ``AsyncSandbox``."""

    id: str
    labels: dict[str, str]
    env: dict[str, str]
    files: dict[str, bytes] = field(default_factory=dict)
    deleted: bool = False
    fail_delete: Exception | None = None
    fail_mkdir: bool = False
    delete_delay: float = 0.0
    network_updates: list[dict[str, Any]] = field(default_factory=list)
    cpu: int | None = None
    memory: int | None = None
    disk: int | None = None

    def __post_init__(self) -> None:
        """Attach the process and file APIs."""
        self.process = FakeProcess(self)
        self.fs = FakeFs(self)

    def check_alive(self) -> None:
        """Raise when the box was deleted.

        Raises:
            FakeNotFoundError: The box was deleted.
        """
        if self.deleted:
            raise FakeNotFoundError(f"sandbox {self.id} not found")

    async def delete(self, timeout: float | None = 60) -> None:  # noqa: ASYNC109 - the SDK signature
        """Delete the box.

        Args:
            timeout: The SDK timeout.

        Raises:
            Exception: ``fail_delete`` is set.
        """
        if self.delete_delay:
            await asyncio.sleep(self.delete_delay)
        if self.fail_delete is not None:
            raise self.fail_delete
        self.check_alive()
        self.deleted = True

    async def update_network_settings(self, **kwargs: Any) -> None:
        """Record a network change.

        Args:
            **kwargs: The network fields.
        """
        self.check_alive()
        self.network_updates.append(kwargs)


class FakeClient:
    """Act as ``AsyncDaytona`` over one shared box registry."""

    boxes: ClassVar[list[FakeBox]] = []
    created: ClassVar[list[FakeParams]] = []
    clients: ClassVar[list[FakeClient]] = []
    create_error: ClassVar[Exception | None] = None
    create_delay: ClassVar[float] = 0.0
    create_times_out: ClassVar[bool] = False
    fail_after_box: ClassVar[Exception | None] = None
    list_error: ClassVar[Exception | None] = None
    list_hidden: ClassVar[int] = 0
    list_calls: ClassVar[int] = 0
    create_called: ClassVar[asyncio.Event] = asyncio.Event()
    _ids: ClassVar[Iterator[int]] = itertools.count()

    def __init__(self) -> None:
        """Open a fake client."""
        self.closed = False
        type(self).clients.append(self)

    async def create(self, params: FakeParams, timeout: float = 60) -> FakeBox:  # noqa: ASYNC109 - the SDK signature
        """Create a box.

        Args:
            params: The create parameters.
            timeout: The SDK timeout.

        Returns:
            The new box.

        Raises:
            Exception: ``create_error`` is set.
            FakeTimeoutError: ``create_times_out`` is set; the box exists.
        """
        cls = type(self)
        cls.create_called.set()
        if cls.create_delay:
            await asyncio.sleep(cls.create_delay)
        if cls.create_error is not None:
            raise cls.create_error
        cls.created.append(params)
        sizes = params.resources or {}
        box = FakeBox(
            id=f"box-{next(cls._ids)}",
            labels=dict(params.labels or {}),
            env=dict(params.env_vars or {}),
            cpu=sizes.get("cpu"),
            memory=sizes.get("memory"),
            disk=sizes.get("disk"),
        )
        cls.boxes.append(box)
        if cls.fail_after_box is not None:
            raise cls.fail_after_box
        if cls.create_times_out:
            raise FakeTimeoutError(f"Sandbox {box.id} failed to become ready", sandbox_id=box.id)
        return box

    async def get(self, sandbox_id_or_name: str) -> FakeBox:
        """Find a live box by id.

        Args:
            sandbox_id_or_name: The box id.

        Returns:
            The box.

        Raises:
            FakeNotFoundError: No live box has the id.
        """
        for box in type(self).boxes:
            if box.id == sandbox_id_or_name and not box.deleted:
                return box
        raise FakeNotFoundError(f"sandbox {sandbox_id_or_name} not found")

    async def list(self, query: Any) -> Any:
        """Yield the live boxes whose labels match the query.

        The first ``list_hidden`` calls yield nothing, as a lagging list does.

        Args:
            query: The list query.

        Yields:
            Each matching box.

        Raises:
            Exception: ``list_error`` is set.
        """
        cls = type(self)
        cls.list_calls += 1
        if cls.list_error is not None:
            raise cls.list_error
        if cls.list_calls <= cls.list_hidden:
            return
        for box in list(type(self).boxes):
            if not box.deleted and all(box.labels.get(k) == v for k, v in query.labels.items()):
                yield box

    async def close(self) -> None:
        """Close the client."""
        self.closed = True


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    """Replace the Daytona SDK names in the provider module with fakes.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fake client class, whose class fields hold the shared state.
    """
    monkeypatch.setattr(FakeClient, "boxes", [])
    monkeypatch.setattr(FakeClient, "created", [])
    monkeypatch.setattr(FakeClient, "clients", [])
    monkeypatch.setattr(FakeClient, "create_error", None)
    monkeypatch.setattr(FakeClient, "create_delay", 0.0)
    monkeypatch.setattr(FakeClient, "create_times_out", False)
    monkeypatch.setattr(FakeClient, "fail_after_box", None)
    monkeypatch.setattr(FakeClient, "list_error", None)
    monkeypatch.setattr(FakeClient, "list_hidden", 0)
    monkeypatch.setattr(FakeClient, "list_calls", 0)
    monkeypatch.setattr(daytona, "LOOKUP_RETRY_SEC", 0.0)
    monkeypatch.setattr(FakeClient, "create_called", asyncio.Event())
    names = {
        "AsyncDaytona": FakeClient,
        "CreateSandboxFromImageParams": FakeParams,
        "DaytonaError": FakeDaytonaError,
        "DaytonaNotFoundError": FakeNotFoundError,
        "DaytonaAuthenticationError": FakeAuthenticationError,
        "DaytonaAuthorizationError": FakeAuthorizationError,
        "DaytonaForbiddenError": FakeForbiddenError,
        "DaytonaRateLimitError": FakeRateLimitError,
        "DaytonaTimeoutError": FakeTimeoutError,
        "ListSandboxesQuery": _fake_query,
        "Resources": _fake_resources,
        "SessionExecuteRequest": _fake_request,
    }
    for name, value in names.items():
        monkeypatch.setattr(daytona, name, value)
    return FakeClient


class TestDaytonaEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the Daytona provider and the fake SDK."""

    provider_cls = DaytonaEnvironment


async def test_start_creates_an_ephemeral_box_from_the_spec(fake_sdk: type[FakeClient]) -> None:
    """Send the image, sizes, env, labels, TTL and network policy on create."""
    spec = SandboxSpec(
        lifetime_seconds=125,
        resources=ResourceRequest(cpus=2, memory_mb=1500, disk_mb=4096),
        env={"A": "1"},
        tags={JOB_TAG: "job-1"},
        image="ubuntu:24.04",
        allowed_hosts=("pypi.org", "10.0.0.0/8"),
    )
    async with DaytonaEnvironment(spec):
        pass
    (params,) = fake_sdk.created
    assert params.image == "ubuntu:24.04"
    assert params.resources == {"cpu": 2, "memory": 2, "disk": 4}
    assert params.env_vars == {"A": "1"}
    assert params.labels == {JOB_TAG: "job-1"}
    assert params.ephemeral is True
    assert params.auto_stop_interval == 0
    assert params.ttl_minutes == 3
    assert params.network_block_all is False
    assert params.domain_allow_list == "pypi.org"
    assert params.network_allow_list == "10.0.0.0/8"
    assert fake_sdk.boxes[0].deleted
    assert fake_sdk.clients[0].closed


async def test_start_uses_the_default_image_and_no_sizes_in_ignore_mode(fake_sdk: type[FakeClient]) -> None:
    """Use the default image, and send no sizes when the spec ignores them."""
    spec = SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(mode=ResourceMode.IGNORE), network_disabled=True)
    async with DaytonaEnvironment(spec):
        pass
    (params,) = fake_sdk.created
    assert params.image == daytona.DEFAULT_IMAGE == "python:3.12.15-slim"
    assert params.resources is None
    assert params.network_block_all is True
    assert params.domain_allow_list is None


async def test_start_twice_creates_one_box(fake_sdk: type[FakeClient]) -> None:
    """Do nothing on a second start."""
    env = DaytonaEnvironment(_SPEC)
    try:
        await env.start()
        await env.start()
    finally:
        await env.stop()
    assert len(fake_sdk.boxes) == 1


async def test_cancel_during_create_still_deletes_the_box(fake_sdk: type[FakeClient]) -> None:
    """Keep a box that Daytona made during a cancelled start, so stop deletes it."""
    fake_sdk.create_delay = 0.05
    env = DaytonaEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(fake_sdk.boxes) == 1
    await env.stop()
    assert fake_sdk.boxes[0].deleted


async def test_create_timeout_raises_start_timeout_and_deletes_the_box(fake_sdk: type[FakeClient]) -> None:
    """Map the SDK create timeout to StartTimeoutError, and delete the box it made."""
    fake_sdk.create_times_out = True
    with pytest.raises(StartTimeoutError) as caught:
        async with DaytonaEnvironment(_SPEC):
            pass
    assert caught.value.seconds == daytona.CREATE_TIMEOUT_SEC
    assert isinstance(caught.value.__cause__, FakeTimeoutError)
    (box,) = fake_sdk.boxes
    assert box.deleted
    assert fake_sdk.clients[0].closed


async def test_create_timeout_keeps_the_timeout_when_the_box_lookup_fails(
    fake_sdk: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise StartTimeoutError, not the lookup error, when Daytona cannot find the box."""
    fake_sdk.create_times_out = True

    async def _lost(self: FakeClient, sandbox_id_or_name: str) -> FakeBox:
        """Fail every lookup.

        Args:
            sandbox_id_or_name: The box id.

        Raises:
            FakeDaytonaError: Always.
        """
        raise FakeDaytonaError("lookup failed")

    monkeypatch.setattr(FakeClient, "get", _lost)
    with pytest.raises(StartTimeoutError):
        async with DaytonaEnvironment(_SPEC):
            pass
    assert fake_sdk.clients[0].closed


async def test_second_cancel_during_create_still_deletes_the_box(fake_sdk: type[FakeClient]) -> None:
    """Keep waiting for the create through a second cancel, so stop deletes the box."""
    fake_sdk.create_delay = 0.05
    env = DaytonaEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(fake_sdk.boxes) == 1
    await env.stop()
    assert fake_sdk.boxes[0].deleted


async def test_second_cancel_during_stop_still_deletes_the_box(fake_sdk: type[FakeClient]) -> None:
    """Finish the delete and close the client through two cancels during stop."""
    env = DaytonaEnvironment(_SPEC)
    await env.start()
    (box,) = fake_sdk.boxes
    box.delete_delay = 0.05
    task = asyncio.create_task(env.stop())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert box.deleted
    assert fake_sdk.clients[0].closed


async def test_cancel_then_create_fails_keeps_the_cancel(fake_sdk: type[FakeClient]) -> None:
    """Raise CancelledError, not the create error, so asyncio.timeout gives TimeoutError."""
    fake_sdk.create_delay = 0.05
    fake_sdk.create_error = FakeDaytonaError("create 500")
    env = DaytonaEnvironment(_SPEC)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await env.start()
    assert fake_sdk.boxes == []
    await env.stop()
    assert fake_sdk.clients[0].closed


async def test_cancel_then_delete_fails_logs_and_keeps_the_cancel(
    fake_sdk: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """Log the failed delete and raise CancelledError, not the delete error."""
    env = DaytonaEnvironment(_SPEC)
    await env.start()
    (box,) = fake_sdk.boxes
    box.delete_delay = 0.05
    box.fail_delete = FakeDaytonaError("delete 500")
    task = asyncio.create_task(env.stop())
    await asyncio.sleep(0.01)
    task.cancel()
    with caplog.at_level("WARNING", logger=daytona.__name__), pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert "The Daytona delete failed during a cancelled stop." in caplog.text
    assert fake_sdk.clients[0].closed


async def test_stop_ignores_a_box_that_is_already_gone(fake_sdk: type[FakeClient]) -> None:
    """Treat a not-found delete as done and still close the client."""
    env = DaytonaEnvironment(_SPEC)
    await env.start()
    fake_sdk.boxes[0].deleted = True
    await env.stop()
    assert fake_sdk.clients[0].closed


async def test_exec_wraps_cwd_env_and_timeout(fake_sdk: type[FakeClient]) -> None:
    """Quote the cwd, the env and the command, and bound the command in the box."""
    async with DaytonaEnvironment(_SPEC) as env:
        result = await env.exec("printenv X", cwd="/work dir", env={"X": "a b"}, timeout_sec=5.5)
    assert result.stdout == "a b\n"
    (line,) = fake_sdk.boxes[0].process.lines
    assert line == "cd '/work dir' && timeout 6 env 'X=a b' bash -c 'printenv X'"


async def test_exec_keeps_stdout_and_stderr_apart(fake_sdk: type[FakeClient]) -> None:
    """Return stderr separately and pass both streams to the output sink."""
    chunks: list[tuple[str, str]] = []
    async with DaytonaEnvironment(_SPEC) as env:
        result = await env.exec("nope", on_output=lambda stream, text: chunks.append((stream, text)))
    assert result.exit_code == 127
    assert result.stdout == ""
    assert "command not found" in result.stderr
    assert chunks == [("stderr", result.stderr)]


async def test_exec_returns_a_timed_out_result(fake_sdk: type[FakeClient]) -> None:
    """Return exit code 124 and timed_out when the command passes its limit."""
    async with DaytonaEnvironment(_SPEC) as env:
        result = await env.exec("sleep 5", timeout_sec=0.05)
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_exec_uses_the_run_phase_timeout_by_default(fake_sdk: type[FakeClient]) -> None:
    """Apply the run phase timeout when the call gives none."""
    timeouts = PhaseTimeouts(start=1, setup=1, run=0.05, score=1)
    async with DaytonaEnvironment(_SPEC, timeouts=timeouts) as env:
        result = await env.exec("sleep 5")
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_exec_refuses_sudo(fake_sdk: type[FakeClient]) -> None:
    """Reject sudo, which the provider does not support."""
    async with DaytonaEnvironment(_SPEC) as env:
        with pytest.raises(SandboxRejectedError):
            await env.exec("true", sudo=True)


async def test_exec_before_start_raises(fake_sdk: type[FakeClient]) -> None:
    """Refuse a command on a sandbox that is not running."""
    with pytest.raises(RuntimeError):
        await DaytonaEnvironment(_SPEC).exec("true")


async def test_upload_makes_parent_folders_and_sends_bytes(fake_sdk: type[FakeClient]) -> None:
    """Make each parent folder once, then send each file as UTF-8 bytes."""
    async with DaytonaEnvironment(_SPEC) as env:
        await env.upload_files({"/a/b/x.txt": "x", "/a/b/y.txt": "בטא", "/z.txt": "z"})
        box = fake_sdk.boxes[0]
        assert box.files["/a/b/y.txt"] == "בטא".encode()
    assert box.process.lines == ["bash -c 'mkdir -p /a/b'"]


async def test_upload_raises_when_the_folders_fail(fake_sdk: type[FakeClient]) -> None:
    """Raise an infra error when the parent folders cannot be made."""
    async with DaytonaEnvironment(_SPEC) as env:
        fake_sdk.boxes[0].fail_mkdir = True
        with pytest.raises(SandboxInfraError, match="denied"):
            await env.upload_files({"/a/x.txt": "x"})


async def test_network_change_clears_the_stored_lists(fake_sdk: type[FakeClient]) -> None:
    """Send block-all, then an allowlist that clears the CIDR list, then open egress."""
    async with DaytonaEnvironment(_SPEC) as env:
        await env.disable_network()
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("pypi.org",)))
        await env.set_network_policy(NetworkPolicy())
        updates = fake_sdk.boxes[0].network_updates
    assert updates == [
        {"network_block_all": True},
        {"network_block_all": False, "domain_allow_list": "pypi.org", "network_allow_list": ""},
        {"network_block_all": False, "domain_allow_list": "", "network_allow_list": ""},
    ]


async def test_stop_job_sandboxes_counts_and_survives_failures(
    fake_sdk: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """Delete tagged boxes, log a failed delete, skip a gone box and close the client."""
    envs = [DaytonaEnvironment(SandboxSpec(lifetime_seconds=60, tags={JOB_TAG: "j"})) for _ in range(3)]
    for env in envs:
        await env.start()
    try:
        fake_sdk.boxes[1].fail_delete = FakeDaytonaError("boom")
        fake_sdk.boxes[2].fail_delete = FakeNotFoundError("gone")
        assert await DaytonaEnvironment.stop_job_sandboxes("j") == 1
        assert "did not delete" in caplog.text
        assert fake_sdk.clients[-1].closed
    finally:
        for box in fake_sdk.boxes:
            box.fail_delete = None
        for env in envs:
            await env.stop()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeAuthenticationError("bad key"), SandboxFatalError),
        (FakeAuthorizationError("no access"), SandboxFatalError),
        (FakeForbiddenError("forbidden"), SandboxFatalError),
        (FakeRateLimitError("slow down"), SandboxTransientError),
        (FakeDaytonaError("Total CPU limit exceeded"), SandboxTransientError),
        (FakeDaytonaError("no capacity in region"), SandboxTransientError),
        (FakeDaytonaError("BUILD_FAILED: bad image"), SandboxRejectedError),
        (FakeDaytonaError("something else"), SandboxInfraError),
    ],
)
async def test_create_errors_map_to_groups_without_retry(
    fake_sdk: type[FakeClient], error: Exception, expected: type[Exception]
) -> None:
    """Map each SDK error to one sandbox error group and call create once."""
    fake_sdk.create_error = error
    env = DaytonaEnvironment(_SPEC)
    with pytest.raises(expected) as raised:
        await env.start()
    assert raised.value.__cause__ is error
    await env.stop()
    assert fake_sdk.clients[0].closed
    assert fake_sdk.created == []


async def test_other_errors_pass_through(fake_sdk: type[FakeClient]) -> None:
    """Leave an error that is not a Daytona SDK error unchanged."""
    fake_sdk.create_error = ValueError("plain")
    env = DaytonaEnvironment(_SPEC)
    with pytest.raises(ValueError, match="plain"):
        await env.start()
    await env.stop()


def test_missing_sdk_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to build or preflight the provider without the daytona extra."""
    monkeypatch.setattr(daytona, "AsyncDaytona", None)
    with pytest.raises(SandboxFatalError, match="daytona` extra"):
        DaytonaEnvironment(_SPEC)
    with pytest.raises(SandboxFatalError, match="daytona` extra"):
        DaytonaEnvironment.preflight()


@pytest.mark.parametrize(
    ("env", "ok"),
    [
        ({}, False),
        ({"DAYTONA_API_KEY": "test-key"}, True),
        ({"DAYTONA_JWT_TOKEN": "test-jwt"}, False),
        ({"DAYTONA_JWT_TOKEN": "test-jwt", "DAYTONA_ORGANIZATION_ID": "org"}, True),
    ],
)
def test_preflight_checks_credentials(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], ok: bool) -> None:
    """Pass with an API key, or with a JWT and an organization id."""
    for name in ("DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if ok:
        DaytonaEnvironment.preflight()
    else:
        with pytest.raises(SandboxFatalError, match="DAYTONA_API_KEY"):
            DaytonaEnvironment.preflight()


def test_factory_creates_the_daytona_provider_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a DaytonaEnvironment through the factory in dev, and pass constructor options."""
    monkeypatch.setattr(factory.settings, "app_environment", "development")
    environment = factory.create_environment("daytona", _SPEC, timeouts=PhaseTimeouts(start=1, setup=1, run=3, score=1))
    assert isinstance(environment, DaytonaEnvironment)
    assert environment.spec is _SPEC


def test_factory_refuses_the_daytona_provider_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the dev-only Daytona provider in production, for create and preflight."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        factory.create_environment("daytona", _SPEC)
    with pytest.raises(ProductionNotAllowedError):
        factory.preflight("daytona")


_PROTECTED = SandboxSpec(lifetime_seconds=125, operation_key="op-1")


class FakeUsage:
    """Record the reservation calls that a protected box makes."""

    def __init__(self, budget: Any, request: dict[str, Any], *, operation_key: str) -> None:
        """Record the reserve call, with the pool lease's factory signature.

        Args:
            budget: The budget runtime.
            request: The priced create parameters.
            operation_key: The box's operation key.
        """
        self.calls: list[Any] = [("reserve", budget, request, operation_key)]
        self.box_label = {"skynet-box": uuid.uuid4().hex}
        self.settle_error: Exception | None = None
        self.raise_in: str | None = None
        self.deleted_at_bind: list[bool] = []
        FakeUsage.made.append(self)

    made: ClassVar[list[FakeUsage]] = []

    def confirm_created(self, sandbox: Any) -> None:
        """Record the confirm call.

        Args:
            sandbox: The created box.
        """
        self.calls.append(("confirm", sandbox.id))

    def bind(self, sandbox: Any) -> None:
        """Record the bind call and whether the box was already deleted, or raise when ``raise_in`` names it.

        Args:
            sandbox: The box.

        Raises:
            RuntimeError: ``raise_in`` is ``"bind"``.
        """
        self.calls.append(("bind", sandbox.id))
        self.deleted_at_bind.append(sandbox.deleted)
        if self.raise_in == "bind":
            raise RuntimeError("ledger down")

    def settle(self) -> None:
        """Record the settle call, or raise the set error.

        Raises:
            Exception: ``settle_error`` is set.
        """
        self.calls.append(("settle",))
        if self.settle_error is not None:
            raise self.settle_error

    def fail(self, error: BaseException, *, refused: bool) -> None:
        """Record the fail call, or raise when ``raise_in`` names it.

        Args:
            error: The create failure.
            refused: Whether Daytona made no box.

        Raises:
            RuntimeError: ``raise_in`` is ``"fail"``.
        """
        self.calls.append(("fail", type(error), refused))
        if self.raise_in == "fail":
            raise RuntimeError("ledger down")

    def pending(self) -> None:
        """Record the pending call."""
        self.calls.append(("pending",))


@pytest.fixture
def usage() -> type[FakeUsage]:
    """Reset the fake usage records.

    Returns:
        The fake usage class.
    """
    FakeUsage.made = []
    return FakeUsage


def _names(made: FakeUsage) -> list[str]:
    """Return the call names that one fake usage recorded.

    Args:
        made: The fake usage.

    Returns:
        The call names in order.
    """
    return [call[0] for call in made.calls]


def test_usage_adapter_is_registered() -> None:
    """Find the Daytona usage adapter, so hosted Skynet can meter the provider."""
    adapter = usage_adapter("daytona")
    assert adapter is not None
    assert adapter.name == "daytona"


def test_protected_box_needs_an_operation_key_and_sizes() -> None:
    """Refuse a protected box that has no operation key or that ignores sizes."""
    with pytest.raises(SandboxSpecError, match="operation key"):
        DaytonaEnvironment(_SPEC, budget=object(), usage_factory=FakeUsage)
    ignored = SandboxSpec(lifetime_seconds=60, operation_key="k", resources=ResourceRequest(mode=ResourceMode.IGNORE))
    with pytest.raises(SandboxSpecError, match="explicit sizes"):
        DaytonaEnvironment(ignored, budget=object(), usage_factory=FakeUsage)


async def test_protected_box_reserves_confirms_and_settles(fake_sdk: type[FakeClient], usage: type[FakeUsage]) -> None:
    """Reserve the whole lifetime before the create, confirm after it and settle after the delete."""
    budget = object()
    async with DaytonaEnvironment(_PROTECTED, budget=budget, usage_factory=usage):
        pass
    (made,) = usage.made
    assert made.calls[0] == (
        "reserve",
        budget,
        {
            "lifetime_ms": 3 * 60_000 + 60_000,
            "vcpus": 2,
            "memory_gib": 1,
            "disk_gib": 3,
            "image": "python:3.12.15-slim",
        },
        "op-1",
    )
    box_id = fake_sdk.boxes[0].id
    assert made.calls[1:] == [("bind", box_id), ("confirm", box_id), ("bind", box_id), ("settle",)]
    assert fake_sdk.boxes[0].labels == made.box_label
    assert set(made.box_label) == {"skynet-box"}
    (params,) = fake_sdk.created
    assert params.resources == {"cpu": 2, "memory": 1, "disk": 3}
    assert fake_sdk.boxes[0].deleted


async def test_unprotected_box_keeps_the_provider_default_sizes(fake_sdk: type[FakeClient]) -> None:
    """Send no memory or disk size for a box without a budget."""
    async with DaytonaEnvironment(_SPEC):
        pass
    assert fake_sdk.created[0].resources == {"cpu": 2}


@pytest.mark.parametrize(
    ("error", "refused"),
    [
        (FakeAuthenticationError("bad key"), True),
        (FakeAuthorizationError("no access"), True),
        (FakeForbiddenError("forbidden"), True),
        (FakeRateLimitError("slow down"), True),
        (FakeDaytonaError("rate limit: no capacity in region"), False),
        (FakeDaytonaError("something else"), False),
    ],
)
async def test_failed_create_releases_only_a_refusal(
    fake_sdk: type[FakeClient], usage: type[FakeUsage], error: Exception, refused: bool
) -> None:
    """Release the hold only for a 401, 403 or 429 answer by type, never by message text."""
    fake_sdk.create_error = error
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(Exception):  # noqa: B017 - the group mapping is tested above
        await env.start()
    await env.stop()
    (made,) = usage.made
    assert made.calls[1][0] == "fail"
    assert made.calls[1][2] is refused


async def test_refusal_after_the_box_exists_settles_and_deletes_it(
    fake_sdk: type[FakeClient], usage: type[FakeUsage]
) -> None:
    """Find, delete and settle a box when a refusal error comes after Daytona made it."""
    fake_sdk.fail_after_box = FakeForbiddenError("status read refused")
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(SandboxFatalError):
        await env.start()
    await env.stop()
    assert _names(usage.made[0]) == ["reserve", "bind", "bind", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_refusal_looks_again_when_the_list_lags(fake_sdk: type[FakeClient], usage: type[FakeUsage]) -> None:
    """Find, delete and settle the box when only the second lookup lists it."""
    fake_sdk.fail_after_box = FakeRateLimitError("status poll slowed")
    fake_sdk.list_hidden = 1
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(SandboxTransientError):
        await env.start()
    await env.stop()
    assert fake_sdk.list_calls == 2
    assert _names(usage.made[0]) == ["reserve", "bind", "bind", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_refusal_releases_only_after_two_empty_lookups(
    fake_sdk: type[FakeClient], usage: type[FakeUsage]
) -> None:
    """Release a refused create's hold only after two lookups find no box."""
    fake_sdk.create_error = FakeRateLimitError("slow down")
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(SandboxTransientError):
        await env.start()
    await env.stop()
    assert fake_sdk.list_calls == 2
    assert usage.made[0].calls[1] == ("fail", SandboxTransientError, True)


async def test_refusal_keeps_the_hold_when_the_box_lookup_fails(
    fake_sdk: type[FakeClient], usage: type[FakeUsage]
) -> None:
    """Keep the hold pending when the lookup cannot prove that no box exists."""
    fake_sdk.create_error = FakeRateLimitError("slow down")
    fake_sdk.list_error = FakeRateLimitError("slow down")
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(SandboxTransientError):
        await env.start()
    await env.stop()
    assert usage.made[0].calls[1] == ("fail", SandboxTransientError, False)


async def test_cancel_during_reserve_releases_the_hold(fake_sdk: type[FakeClient], usage: type[FakeUsage]) -> None:
    """Release the hold when a cancel ends the start before the create is sent."""

    def slow(*args: Any, **kwargs: Any) -> FakeUsage:
        """Reserve after a short wait.

        Args:
            *args: The reserve arguments.
            **kwargs: The reserve keyword arguments.

        Returns:
            The fake usage.
        """
        time.sleep(0.05)
        return usage(*args, **kwargs)

    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=slow)
    task = asyncio.create_task(env.start())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await env.stop()
    assert usage.made[0].calls[1] == ("fail", SandboxInfraError, True)
    assert fake_sdk.created == []


async def test_failure_before_the_create_call_releases_the_hold(
    fake_sdk: type[FakeClient], usage: type[FakeUsage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release the hold when the client or the create parameters fail before the create is sent."""

    def broken(**_kwargs: Any) -> Any:
        """Fail to build the create parameters.

        Args:
            **_kwargs: Ignored.

        Raises:
            ValueError: Always.
        """
        raise ValueError("bad params")

    monkeypatch.setattr(daytona, "CreateSandboxFromImageParams", broken)
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(ValueError, match="bad params"):
        await env.start()
    await env.stop()
    assert usage.made[0].calls[1] == ("fail", SandboxInfraError, True)
    assert fake_sdk.created == []


async def test_client_failure_releases_the_hold(
    fake_sdk: type[FakeClient], usage: type[FakeUsage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release the hold when the client cannot be made."""

    def no_client() -> Any:
        """Fail to make the client.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("no client")

    monkeypatch.setattr(daytona, "_client", no_client)
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(RuntimeError, match="no client"):
        await env.start()
    await env.stop()
    assert usage.made[0].calls[1] == ("fail", SandboxInfraError, True)


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_real_reservation_settles_a_box_that_was_never_confirmed(
    fake_sdk: type[FakeClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """Bind and settle, with the real reservation, a box that a timed-out or cancelled create made.

    Args:
        fake_sdk: The fake SDK.
        tmp_path: Per-test directory for the SQLite ledger.
        monkeypatch: The pytest monkeypatch fixture.
        ending: How the start ends after Daytona made the box.
    """
    monkeypatch.delenv("DAYTONA_ORGANIZATION_ID", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    runtime = BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)
    made: list[DaytonaUsageReservation] = []

    def reserve(*args: Any, **kwargs: Any) -> DaytonaUsageReservation:
        """Make the real reservation and keep it for the checks.

        Args:
            *args: The reserve arguments.
            **kwargs: The reserve keyword arguments.

        Returns:
            The real reservation.
        """
        made.append(DaytonaUsageReservation(*args, **kwargs))
        return made[-1]

    env = DaytonaEnvironment(_PROTECTED, budget=runtime, usage_factory=reserve)
    try:
        if ending == "timeout":
            fake_sdk.create_times_out = True
            with pytest.raises(StartTimeoutError):
                await env.start()
        else:
            fake_sdk.create_delay = 0.05
            task = asyncio.create_task(env.start())
            await fake_sdk.create_called.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await env.stop()
        operation = service.get_operation(made[0].operation.id, "alice")
        assert operation.state == "settled"
        assert operation.provider_request_id == fake_sdk.boxes[0].id
        assert fake_sdk.boxes[0].deleted
    finally:
        engine.dispose()


async def test_create_timeout_settles_the_box_it_made(fake_sdk: type[FakeClient], usage: type[FakeUsage]) -> None:
    """Settle a box that a timed-out create made, after the delete."""
    fake_sdk.create_times_out = True
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(StartTimeoutError):
        await env.start()
    await env.stop()
    assert _names(usage.made[0]) == ["reserve", "bind", "bind", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_cancel_during_create_settles_the_box(fake_sdk: type[FakeClient], usage: type[FakeUsage]) -> None:
    """Settle a box that Daytona made during a cancelled start."""
    fake_sdk.create_delay = 0.05
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    task = asyncio.create_task(env.start())
    await fake_sdk.create_called.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await env.stop()
    assert _names(usage.made[0]) == ["reserve", "bind", "bind", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_failed_delete_keeps_the_hold_pending(fake_sdk: type[FakeClient], usage: type[FakeUsage]) -> None:
    """Keep the whole hold when the delete fails, because the box can still run."""
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    await env.start()
    fake_sdk.boxes[0].fail_delete = FakeDaytonaError("delete broke")
    with pytest.raises(SandboxInfraError):
        await env.stop()
    assert _names(usage.made[0]) == ["reserve", "bind", "confirm", "pending"]


async def test_settle_failure_raises_a_pending_error(fake_sdk: type[FakeClient], usage: type[FakeUsage]) -> None:
    """Raise a usage error without database details when the settle fails."""
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    await env.start()
    usage.made[0].settle_error = RuntimeError("db password=secret")
    with pytest.raises(UsagePendingError) as raised:
        await env.stop()
    assert "secret" not in str(raised.value)
    await env.stop()
    assert _names(usage.made[0]) == ["reserve", "bind", "confirm", "bind", "settle", "pending"]


async def test_failed_reserve_creates_no_box(fake_sdk: type[FakeClient]) -> None:
    """Create no box when the reservation fails."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        """Refuse the reservation.

        Args:
            *_args: Ignored.
            **_kwargs: Ignored.

        Raises:
            UsagePendingError: Always.
        """
        raise UsagePendingError("no funds")

    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=refuse)
    with pytest.raises(UsagePendingError):
        await env.start()
    await env.stop()
    assert fake_sdk.created == []


def _ledger(tmp_path: Path) -> tuple[Engine, BudgetService, BudgetRuntime]:
    """Create a funded local ledger and a budget runtime on it.

    Args:
        tmp_path: Per-test directory for the SQLite ledger.

    Returns:
        The engine, the budget service and a runtime for a 50-cent budget.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    runtime = BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)
    return engine, service, runtime


async def test_size_mismatch_settles_measured_time_at_the_larger_size(
    fake_sdk: type[FakeClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail the start of a box of another size, then delete it and settle at the larger size on stop.

    Args:
        fake_sdk: The fake SDK.
        tmp_path: Per-test directory for the SQLite ledger.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.delenv("DAYTONA_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.setattr(daytona, "Resources", lambda **sizes: {**sizes, "memory": 2})
    engine, service, runtime = _ledger(tmp_path)
    made: list[DaytonaUsageReservation] = []

    def reserve(*args: Any, **kwargs: Any) -> DaytonaUsageReservation:
        """Make the real reservation and keep it for the checks.

        Args:
            *args: The reserve arguments.
            **kwargs: The reserve keyword arguments.

        Returns:
            The real reservation.
        """
        made.append(DaytonaUsageReservation(*args, **kwargs))
        return made[-1]

    env = DaytonaEnvironment(_PROTECTED, budget=runtime, usage_factory=reserve)
    try:
        with pytest.raises(UsagePendingError):
            await env.start()
        await env.stop()
        assert fake_sdk.boxes[0].deleted
        operation_id = made[0].operation.id
        assert service.get_operation(operation_id, "alice").state == "settled"
        (settled,) = [item for item in service.get_reconciliation(operation_id, "alice").evidence if "source" in item]
        assert settled["funded_sizes"] == {"vcpus": 2, "memory_gib": 1, "disk_gib": 3}
        assert settled["actual_sizes"] == {"vcpus": 2, "memory_gib": 2, "disk_gib": 3}
        larger = Decimal("0.0504") * 2 + Decimal("0.0162") * 2 + Decimal("0.000108") * 3
        assert Decimal(settled["measured_usd"]) == settled["measured_seconds"] * larger / 3600
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure", ["timeout", "refusal"])
async def test_box_id_reaches_the_ledger_before_the_delete(
    fake_sdk: type[FakeClient], usage: type[FakeUsage], failure: str
) -> None:
    """Bind a box that a failed create made before the delete, so a crash leaves a hold that names it.

    Args:
        fake_sdk: The fake SDK.
        usage: The fake usage class.
        failure: How the create fails after Daytona made the box.
    """
    if failure == "timeout":
        fake_sdk.create_times_out = True
    else:
        fake_sdk.fail_after_box = FakeForbiddenError("status read refused")
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    with pytest.raises(SandboxError):
        await env.start()
    await env.stop()
    assert usage.made[0].deleted_at_bind[0] is False
    assert fake_sdk.boxes[0].deleted


@pytest.mark.parametrize("step", ["bind", "fail"])
async def test_failed_ledger_step_after_the_delete_marks_the_hold_pending(
    fake_sdk: type[FakeClient], usage: type[FakeUsage], step: str
) -> None:
    """Mark the hold pending when the bind or the release fails after the delete.

    Args:
        fake_sdk: The fake SDK.
        usage: The fake usage class.
        step: The reservation call that fails.
    """
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    if step == "fail":
        fake_sdk.create_error = FakeDaytonaError("something else")
        with pytest.raises(SandboxError):
            await env.start()
    else:
        await env.start()
    usage.made[0].raise_in = step
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert _names(usage.made[0])[-2:] == [step, "pending"]


async def test_each_reservation_gets_its_own_box_label(fake_sdk: type[FakeClient], usage: type[FakeUsage]) -> None:
    """Label each new reservation's box with a new label, so a lookup never finds an older box."""
    env = DaytonaEnvironment(_PROTECTED, budget=object(), usage_factory=usage)
    for _ in range(2):
        await env.start()
        await env.stop()
    first, second = usage.made
    assert first.box_label != second.box_label
    assert [box.labels for box in fake_sdk.boxes] == [first.box_label, second.box_label]

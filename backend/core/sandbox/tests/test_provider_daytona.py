"""Tests for the Daytona sandbox provider, with a fake Daytona SDK.

The tests never import the real SDK and never call Daytona. A module-level
fixture replaces the SDK names in ``core.sandbox.providers.daytona`` with
small in-memory fakes.
"""

from __future__ import annotations

import asyncio
import itertools
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from core.sandbox import JOB_TAG, NetworkPolicy, ResourceRequest, SandboxSpec, factory
from core.sandbox.errors import (
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.factory import ProductionNotAllowedError
from core.sandbox.providers import daytona
from core.sandbox.providers.daytona import DaytonaEnvironment
from core.sandbox.resources import ResourceMode
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.sandbox.timeouts import PhaseTimeouts

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
        if cls.create_delay:
            await asyncio.sleep(cls.create_delay)
        if cls.create_error is not None:
            raise cls.create_error
        cls.created.append(params)
        box = FakeBox(id=f"box-{next(cls._ids)}", labels=dict(params.labels or {}), env=dict(params.env_vars or {}))
        cls.boxes.append(box)
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

        Args:
            query: The list query.

        Yields:
            Each matching box.
        """
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

"""Tests for the Mosaic sandbox provider, against an in-memory fake of the SDK.

No test reaches Mosaic: an autouse fixture swaps the SDK classes, its error
classes and its token lookup for fakes, so the suite needs no network, no
API token and no ``mosaic`` extra.
"""

from __future__ import annotations

import asyncio
import itertools
import shlex
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import settings
from core.sandbox import factory
from core.sandbox.environment import JOB_TAG
from core.sandbox.errors import (
    RunTimeoutError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.providers import mosaic
from core.sandbox.providers.mosaic import BOX_TAG, MosaicEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.timeouts import PhaseTimeouts

from .test_environment import SandboxEnvironmentContract


class FakeMosaicError(Exception):
    """Stand in for ``mosaic_sandbox.MosaicError``."""

    def __init__(self, message: str = "", *, status: int | None = None, code: str | None = None) -> None:
        """Keep the HTTP status and the error code.

        Args:
            message: The error message.
            status: The HTTP status, or ``None``.
            code: The SDK error code, or ``None``.
        """
        super().__init__(message)
        self.status = status
        self.code = code


class FakeAuthenticationError(FakeMosaicError):
    """Stand in for ``AuthenticationError``."""


class FakePermissionError(FakeMosaicError):
    """Stand in for ``mosaic_sandbox.PermissionError``."""


class FakeNotFoundError(FakeMosaicError):
    """Stand in for ``NotFoundError``."""


class FakeRateLimitError(FakeMosaicError):
    """Stand in for ``RateLimitError``."""


class FakeTimeoutError(FakeMosaicError):
    """Stand in for ``mosaic_sandbox.TimeoutError``."""


class FakeUnknownFieldError(FakeMosaicError):
    """Stand in for ``UnknownFieldError``."""


class FakeUnsupportedShapeError(FakeMosaicError):
    """Stand in for ``UnsupportedShapeError``."""


class FakeUnsupportedTemplateError(FakeMosaicError):
    """Stand in for ``UnsupportedTemplateError``."""


@dataclass
class BoxState:
    """Hold one fake sandbox in the fake cloud."""

    id: str
    params: dict[str, Any]
    state: str = "running"
    files: dict[str, str] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=lambda: {"/", "/tmp"})


@dataclass
class FakeCloud:
    """Hold the shared state of the fake Mosaic account."""

    boxes: dict[str, BoxState] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    token: str | None = "fake-token-for-tests"
    create_error: BaseException | None = None
    create_error_after_box: bool = False
    create_delay: float = 0.0
    list_error: BaseException | None = None
    list_delay: float = 0.0
    destroy_fails: set[str] = field(default_factory=set)
    exec_hangs: bool = False
    exit_code_in_info: bool = False
    no_exit_code: bool = False
    ids: Any = field(default_factory=lambda: itertools.count(1))

    def run(self, box: BoxState, command: str) -> tuple[int, str, str]:
        """Run the provider's wrapper line in a tiny shell model.

        Args:
            box: The sandbox.
            command: The full wrapper line that ``process.start`` received.

        Returns:
            The exit code, stdout and stderr.
        """
        tokens = shlex.split(command)
        if tokens[0] == "cd":
            tokens = tokens[3:]
        env: dict[str, str] = {}
        while "=" in tokens[0] and tokens[0] != "bash":
            key, _, value = tokens.pop(0).partition("=")
            env[key] = value
        assert tokens[:2] == ["bash", "-c"]
        words = shlex.split(tokens[2])
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
            return (0, box.files[args[0]], "") if args[0] in box.files else (1, "", "No such file\n")
        if verb == "mkdir" and args[0] == "-p":
            for path in args[1:]:
                parts = path.strip("/").split("/")
                box.dirs.update("/" + "/".join(parts[: n + 1]) for n in range(len(parts)))
            return 0, "", ""
        return 127, "", f"{verb}: command not found\n"


CLOUD = FakeCloud()


class FakeSyncSandbox:
    """Stand in for the sync ``mosaic_sandbox.Sandbox`` instance."""

    def __init__(self, box: BoxState) -> None:
        """Wrap one fake box.

        Args:
            box: The box state.
        """
        self.id = box.id
        self.box = box

    def destroy(self) -> None:
        """Destroy the box, or fail when the cloud says so.

        Raises:
            FakeMosaicError: The cloud fails this destroy.
        """
        CLOUD.calls.append(f"destroy:{self.id}")
        if self.id in CLOUD.destroy_fails:
            raise FakeMosaicError("boom", status=500)
        self.box.state = "destroyed"


class FakeSandbox:
    """Stand in for the ``mosaic_sandbox.Sandbox`` class methods."""

    @classmethod
    def create(cls, **params: Any) -> FakeSyncSandbox:
        """Create a box, block for a short time and return it.

        Args:
            **params: The create parameters.

        Returns:
            The sync sandbox.

        Raises:
            BaseException: The cloud's create error.
        """
        CLOUD.calls.append("create")
        if CLOUD.create_error is not None and not CLOUD.create_error_after_box:
            raise CLOUD.create_error
        box = BoxState(id=f"sbx-{next(CLOUD.ids)}", params=params)
        CLOUD.boxes[box.id] = box
        if CLOUD.create_delay:
            time.sleep(CLOUD.create_delay)
        if CLOUD.create_error is not None:
            raise CLOUD.create_error
        return FakeSyncSandbox(box)

    @classmethod
    def list(cls, *, metadata: dict[str, str]) -> list[SimpleNamespace]:
        """Return the boxes whose metadata holds every given pair.

        Args:
            metadata: The metadata filter.

        Returns:
            Sandbox summaries.

        Raises:
            BaseException: The cloud's list error.
        """
        CLOUD.calls.append("list")
        if CLOUD.list_delay:
            time.sleep(CLOUD.list_delay)
        if CLOUD.list_error is not None:
            raise CLOUD.list_error
        return [
            SimpleNamespace(id=box.id, state=box.state, metadata=dict(box.params["metadata"]))
            for box in list(CLOUD.boxes.values())
            if metadata.items() <= box.params["metadata"].items()
        ]

    @classmethod
    def connect(cls, sandbox_id: str) -> FakeSyncSandbox:
        """Return the sync sandbox of an id.

        Args:
            sandbox_id: The sandbox id.

        Returns:
            The sync sandbox.

        Raises:
            FakeNotFoundError: The id is unknown.
        """
        if sandbox_id not in CLOUD.boxes:
            raise FakeNotFoundError(sandbox_id, status=404)
        return FakeSyncSandbox(CLOUD.boxes[sandbox_id])


class FakeProcess:
    """Stand in for ``AsyncProcessHandle``."""

    def __init__(self, process_id: str, outcome: tuple[int, str, str]) -> None:
        """Keep the outcome of the command.

        Args:
            process_id: The process id.
            outcome: The exit code, stdout and stderr.
        """
        self.id = process_id
        self._outcome = outcome
        self._read = False

    async def logs(self) -> dict[str, Any]:
        """Return the new output, and the finished state unless the command hangs.

        Returns:
            A payload like the SDK's ``logs()``.
        """
        if CLOUD.exec_hangs:
            return {"stdout": "", "stderr": "", "state": "running"}
        code, out, err = self._outcome
        payload: dict[str, Any] = {"state": "finished"}
        if not self._read:
            payload.update(stdout=out, stderr=err)
            self._read = True
        if not (CLOUD.exit_code_in_info or CLOUD.no_exit_code):
            payload["exit_code"] = code
        return payload

    async def info(self) -> SimpleNamespace:
        """Return the process info.

        Returns:
            An object with the exit code, or ``None`` when the cloud gives none.
        """
        return SimpleNamespace(exit_code=None if CLOUD.no_exit_code else self._outcome[0])

    async def kill(self) -> None:
        """Record that the command was killed."""
        CLOUD.killed.append(self.id)


class FakeAsyncSandbox:
    """Stand in for ``mosaic_sandbox.AsyncSandbox``."""

    def __init__(self, sync: FakeSyncSandbox) -> None:
        """Wrap one sync sandbox.

        Args:
            sync: The sync sandbox.
        """
        self.id = sync.id
        self._sync = sync
        self._box = sync.box
        self.process = SimpleNamespace(start=self._start)
        self.files = SimpleNamespace(write=self._write, read_bytes=self._read_bytes)

    async def _start(self, command: str) -> FakeProcess:
        """Run the command in the shell model.

        Args:
            command: The wrapper line.

        Returns:
            The process handle.
        """
        CLOUD.calls.append(f"exec:{command}")
        return FakeProcess(f"proc-{next(CLOUD.ids)}", CLOUD.run(self._box, command))

    async def _write(self, path: str, content: str, *, create_parents: bool = False) -> None:
        """Write a file whose parent directory exists.

        Args:
            path: The absolute path.
            content: The text.
            create_parents: Must be true; the provider always sets it.

        Raises:
            FakeMosaicError: The parent directory does not exist.
        """
        assert create_parents is True
        if str(PurePosixPath(path).parent) not in self._box.dirs:
            raise FakeMosaicError("no parent directory", status=400)
        self._box.files[path] = content

    async def _read_bytes(self, path: str) -> bytes:
        """Return the file bytes.

        Args:
            path: The absolute path.

        Returns:
            The UTF-8 bytes.

        Raises:
            FakeNotFoundError: The file does not exist.
        """
        if path not in self._box.files:
            raise FakeNotFoundError(path, status=404)
        return self._box.files[path].encode("utf-8")

    async def destroy(self) -> None:
        """Destroy the box."""
        await asyncio.to_thread(self._sync.destroy)


def _resolve_token() -> tuple[str | None, str]:
    """Return the fake cloud's token, like ``credentials.resolve_token``.

    Returns:
        The token and its source.
    """
    return CLOUD.token, "env"


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    """Swap the Mosaic SDK for the fake and reset the fake cloud.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fresh fake cloud.
    """
    global CLOUD
    CLOUD = FakeCloud()
    fakes = {
        "AsyncSandbox": FakeAsyncSandbox,
        "Sandbox": FakeSandbox,
        "MosaicError": FakeMosaicError,
        "AuthenticationError": FakeAuthenticationError,
        "MosaicPermissionError": FakePermissionError,
        "NotFoundError": FakeNotFoundError,
        "RateLimitError": FakeRateLimitError,
        "MosaicTimeoutError": FakeTimeoutError,
        "UnknownFieldError": FakeUnknownFieldError,
        "UnsupportedShapeError": FakeUnsupportedShapeError,
        "UnsupportedTemplateError": FakeUnsupportedTemplateError,
        "resolve_token": _resolve_token,
    }
    for name, value in fakes.items():
        monkeypatch.setattr(mosaic, name, value)
    monkeypatch.setattr(mosaic, "STOP_TIMEOUT", 1.0)
    monkeypatch.setattr(mosaic, "KILL_TIMEOUT", 1.0)
    monkeypatch.setattr(mosaic, "POLL_INTERVAL", 0.001)
    return CLOUD


class TestMosaicEnvironmentContract(SandboxEnvironmentContract):
    """Run the shared provider contract against the fake Mosaic cloud."""

    provider_cls = MosaicEnvironment


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), **overrides})


def _timeouts(*, start: float = 30, run: float = 30) -> PhaseTimeouts:
    """Return phase timeouts with these start and run limits.

    Args:
        start: The start limit in seconds.
        run: The run limit in seconds.

    Returns:
        The phase timeouts.
    """
    return PhaseTimeouts(start=start, setup=30, run=run, score=30)


def test_mosaic_is_not_allowed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the Mosaic provider in production before its import (D1)."""
    assert factory.PROVIDERS["mosaic"].production_allowed is False
    assert factory.PROVIDERS["mosaic"].extra == "mosaic"
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="mosaic"):
        factory.load_provider("mosaic")


def test_factory_loads_mosaic_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the Mosaic class from the registry outside production."""
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("mosaic") is MosaicEnvironment


def test_the_default_sandbox_backend_stays_legacy() -> None:
    """Keep the legacy backend as the default."""
    assert type(settings).model_fields["sandbox_backend"].default == "legacy"


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the extra when the SDK is not installed."""
    monkeypatch.setattr(mosaic, "Sandbox", None)
    with pytest.raises(SandboxFatalError, match="`mosaic` extra"):
        MosaicEnvironment.preflight()


def test_preflight_needs_a_token(fake_sdk: FakeCloud) -> None:
    """Name the variable when Mosaic finds no API token."""
    fake_sdk.token = None
    with pytest.raises(SandboxFatalError, match=mosaic.API_TOKEN_ENV):
        MosaicEnvironment.preflight()


async def test_start_without_a_token_creates_nothing(fake_sdk: FakeCloud) -> None:
    """Fail the start before any create call when the token is missing."""
    fake_sdk.token = None
    with pytest.raises(SandboxFatalError):
        await MosaicEnvironment(_spec()).start()
    assert "create" not in fake_sdk.calls


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeRateLimitError("429", status=429), SandboxTransientError),
        (FakeTimeoutError("slow"), SandboxTransientError),
        (FakeMosaicError("down", code="transport"), SandboxTransientError),
        (FakeMosaicError("late", code="client_timeout"), SandboxTransientError),
        (FakeMosaicError("500", status=503), SandboxTransientError),
        (FakeAuthenticationError("401", status=401), SandboxFatalError),
        (FakePermissionError("403", status=403), SandboxFatalError),
        (FakeUnknownFieldError("field"), SandboxRejectedError),
        (FakeUnsupportedShapeError("shape"), SandboxRejectedError),
        (FakeUnsupportedTemplateError("template"), SandboxRejectedError),
        (FakeMosaicError("400", status=400), SandboxRejectedError),
        (FakeMosaicError("422", status=422), SandboxRejectedError),
        (FakeMosaicError("failed"), SandboxInfraError),
    ],
)
async def test_sdk_errors_map_to_sandbox_errors(
    fake_sdk: FakeCloud, error: Exception, expected: type[Exception]
) -> None:
    """Map each SDK error group to its sandbox error, keep the cause and never retry."""
    fake_sdk.create_error = error
    with pytest.raises(expected) as caught:
        await MosaicEnvironment(_spec()).start()
    assert caught.value.__cause__ is error
    assert fake_sdk.calls.count("create") == 1


async def test_create_sends_the_tags_ttl_and_idempotency_key(fake_sdk: FakeCloud) -> None:
    """Tag the box, round the lifetime up and use the box tag as the idempotency key."""
    env = MosaicEnvironment(_spec(tags={JOB_TAG: "job-1"}, lifetime_seconds=10.5))
    async with env:
        (box,) = fake_sdk.boxes.values()
    params = box.params
    assert params["metadata"] == {JOB_TAG: "job-1", BOX_TAG: env._box_tag}
    assert params["idempotency_key"] == env._box_tag
    assert params["ttl_seconds"] == 11
    assert params["name"] == f"skynet-{env._box_tag[:12]}"
    assert box.state == "destroyed"


def test_resources_map_to_vcpu_and_memory() -> None:
    """Send the vCPUs and the memory, and no size in ignore mode."""
    params = MosaicEnvironment(_spec(resources=ResourceRequest(cpus=2, memory_mb=4096)))._create_params
    assert (params["vcpu"], params["memory_mb"]) == (2, 4096)
    assert "memory_mb" not in MosaicEnvironment(_spec())._create_params
    ignored = MosaicEnvironment(_spec(resources=ResourceRequest(cpus=64, mode=ResourceMode.IGNORE)))._create_params
    assert "vcpu" not in ignored
    assert "memory_mb" not in ignored


def test_network_modes_map_to_the_create_fields() -> None:
    """Send no network field for open egress, and the flag and hosts otherwise."""
    open_params = MosaicEnvironment(_spec())._create_params
    assert "network_enabled" not in open_params
    assert "network_allow" not in open_params
    denied = MosaicEnvironment(_spec(network_disabled=True))._create_params
    assert denied["network_enabled"] is False
    assert "network_allow" not in denied
    allowed = MosaicEnvironment(_spec(allowed_hosts=("b.example", "a.example", "b.example")))._create_params
    assert allowed["network_enabled"] is True
    assert allowed["network_allow"] == ["b.example", "a.example"]


async def test_a_second_start_is_refused(fake_sdk: FakeCloud) -> None:
    """Refuse a second start, while the box runs and after the stop."""
    env = MosaicEnvironment(_spec())
    await env.start()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    assert fake_sdk.calls.count("create") == 1


async def test_a_stop_before_the_start_still_lets_the_box_stop(fake_sdk: FakeCloud) -> None:
    """Start after an early stop, then destroy the box at the next stop."""
    env = MosaicEnvironment(_spec())
    await env.stop()
    await env.start()
    await env.stop()
    (box,) = fake_sdk.boxes.values()
    assert box.state == "destroyed"


async def test_exec_wraps_cwd_and_env() -> None:
    """Run in the cwd with quoted env values and the spec env."""
    async with MosaicEnvironment(_spec(env={"BASE": "1"})) as env:
        result = await env.exec("printenv X", cwd="/tmp", env={"X": "a b'c"})
        base = await env.exec("printenv BASE")
    assert result.stdout == "a b'c\n"
    assert base.stdout == "1\n"
    assert MosaicEnvironment._wrap("ls", "/w d", {"K": "v"}) == "cd '/w d' && K=v bash -c ls"


async def test_exec_rejects_a_bad_env_name() -> None:
    """Reject an env name that the shell cannot set."""
    async with MosaicEnvironment(_spec()) as env:
        with pytest.raises(SandboxRejectedError):
            await env.exec("true", env={"A;rm": "x"})


async def test_exec_before_start_is_an_infra_error() -> None:
    """Refuse a command before the box runs."""
    with pytest.raises(SandboxInfraError, match="not running"):
        await MosaicEnvironment(_spec()).exec("true")


async def test_exec_reads_the_exit_code_from_info(fake_sdk: FakeCloud) -> None:
    """Ask for the process info when the logs give no exit code."""
    fake_sdk.exit_code_in_info = True
    async with MosaicEnvironment(_spec()) as env:
        assert (await env.exec("exit 4")).exit_code == 4


async def test_a_missing_exit_code_is_an_infra_error(fake_sdk: FakeCloud) -> None:
    """Raise when neither the logs nor the info give an exit code."""
    fake_sdk.no_exit_code = True
    async with MosaicEnvironment(_spec()) as env:
        with pytest.raises(SandboxInfraError, match="no exit code"):
            await env.exec("true")
    assert fake_sdk.killed == []


async def test_a_timed_out_command_is_killed(fake_sdk: FakeCloud) -> None:
    """Kill the command and raise RunTimeoutError at the time limit."""
    async with MosaicEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true", timeout_sec=0.05)
    assert len(fake_sdk.killed) == 1


async def test_the_run_timeout_is_the_default_limit(fake_sdk: FakeCloud) -> None:
    """Use the run phase timeout when the call sets no limit."""
    async with MosaicEnvironment(_spec(), timeouts=_timeouts(run=0.05)) as env:
        fake_sdk.exec_hangs = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true")
    assert len(fake_sdk.killed) == 1


async def test_a_cancelled_command_is_killed(fake_sdk: FakeCloud) -> None:
    """Kill the command when the caller cancels the exec."""
    async with MosaicEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        task = asyncio.ensure_future(env.exec("true"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(fake_sdk.killed) == 1


async def test_upload_makes_the_parent_directories_once(fake_sdk: FakeCloud) -> None:
    """Make every parent directory in one command before the writes."""
    async with MosaicEnvironment(_spec()) as env:
        await env.upload_files({"/w/a/x.txt": "x", "/w/b/y.txt": "y", "/top.txt": "t"})
        assert await env.download_file("/w/b/y.txt") == "y"
        assert await env.download_file("/top.txt") == "t"
    assert sum(call.startswith("exec:") and "mkdir" in call for call in fake_sdk.calls) == 1


async def test_a_read_error_other_than_not_found_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise the mapped error when a read fails for another reason."""

    async def failing(self: FakeAsyncSandbox, path: str) -> bytes:
        """Fail every read."""
        raise FakeMosaicError("500", status=500)

    monkeypatch.setattr(FakeAsyncSandbox, "_read_bytes", failing)
    async with MosaicEnvironment(_spec()) as env:
        with pytest.raises(SandboxTransientError):
            await env.download_file("/tmp/a.txt")


async def test_start_timeout_destroys_the_lost_box(fake_sdk: FakeCloud) -> None:
    """Raise StartTimeoutError and destroy the box that the create thread made."""
    fake_sdk.create_delay = 0.3
    env = MosaicEnvironment(_spec(), timeouts=_timeouts(start=0.05))
    with pytest.raises(StartTimeoutError):
        await env.start()
    (box,) = fake_sdk.boxes.values()
    assert box.state == "destroyed"


async def test_a_failed_create_with_a_server_box_is_found_by_its_tag(fake_sdk: FakeCloud) -> None:
    """Find and destroy, by the box tag, a box whose create answer was lost."""
    fake_sdk.create_error = FakeMosaicError("lost", code="transport")
    fake_sdk.create_error_after_box = True
    with pytest.raises(SandboxTransientError):
        await MosaicEnvironment(_spec()).start()
    (box,) = fake_sdk.boxes.values()
    assert box.state == "destroyed"
    assert "list" in fake_sdk.calls


async def test_stop_during_start_destroys_the_box(fake_sdk: FakeCloud) -> None:
    """End a running start from another task and leak no box."""
    fake_sdk.create_delay = 0.2
    env = MosaicEnvironment(_spec())
    start = asyncio.ensure_future(env.start())
    await asyncio.sleep(0.02)
    await env.stop()
    with pytest.raises(SandboxInfraError, match="during its start"):
        await start
    (box,) = fake_sdk.boxes.values()
    assert box.state == "destroyed"


async def test_cancelled_start_destroys_the_box(fake_sdk: FakeCloud) -> None:
    """Keep the cancel and leak no box when the caller cancels the start."""
    fake_sdk.create_delay = 0.2
    start = asyncio.ensure_future(MosaicEnvironment(_spec()).start())
    await asyncio.sleep(0.02)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    (box,) = fake_sdk.boxes.values()
    assert box.state == "destroyed"


async def test_orphan_sweep_leaves_other_boxes_running(fake_sdk: FakeCloud) -> None:
    """Destroy only the box with this provider's own box tag."""
    other = MosaicEnvironment(_spec())
    await other.start()
    fake_sdk.create_error = FakeMosaicError("lost", code="transport")
    fake_sdk.create_error_after_box = True
    with pytest.raises(SandboxTransientError):
        await MosaicEnvironment(_spec()).start()
    assert [box.state for box in fake_sdk.boxes.values()] == ["running", "destroyed"]
    fake_sdk.create_error = None
    await other.stop()


async def test_stop_job_sandboxes_skips_ended_boxes(fake_sdk: FakeCloud) -> None:
    """Count only the running boxes of the job."""
    envs = [MosaicEnvironment(_spec(tags={JOB_TAG: "job-9"})) for _ in range(2)]
    for env in envs:
        await env.start()
    await envs[0].stop()
    assert await MosaicEnvironment.stop_job_sandboxes("job-9") == 1
    await envs[1].stop()


async def test_stop_job_sandboxes_skips_other_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip a listed box whose job tag does not match, even when the list returns it."""
    stray = SimpleNamespace(id="sbx-stray", state="running", metadata={JOB_TAG: "other"})
    monkeypatch.setattr(FakeSandbox, "list", classmethod(lambda cls, *, metadata: [stray]))
    assert await MosaicEnvironment.stop_job_sandboxes("job-3") == 0


async def test_stop_job_sandboxes_counts_only_successful_destroys(fake_sdk: FakeCloud) -> None:
    """Leave a failed destroy out of the count and go on."""
    envs = [MosaicEnvironment(_spec(tags={JOB_TAG: "job-7"})) for _ in range(2)]
    for env in envs:
        await env.start()
    fake_sdk.destroy_fails = {envs[0]._box.id}
    assert await MosaicEnvironment.stop_job_sandboxes("job-7") == 1
    fake_sdk.destroy_fails = set()
    for env in envs:
        await env.stop()


async def test_stop_job_sandboxes_bounds_the_list(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """Log a list that runs past the limit and destroy nothing."""
    monkeypatch.setattr(mosaic, "STOP_TIMEOUT", 0.05)
    fake_sdk.list_delay = 0.2
    assert await MosaicEnvironment.stop_job_sandboxes("job-5") == 0


async def test_stop_job_sandboxes_raises_a_list_error(fake_sdk: FakeCloud) -> None:
    """Raise the mapped error when the list call fails."""
    fake_sdk.list_error = FakeAuthenticationError("401", status=401)
    with pytest.raises(SandboxFatalError):
        await MosaicEnvironment.stop_job_sandboxes("job-6")

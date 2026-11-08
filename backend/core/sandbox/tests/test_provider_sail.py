"""Tests for the Sail sandbox provider, against an in-memory fake of the SDK.

No test reaches Sail: an autouse fixture swaps the SDK names in the provider
module for fakes, so the suite needs no network, no credentials and no
``sail`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import settings
from core.sandbox import factory
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import RunTimeoutError, SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.providers import sail
from core.sandbox.providers.sail import SailEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.telemetry import use_output_sink
from core.sandbox.timeouts import PhaseTimeouts

from .test_environment import SandboxEnvironmentContract


class FakeSailError(Exception):
    """Stand in for an SDK error whose class the provider does not know."""


def _aio(fn: Any) -> SimpleNamespace:
    """Wrap a coroutine function like an SDK method with an ``.aio`` form.

    Args:
        fn: The coroutine function.

    Returns:
        An object whose ``aio`` attribute is ``fn``.
    """
    return SimpleNamespace(aio=fn)


@dataclass
class BoxState:
    """Hold one fake Sailbox in the fake cloud."""

    id: str
    params: dict[str, Any]
    terminated: bool = False
    egress: Any = None
    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=lambda: {"/", "/tmp"})


@dataclass
class FakeCloud:
    """Hold the shared state of the fake Sail account."""

    boxes: list[BoxState] = field(default_factory=list)
    apps: list[tuple[str, bool]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    size_calls: list[dict[str, Any]] = field(default_factory=list)
    create_calls: int = 0
    create_started: asyncio.Event = field(default_factory=asyncio.Event)
    create_error: BaseException | None = None
    create_delay: float = 0.0
    exec_hangs: bool = False
    exec_times_out: bool = False
    exec_truncates: bool = False
    read_error: type[BaseException] = FileNotFoundError
    terminate_fails: set[str] = field(default_factory=set)
    no_auth: bool = False
    ids: Any = field(default_factory=lambda: itertools.count(1))

    def run(self, box: BoxState, argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        """Run the provider's argv in a tiny shell model.

        Args:
            box: The Sailbox.
            argv: The ``bash -c`` argv that ``run.aio`` received.
            env: The command environment.

        Returns:
            The exit code, stdout and stderr.
        """
        assert argv[:2] == ["bash", "-c"]
        command = argv[2]
        if command.startswith("cd "):
            tokens = shlex.split(command)
            assert tokens[2:6] == ["&&", "exec", "bash", "-c"]
            command = tokens[6]
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
        if verb == "test" and args[0] == "-e":
            return (0, "", "") if args[1] in box.files or args[1] in box.dirs else (1, "", "")
        if verb == "mkdir" and args[0] == "-p":
            for path in args[1:]:
                parts = path.strip("/").split("/")
                box.dirs.update("/" + "/".join(parts[: n + 1]) for n in range(len(parts)))
            return 0, "", ""
        return 127, "", f"{verb}: command not found\n"


CLOUD = FakeCloud()


class FakeConfig:
    """Stand in for ``sail.Config``."""

    @staticmethod
    def from_env() -> FakeConfig:
        """Read the fake credentials.

        Returns:
            A config.

        Raises:
            ValueError: The fake cloud says no credentials exist.
        """
        if CLOUD.no_auth:
            raise ValueError("no Sail token")
        return FakeConfig()


class FakeImage:
    """Stand in for ``sail.Image``."""

    @staticmethod
    def from_registry(ref: str) -> SimpleNamespace:
        """Describe a registry image.

        Args:
            ref: The image reference.

        Returns:
            The image definition.
        """
        return SimpleNamespace(ref=ref)


class FakeEgressPolicy:
    """Stand in for ``sail.EgressPolicy``."""

    @staticmethod
    def no_network() -> dict[str, Any]:
        """Return the no-network document.

        Returns:
            The policy document.
        """
        return {"mode": "none"}

    @staticmethod
    def allow_only(*hosts: str) -> dict[str, Any]:
        """Return an allowlist document.

        Args:
            *hosts: The allowed hosts.

        Returns:
            The policy document.
        """
        return {"mode": "allow", "hosts": list(hosts)}

    @staticmethod
    def allow_all() -> dict[str, Any]:
        """Return the open document.

        Returns:
            The policy document.
        """
        return {"mode": "all"}


async def _find_app(name: str, *, mint_if_missing: bool) -> SimpleNamespace:
    """Find or make the fake app.

    Args:
        name: The app name.
        mint_if_missing: Whether to make the app.

    Returns:
        The app.
    """
    CLOUD.apps.append((name, mint_if_missing))
    return SimpleNamespace(name=name)


class FakeApp:
    """Stand in for ``sail.App``."""

    find = _aio(_find_app)


class FakeStream:
    """Stand in for the async byte stream of ``fs.read_stream``."""

    def __init__(self, data: bytes) -> None:
        """Split the data into two chunks, so a UTF-8 character may span them.

        Args:
            data: The file bytes.
        """
        half = len(data) // 2
        self._chunks = [data[:half], data[half:]]

    def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the chunks.

        Returns:
            The chunk iterator.
        """
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        """Yield each chunk.

        Yields:
            The byte chunks.
        """
        for chunk in self._chunks:
            yield chunk


class FakeSailbox:
    """Stand in for ``sail.Sailbox``."""

    def __init__(self, box: BoxState) -> None:
        """Wrap one fake box.

        Args:
            box: The box state.
        """
        self.sailbox_id = box.id
        self._box = box
        self.run = _aio(self._run)
        self.terminate = _aio(self._terminate)
        self.set_egress_policy = _aio(self._set_egress_policy)
        self.fs = SimpleNamespace(write=_aio(self._write), read_stream=self._read_stream)

    @staticmethod
    def size_for(*, cpus: float, memory_gib: float | None, disk_gib: float | None) -> SimpleNamespace:
        """Fit a fake size.

        Args:
            cpus: The vCPUs.
            memory_gib: The memory, or ``None``.
            disk_gib: The disk, or ``None``.

        Returns:
            The size fit.
        """
        CLOUD.size_calls.append({"cpus": cpus, "memory_gib": memory_gib, "disk_gib": disk_gib})
        return SimpleNamespace(size=f"cpu-{cpus}", memory_limit_gib=memory_gib or 2, disk_limit_gib=disk_gib or 10)

    @staticmethod
    async def _create(**params: Any) -> FakeSailbox:
        """Create a box, after the delay that the fake cloud sets.

        Args:
            **params: The create parameters.

        Returns:
            The Sailbox handle.
        """
        CLOUD.create_calls += 1
        CLOUD.create_started.set()
        if CLOUD.create_error is not None:
            raise CLOUD.create_error
        if CLOUD.create_delay:
            await asyncio.sleep(CLOUD.create_delay)
        box = BoxState(id=f"sb-{next(CLOUD.ids)}", params=params, egress=params["egress_policy"])
        CLOUD.boxes.append(box)
        return FakeSailbox(box)

    async def _run(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        timeout: int | None,  # noqa: ASYNC109 - the SDK method has this name
        user: str | None,
        output_buffer_bytes: int,
    ) -> SimpleNamespace:
        """Run a command in the shell model.

        Args:
            argv: The argv.
            env: The command environment.
            timeout: The SDK time limit.
            user: The user, or ``None``.
            output_buffer_bytes: The output buffer size.

        Returns:
            The run result.
        """
        CLOUD.runs.append({"argv": argv, "env": env, "timeout": timeout, "user": user})
        if CLOUD.exec_hangs:
            await asyncio.Event().wait()
        code, out, err = CLOUD.run(self._box, argv, env)
        return SimpleNamespace(
            exit_code=code,
            stdout=out,
            stderr=err,
            timed_out=CLOUD.exec_times_out,
            stdout_truncated=CLOUD.exec_truncates,
            stderr_truncated=False,
        )

    async def _write(self, path: str, data: Any) -> None:
        """Write a file whose parent directory exists.

        Args:
            path: The absolute path.
            data: A binary file object.

        Raises:
            FakeSailError: The parent directory does not exist.
        """
        if str(PurePosixPath(path).parent) not in self._box.dirs:
            raise FakeSailError("no parent directory")
        self._box.files[path] = data.read()

    @contextlib.asynccontextmanager
    async def _read_stream(self, path: str) -> AsyncIterator[FakeStream]:
        """Open a file as a byte stream.

        Args:
            path: The absolute path.

        Yields:
            The stream.

        Raises:
            FileNotFoundError: The file does not exist, unless the fake cloud
                sets another error.
            FakeSailError: The fake cloud says reads fail.
        """
        if CLOUD.read_error is FakeSailError:
            raise FakeSailError(path)
        if path not in self._box.files:
            raise CLOUD.read_error(path)
        yield FakeStream(self._box.files[path])

    async def _terminate(self) -> None:
        """Terminate the box, or fail when the fake cloud says so.

        Raises:
            FakeSailError: The fake cloud says this box fails to terminate.
        """
        if self.sailbox_id in CLOUD.terminate_fails:
            raise FakeSailError("terminate failed")
        self._box.terminated = True

    async def _set_egress_policy(self, policy: Any) -> None:
        """Change the egress policy.

        Args:
            policy: The policy document.
        """
        self._box.egress = policy


FakeSailbox.create = _aio(FakeSailbox._create)


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    """Swap the Sail SDK for the fake and reset the fake cloud.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fresh fake cloud.
    """
    global CLOUD
    CLOUD = FakeCloud()
    fakes = {
        "App": FakeApp,
        "Config": FakeConfig,
        "EgressPolicy": FakeEgressPolicy,
        "Image": FakeImage,
        "Sailbox": FakeSailbox,
    }
    for name, value in fakes.items():
        monkeypatch.setattr(sail, name, value)
    monkeypatch.setattr(sail, "STOP_TIMEOUT", 1.0)
    monkeypatch.setattr(sail, "_JOB_BOXES", {})
    monkeypatch.setattr(sail, "_LATE_TASKS", set())
    return CLOUD


class TestSailEnvironmentContract(SandboxEnvironmentContract):
    """Run the shared provider contract against the fake Sail cloud."""

    provider_cls = SailEnvironment


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


def test_sail_is_not_allowed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the Sail provider in production before its import (D1)."""
    assert factory.PROVIDERS["sail"].production_allowed is False
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="sail"):
        factory.load_provider("sail")


def test_factory_loads_sail_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the Sail class from the registry outside production."""
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("sail") is SailEnvironment


def test_the_default_sandbox_backend_stays_legacy() -> None:
    """Keep the legacy backend as the default."""
    assert type(settings).model_fields["sandbox_backend"].default == "legacy"


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the extra when the SDK is not installed."""
    monkeypatch.setattr(sail, "Sailbox", None)
    with pytest.raises(SandboxFatalError, match="`sail` extra"):
        SailEnvironment.preflight()


async def test_start_needs_credentials(fake_sdk: FakeCloud) -> None:
    """Refuse to start, with no create call, when Sail finds no credentials."""
    fake_sdk.no_auth = True
    with pytest.raises(SandboxFatalError, match="credentials"):
        await SailEnvironment(_spec()).start()
    assert fake_sdk.create_calls == 0


async def test_create_sends_the_name_image_lifetime_and_open_egress(fake_sdk: FakeCloud) -> None:
    """Create one box in the Skynet app with the spec name, a lifetime and open egress."""
    async with SailEnvironment(_spec(name="box-a", lifetime_seconds=300.5), timeouts=_timeouts(start=12.2)):
        pass
    assert fake_sdk.apps == [(sail.APP_NAME, True)]
    params = fake_sdk.boxes[0].params
    assert params["name"] == "box-a"
    assert params["image"].ref == sail.DEFAULT_IMAGE == "python:3.12.15-slim"
    assert params["max_lifetime_seconds"] == 301
    assert params["image_build_timeout"] == 13
    assert params["egress_policy"] == {"mode": "all"}
    assert fake_sdk.boxes[0].terminated


async def test_create_uses_the_spec_image_and_no_build_limit_without_timeouts(fake_sdk: FakeCloud) -> None:
    """Boot the spec image, and send no build limit when no start limit exists."""
    async with SailEnvironment(_spec(image="ghcr.io/acme/tool:1")):
        pass
    params = fake_sdk.boxes[0].params
    assert params["image"].ref == "ghcr.io/acme/tool:1"
    assert "image_build_timeout" not in params


async def test_sizes_map_to_size_for(fake_sdk: FakeCloud) -> None:
    """Send the spec vCPUs, memory and disk in GiB to the size fit."""
    resources = ResourceRequest(cpus=2, memory_mb=4096, disk_mb=20480)
    async with SailEnvironment(_spec(resources=resources)):
        pass
    assert fake_sdk.size_calls == [{"cpus": 2, "memory_gib": 4.0, "disk_gib": 20.0}]
    params = fake_sdk.boxes[0].params
    assert (params["size"], params["memory_limit_gib"], params["disk_limit_gib"]) == ("cpu-2", 4.0, 20.0)


async def test_ignore_mode_sends_no_size(fake_sdk: FakeCloud) -> None:
    """Send no size arguments when the spec ignores sizes."""
    async with SailEnvironment(_spec(resources=ResourceRequest(cpus=1, mode=ResourceMode.IGNORE))):
        pass
    assert fake_sdk.size_calls == []
    assert "size" not in fake_sdk.boxes[0].params


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"network_disabled": True}, {"mode": "none"}),
        (
            {"allowed_hosts": ("a.example.com", "a.example.com", "*.b.example.com")},
            {"mode": "allow", "hosts": ["a.example.com", "*.b.example.com"]},
        ),
    ],
)
async def test_the_spec_network_maps_to_the_create_egress(
    fake_sdk: FakeCloud, overrides: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Set the egress policy at create, so it covers the box from its first second."""
    async with SailEnvironment(_spec(**overrides)):
        pass
    assert fake_sdk.boxes[0].params["egress_policy"] == expected


async def test_set_network_policy_changes_the_box_egress(fake_sdk: FakeCloud) -> None:
    """Send a changed policy to the running box."""
    async with SailEnvironment(_spec()) as env:
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("api.example.com",)))
        assert fake_sdk.boxes[0].egress == {"mode": "allow", "hosts": ["api.example.com"]}
        await env.disable_network()
        assert fake_sdk.boxes[0].egress == {"mode": "none"}


async def test_exec_sends_env_user_timeout_and_cwd(fake_sdk: FakeCloud) -> None:
    """Merge the spec env, run as root for sudo, and wrap the cwd."""
    async with SailEnvironment(_spec(env={"BASE": "1", "SHARED": "spec"}), timeouts=_timeouts(run=9.5)) as env:
        await env.exec("echo hi", cwd="/work dir", env={"SHARED": "call"}, sudo=True)
        await env.exec("true", timeout_sec=3)
    first, second = fake_sdk.runs
    assert first["env"] == {"BASE": "1", "SHARED": "call"}
    assert first["user"] == "root"
    assert first["timeout"] == 10
    assert first["argv"] == ["bash", "-c", "cd '/work dir' && exec bash -c 'echo hi'"]
    assert second["user"] is None
    assert second["timeout"] == 3


async def test_a_timed_out_command_raises(fake_sdk: FakeCloud) -> None:
    """Raise RunTimeoutError when Sail reports a timeout."""
    async with SailEnvironment(_spec()) as env:
        fake_sdk.exec_times_out = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true", timeout_sec=1)


async def test_a_timed_out_command_without_a_limit_raises(fake_sdk: FakeCloud) -> None:
    """Raise RunTimeoutError when Sail reports a timeout and no limit is set."""
    async with SailEnvironment(_spec()) as env:
        fake_sdk.exec_times_out = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true")


async def test_a_hung_run_call_raises_a_run_timeout(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """End a run call that the SDK never returns, after the limit and the grace."""
    monkeypatch.setattr(sail, "EXEC_GRACE", 0.01)
    async with SailEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true", timeout_sec=0.01)


async def test_truncated_output_logs_a_warning(fake_sdk: FakeCloud, caplog: pytest.LogCaptureFixture) -> None:
    """Warn when Sail kept only the end of the output."""
    async with SailEnvironment(_spec()) as env:
        fake_sdk.exec_truncates = True
        with caplog.at_level(logging.WARNING, logger=sail.__name__):
            assert (await env.exec("echo hi")).ok
    assert "last 64 MiB" in caplog.text


async def test_exec_uses_the_current_output_sink(fake_sdk: FakeCloud) -> None:
    """Send the output to the task sink when the call gives no sink."""
    chunks: list[tuple[str, str]] = []
    async with SailEnvironment(_spec()) as env:
        with use_output_sink(lambda stream, text: chunks.append((stream, text))):
            await env.exec("echo sink")
    assert chunks == [("stdout", "sink\n")]


async def test_download_probes_after_an_unknown_read_error(fake_sdk: FakeCloud) -> None:
    """Return None for a missing file, and raise for a file that exists."""
    async with SailEnvironment(_spec()) as env:
        await env.upload_files({"/tmp/here.txt": "x"})
        fake_sdk.read_error = FakeSailError
        assert await env.download_file("/tmp/gone.txt") is None
        with pytest.raises(SandboxInfraError, match="FakeSailError"):
            await env.download_file("/tmp/here.txt")


async def test_a_non_utf8_download_is_infra_without_the_bytes(fake_sdk: FakeCloud) -> None:
    """Map a file that is not UTF-8 to SandboxInfraError, with no file bytes in the message."""
    async with SailEnvironment(_spec()) as env:
        await env.upload_files({"/tmp/b.bin": "x"})
        env._sailbox._box.files["/tmp/b.bin"] = b"\xff\xfe secret"
        with pytest.raises(SandboxInfraError, match="not UTF-8") as info:
            await env.download_file("/tmp/b.bin")
    assert "secret" not in str(info.value)
    assert "xff" not in str(info.value)


async def test_a_create_error_is_infra_and_leaves_no_box(fake_sdk: FakeCloud) -> None:
    """Map an SDK create error to SandboxInfraError, with one create call."""
    fake_sdk.create_error = FakeSailError("quota")
    with pytest.raises(SandboxInfraError, match="FakeSailError"):
        await SailEnvironment(_spec()).start()
    assert fake_sdk.create_calls == 1
    assert fake_sdk.boxes == []


async def test_a_start_timeout_terminates_the_late_box(fake_sdk: FakeCloud) -> None:
    """Wait the grace for a create past the start limit, then terminate its box."""
    fake_sdk.create_delay = 0.2
    with pytest.raises(StartTimeoutError):
        await SailEnvironment(_spec(), timeouts=_timeouts(start=0.05)).start()
    assert [box.terminated for box in fake_sdk.boxes] == [True]


def _server_side_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the fake create act like a real cloud call.

    Once the request is sent, the server makes the box even when the client
    cancels its wait.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    server_work: set[asyncio.Future[Any]] = set()

    async def make_box(params: dict[str, Any]) -> FakeSailbox:
        """Make the box after the delay that the fake cloud sets.

        Args:
            params: The create parameters.

        Returns:
            The Sailbox handle.
        """
        await asyncio.sleep(CLOUD.create_delay)
        box = BoxState(id=f"sb-{next(CLOUD.ids)}", params=params, egress=params["egress_policy"])
        CLOUD.boxes.append(box)
        return FakeSailbox(box)

    async def create(**params: Any) -> FakeSailbox:
        """Send the create request and wait for the box.

        Args:
            **params: The create parameters.

        Returns:
            The Sailbox handle.
        """
        CLOUD.create_calls += 1
        CLOUD.create_started.set()
        server = asyncio.ensure_future(make_box(params))
        server_work.add(server)
        return await asyncio.shield(server)

    monkeypatch.setattr(FakeSailbox, "create", _aio(create))


async def _late_tasks_done() -> None:
    """Wait until every late create and its terminate call end."""
    async with asyncio.timeout(5):
        # A late create adds its terminate call to the set when it ends.
        while sail._LATE_TASKS:
            await asyncio.wait(set(sail._LATE_TASKS))
            await asyncio.sleep(0)


async def test_a_create_past_the_grace_is_terminated_when_it_returns(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a create that outlives the grace, and terminate its box when it arrives."""
    monkeypatch.setattr(sail, "CREATE_GRACE", 0.01)
    _server_side_create(monkeypatch)
    fake_sdk.create_delay = 0.2
    with pytest.raises(StartTimeoutError):
        await SailEnvironment(_spec(), timeouts=_timeouts(start=0.01)).start()
    assert fake_sdk.boxes == []
    await _late_tasks_done()
    assert [box.terminated for box in fake_sdk.boxes] == [True]


async def test_a_single_cancel_of_a_server_side_create_terminates_the_box(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminate the box after a start timeout, inside the grace."""
    _server_side_create(monkeypatch)
    fake_sdk.create_delay = 0.2
    with pytest.raises(StartTimeoutError):
        await SailEnvironment(_spec(), timeouts=_timeouts(start=0.05)).start()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    assert not sail._LATE_TASKS


async def test_a_stop_during_the_create_grace_terminates_the_box(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wait for the box in the grace when a stop comes, then terminate it."""
    _server_side_create(monkeypatch)
    fake_sdk.create_delay = 0.3
    env = SailEnvironment(_spec(tags={JOB_TAG: "job-grace"}), timeouts=_timeouts(start=0.05))
    starting = asyncio.ensure_future(env.start())
    await asyncio.sleep(0.15)
    await env.stop()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    with pytest.raises(StartTimeoutError):
        await starting
    assert sail._JOB_BOXES == {}


async def test_a_second_cancel_during_the_create_grace_terminates_the_box(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass the cancel on, and terminate the late box when the handle arrives."""
    _server_side_create(monkeypatch)
    fake_sdk.create_delay = 0.3
    starting = asyncio.ensure_future(SailEnvironment(_spec()).start())
    await fake_sdk.create_started.wait()
    starting.cancel()
    await asyncio.sleep(0.05)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    await _late_tasks_done()
    assert [box.terminated for box in fake_sdk.boxes] == [True]


async def test_a_stop_during_start_terminates_the_box(fake_sdk: FakeCloud) -> None:
    """End the start with SandboxInfraError and terminate the box made meanwhile."""
    fake_sdk.create_delay = 0.05
    env = SailEnvironment(_spec())
    starting = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="stopped during its start"):
        await starting
    assert [box.terminated for box in fake_sdk.boxes] == [True]


async def test_a_cancelled_start_terminates_the_box(fake_sdk: FakeCloud) -> None:
    """Pass the cancel on and terminate the box made meanwhile."""
    fake_sdk.create_delay = 0.05
    starting = asyncio.ensure_future(SailEnvironment(_spec()).start())
    await fake_sdk.create_started.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert [box.terminated for box in fake_sdk.boxes] == [True]


async def test_a_second_start_raises(fake_sdk: FakeCloud) -> None:
    """Refuse to start the same sandbox twice."""
    env = SailEnvironment(_spec())
    async with env:
        pass
    with pytest.raises(SandboxInfraError, match="already started"):
        await env.start()
    assert fake_sdk.create_calls == 1


async def test_stop_job_sandboxes_goes_on_after_a_failure(fake_sdk: FakeCloud) -> None:
    """Count only the terminated boxes, and try every box of the job."""
    envs = [SailEnvironment(_spec(tags={JOB_TAG: "job-1"})) for _ in range(2)]
    for env in envs:
        await env.start()
    fake_sdk.terminate_fails.add(fake_sdk.boxes[0].id)
    assert await SailEnvironment.stop_job_sandboxes("job-1") == 1
    assert [box.terminated for box in fake_sdk.boxes] == [False, True]
    assert sail._JOB_BOXES == {}
    assert await SailEnvironment.stop_job_sandboxes("job-1") == 0

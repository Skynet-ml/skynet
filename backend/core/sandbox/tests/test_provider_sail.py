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
import math
import shlex
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.runtime import UsagePendingError
from core.billing.sail_usage import SailUsageReservation, quote_sail_sandbox
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import sail
from core.sandbox.providers.sail import SailEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.telemetry import use_output_sink
from core.sandbox.timeouts import PhaseTimeouts

from .test_environment import SandboxEnvironmentContract


class FakeSailError(Exception):
    """Stand in for an SDK error whose class the provider does not know."""


class FakeApiError(Exception):
    """Stand in for an SDK error that carries the HTTP status of the call."""

    def __init__(self, status_code: int) -> None:
        """Keep the status.

        Args:
            status_code: The HTTP status that Sail answered.
        """
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class InvalidArgumentError(Exception):
    """Stand in for the SDK error of a call that the client refused before any request."""


class ImageBuildError(Exception):
    """Stand in for the SDK error of an image build that failed before the create request."""


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
    status: str = "running"
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
    create_makes_box: bool = False
    lost_box_status: str = "running"
    create_delay: float = 0.0
    list_error: BaseException | None = None
    exec_hangs: bool = False
    exec_times_out: bool = False
    exec_truncates: bool = False
    read_error: type[BaseException] = FileNotFoundError
    terminate_fails: set[str] = field(default_factory=set)
    terminate_started: asyncio.Event = field(default_factory=asyncio.Event)
    terminate_gate: asyncio.Event | None = None
    no_auth: bool = False
    limits: tuple[int, int, int] = (1, 16 * 1024, 32)
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
        self.name = box.params.get("name")
        self.status = box.status
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
        size = "s" if cpus <= 1 else "m" if cpus <= 4 else "l"
        return SimpleNamespace(
            size=size,
            memory_limit_gib=math.ceil(memory_gib) if memory_gib else None,
            disk_limit_gib=math.ceil(disk_gib) if disk_gib else None,
        )

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
        if CLOUD.create_delay:
            await asyncio.sleep(CLOUD.create_delay)
        if CLOUD.create_error is not None:
            if CLOUD.create_makes_box:
                # Sail made the box, but the answer that names it was lost.
                CLOUD.boxes.append(BoxState(id=f"sb-{next(CLOUD.ids)}", params=params, status=CLOUD.lost_box_status))
            raise CLOUD.create_error
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
        CLOUD.terminate_started.set()
        if CLOUD.terminate_gate is not None:
            await CLOUD.terminate_gate.wait()
        if self.sailbox_id in CLOUD.terminate_fails:
            raise FakeSailError("terminate failed")
        self._box.terminated = True

    async def _set_egress_policy(self, policy: Any) -> None:
        """Change the egress policy.

        Args:
            policy: The policy document.
        """
        self._box.egress = policy


async def _get(sailbox_id: str) -> SimpleNamespace:
    """Read the full fake box, with the limits that the fake cloud sets.

    Args:
        sailbox_id: The box id.

    Returns:
        The box id and its vCPU, memory and disk limits.
    """
    vcpus, memory_mib, disk_gib = CLOUD.limits
    return SimpleNamespace(sailbox_id=sailbox_id, vcpu_count=vcpus, memory_mib=memory_mib, state_disk_size_gib=disk_gib)


async def _list(*, name: str) -> list[FakeSailbox]:
    """List the fake boxes with the name, or fail as the fake cloud sets.

    Args:
        name: The whole box name.

    Returns:
        The handles of the boxes with that name.

    Raises:
        BaseException: The list error that the fake cloud sets.
    """
    if CLOUD.list_error is not None:
        raise CLOUD.list_error
    return [FakeSailbox(box) for box in CLOUD.boxes if box.params.get("name") == name]


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
    # The SDK's class-level calls, which the fake class does not declare.
    for name, value in {"create": _aio(FakeSailbox._create), "get": _aio(_get), "list": _aio(_list)}.items():
        monkeypatch.setattr(FakeSailbox, name, value, raising=False)
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
    """Refuse the Sail provider in production before its import (D1).

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    assert factory.PROVIDERS["sail"].production_allowed is False
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="sail"):
        factory.load_provider("sail")


def test_factory_loads_sail_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the Sail class from the registry outside production.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("sail") is SailEnvironment


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the extra when the SDK is not installed.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(sail, "Sailbox", None)
    with pytest.raises(SandboxFatalError, match="`sail` extra"):
        SailEnvironment.preflight()


async def test_start_needs_credentials(fake_sdk: FakeCloud) -> None:
    """Refuse to start, with no create call, when Sail finds no credentials.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.no_auth = True
    with pytest.raises(SandboxFatalError, match="credentials"):
        await SailEnvironment(_spec()).start()
    assert fake_sdk.create_calls == 0


async def test_create_sends_the_name_image_lifetime_and_open_egress(fake_sdk: FakeCloud) -> None:
    """Create one box in the Skynet app with the spec name, a lifetime and open egress.

    Args:
        fake_sdk: The fake Sail cloud.
    """
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
    """Boot the spec image, and send no build limit when no start limit exists.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    async with SailEnvironment(_spec(image="ghcr.io/acme/tool:1")):
        pass
    params = fake_sdk.boxes[0].params
    assert params["image"].ref == "ghcr.io/acme/tool:1"
    assert "image_build_timeout" not in params


async def test_sizes_map_to_size_for(fake_sdk: FakeCloud) -> None:
    """Send the spec vCPUs, memory and disk in GiB to the size fit.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    resources = ResourceRequest(cpus=2, memory_mb=4096, disk_mb=20480)
    async with SailEnvironment(_spec(resources=resources)):
        pass
    assert fake_sdk.size_calls == [{"cpus": 2, "memory_gib": 4.0, "disk_gib": 20.0}]
    params = fake_sdk.boxes[0].params
    assert (params["size"], params["memory_limit_gib"], params["disk_limit_gib"]) == ("m", 4, 20)


async def test_ignore_mode_sends_no_size(fake_sdk: FakeCloud) -> None:
    """Send no size arguments when the spec ignores sizes.

    Args:
        fake_sdk: The fake Sail cloud.
    """
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
        (
            {"allowed_hosts": ("2001:db8::1", "192.0.2.10")},
            {"mode": "allow", "hosts": ["2001:db8::1", "192.0.2.10"]},
        ),
    ],
)
async def test_the_spec_network_maps_to_the_create_egress(
    fake_sdk: FakeCloud, overrides: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Set the egress policy at create, so it covers the box from its first second.

    Args:
        fake_sdk: The fake Sail cloud.
        overrides: The spec network fields.
        expected: The egress policy document that the create gets.
    """
    async with SailEnvironment(_spec(**overrides)):
        pass
    assert fake_sdk.boxes[0].params["egress_policy"] == expected


async def test_set_network_policy_changes_the_box_egress(fake_sdk: FakeCloud) -> None:
    """Send a changed policy to the running box.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    async with SailEnvironment(_spec()) as env:
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("api.example.com",)))
        assert fake_sdk.boxes[0].egress == {"mode": "allow", "hosts": ["api.example.com"]}
        await env.disable_network()
        assert fake_sdk.boxes[0].egress == {"mode": "none"}


async def test_exec_sends_env_user_timeout_and_cwd(fake_sdk: FakeCloud) -> None:
    """Merge the spec env, run as root for sudo, and wrap the cwd.

    Args:
        fake_sdk: The fake Sail cloud.
    """
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


async def test_a_timed_out_command_returns_a_timed_out_result(fake_sdk: FakeCloud) -> None:
    """Return a timed-out result when Sail reports a timeout.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    async with SailEnvironment(_spec()) as env:
        fake_sdk.exec_times_out = True
        result = await env.exec("true", timeout_sec=1)
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_a_timed_out_command_without_a_limit_returns_a_timed_out_result(fake_sdk: FakeCloud) -> None:
    """Return a timed-out result when Sail reports a timeout and no limit is set.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    async with SailEnvironment(_spec()) as env:
        fake_sdk.exec_times_out = True
        result = await env.exec("true")
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_a_hung_run_call_returns_a_timed_out_result(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """End a run call that the SDK never returns, after the limit and the grace.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(sail, "EXEC_GRACE", 0.01)
    async with SailEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        result = await env.exec("true", timeout_sec=0.01)
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_truncated_output_logs_a_warning(fake_sdk: FakeCloud, caplog: pytest.LogCaptureFixture) -> None:
    """Warn when Sail kept only the end of the output.

    Args:
        fake_sdk: The fake Sail cloud.
        caplog: Captures the provider log.
    """
    async with SailEnvironment(_spec()) as env:
        fake_sdk.exec_truncates = True
        with caplog.at_level(logging.WARNING, logger=sail.__name__):
            assert (await env.exec("echo hi")).ok
    assert "last 64 MiB" in caplog.text


async def test_exec_uses_the_current_output_sink(fake_sdk: FakeCloud) -> None:
    """Send the output to the task sink when the call gives no sink.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    chunks: list[tuple[str, str]] = []
    async with SailEnvironment(_spec()) as env:
        with use_output_sink(lambda stream, text: chunks.append((stream, text))):
            await env.exec("echo sink")
    assert chunks == [("stdout", "sink\n")]


async def test_download_probes_after_an_unknown_read_error(fake_sdk: FakeCloud) -> None:
    """Return None for a missing file, and raise for a file that exists.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    async with SailEnvironment(_spec()) as env:
        await env.upload_files({"/tmp/here.txt": "x"})
        fake_sdk.read_error = FakeSailError
        assert await env.download_file("/tmp/gone.txt") is None
        with pytest.raises(SandboxInfraError, match="FakeSailError"):
            await env.download_file("/tmp/here.txt")


async def test_a_non_utf8_download_is_infra_without_the_bytes(fake_sdk: FakeCloud) -> None:
    """Map a file that is not UTF-8 to SandboxInfraError, with no file bytes in the message.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    async with SailEnvironment(_spec()) as env:
        await env.upload_files({"/tmp/b.bin": "x"})
        env._sailbox._box.files["/tmp/b.bin"] = b"\xff\xfe secret"
        with pytest.raises(SandboxInfraError, match="not UTF-8") as info:
            await env.download_file("/tmp/b.bin")
    assert "secret" not in str(info.value)
    assert "xff" not in str(info.value)


def test_mapped_turns_an_sdk_error_into_infra() -> None:
    """Map an unknown SDK error to SandboxInfraError that names the action and keeps the cause."""
    with (
        pytest.raises(SandboxInfraError, match="Sail could not run a step: FakeSailError") as info,
        sail._mapped("run a step"),
    ):
        raise FakeSailError("detail")
    assert isinstance(info.value.__cause__, FakeSailError)
    assert "detail" not in str(info.value)


def test_mapped_keeps_a_sandbox_error_unchanged() -> None:
    """Let a sandbox error pass through the mapping as the same object."""
    error = SandboxFatalError("already mapped")
    with pytest.raises(SandboxFatalError) as info, sail._mapped("run a step"):
        raise error
    assert info.value is error


def test_mapped_does_not_catch_a_cancel() -> None:
    """Let a cancel pass through the mapping, so it is never turned into an infra error."""
    with pytest.raises(asyncio.CancelledError), sail._mapped("run a step"):
        raise asyncio.CancelledError


async def test_a_refused_egress_policy_is_infra(fake_sdk: FakeCloud) -> None:
    """Map an SDK error from a policy change to SandboxInfraError.

    Args:
        fake_sdk: The fake Sail cloud.
    """

    async def refuse(policy: Any) -> None:
        """Refuse every policy change.

        Args:
            policy: The egress policy.

        Raises:
            FakeSailError: Always.
        """
        raise FakeSailError("refused")

    async with SailEnvironment(_spec()) as env:
        env._sailbox.set_egress_policy = _aio(refuse)
        with pytest.raises(SandboxInfraError, match="change the egress policy: FakeSailError"):
            await env.set_network_policy(NetworkPolicy(allowed_hosts=("api.example.com",)))


async def test_a_create_error_is_infra_and_leaves_no_box(fake_sdk: FakeCloud) -> None:
    """Map an SDK create error to SandboxInfraError, with one create call.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_error = FakeSailError("quota")
    with pytest.raises(SandboxInfraError, match="FakeSailError"):
        await SailEnvironment(_spec()).start()
    assert fake_sdk.create_calls == 1
    assert fake_sdk.boxes == []


async def test_a_start_timeout_terminates_the_late_box(fake_sdk: FakeCloud) -> None:
    """Wait the grace for a create past the start limit, then terminate its box.

    Args:
        fake_sdk: The fake Sail cloud.
    """
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
    """Keep a create that outlives the grace, and terminate its box when it arrives.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
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
    """Terminate the box after a start timeout, inside the grace.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    _server_side_create(monkeypatch)
    fake_sdk.create_delay = 0.2
    with pytest.raises(StartTimeoutError):
        await SailEnvironment(_spec(), timeouts=_timeouts(start=0.05)).start()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    assert not sail._LATE_TASKS


async def test_a_stop_during_the_create_grace_terminates_the_box(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wait for the box in the grace when a stop comes, then terminate it.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
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
    """Pass the cancel on, and terminate the late box when the handle arrives.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
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
    """End the start with SandboxInfraError and terminate the box made meanwhile.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_delay = 0.05
    env = SailEnvironment(_spec())
    starting = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="stopped during its start"):
        await starting
    assert [box.terminated for box in fake_sdk.boxes] == [True]


async def test_a_cancelled_start_terminates_the_box(fake_sdk: FakeCloud) -> None:
    """Pass the cancel on and terminate the box made meanwhile.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_delay = 0.05
    starting = asyncio.ensure_future(SailEnvironment(_spec()).start())
    await fake_sdk.create_started.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert [box.terminated for box in fake_sdk.boxes] == [True]


async def test_a_second_start_raises(fake_sdk: FakeCloud) -> None:
    """Refuse to start the same sandbox twice.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env = SailEnvironment(_spec())
    async with env:
        pass
    with pytest.raises(SandboxInfraError, match="already started"):
        await env.start()
    assert fake_sdk.create_calls == 1


async def test_stop_job_sandboxes_goes_on_after_a_failure(fake_sdk: FakeCloud) -> None:
    """Count only the terminated boxes, and try every box of the job.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    envs = [SailEnvironment(_spec(tags={JOB_TAG: "job-1"})) for _ in range(2)]
    for env in envs:
        await env.start()
    fake_sdk.terminate_fails.add(fake_sdk.boxes[0].id)
    assert await SailEnvironment.stop_job_sandboxes("job-1") == 1
    assert [box.terminated for box in fake_sdk.boxes] == [False, True]
    assert sail._JOB_BOXES == {}
    assert await SailEnvironment.stop_job_sandboxes("job-1") == 0


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
        self.sailbox_id: str | None = None
        self.sailbox_name: str | None = None
        self.confirm_error: BaseException | None = None
        self.settle_error: BaseException | None = None
        self.spending: list[int] | None = None
        self.spend_gate: asyncio.Event | None = None
        self.budgets: list[float | None] = []
        self.release_entered = threading.Event()
        self.release_gate: threading.Event | None = None

    def begin_create(self, name: str) -> None:
        """Record the create call, with the boxes that existed before it.

        Args:
            name: The unique name of the box.
        """
        self.sailbox_name = name
        self.calls.append(("begin_create", CLOUD.create_calls))

    def bind(self, sailbox_id: str) -> None:
        """Record the box identity.

        Args:
            sailbox_id: The Sail id of the box.
        """
        self.sailbox_id = sailbox_id
        self.calls.append(("bind", sailbox_id))

    def confirm_created(self, sailbox: Any) -> None:
        """Run the real bind and envelope check, record the checked box, or fail as the test sets.

        Args:
            sailbox: The full fake box.

        Raises:
            BaseException: The error that the test sets.
            UsagePendingError: The box is larger than the funded size.
        """
        try:
            # The real check, so the fake size fit must name a size that Skynet funds.
            SailUsageReservation.confirm_created(self, sailbox)  # type: ignore[arg-type]
        finally:
            self.calls.append(("confirm", sailbox.memory_mib))
        if self.confirm_error is not None:
            raise self.confirm_error

    def release(self, *, refused: bool = False) -> None:
        """Record a release, and whether Sail refused the create.

        Args:
            refused: True when the provider saw a refused create.
        """
        self.release_entered.set()
        if self.release_gate is not None:
            self.release_gate.wait(5)
        self.calls.append(("release", refused))

    def pending(self) -> None:
        """Record a pending hold."""
        self.calls.append(("pending", None))

    async def final_spend(self, budget: float | None = None) -> dict[str, Any]:
        """Wait a moment for the spend, or fail as the test sets.

        Args:
            budget: The seconds that the teardown can still wait.

        Returns:
            A fake spend line.

        Raises:
            BaseException: The error that the test sets.
        """
        self.budgets.append(budget)
        if self.spend_gate is not None:
            await self.spend_gate.wait()
        if self.spending is not None:
            # Shared by the boxes of one test, to see how many waits overlap.
            self.spending[0] += 1
            self.spending[1] = max(self.spending[1], self.spending[0])
            await asyncio.sleep(0.02)
            self.spending[0] -= 1
        if self.settle_error is not None:
            raise self.settle_error
        return {"sailbox_id": self.sailbox_id}

    def settle_spend(self, spend: dict[str, Any]) -> None:
        """Record a settle, with the box state at that time.

        Args:
            spend: The spend line from ``final_spend``.
        """
        self.calls.append(("settle", [box.terminated for box in CLOUD.boxes]))

    def names(self) -> list[str]:
        """Return the names of the recorded calls.

        Returns:
            The call names in order.
        """
        return [name for name, _ in self.calls]


def _protected(**spec: Any) -> tuple[SailEnvironment, list[FakeUsage]]:
    """Open a protected box whose reservations are fakes.

    Args:
        **spec: Spec fields to replace.

    Returns:
        The box and the list that gets its reservation.
    """
    made: list[FakeUsage] = []

    def factory_(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make and keep one fake reservation.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        assert budget == "budget"
        made.append(FakeUsage(request, operation_key))
        return made[-1]

    env = SailEnvironment(_spec(operation_key="box-1", **spec), budget="budget", usage_factory=factory_)
    return env, made


def test_sail_is_billable_and_has_a_usage_adapter() -> None:
    """Let hosted Skynet meter Sail: the provider is billable and its adapter exists."""
    assert sail.SAIL_CAPABILITIES.billable is True
    assert usage_adapter("sail") is not None
    assert factory.PROVIDERS["sail"].production_allowed is False


def test_a_protected_box_needs_an_operation_key() -> None:
    """Refuse a budget without an operation key, before any call."""
    with pytest.raises(SandboxSpecError, match="operation key"):
        SailEnvironment(_spec(), budget="budget")


async def test_a_protected_box_reserves_before_the_create_and_settles_after_the_terminate(
    fake_sdk: FakeCloud,
) -> None:
    """Reserve the whole lifetime first, confirm the created box, and settle once it terminated.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected(lifetime_seconds=300.5)
    async with env:
        pass
    usage = made[0]
    assert usage.operation_key == "box-1"
    assert usage.request["lifetime_ms"] == 301_000
    assert usage.request["size"] == "s"
    assert usage.request["network_disabled"] is False
    assert usage.calls == [
        ("begin_create", 0),
        ("bind", "sb-1"),
        ("confirm", 16 * 1024),
        ("settle", [True]),
    ]


async def test_a_box_over_its_funding_is_terminated_and_settled(fake_sdk: FakeCloud) -> None:
    """End the start when the box is larger than its funding, and still settle the box that exists.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.limits = (1, 32 * 1024, 32)
    env, made = _protected()
    with pytest.raises(UsagePendingError, match="inside its funding"):
        await env.start()
    await env.stop()
    assert made[0].names() == ["begin_create", "bind", "confirm", "settle"]
    assert [box.terminated for box in fake_sdk.boxes] == [True]


async def test_a_failed_create_releases_the_reservation(fake_sdk: FakeCloud) -> None:
    """Let the reservation release or keep its hold when the create returns no box.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_error = FakeSailError("quota")
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    # An unknown SDK error can come after Sail made the box, so the hold is not freed.
    assert made[0].calls[-1] == ("release", False)


@pytest.mark.parametrize(
    "error",
    [
        FakeApiError(402),
        FakeApiError(403),
        FakeApiError(413),
        InvalidArgumentError("bad size"),
        ImageBuildError("build failed"),
    ],
)
async def test_a_refused_create_frees_the_reservation(fake_sdk: FakeCloud, error: BaseException) -> None:
    """Free the hold when Sail or the SDK refused the create and no box has the name.

    Args:
        fake_sdk: The fake Sail cloud.
        error: The refusal of the create.
    """
    fake_sdk.create_error = error
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert made[0].calls[-1] == ("release", True)


@pytest.mark.parametrize("error", [FakeApiError(502), FakeApiError(429), FakeApiError(409), TypeError("bad row")])
async def test_an_unclear_create_error_keeps_the_hold(fake_sdk: FakeCloud, error: BaseException) -> None:
    """Keep the hold for a 5xx, a 429, a 409 or a TypeError, because Sail may have made the box.

    Args:
        fake_sdk: The fake Sail cloud.
        error: The final create error.
    """
    fake_sdk.create_error = error
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert made[0].calls[-1] == ("release", False)


@pytest.mark.parametrize(
    "error", [FakeApiError(429), FakeApiError(403), FakeApiError(409), TypeError("bad answer row")]
)
async def test_a_failed_create_that_made_a_box_is_terminated_and_settled(
    fake_sdk: FakeCloud, error: BaseException
) -> None:
    """Find the box of a failed create by its name, terminate it and settle it.

    A retry can get a 429 after an earlier attempt made the box, and reading
    the answer can raise a TypeError after Sail made the box.

    Args:
        fake_sdk: The fake Sail cloud.
        error: The final create error.
    """
    fake_sdk.create_error = error
    fake_sdk.create_makes_box = True
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    await env.stop()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    assert made[0].calls[-2:] == [("bind", "sb-1"), ("settle", [True])]


async def test_a_failed_lookup_after_a_refused_create_keeps_the_hold(fake_sdk: FakeCloud) -> None:
    """Keep the hold, and raise it from the next stop, when Sail cannot list the boxes.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_error = FakeApiError(403)
    fake_sdk.list_error = FakeSailError("list down")
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert made[0].names() == ["begin_create", "pending"]
    with pytest.raises(UsagePendingError, match="could not list"):
        await env.stop()


async def test_a_cancelled_protected_start_settles_the_box(fake_sdk: FakeCloud) -> None:
    """Terminate the box of a cancelled start, then settle it.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_delay = 0.05
    env, made = _protected()
    starting = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    await env.stop()
    assert made[0].names() == ["begin_create", "bind", "settle"]
    assert made[0].calls[-1] == ("settle", [True])


async def test_a_settle_failure_does_not_hide_the_start_error(fake_sdk: FakeCloud) -> None:
    """Raise the start error from a failed start, and keep the settle failure for the next stop.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected()

    def failing(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a fake reservation whose confirm and settle fail.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        made.append(FakeUsage(request, operation_key))
        made[-1].confirm_error = SandboxInfraError("confirm failed")
        made[-1].settle_error = RuntimeError("spend API down")
        return made[-1]

    env._usage_factory = failing
    with pytest.raises(SandboxInfraError, match="confirm failed"):
        await env.start()
    with pytest.raises(UsagePendingError, match="not yet settled"):
        await env.stop()
    assert made[0].names()[-1] == "pending"
    await env.stop()


async def test_a_settle_failure_does_not_hide_a_cancel(fake_sdk: FakeCloud) -> None:
    """Let a cancelled start end as a cancel even when its settle fails.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_delay = 0.05
    env, made = _protected()
    starting = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    made[0].settle_error = UsagePendingError("late spend")
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    with pytest.raises(UsagePendingError, match="late spend"):
        await env.stop()


async def test_a_box_that_did_not_terminate_stays_pending(fake_sdk: FakeCloud) -> None:
    """Keep the hold pending, and raise it from stop, when the terminate fails.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected()
    await env.start()
    fake_sdk.terminate_fails.add(fake_sdk.boxes[0].id)
    with pytest.raises(UsagePendingError, match="did not terminate"):
        await env.stop()
    assert made[0].names()[-1] == "pending"
    assert "settle" not in made[0].names()


async def test_a_settle_failure_is_raised_once(fake_sdk: FakeCloud) -> None:
    """Raise a failed settle from the first stop only, as a pending usage error.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected()
    await env.start()
    made[0].settle_error = RuntimeError("spend API down")
    with pytest.raises(UsagePendingError, match="not yet settled") as raised:
        await env.stop()
    assert isinstance(raised.value.__cause__, RuntimeError)
    await env.stop()
    assert made[0].names() == ["begin_create", "bind", "confirm", "pending"]


async def test_the_job_sweep_goes_on_after_a_pending_settle(fake_sdk: FakeCloud) -> None:
    """Keep sweeping the job when one protected box cannot settle.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    first, made_first = _protected(tags={JOB_TAG: "job-1"})
    second, _ = _protected(tags={JOB_TAG: "job-1"}, name="box-b")
    for env in (first, second):
        await env.start()
    made_first[0].settle_error = UsagePendingError("late spend")
    assert await SailEnvironment.stop_job_sandboxes("job-1") == 2
    assert [box.terminated for box in fake_sdk.boxes] == [True, True]


async def test_the_job_sweep_waits_for_all_spends_at_the_same_time(fake_sdk: FakeCloud) -> None:
    """Stop the job's boxes together, so their minutes-long spend waits overlap.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    envs = [_protected(tags={JOB_TAG: "job-2"}, name=f"box-{index}") for index in range(3)]
    spending = [0, 0]
    for env, made in envs:
        await env.start()
        made[0].spending = spending
    assert await SailEnvironment.stop_job_sandboxes("job-2") == 3
    assert spending[1] == 3
    assert all(made[0].names()[-1] == "settle" for _, made in envs)


async def test_a_failed_start_returns_before_the_spend_wait_ends(fake_sdk: FakeCloud) -> None:
    """Raise the start error once the box is gone, and settle the box in the next stop.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected()

    def gated(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a fake reservation whose confirm fails and whose spend waits on a gate.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        made.append(FakeUsage(request, operation_key))
        made[-1].confirm_error = SandboxInfraError("confirm failed")
        return made[-1]

    env._usage_factory = gated
    gate = asyncio.Event()
    original = FakeUsage.final_spend

    async def waiting(self: FakeUsage, budget: float | None = None) -> dict[str, Any]:
        """Wait for the gate, then return the fake spend.

        Args:
            self: The fake reservation.
            budget: The seconds that the teardown can still wait.

        Returns:
            The fake spend line.
        """
        await gate.wait()
        return await original(self, budget)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeUsage, "final_spend", waiting)
        with pytest.raises(SandboxInfraError, match="confirm failed"):
            await env.start()
        assert [box.terminated for box in fake_sdk.boxes] == [True]
        assert "settle" not in made[0].names()
        gate.set()
        await env.stop()
    assert made[0].calls[-1] == ("settle", [True])


async def test_a_stop_during_a_slow_reserve_releases_the_late_reservation(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wait for a reserve that outlasts the start grace, and release the hold it returns.

    The reserve wait shares ``CREATE_GRACE + STOP_TIMEOUT``, so the 60 s stop
    timeout keeps this stop waiting.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: Shortens the start grace.
    """
    monkeypatch.setattr(sail, "CREATE_GRACE", 0.01)
    entered = threading.Event()
    gate = threading.Event()
    made: list[FakeUsage] = []

    def slow(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a fake reservation after the test opens the gate.

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

    env = SailEnvironment(_spec(operation_key="box-1"), budget="budget", usage_factory=slow)
    starting = asyncio.ensure_future(env.start())
    try:
        await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.ensure_future(env.stop())
        await asyncio.sleep(0.1)
        assert not stopping.done()
    finally:
        gate.set()
    await stopping
    with pytest.raises(SandboxInfraError, match="stopped during its start"):
        await starting
    assert made[0].calls == [("release", False)]
    assert fake_sdk.create_calls == 0


async def test_a_reserve_that_outlives_the_teardown_releases_its_hold_later(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End the stop at the wait limit, and release the hold when the slow reserve returns.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: Shortens the start and reserve waits.
    """
    monkeypatch.setattr(sail, "CREATE_GRACE", 0.01)
    monkeypatch.setattr(sail, "STOP_TIMEOUT", 0.01)
    entered = threading.Event()
    gate = threading.Event()
    made: list[FakeUsage] = []

    def slow(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a fake reservation after the test opens the gate.

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

    env = SailEnvironment(_spec(operation_key="box-1"), budget="budget", usage_factory=slow)
    starting = asyncio.ensure_future(env.start())
    try:
        await asyncio.to_thread(entered.wait, 5)
        await asyncio.wait_for(env.stop(), 1)
        with pytest.raises(SandboxInfraError, match="stopped during its start"):
            await starting
        assert made == []
    finally:
        gate.set()
    for _ in range(100):
        if made and made[0].calls:
            break
        await asyncio.sleep(0.01)
    assert made[0].calls == [("release", False)]
    assert fake_sdk.create_calls == 0


async def test_the_spend_wait_gets_the_rest_of_the_teardown_budget(fake_sdk: FakeCloud) -> None:
    """Give the spend wait only the time that is left of the teardown budget.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected()
    await env.start()
    await env.stop()
    (budget,) = made[0].budgets
    assert budget is not None
    assert sail.TEARDOWN_SECONDS - 5 < budget <= sail.TEARDOWN_SECONDS


async def test_a_create_that_fails_in_its_grace_is_looked_up(fake_sdk: FakeCloud) -> None:
    """Find, terminate and settle the box of a cancelled create that then fails.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_delay = 0.05
    fake_sdk.create_error = FakeApiError(403)
    fake_sdk.create_makes_box = True
    env, made = _protected()
    starting = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    await env.stop()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    assert made[0].calls[-2:] == [("bind", "sb-1"), ("settle", [True])]


async def test_a_failed_bind_of_a_late_box_keeps_the_hold_pending(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record the hold as pending when the ledger write of a late box's identity fails.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: Makes the bind write fail.
    """

    def failing_bind(self: FakeUsage, sailbox_id: str) -> None:
        """Fail like a ledger that cannot write.

        Args:
            self: The fake reservation.
            sailbox_id: The Sail id of the box.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("ledger down")

    monkeypatch.setattr(FakeUsage, "bind", failing_bind)
    fake_sdk.create_delay = 0.05
    fake_sdk.create_error = FakeApiError(403)
    fake_sdk.create_makes_box = True
    env, made = _protected()
    starting = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    with pytest.raises(UsagePendingError, match="not yet settled"):
        await env.stop()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    assert made[0].calls[-1] == ("pending", None)


async def test_a_size_error_before_the_reserve_leaves_no_hold(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Map an SDK size error to a sandbox error before any hold or create.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: Makes the size lookup fail.
    """

    def bad_size(**limits: Any) -> SimpleNamespace:
        """Fail like the SDK's argument check.

        Args:
            **limits: The size limits.

        Raises:
            FakeApiError: Always.
        """
        raise FakeApiError(400)

    monkeypatch.setattr(FakeSailbox, "size_for", staticmethod(bad_size))
    env, made = _protected()
    with pytest.raises(SandboxInfraError, match="prepare the Sailbox"):
        await env.start()
    await env.stop()
    assert made == []
    assert fake_sdk.create_calls == 0


async def test_an_unfunded_size_is_a_spec_error(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """Map a quote that refuses the size to a spec error, with no hold and no create.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: Makes the size fit name a size that Skynet does not fund.
    """

    def unfunded(**limits: Any) -> SimpleNamespace:
        """Fit a size that Skynet does not fund.

        Args:
            **limits: The size limits.

        Returns:
            The size fit.
        """
        return SimpleNamespace(size="xl", memory_limit_gib=None, disk_limit_gib=None)

    def quoting(budget: Any, request: dict[str, Any], *, operation_key: str) -> None:
        """Price the request like the real reservation does.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.
        """
        quote_sail_sandbox(request)

    monkeypatch.setattr(FakeSailbox, "size_for", staticmethod(unfunded))
    env = SailEnvironment(_spec(operation_key="box-1"), budget="budget", usage_factory=quoting)
    with pytest.raises(SandboxSpecError, match="cannot fund"):
        await env.start()
    await env.stop()
    assert fake_sdk.create_calls == 0


async def test_a_failed_pending_call_does_not_hide_a_cancel(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the cancel of the spend wait when the pending write then fails.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: Makes the pending write fail.
    """

    def failing_pending(self: FakeUsage) -> None:
        """Record the call, then fail like a ledger that cannot write.

        Args:
            self: The fake reservation.

        Raises:
            RuntimeError: Always.
        """
        self.calls.append(("pending", None))
        raise RuntimeError("ledger down")

    monkeypatch.setattr(FakeUsage, "pending", failing_pending)
    env, made = _protected()
    await env.start()
    made[0].settle_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await env.stop()
    assert made[0].names().count("pending") == 1


@pytest.mark.parametrize("path", ["settle", "terminate"])
async def test_a_ledger_error_marks_the_hold_pending_once(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Call pending once when a ledger write fails after a pending call.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: Makes the ledger writes fail.
        path: ``settle`` for a failed settle, ``terminate`` for a box that did not terminate.
    """

    def failing_pending(self: FakeUsage) -> None:
        """Record the call, then fail like a ledger that cannot write.

        Args:
            self: The fake reservation.

        Raises:
            RuntimeError: Always.
        """
        self.calls.append(("pending", None))
        raise RuntimeError("ledger down")

    def failing_settle(self: FakeUsage, spend: dict[str, Any]) -> None:
        """Fail like the real settle, which marks the hold pending first.

        Args:
            self: The fake reservation.
            spend: The spend line.

        Raises:
            RuntimeError: Always.
        """
        self.calls.append(("pending", None))
        raise RuntimeError("ledger down")

    monkeypatch.setattr(FakeUsage, "pending", failing_pending)
    monkeypatch.setattr(FakeUsage, "settle_spend", failing_settle)
    env, made = _protected()
    await env.start()
    if path == "terminate":
        fake_sdk.terminate_fails.add(fake_sdk.boxes[0].id)
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert made[0].names().count("pending") == 1


async def test_a_cancelled_teardown_during_the_start_releases_a_late_hold(fake_sdk: FakeCloud) -> None:
    """Release the hold of a slow reserve when the teardown is cancelled while it waits for the start.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    entered = threading.Event()
    gate = threading.Event()
    made: list[FakeUsage] = []

    def slow(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a fake reservation after the test opens the gate.

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

    env = SailEnvironment(_spec(operation_key="box-1"), budget="budget", usage_factory=slow)
    starting = asyncio.ensure_future(env.start())
    try:
        await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.ensure_future(env.stop())
        # The first loop turn runs stop(), which makes the teardown task; the
        # second lets the teardown reach its wait for the start.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert env._stop_task is not None
        env._stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        with contextlib.suppress(BaseException):
            await starting
    finally:
        gate.set()
    for _ in range(100):
        if made and made[0].calls:
            break
        await asyncio.sleep(0.01)
    assert made[0].calls == [("release", False)]
    assert fake_sdk.create_calls == 0


async def test_the_create_names_the_box_in_its_hold(fake_sdk: FakeCloud) -> None:
    """Give the reservation the box name before the create, as a link when no id comes back.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected()
    await env.start()
    await env.stop()
    assert made[0].sailbox_name == env._name == fake_sdk.boxes[0].params["name"]


async def test_a_reserve_that_ends_with_the_cancel_still_releases_its_hold(fake_sdk: FakeCloud) -> None:
    """Release the hold when the teardown is cancelled at the moment the reserve returns.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    entered = threading.Event()
    gate = threading.Event()
    made: list[FakeUsage] = []

    def slow(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a fake reservation after the test opens the gate.

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

    def cancel_teardown(_: asyncio.Future[Any]) -> None:
        """Cancel the teardown before its wait sees the finished reserve.

        Args:
            _: The finished reserve call.
        """
        assert env._stop_task is not None
        env._stop_task.cancel()

    env = SailEnvironment(_spec(operation_key="box-1"), budget="budget", usage_factory=slow)
    starting = asyncio.ensure_future(env.start())
    try:
        await asyncio.to_thread(entered.wait, 5)
        billing = env._billing_call
        assert billing is not None
        # Added before the teardown waits, so this callback runs first.
        billing.add_done_callback(cancel_teardown)
        stopping = asyncio.ensure_future(env.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()
    finally:
        gate.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    with contextlib.suppress(BaseException):
        await starting
    for _ in range(100):
        if made and made[0].calls:
            break
        await asyncio.sleep(0.01)
    assert made[0].calls == [("release", False)]
    assert fake_sdk.create_calls == 0


@pytest.mark.parametrize(("status", "settles"), [("terminated", True), ("failed", False)])
async def test_only_a_terminated_box_settles_when_its_terminate_fails(
    fake_sdk: FakeCloud, status: str, settles: bool
) -> None:
    """Settle a lost box that Sail lists as terminated, and keep a failed one pending.

    A failed box may still hold billable state disk, so its terminate must succeed.

    Args:
        fake_sdk: The fake Sail cloud.
        status: The status that Sail lists for the lost box.
        settles: True when the usage settles.
    """
    fake_sdk.create_error = FakeApiError(500)
    fake_sdk.create_makes_box = True
    fake_sdk.lost_box_status = status
    fake_sdk.terminate_fails.add("sb-1")
    env, made = _protected()
    with pytest.raises(SandboxInfraError, match="create the Sailbox"):
        await env.start()
    if settles:
        await env.stop()
        assert made[0].calls[-2:] == [("bind", "sb-1"), ("settle", [False])]
    else:
        with pytest.raises(UsagePendingError, match="did not terminate"):
            await env.stop()
        assert made[0].calls[-1] == ("pending", None)
        assert "settle" not in made[0].names()


async def test_a_reserve_that_ends_with_the_cancel_of_the_start_wait_releases_its_hold(
    fake_sdk: FakeCloud,
) -> None:
    """Release the hold when the teardown is cancelled in its start wait as the reserve returns.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    entered = threading.Event()
    gate = threading.Event()
    made: list[FakeUsage] = []

    def slow(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Block until the test opens the gate, then reserve.

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

    env = SailEnvironment(_spec(operation_key="box-1"), budget="budget", usage_factory=slow)
    starting = asyncio.ensure_future(env.start())
    try:
        await asyncio.to_thread(entered.wait, 5)
        create = env._starting
        assert create is not None
        assert env._billing_call is not None
        # A create in its grace gets no cancel from the teardown, so the
        # teardown stays in its start wait until the reserve returns.
        env._settling = True
        stopping = asyncio.ensure_future(env.stop())
        await asyncio.sleep(0)
        assert env._stop_task is not None
        teardown = env._stop_task
        reserved = asyncio.Event()

        def cancel_teardown(_: asyncio.Future[Any]) -> None:
            """Cancel the teardown in the loop turn where the reserve returns.

            Args:
                _: The finished reserve call.
            """
            # The create would take the hold on its next step; the cancel
            # stops that, so only the teardown can release the hold.
            create.cancel()
            teardown.cancel()
            reserved.set()

        env._billing_call.add_done_callback(cancel_teardown)
        gate.set()
        await reserved.wait()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        with contextlib.suppress(BaseException):
            await starting
    finally:
        gate.set()
    for _ in range(100):
        if made and made[0].calls:
            break
        await asyncio.sleep(0.01)
    assert made[0].calls == [("release", False)]
    assert fake_sdk.create_calls == 0


async def test_a_cancel_during_the_terminate_keeps_the_hold_pending_once(fake_sdk: FakeCloud) -> None:
    """Mark the hold pending once, and pass the cancel on, when the teardown is cancelled in its terminate.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected()
    await env.start()
    fake_sdk.terminate_gate = asyncio.Event()
    stopping = asyncio.ensure_future(env.stop())
    await fake_sdk.terminate_started.wait()
    assert env._stop_task is not None
    env._stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert made[0].names().count("pending") == 1
    assert "settle" not in made[0].names()
    # A later stop terminates the box again but finds no usage to settle.
    fake_sdk.terminate_gate.set()
    await env.stop()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    assert made[0].names().count("pending") == 1
    assert "settle" not in made[0].names()


async def test_a_cancel_during_the_release_call_keeps_the_hold_pending_once(fake_sdk: FakeCloud) -> None:
    """Mark the hold pending once when the teardown is cancelled in its release call.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_error = FakeApiError(500)
    env, made = _protected()
    starting = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    gate = threading.Event()
    made[0].release_gate = gate
    try:
        await asyncio.to_thread(made[0].release_entered.wait, 5)
        assert env._stop_task is not None
        env._stop_task.cancel()
        with contextlib.suppress(BaseException):
            await starting
        assert made[0].names().count("pending") == 1
    finally:
        gate.set()
    await env.stop()
    assert made[0].names().count("pending") == 1
    assert "settle" not in made[0].names()


async def test_a_stop_after_a_cancelled_teardown_terminates_and_settles_once(fake_sdk: FakeCloud) -> None:
    """Run a fresh teardown in a later stop when the first one was cancelled in its start wait.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_delay = 0.2
    env, made = _protected()
    starting = asyncio.ensure_future(env.start())
    await fake_sdk.create_started.wait()
    stopping = asyncio.ensure_future(env.stop())
    await asyncio.sleep(0.05)
    # The create is in its grace, so the teardown waits for the start.
    assert not stopping.done()
    assert env._stop_task is not None
    env._stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    with contextlib.suppress(BaseException):
        await starting
    # The start's own cleanup may run the fresh teardown first; the later
    # stops must then only wait for it, never settle again.
    await env.stop()
    await env.stop()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    assert made[0].names().count("settle") == 1


async def test_a_lookup_that_finds_two_boxes_keeps_the_hold(fake_sdk: FakeCloud) -> None:
    """Terminate every box with the name, and keep the hold when there is more than one.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    fake_sdk.create_error = FakeApiError(429)
    fake_sdk.create_makes_box = True
    env, made = _protected()
    fake_sdk.boxes.append(BoxState(id="sb-old", params={"name": env._name}))
    with pytest.raises(SandboxInfraError):
        await env.start()
    with pytest.raises(UsagePendingError, match="more than one box"):
        await env.stop()
    assert [box.terminated for box in fake_sdk.boxes] == [True, True]
    assert made[0].names() == ["begin_create", "pending"]


async def test_a_lookup_never_terminates_a_box_with_another_name(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminate and settle only the box with the exact name, when the list returns more.

    Args:
        fake_sdk: The fake Sail cloud.
        monkeypatch: Makes the list return every box.
    """

    async def list_all(*, name: str) -> list[FakeSailbox]:
        """List every fake box, whatever its name.

        Args:
            name: The box name, which this list ignores.

        Returns:
            The handles of all boxes.
        """
        return [FakeSailbox(box) for box in fake_sdk.boxes]

    monkeypatch.setattr(FakeSailbox, "list", _aio(list_all), raising=False)
    fake_sdk.create_error = FakeApiError(429)
    fake_sdk.create_makes_box = True
    env, made = _protected()
    fake_sdk.boxes.append(BoxState(id="sb-other", params={"name": "someone-else"}))
    with pytest.raises(SandboxInfraError):
        await env.start()
    await env.stop()
    terminated = {box.id: box.terminated for box in fake_sdk.boxes}
    assert terminated == {"sb-1": True, "sb-other": False}
    assert "settle" in made[0].names()


async def test_a_start_after_an_early_stop_waits_for_its_box_to_go(fake_sdk: FakeCloud) -> None:
    """Return a failed start only after its box is gone, also after a stop before the start.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    env, made = _protected()
    await env.stop()

    def gated(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make a fake reservation whose confirm fails.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        made.append(FakeUsage(request, operation_key))
        made[-1].confirm_error = SandboxInfraError("confirm failed")
        return made[-1]

    env._usage_factory = gated
    with pytest.raises(SandboxInfraError, match="confirm failed"):
        await env.start()
    assert [box.terminated for box in fake_sdk.boxes] == [True]
    await env.stop()


async def test_protected_boxes_with_one_spec_name_get_different_sail_names(fake_sdk: FakeCloud) -> None:
    """Free a refused create's hold even when an old box of the same spec name is still listed.

    Args:
        fake_sdk: The fake Sail cloud.
    """
    first, _ = _protected(name="worker")
    async with first:
        pass
    fake_sdk.create_error = FakeApiError(403)
    second, made = _protected(name="worker")
    with pytest.raises(SandboxInfraError):
        await second.start()
    assert made[0].calls[-1] == ("release", True)
    assert first._name != second._name
    assert first._name.startswith("worker-")
    assert fake_sdk.boxes[0].params["name"] == first._name

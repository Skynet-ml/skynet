"""Tests for the async Kata Containers sandbox provider.

Every test replaces the Docker CLI with a fake in this file; no test runs a
real ``docker``, ``kata-runtime``, ``nerdctl`` or ``ctr`` binary, starts a
container or a VM, or makes a network call.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import subprocess
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from core.sandbox.capabilities import NetworkMode, SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import SandboxFatalError, SandboxInfraError
from core.sandbox.factory import PROVIDERS
from core.sandbox.providers import docker as docker_provider
from core.sandbox.providers import kata as kata_provider
from core.sandbox.providers.docker import _CliResult
from core.sandbox.providers.kata import KATA_CAPABILITIES, KataEnvironment
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

IMAGE = "localhost/skynet-test:latest"
FAKE_BIN = "docker"
ROLE_FORMAT = '{{.ID}} {{.Label "skynet.role"}}'


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small offline spec with an image.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    fields: dict[str, Any] = {
        "lifetime_seconds": 300,
        "resources": ResourceRequest(cpus=1),
        "image": IMAGE,
        "network_disabled": True,
    }
    return SandboxSpec(**{**fields, **overrides})


def _flag_values(args: Sequence[str], flag: str) -> list[str]:
    """Return every value that follows one flag in an argument list.

    Args:
        args: The CLI arguments.
        flag: The flag, such as ``--network``.

    Returns:
        The values in order.
    """
    return [args[index + 1] for index, item in enumerate(args) if item == flag]


def _parse_env(stream: bytes) -> dict[str, str]:
    """Decode the NUL-terminated ``NAME=value`` pairs that exec sends on stdin.

    Args:
        stream: The bytes from stdin.

    Returns:
        The environment.
    """
    pairs = [pair for pair in stream.decode("utf-8").split("\0") if pair]
    return dict(pair.split("=", 1) for pair in pairs)


class _Gate:
    """Pause one fake CLI call until the test releases it."""

    def __init__(self) -> None:
        """Create the two events of the pause."""
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        """Tell the test that the call arrived, then wait for the release."""
        self.reached.set()
        await self.release.wait()


class _FakeDocker:
    """Imitate the Docker CLI with containers and files in memory.

    It answers the ``_docker`` helper and the ``exec`` subprocess, and it
    runs a small command set: ``true``, ``false``, ``exit N``, ``echo``,
    ``cat`` and ``printenv``. A gate in ``gates`` pauses the first matching
    call: ``run-before`` and ``rm-before`` pause before the change,
    ``run-after`` pauses after the box exists.
    """

    def __init__(self) -> None:
        """Create a daemon with no containers."""
        self.calls: list[list[str]] = []
        self.deadlines: dict[str, float] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.answers: dict[str, _CliResult] = {}
        self.gates: dict[str, _Gate] = {}
        self._next_id = 0

    def _find(self, ref: str) -> str | None:
        """Return the name of the container with this name or id.

        Args:
            ref: A container name or id.

        Returns:
            The container name, or ``None`` when it does not exist.
        """
        if ref in self.containers:
            return ref
        return next((name for name, box in self.containers.items() if box["id"] == ref), None)

    async def _pause(self, key: str) -> None:
        """Hold the call at the gate with this key once, when the test set one.

        Args:
            key: The gate key, such as ``run-after``.
        """
        gate = self.gates.pop(key, None)
        if gate is not None:
            await gate.hold()

    async def cli(
        self, docker_bin: str, args: Sequence[str], *, stdin: bytes | None = None, cli_seconds: float = 60.0
    ) -> _CliResult:
        """Answer one CLI call in place of ``docker_provider._docker``.

        Args:
            docker_bin: The CLI path; it must be the Docker CLI.
            args: The CLI arguments.
            stdin: The bytes on the command's stdin.
            cli_seconds: The call's deadline; the fake records it.

        Returns:
            The fake result.
        """
        assert docker_bin == FAKE_BIN
        args = list(args)
        self.calls.append(args)
        self.deadlines[" ".join(args[:2])] = cli_seconds
        await self._pause(f"{args[0]}-before")
        joined = " ".join(args)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if joined.startswith(prefix):
                return self.answers[prefix]
        result = self._answer(args, stdin)
        await self._pause(f"{args[0]}-after")
        return result

    def _answer(self, args: list[str], stdin: bytes | None) -> _CliResult:
        """Change the fake daemon for one call and build its result.

        Args:
            args: The CLI arguments.
            stdin: The bytes on the command's stdin.

        Returns:
            The fake result.
        """
        ok = _CliResult(0, b"", b"")
        if args[:2] == ["run", "-d"]:
            name = args[args.index("--name") + 1]
            labels = dict(label.split("=", 1) for label in _flag_values(args, "--label"))
            self._next_id += 1
            box_id = f"id{self._next_id:04d}"
            self.containers[name] = {"id": box_id, "labels": labels, "files": {}}
            return _CliResult(0, f"{box_id}\n".encode(), b"")
        if args[:2] == ["rm", "-f"]:
            name = self._find(args[2])
            if name is None:
                return _CliResult(1, b"", f"Error response from daemon: No such container: {args[2]}".encode())
            del self.containers[name]
            return ok
        if args[:3] == ["network", "ls", "-q"]:
            return ok
        if args[:2] == ["ps", "-a"]:
            assert args[args.index("--format") + 1] == ROLE_FORMAT
            key, _, value = args[args.index("--filter") + 1].removeprefix("label=").partition("=")
            lines = [
                f"{box['id']} {box['labels'].get('skynet.role', '')}\n"
                for box in self.containers.values()
                if box["labels"].get(key) == value
            ]
            return _CliResult(0, "".join(lines).encode(), b"")
        if args[:1] == ["exec"]:
            return self._exec_cli(args, stdin)
        return ok

    def _exec_cli(self, args: list[str], stdin: bytes | None) -> _CliResult:
        """Answer the upload, download and marker calls that run ``exec``.

        Args:
            args: The CLI arguments.
            stdin: The bytes on the command's stdin.

        Returns:
            The fake result.
        """
        if "tar" in args and stdin is not None:
            name = args[args.index("tar") - 1]
            with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
                for member in archive.getmembers():
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    self.containers[name]["files"]["/" + member.name] = extracted.read()
            return _CliResult(0, b"", b"")
        if args[2:4] == ["sh", "-c"]:
            files = self.containers[args[1]]["files"]
            target = args[-1]
            if target not in files:
                return _CliResult(3, b"", b"")
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                info = tarfile.TarInfo(target.rsplit("/", 1)[-1])
                info.size = len(files[target])
                archive.addfile(info, io.BytesIO(files[target]))
            return _CliResult(0, buffer.getvalue(), b"")
        # A marker probe: the fake never leaves a marker file.
        return _CliResult(1, b"", b"")

    def run_command(self, name: str, command: str, env: Mapping[str, str]) -> tuple[int, bytes, bytes]:
        """Run one command from the fake command set in a container.

        Args:
            name: The container name.
            command: The shell command.
            env: The command's environment.

        Returns:
            The exit code, stdout and stderr.
        """
        box = self.containers.get(name)
        if box is None:
            return 125, b"", b"Error response from daemon: No such container\n"
        word, _, rest = command.partition(" ")
        if command == "true":
            return 0, b"", b""
        if command == "false":
            return 1, b"", b""
        if word == "exit":
            return int(rest), b"", b""
        if word == "echo":
            return 0, f"{rest}\n".encode(), b""
        if word == "printenv":
            return (0, f"{env[rest]}\n".encode(), b"") if rest in env else (1, b"", b"")
        if word == "cat":
            data = box["files"].get(rest)
            return (0, data, b"") if data is not None else (1, b"", b"cat: no such file\n")
        return 127, b"", f"unknown fake command: {command}\n".encode()


class _FakeStdin:
    """Collect the bytes that exec writes, and run the command when it closes."""

    def __init__(self, process: _FakeProcess) -> None:
        """Bind the stdin pipe to its process.

        Args:
            process: The process that runs once stdin closes.
        """
        self._process = process
        self._data = bytearray()

    def write(self, data: bytes) -> None:
        """Keep the written bytes.

        Args:
            data: The bytes.
        """
        self._data.extend(data)

    async def drain(self) -> None:
        """Return at once; the fake pipe never fills."""

    def close(self) -> None:
        """Run the command with the environment that stdin carried."""
        self._process.finish(bytes(self._data))

    async def wait_closed(self) -> None:
        """Return at once; the fake pipe closes at once."""


class _FakeProcess:
    """Stand in for one ``docker exec`` client process."""

    def __init__(self, engine: _FakeDocker, argv: Sequence[str]) -> None:
        """Create the pipes for one exec call.

        Args:
            engine: The fake engine that runs the command.
            argv: The CLI arguments after the binary.
        """
        self._engine = engine
        self._argv = list(argv)
        self.stdin = _FakeStdin(self)
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self._done = asyncio.Event()

    def finish(self, stream: bytes) -> None:
        """Run the command once and feed its output to the pipes.

        Args:
            stream: The environment stream from stdin.
        """
        if self._done.is_set():
            return
        name = self._argv[self._argv.index("-w") + 2]
        code, out, err = self._engine.run_command(name, self._argv[-1], _parse_env(stream))
        self.stdout.feed_data(out)
        self.stdout.feed_eof()
        self.stderr.feed_data(err)
        self.stderr.feed_eof()
        self.returncode = code
        self._done.set()

    async def wait(self) -> int:
        """Wait for the command to end.

        Returns:
            The exit code.
        """
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        """End the process with exit code 137 when it still runs."""
        if not self._done.is_set():
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode = 137
            self._done.set()


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> _FakeDocker:
    """Replace every Docker CLI call with an in-memory fake.

    Returns:
        The fake daemon.
    """
    engine = _FakeDocker()
    original = asyncio.create_subprocess_exec

    async def create_subprocess_exec(program: str, *args: str, **kwargs: Any) -> Any:
        """Return a fake exec process for the Docker CLI, else run the real call.

        Args:
            program: The binary.
            *args: Its arguments.
            **kwargs: The pipe options.

        Returns:
            The process.
        """
        if program != FAKE_BIN:
            return await original(program, *args, **kwargs)
        engine.calls.append(list(args))
        return _FakeProcess(engine, args)

    monkeypatch.setattr(docker_provider, "_docker", engine.cli)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    return engine


class TestKataSandboxContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake Docker CLI."""

    provider_cls = KataEnvironment

    @pytest.fixture(autouse=True)
    def _fake_cli(self, fake_docker: _FakeDocker) -> _FakeDocker:
        """Use the fake Docker CLI in every contract test.

        Args:
            fake_docker: The fake daemon.

        Returns:
            The fake daemon.
        """
        return fake_docker

    def make_spec(self, **overrides: Any) -> SandboxSpec:
        """Return an offline spec with an image, as the provider needs.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return _spec(**overrides)


def _runs(engine: _FakeDocker) -> list[list[str]]:
    """Return every ``docker run`` call that the fake received.

    Args:
        engine: The fake daemon.

    Returns:
        The run calls in order.
    """
    return [call for call in engine.calls if call[:2] == ["run", "-d"]]


def test_factory_registers_kata_as_dev_only_before_openshift() -> None:
    """Register the provider with no extra, never for production, and keep openshift last."""
    entry = PROVIDERS["kata"]
    assert entry.production_allowed is False
    assert entry.extra is None
    assert getattr(importlib.import_module(entry.module), entry.cls) is KataEnvironment
    names = list(PROVIDERS)
    assert names[-1] == "openshift"
    assert names.index("kata") < names.index("openshift")


def test_capabilities_allow_only_deny_all_fixed_at_start() -> None:
    """Keep Docker's limits, allow only deny-all and refuse a policy change."""
    assert KATA_CAPABILITIES.name == "kata"
    assert KATA_CAPABILITIES.network_modes == frozenset({NetworkMode.DENY_ALL})
    assert KATA_CAPABILITIES.dynamic_network_policy is False
    assert KATA_CAPABILITIES.max_vcpus == docker_provider.DOCKER_CAPABILITIES.max_vcpus


@pytest.mark.parametrize(
    "overrides",
    [{"network_disabled": False, "allowed_hosts": ("pypi.org",)}, {"network_disabled": False}],
)
def test_egress_is_refused_when_the_sandbox_is_made(overrides: dict[str, Any]) -> None:
    """Refuse an allowlist and open egress before any CLI call."""
    with pytest.raises(SandboxSpecError, match="network mode"):
        KataEnvironment(_spec(**overrides))


async def test_a_policy_change_after_start_is_refused(fake_docker: _FakeDocker) -> None:
    """Raise SandboxSpecError and send no CLI call for a policy change."""
    async with KataEnvironment(_spec()) as env:
        before = len(fake_docker.calls)
        with pytest.raises(SandboxSpecError, match="cannot change its network policy"):
            await env.set_network_policy(NetworkPolicy(allowed_hosts=("example.org",)))
        assert len(fake_docker.calls) == before


async def test_the_box_runs_on_the_kata_runtime_with_no_network(fake_docker: _FakeDocker) -> None:
    """Start the box with ``--runtime``, ``--network none``, the job label and a long deadline."""
    async with KataEnvironment(_spec(tags={JOB_TAG: "job-1"})) as env:
        (run,) = _runs(fake_docker)
        assert _flag_values(run, "--runtime") == ["kata"]
        assert _flag_values(run, "--network") == ["none"]
        assert "skynet.job=job-1" in _flag_values(run, "--label")
        assert not any(value.startswith(("HTTP_PROXY=", "HTTPS_PROXY=")) for value in _flag_values(run, "-e"))
        assert fake_docker.deadlines["run -d"] >= 300
        assert env.container_name in fake_docker.containers
    assert not any(call[:1] == ["network"] for call in fake_docker.calls)
    assert not fake_docker.containers


async def test_a_custom_runtime_name_reaches_docker(fake_docker: _FakeDocker) -> None:
    """Pass the configured runtime name to ``docker run``."""
    async with KataEnvironment(_spec(), runtime="kata-qemu"):
        (run,) = _runs(fake_docker)
        assert _flag_values(run, "--runtime") == ["kata-qemu"]


@pytest.mark.parametrize(
    "stderr",
    [
        b"docker: Error response from daemon: unknown or invalid runtime name: kata.",
        b"docker: Error response from daemon: Could not access KVM kernel module: No such file or directory",
        b"failed to create shim task: open /dev/kvm: permission denied",
    ],
)
async def test_a_host_error_is_fatal(fake_docker: _FakeDocker, stderr: bytes) -> None:
    """Raise SandboxFatalError for an unknown runtime or missing KVM, and leave no box."""
    fake_docker.answers["run -d"] = _CliResult(125, b"", stderr)
    with pytest.raises(SandboxFatalError, match="cannot run the Kata sandbox"):
        async with KataEnvironment(_spec()):
            pass
    assert not fake_docker.containers


async def test_another_run_failure_is_an_infra_error(fake_docker: _FakeDocker) -> None:
    """Raise SandboxInfraError, not a fatal error, for an ordinary run failure."""
    fake_docker.answers["run -d"] = _CliResult(125, b"", b"docker: Error response from daemon: conflict.")
    env = KataEnvironment(_spec())
    with pytest.raises(SandboxInfraError, match="Could not start the Kata sandbox") as caught:
        await env.start()
    assert not isinstance(caught.value, SandboxFatalError)


async def test_a_cli_timeout_is_an_infra_error_and_removes_the_box(
    fake_docker: _FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Map a ``docker run`` that passed its deadline to SandboxInfraError and remove by name."""
    answer = fake_docker.cli

    async def slow(docker_bin: str, args: Sequence[str], **kwargs: Any) -> _CliResult:
        """Create the box, then report the deadline as ``_docker`` does.

        Args:
            docker_bin: The CLI path.
            args: The CLI arguments.
            **kwargs: The stdin and deadline options.

        Returns:
            The fake result of a call that is not ``run``.

        Raises:
            SandboxInfraError: The call is ``run``.
        """
        result = await answer(docker_bin, args, **kwargs)
        if list(args[:2]) == ["run", "-d"]:
            raise SandboxInfraError("docker run did not finish in 300s.")
        return result

    monkeypatch.setattr(docker_provider, "_docker", slow)
    env = KataEnvironment(_spec())
    with pytest.raises(SandboxInfraError, match="did not finish"):
        await env.start()
    assert not fake_docker.containers


async def test_a_double_cancel_during_start_leaves_no_box(fake_docker: _FakeDocker) -> None:
    """Remove the box that ``docker run`` made, through two cancels, then reach the caller."""
    run_gate, rm_gate = _Gate(), _Gate()
    fake_docker.gates.update({"run-after": run_gate, "rm-before": rm_gate})
    env = KataEnvironment(_spec())
    task = asyncio.create_task(env.start())
    await run_gate.reached.wait()
    assert len(fake_docker.containers) == 1
    task.cancel()
    await rm_gate.reached.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    rm_gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not fake_docker.containers
    await env.stop()
    assert not fake_docker.containers


async def test_a_double_cancel_during_stop_leaves_no_box(fake_docker: _FakeDocker) -> None:
    """Finish the removal through two cancels, then raise the cancel."""
    env = KataEnvironment(_spec())
    await env.start()
    rm_gate = _Gate()
    fake_docker.gates["rm-before"] = rm_gate
    task = asyncio.create_task(env.stop())
    await rm_gate.reached.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    rm_gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not fake_docker.containers


async def test_a_stop_during_start_leaves_no_box(fake_docker: _FakeDocker) -> None:
    """Remove a box that the daemon made after a concurrent stop removed its name."""
    run_gate = _Gate()
    fake_docker.gates["run-before"] = run_gate
    env = KataEnvironment(_spec())
    task = asyncio.create_task(env.start())
    await run_gate.reached.wait()
    await env.stop()
    assert not fake_docker.containers
    run_gate.release.set()
    with pytest.raises(SandboxInfraError, match="stopped while it started"):
        await task
    assert not fake_docker.containers


async def test_a_stop_before_start_does_nothing(fake_docker: _FakeDocker) -> None:
    """Send no CLI call for an early stop, and still allow one start."""
    env = KataEnvironment(_spec())
    await env.stop()
    assert not fake_docker.calls
    await env.start()
    assert len(fake_docker.containers) == 1
    await env.stop()
    assert not fake_docker.containers


async def test_a_start_after_stop_is_refused(fake_docker: _FakeDocker) -> None:
    """Raise RuntimeError and run no second box after a stop."""
    env = KataEnvironment(_spec())
    await env.start()
    await env.stop()
    with pytest.raises(RuntimeError, match="already started"):
        await env.start()
    assert len(_runs(fake_docker)) == 1
    assert not fake_docker.containers


async def test_stop_job_sandboxes_counts_only_job_boxes(
    fake_docker: _FakeDocker, caplog: pytest.LogCaptureFixture
) -> None:
    """Remove the job's boxes, skip a failed removal and keep other jobs."""
    fake_docker.containers.update(
        {
            "box-a": {"id": "a1", "labels": {"skynet.job": "job-9"}, "files": {}},
            "box-b": {"id": "b1", "labels": {"skynet.job": "job-9"}, "files": {}},
            "box-c": {"id": "c1", "labels": {"skynet.job": "job-other"}, "files": {}},
        }
    )
    fake_docker.answers["rm -f b1"] = _CliResult(1, b"", b"Error response from daemon: container is busy")
    with caplog.at_level(logging.WARNING, logger=docker_provider.__name__):
        assert await KataEnvironment.stop_job_sandboxes("job-9") == 1
    assert set(fake_docker.containers) == {"box-b", "box-c"}
    assert "container is busy" in caplog.text


@pytest.fixture
def kata_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make preflight see a Linux host with KVM and the Docker CLI on PATH.

    Returns:
        The stand-in KVM device file.
    """
    kvm = tmp_path / "kvm"
    kvm.write_bytes(b"")
    monkeypatch.setattr(kata_provider, "_host_system", lambda: "Linux")
    monkeypatch.setattr(kata_provider, "_KVM_DEVICE", str(kvm))
    monkeypatch.setattr(kata_provider.shutil, "which", lambda _name: "/usr/bin/docker")
    return kvm


def _plan_runtimes(
    monkeypatch: pytest.MonkeyPatch, outcome: BaseException | subprocess.CompletedProcess[bytes]
) -> None:
    """Make ``docker info`` return or raise a planned outcome.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        outcome: The finished process to return, or the error to raise.
    """

    def runtimes(_bin: str) -> subprocess.CompletedProcess[bytes]:
        """Return or raise the planned outcome.

        Returns:
            The planned finished process.

        Raises:
            BaseException: The planned error.
        """
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(kata_provider, "_docker_runtimes", runtimes)


def _info(code: int, stdout: bytes, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Return a finished ``docker info`` process.

    Args:
        code: The exit code.
        stdout: The standard output.
        stderr: The standard error.

    Returns:
        The finished process.
    """
    return subprocess.CompletedProcess(["docker", "info"], code, stdout, stderr)


def test_preflight_passes_on_a_kata_host(kata_host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return when the host is Linux, has KVM and the daemon has the runtime."""
    del kata_host
    _plan_runtimes(monkeypatch, _info(0, b'{"kata":{"path":"containerd-shim-kata-v2"},"runc":{"path":"runc"}}\n'))
    KataEnvironment.preflight()


def test_preflight_refuses_a_host_that_is_not_linux(kata_host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a fatal error on macOS or Windows without running the CLI."""
    del kata_host
    monkeypatch.setattr(kata_provider, "_host_system", lambda: "Darwin")
    _plan_runtimes(monkeypatch, AssertionError("docker info must not run"))
    with pytest.raises(SandboxFatalError, match="only on a Linux host"):
        KataEnvironment.preflight()


def test_preflight_refuses_a_host_without_kvm(kata_host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a fatal error when the KVM device does not exist."""
    kata_host.unlink()
    _plan_runtimes(monkeypatch, AssertionError("docker info must not run"))
    with pytest.raises(SandboxFatalError, match="does not exist"):
        KataEnvironment.preflight()


def test_preflight_refuses_kvm_without_access(kata_host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a fatal error when the user cannot read and write the KVM device."""
    del kata_host
    monkeypatch.setattr(kata_provider.os, "access", lambda _path, _mode: False)
    _plan_runtimes(monkeypatch, AssertionError("docker info must not run"))
    with pytest.raises(SandboxFatalError, match="read and write access"):
        KataEnvironment.preflight()


def test_preflight_refuses_a_missing_cli(kata_host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a fatal error when docker is not on PATH, without running it."""
    del kata_host
    monkeypatch.setattr(kata_provider.shutil, "which", lambda _name: None)
    _plan_runtimes(monkeypatch, AssertionError("docker info must not run"))
    with pytest.raises(SandboxFatalError, match="on PATH"):
        KataEnvironment.preflight()


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (subprocess.TimeoutExpired(["docker", "info"], 10), "did not finish"),
        (OSError("exec format error"), "could not start"),
        (_info(1, b"", b"Cannot connect to the Docker daemon\n"), "exit code 1"),
        (_info(0, b"not json"), "not JSON"),
        (_info(0, b'{"runc":{"path":"runc"}}'), "no 'kata' runtime. It has: runc"),
        (_info(0, b"null"), "no 'kata' runtime. It has: none"),
    ],
)
def test_preflight_refuses_a_daemon_without_the_runtime(
    kata_host: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: BaseException | subprocess.CompletedProcess[bytes],
    message: str,
) -> None:
    """Raise a fatal error when ``docker info`` fails or lists no Kata runtime."""
    del kata_host
    _plan_runtimes(monkeypatch, outcome)
    with pytest.raises(SandboxFatalError, match=message):
        KataEnvironment.preflight()


@pytest.mark.parametrize(
    ("runtime", "stdout"),
    [
        ("kata-qemu", b'{"kata-qemu":{"path":"/opt/kata/bin/containerd-shim-kata-qemu-v2"}}'),
        ("vm", b'{"vm":{"runtimeType":"io.containerd.kata.v2"}}'),
        ("vm", b'{"vm":{"path":"/usr/bin/kata-runtime"}}'),
    ],
)
def test_preflight_accepts_a_kata_runtime_under_any_name(
    kata_host: Path, monkeypatch: pytest.MonkeyPatch, runtime: str, stdout: bytes
) -> None:
    """Accept a runtime whose name, path or runtime type names Kata."""
    del kata_host
    _plan_runtimes(monkeypatch, _info(0, stdout))
    KataEnvironment.preflight(runtime=runtime)


@pytest.mark.parametrize(
    ("runtime", "stdout"),
    [
        ("runc", b'{"runc":{"path":"runc"}}'),
        ("vm", b'{"vm":{"path":"/usr/bin/crun"}}'),
        ("vm", b'{"vm":null}'),
    ],
)
def test_preflight_refuses_a_runtime_that_is_not_kata(
    kata_host: Path, monkeypatch: pytest.MonkeyPatch, runtime: str, stdout: bytes
) -> None:
    """Raise a fatal error when the named runtime would boot no Kata VM."""
    del kata_host
    _plan_runtimes(monkeypatch, _info(0, stdout))
    with pytest.raises(SandboxFatalError, match="is not a Kata runtime"):
        KataEnvironment.preflight(runtime=runtime)


def test_preflight_runs_docker_info_with_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ask ``docker info`` for the runtimes as JSON, with a timeout."""
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Record the call in place of ``subprocess.run``.

        Args:
            argv: The command.
            **kwargs: The run options.

        Returns:
            A finished process with exit code 0.
        """
        seen.update(argv=argv, **kwargs)
        return _info(0, b"{}")

    monkeypatch.setattr(kata_provider.subprocess, "run", run)
    kata_provider._docker_runtimes("docker")
    assert seen["argv"] == ["docker", "info", "--format", "{{json .Runtimes}}"]
    assert seen["timeout"] == 10

"""Tests for the async Podman sandbox provider.

Every test replaces the Podman CLI with a fake in this file; no test runs a
real ``podman`` binary, starts a container or makes a network call.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import subprocess
import tarfile
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import SandboxFatalError, SandboxInfraError
from core.sandbox.factory import PROVIDERS
from core.sandbox.providers import docker as docker_provider
from core.sandbox.providers import podman as podman_provider
from core.sandbox.providers.docker import READY_MARKER, _CliResult
from core.sandbox.providers.podman import PODMAN_CAPABILITIES, PodmanEnvironment
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

IMAGE = "localhost/skynet-test:latest"
FAKE_BIN = "podman"
ROLE_FORMAT = '{{.ID}} {{index .Labels "skynet.role"}}'


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


class _FakePodman:
    """Imitate the Podman CLI with containers, files and networks in memory.

    It answers the ``_docker`` helper and the ``exec`` subprocess, and it
    runs a small command set: ``true``, ``false``, ``exit N``, ``echo``,
    ``cat`` and ``printenv``.
    """

    def __init__(self) -> None:
        """Create an engine with no containers and no networks."""
        self.calls: list[list[str]] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.networks: dict[str, dict[str, str]] = {}
        self.rootless = True
        self.answers: dict[str, _CliResult] = {}
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

    async def cli(
        self, docker_bin: str, args: Sequence[str], *, stdin: bytes | None = None, cli_seconds: float = 60.0
    ) -> _CliResult:
        """Answer one CLI call in place of ``docker_provider._docker``.

        Args:
            docker_bin: The CLI path; it must be the Podman CLI.
            args: The CLI arguments.
            stdin: The bytes on the command's stdin.
            cli_seconds: Ignored.

        Returns:
            The fake result.
        """
        del cli_seconds
        assert docker_bin == FAKE_BIN
        args = list(args)
        self.calls.append(args)
        joined = " ".join(args)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if joined.startswith(prefix):
                return self.answers[prefix]
        return self._answer(args, stdin)

    def _answer(self, args: list[str], stdin: bytes | None) -> _CliResult:
        """Change the fake engine for one call and build its result.

        Args:
            args: The CLI arguments.
            stdin: The bytes on the command's stdin.

        Returns:
            The fake result.
        """
        ok = _CliResult(0, b"", b"")
        if args[:2] == ["info", "--format"]:
            return _CliResult(0, b"true\n" if self.rootless else b"false\n", b"")
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
                return _CliResult(
                    1, b"", f'Error: no container with name or ID "{args[2]}" found: no such container'.encode()
                )
            del self.containers[name]
            return ok
        if args[:1] == ["logs"]:
            return _CliResult(0, f"{READY_MARKER}\n".encode(), b"")
        if args[:3] == ["network", "create", "--internal"]:
            self.networks[args[-1]] = dict(label.split("=", 1) for label in _flag_values(args, "--label"))
            return ok
        if args[:2] == ["network", "rm"]:
            if self.networks.pop(args[2], None) is None:
                return _CliResult(1, b"", b"Error: unable to find network: network not found")
            return ok
        if args[:3] == ["network", "ls", "-q"]:
            key, _, value = args[args.index("--filter") + 1].removeprefix("label=").partition("=")
            found = [name for name, labels in self.networks.items() if labels.get(key) == value]
            return _CliResult(0, "".join(f"{name}\n" for name in found).encode(), b"")
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
            return 125, b"", b"Error: no such container\n"
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
    """Stand in for one ``podman exec`` client process."""

    def __init__(self, engine: _FakePodman, argv: Sequence[str]) -> None:
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
def fake_podman(monkeypatch: pytest.MonkeyPatch) -> _FakePodman:
    """Replace every Podman CLI call with an in-memory fake.

    Returns:
        The fake engine.
    """
    engine = _FakePodman()
    original = asyncio.create_subprocess_exec

    async def create_subprocess_exec(program: str, *args: str, **kwargs: Any) -> Any:
        """Return a fake exec process for the Podman CLI, else run the real call.

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


class TestPodmanSandboxContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake Podman CLI."""

    provider_cls = PodmanEnvironment

    @pytest.fixture(autouse=True)
    def _fake_cli(self, fake_podman: _FakePodman) -> _FakePodman:
        """Use the fake Podman CLI in every contract test.

        Args:
            fake_podman: The fake engine.

        Returns:
            The fake engine.
        """
        return fake_podman

    def make_spec(self, **overrides: Any) -> SandboxSpec:
        """Return an offline spec with an image, as the provider needs.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return _spec(**overrides)


def test_factory_registers_podman_as_dev_only() -> None:
    """Register the provider with no extra and never for production."""
    entry = PROVIDERS["podman"]
    assert entry.production_allowed is False
    assert entry.extra is None
    assert getattr(importlib.import_module(entry.module), entry.cls) is PodmanEnvironment


def test_capabilities_match_docker_but_fix_the_policy_at_start() -> None:
    """Keep Docker's limits and refuse a policy change after start."""
    assert PODMAN_CAPABILITIES.name == "podman"
    assert PODMAN_CAPABILITIES.dynamic_network_policy is False
    assert PODMAN_CAPABILITIES.max_vcpus == docker_provider.DOCKER_CAPABILITIES.max_vcpus


def test_preflight_fails_without_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a fatal error when podman is not on PATH, without running it."""
    monkeypatch.setattr(podman_provider.shutil, "which", lambda _name: None)

    def fail(_bin: str) -> subprocess.CompletedProcess[bytes]:
        """Fail the test when preflight runs the CLI.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("podman info must not run")

    monkeypatch.setattr(podman_provider, "_podman_info", fail)
    with pytest.raises(SandboxFatalError, match="on PATH"):
        PodmanEnvironment.preflight()


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (subprocess.TimeoutExpired(["podman", "info"], 10), "did not finish"),
        (OSError("exec format error"), "could not start"),
        (subprocess.CompletedProcess(["podman", "info"], 125, b"", b"Error: cannot connect\n"), "exit code 125"),
    ],
)
def test_preflight_fails_when_the_engine_does_not_answer(
    monkeypatch: pytest.MonkeyPatch, outcome: BaseException | subprocess.CompletedProcess[bytes], message: str
) -> None:
    """Raise a fatal error for a timeout, a start failure or a nonzero exit."""
    monkeypatch.setattr(podman_provider.shutil, "which", lambda _name: "/usr/bin/podman")

    def info(_bin: str) -> subprocess.CompletedProcess[bytes]:
        """Return or raise the planned outcome.

        Returns:
            The planned finished process.

        Raises:
            BaseException: The planned error.
        """
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(podman_provider, "_podman_info", info)
    with pytest.raises(SandboxFatalError, match=message):
        PodmanEnvironment.preflight()


def test_preflight_passes_when_the_engine_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return when podman is on PATH and ``podman info`` exits 0."""
    calls: list[str] = []
    monkeypatch.setattr(podman_provider.shutil, "which", lambda _name: "/usr/bin/podman")

    def info(podman_bin: str) -> subprocess.CompletedProcess[bytes]:
        """Record the call and report a healthy engine.

        Returns:
            A finished process with exit code 0.
        """
        calls.append(podman_bin)
        return subprocess.CompletedProcess([podman_bin, "info"], 0, b"", b"")

    monkeypatch.setattr(podman_provider, "_podman_info", info)
    PodmanEnvironment.preflight()
    assert calls == ["podman"]


async def test_an_offline_box_runs_with_no_network(fake_podman: _FakePodman) -> None:
    """Start an offline box on ``--network none`` with no internal network."""
    async with PodmanEnvironment(_spec(tags={JOB_TAG: "job-1"})) as env:
        (run,) = [call for call in fake_podman.calls if call[:2] == ["run", "-d"]]
        assert _flag_values(run, "--network") == ["none"]
        assert "skynet.job=job-1" in _flag_values(run, "--label")
        assert env.container_name in fake_podman.containers
        assert not any(call[:1] == ["network"] for call in fake_podman.calls)
    assert not fake_podman.networks


async def test_a_rootful_engine_refuses_an_allowlist(fake_podman: _FakePodman) -> None:
    """Refuse an allowlist on rootful Podman as fatal, before any network or container exists."""
    fake_podman.rootless = False
    env = PodmanEnvironment(_spec(network_disabled=False, allowed_hosts=("example.org",)))
    with pytest.raises(SandboxFatalError, match="rootless"):
        await env.start()
    await env.stop()
    assert not any(call[:2] in (["network", "create"], ["run", "-d"]) for call in fake_podman.calls)
    assert not fake_podman.containers


async def test_a_rootless_allowlist_puts_the_proxy_on_both_networks(fake_podman: _FakePodman) -> None:
    """Create a plain internal network and start the proxy on it and on ``podman``."""
    env = PodmanEnvironment(_spec(network_disabled=False, allowed_hosts=("pypi.org", "example.org")))
    await env.start()
    (created,) = [call for call in fake_podman.calls if call[:2] == ["network", "create"]]
    assert "-o" not in created
    proxy_run, box_run = [call for call in fake_podman.calls if call[:2] == ["run", "-d"]]
    network = created[-1]
    assert _flag_values(proxy_run, "--network") == [network, "podman"]
    assert "skynet.role=egress" in _flag_values(proxy_run, "--label")
    assert proxy_run[-2:] == ["example.org", "pypi.org"]
    assert _flag_values(box_run, "--network") == [network]
    assert not any(call[:2] == ["network", "connect"] for call in fake_podman.calls)
    await env.stop()
    assert not fake_podman.containers
    assert not fake_podman.networks


async def test_a_proxy_that_stops_removes_the_network(fake_podman: _FakePodman) -> None:
    """Remove the proxy and the network when the proxy exits before it is ready."""
    fake_podman.answers["logs"] = _CliResult(1, b"", b"Error: no such container")
    env = PodmanEnvironment(_spec(network_disabled=False, allowed_hosts=("example.org",)))
    with pytest.raises(SandboxInfraError, match="egress proxy stopped"):
        await env.start()
    assert not fake_podman.networks
    assert not any(box["labels"].get("skynet.role") == "egress" for box in fake_podman.containers.values())
    await env.stop()


async def test_a_policy_change_after_start_is_refused(fake_podman: _FakePodman) -> None:
    """Raise SandboxSpecError and send no CLI call for a policy change."""
    async with PodmanEnvironment(_spec()) as env:
        before = len(fake_podman.calls)
        with pytest.raises(SandboxSpecError, match="cannot change its network policy"):
            await env.set_network_policy(NetworkPolicy(allowed_hosts=("example.org",)))
        assert len(fake_podman.calls) == before
        assert env.network_policy == NetworkPolicy(disabled=True)


async def test_stop_job_sandboxes_counts_only_boxes(fake_podman: _FakePodman, caplog: pytest.LogCaptureFixture) -> None:
    """Count boxes, not proxies, skip a failed removal and remove the job's networks."""
    fake_podman.containers.update(
        {
            "box-a": {"id": "a1", "labels": {"skynet.job": "job-9"}, "files": {}},
            "box-a-egress": {"id": "a2", "labels": {"skynet.job": "job-9", "skynet.role": "egress"}, "files": {}},
            "box-b": {"id": "b1", "labels": {"skynet.job": "job-9"}, "files": {}},
            "box-c": {"id": "c1", "labels": {"skynet.job": "job-other"}, "files": {}},
        }
    )
    fake_podman.networks.update({"box-a-net": {"skynet.job": "job-9"}, "box-c-net": {"skynet.job": "job-other"}})
    fake_podman.answers["rm -f b1"] = _CliResult(1, b"", b"Error: container is busy")
    with caplog.at_level(logging.WARNING, logger=podman_provider.__name__):
        assert await PodmanEnvironment.stop_job_sandboxes("job-9") == 1
    assert set(fake_podman.containers) == {"box-b", "box-c"}
    assert set(fake_podman.networks) == {"box-c-net"}
    assert "container is busy" in caplog.text


async def test_stop_job_sandboxes_returns_zero_when_the_list_fails(fake_podman: _FakePodman) -> None:
    """Return 0 and remove nothing when ``ps`` fails."""
    fake_podman.answers["ps"] = _CliResult(125, b"", b"Error: engine down")
    fake_podman.containers["box-a"] = {"id": "a1", "labels": {"skynet.job": "job-9"}, "files": {}}
    assert await PodmanEnvironment.stop_job_sandboxes("job-9") == 0
    assert "box-a" in fake_podman.containers


async def test_removal_of_a_gone_box_logs_no_warning(
    fake_podman: _FakePodman, caplog: pytest.LogCaptureFixture
) -> None:
    """Treat Podman's lower-case ``no such container`` as already removed."""
    env = PodmanEnvironment(_spec())
    await env.start()
    fake_podman.containers.clear()
    with caplog.at_level(logging.WARNING):
        await env.stop()
    assert "Could not remove" not in caplog.text
    assert ["rm", "-f", env.container_name] in fake_podman.calls

"""Tests for the async Docker sandbox provider.

The unit tests replace the Docker CLI call. The integration tests run real
containers and skip when Docker or the local image is missing; they never
pull an image. Every container and network that they create carries a label
that is unique to this test process, and a module finalizer removes them.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import time
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import SandboxInfraError
from core.sandbox.factory import PROVIDERS
from core.sandbox.providers import docker as docker_provider
from core.sandbox.providers.docker import (
    EXEC_STATE_DIR,
    DockerEnvironment,
    _CliResult,
    _env_stream,
    _sandbox_path,
)
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

BASE_IMAGE = "ubuntu:24.04"
PROXY_IMAGE_REPOSITORY = "skynet-local-sandbox"
TEST_LABEL = "skynet.test.sky413"
TEST_RUN = uuid.uuid4().hex
TEST_TAGS = {TEST_LABEL: TEST_RUN}
PROXY_PROBE = (
    "import http.client, os, urllib.parse\n"
    "proxy = urllib.parse.urlparse(os.environ['HTTPS_PROXY'])\n"
    "connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=10)\n"
    "connection.request('CONNECT', 'example.org:443')\n"
    "print(connection.getresponse().status)\n"
)
DIRECT_PROBE = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 443), timeout=3).close()\n"
    "    print('open')\n"
    "except OSError:\n"
    "    print('blocked')\n"
)


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the Docker CLI synchronously for test setup and checks.

    Args:
        *args: The CLI arguments.

    Returns:
        The finished process; a missing CLI gives exit code 127.
    """
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 127, "", "")


def _local_image(repository: str) -> str | None:
    """Return the first local tag of an image repository, without a pull.

    Args:
        repository: The image repository, or ``repo:tag``.

    Returns:
        ``repo:tag``, or ``None`` when no such image is on this machine.
    """
    listed = _cli("images", repository, "--format", "{{.Repository}}:{{.Tag}}")
    tags = [line for line in listed.stdout.split() if not line.endswith(":<none>")]
    return tags[0] if listed.returncode == 0 and tags else None


DOCKER_UP = _cli("info", "--format", "{{.ServerVersion}}").returncode == 0
HAS_BASE_IMAGE = DOCKER_UP and _local_image(BASE_IMAGE) is not None
PROXY_IMAGE = _local_image(PROXY_IMAGE_REPOSITORY) if DOCKER_UP else None
needs_docker = pytest.mark.skipif(not HAS_BASE_IMAGE, reason=f"Docker or the local {BASE_IMAGE} image is missing.")
needs_proxy_image = pytest.mark.skipif(
    PROXY_IMAGE is None, reason=f"Docker or a local {PROXY_IMAGE_REPOSITORY} image with python3 is missing."
)


def _leftovers() -> tuple[list[str], list[str]]:
    """List the containers and networks that carry this run's label.

    Returns:
        The container ids and the network ids.
    """
    selector = f"label={TEST_LABEL}={TEST_RUN}"
    containers = _cli("ps", "-aq", "--filter", selector).stdout.split()
    networks = _cli("network", "ls", "-q", "--filter", selector).stdout.split()
    return containers, networks


def _wait_for_no_leftovers(seconds: float = 10) -> tuple[list[str], list[str]]:
    """Wait until no labelled container or network remains.

    ``docker rm -f`` returns before ``--rm`` cleanup of a container ends, so
    the check polls for a short time.

    Args:
        seconds: The time limit.

    Returns:
        The labelled containers and networks that remain at the end.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = _leftovers()
        if remaining == ([], []) or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.2)


@pytest.fixture(scope="module", autouse=True)
def _remove_test_resources() -> Iterator[None]:
    """Remove every labelled container and network after the module."""
    yield
    if not DOCKER_UP:
        return
    containers, networks = _leftovers()
    if containers:
        _cli("rm", "-f", *containers)
    for network in networks:
        _cli("network", "rm", network)


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small offline spec with the test label.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    fields: dict[str, Any] = {
        "lifetime_seconds": 300,
        "resources": ResourceRequest(cpus=1),
        "image": BASE_IMAGE,
        "network_disabled": True,
    }
    fields.update(overrides)
    tags: dict[str, str] = overrides.get("tags", {})
    fields["tags"] = {**TEST_TAGS, **tags}
    return SandboxSpec(**fields)


class _FakeDocker:
    """Record Docker CLI calls and answer them from a script."""

    def __init__(self, answers: dict[str, _CliResult] | None = None) -> None:
        """Keep the scripted answers.

        Args:
            answers: Maps a space-joined argument prefix to its answer.
        """
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    async def __call__(
        self, docker_bin: str, args: Sequence[str], *, stdin: bytes | None = None, cli_seconds: float = 0
    ) -> _CliResult:
        """Record one call and return the answer for the longest matching prefix.

        Args:
            docker_bin: The Docker CLI path.
            args: The CLI arguments.
            stdin: The stdin bytes.
            cli_seconds: The CLI time limit.

        Returns:
            The scripted answer, or a success with no output.
        """
        del docker_bin, stdin, cli_seconds
        self.calls.append(list(args))
        joined = " ".join(args)
        matches = [prefix for prefix in self.answers if joined.startswith(prefix)]
        if not matches:
            return _CliResult(0, b"", b"")
        return self.answers[max(matches, key=len)]


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> _FakeDocker:
    """Replace the Docker CLI call with a recorder.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The recorder.
    """
    fake = _FakeDocker({"run": _CliResult(0, b"container-id\n", b"")})
    monkeypatch.setattr(docker_provider, "_docker", fake)
    return fake


def test_factory_keeps_docker_out_of_production() -> None:
    """Register the provider for development only."""
    entry = PROVIDERS["docker"]
    assert entry.production_allowed is False
    assert getattr(importlib.import_module(entry.module), entry.cls) is DockerEnvironment


def test_ready_marker_matches_the_proxy() -> None:
    """Wait for the same marker that the proxy prints."""
    proxy = importlib.import_module("core.service_gateway.optimization.blackbox.docker_egress_proxy")
    assert proxy.READY_MARKER == docker_provider.READY_MARKER


def test_rejects_open_egress() -> None:
    """Refuse a spec that neither disables the network nor lists hosts."""
    with pytest.raises(SandboxSpecError):
        DockerEnvironment(SandboxSpec(lifetime_seconds=60, image=BASE_IMAGE))


def test_rejects_a_spec_without_an_image() -> None:
    """Refuse a spec when neither the spec nor the provider names an image."""
    with pytest.raises(SandboxSpecError, match="image"):
        DockerEnvironment(SandboxSpec(lifetime_seconds=60, network_disabled=True))


def test_rejects_header_injection() -> None:
    """Refuse header injection, which the proxy cannot do."""
    with pytest.raises(SandboxSpecError):
        DockerEnvironment(
            SandboxSpec(
                lifetime_seconds=60,
                image=BASE_IMAGE,
                allowed_hosts=("api.example.com",),
                inject_headers={"api.example.com": {"X-Key": "value"}},
            )
        )


def test_env_stream_rejects_nul() -> None:
    """Refuse an environment value that no process can receive."""
    assert _env_stream({"A": "1"}) == b"A=1\0"
    with pytest.raises(SandboxSpecError):
        _env_stream({"A": "x\0y"})


@pytest.mark.parametrize(
    ("path", "expected"),
    [("a.txt", "/workspace/a.txt"), ("sub/../b.txt", "/workspace/b.txt"), ("/tmp/x", "/tmp/x"), ("../etc", "/etc")],
)
def test_sandbox_path_resolves_relative_to_the_workspace(path: str, expected: str) -> None:
    """Resolve relative paths against the workspace and keep absolute ones."""
    assert _sandbox_path(path) == expected


@pytest.mark.parametrize("path", ["", "/", "..", "a\0b"])
def test_sandbox_path_rejects_empty_root_and_nul(path: str) -> None:
    """Refuse a path that names nothing or the root."""
    with pytest.raises(SandboxSpecError):
        _sandbox_path(path)


async def test_offline_start_runs_a_hardened_box(fake_docker: _FakeDocker) -> None:
    """Start an offline box with no network, the limits and the labels."""
    env = DockerEnvironment(_spec(name="My Box!", tags={JOB_TAG: "job-1"}))
    await env.start()
    (run,) = fake_docker.calls
    assert run[:2] == ["run", "-d"]
    assert run[run.index("--network") + 1] == "none"
    assert run[run.index("--cpus") + 1] == "1"
    assert run[run.index("--memory") + 1] == "2048m"
    for flag in ("--read-only", "--init", "--rm"):
        assert flag in run
    assert run[run.index("--cap-drop") + 1] == "ALL"
    labels = {run[index + 1] for index, item in enumerate(run) if item == "--label"}
    assert {"skynet=blackbox", "skynet.job=job-1", "skynet_job=job-1", f"{TEST_LABEL}={TEST_RUN}"} <= labels
    assert env.container_name is not None
    assert env.container_name.startswith("my-box-")
    assert run[-2:] == [BASE_IMAGE, "300"]


async def test_tags_cannot_replace_the_sweep_labels(fake_docker: _FakeDocker) -> None:
    """Keep the sandbox and job labels, and drop a role tag, whatever the tags say."""
    tags = {"skynet": "other", "skynet.job": "other", "skynet.role": "egress", JOB_TAG: "job-1"}
    await DockerEnvironment(_spec(tags=tags)).start()
    (run,) = fake_docker.calls
    labels = {run[index + 1] for index, item in enumerate(run) if item == "--label"}
    assert {"skynet=blackbox", "skynet.job=job-1"} <= labels
    assert not any(label.startswith("skynet.role=") for label in labels)


async def test_a_job_label_tag_without_a_job_is_dropped(fake_docker: _FakeDocker) -> None:
    """Put a box in no job when only a raw job label tag names one."""
    await DockerEnvironment(_spec(tags={"skynet.job": "other"})).start()
    (run,) = fake_docker.calls
    labels = {run[index + 1] for index, item in enumerate(run) if item == "--label"}
    assert not any(label.startswith("skynet.job=") for label in labels)


async def test_ignore_mode_sets_no_limits(fake_docker: _FakeDocker) -> None:
    """Pass no CPU or memory limit when the resource mode is IGNORE."""
    await DockerEnvironment(_spec(resources=ResourceRequest(cpus=3, mode=ResourceMode.IGNORE))).start()
    (run,) = fake_docker.calls
    assert "--cpus" not in run
    assert "--memory" not in run


async def test_allowlist_start_opens_the_proxy_first(fake_docker: _FakeDocker) -> None:
    """Create the internal network and the proxy before the box."""
    fake_docker.answers["logs"] = _CliResult(0, docker_provider.READY_MARKER.encode(), b"")
    env = DockerEnvironment(_spec(network_disabled=False, allowed_hosts=("b.example.com", "a.example.com")))
    await env.start()
    commands = [call[:2] for call in fake_docker.calls]
    assert commands == [
        ["network", "create"],
        ["run", "-d"],
        ["network", "connect"],
        ["logs", f"{env.container_name}-egress"],
        ["run", "-d"],
    ]
    assert "--internal" in fake_docker.calls[0]
    create = fake_docker.calls[0]
    assert create[create.index("-o") + 1] == "com.docker.network.bridge.inhibit_ipv4=true"
    proxy_run = fake_docker.calls[1]
    assert proxy_run[proxy_run.index("--user") + 1] == "65534:65534"
    assert proxy_run[-2:] == ["a.example.com", "b.example.com"]
    box_run = fake_docker.calls[-1]
    assert box_run[box_run.index("--network") + 1] == f"{env.container_name}-net"


async def test_failed_proxy_start_removes_the_network(fake_docker: _FakeDocker) -> None:
    """Remove the proxy and the network when the proxy does not start."""
    fake_docker.answers["run -d --rm --init --name"] = _CliResult(125, b"", b"proxy failed")
    env = DockerEnvironment(_spec(network_disabled=False, allowed_hosts=("a.example.com",)))
    with pytest.raises(docker_provider.SandboxInfraError, match="proxy failed"):
        await env.start()
    assert [call[:2] for call in fake_docker.calls[-2:]] == [["rm", "-f"], ["network", "rm"]]


async def test_failed_proxy_restart_can_be_retried(fake_docker: _FakeDocker) -> None:
    """Keep the network and remove the failed proxy, so a retry of the allowlist works."""
    fake_docker.answers["logs"] = _CliResult(0, docker_provider.READY_MARKER.encode(), b"")
    env = DockerEnvironment(_spec(network_disabled=False, allowed_hosts=("a.example.com",)))
    await env.start()
    network = f"{env.container_name}-net"
    proxy = f"{env.container_name}-egress"
    fake_docker.answers["inspect"] = _CliResult(0, json.dumps({network: {}}).encode(), b"")
    fake_docker.answers["run -d --rm --init --name"] = _CliResult(125, b"", b"proxy failed")
    fake_docker.calls.clear()
    with pytest.raises(docker_provider.SandboxInfraError, match="proxy failed"):
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("b.example.com",)))
    assert fake_docker.calls[-1] == ["rm", "-f", proxy]
    assert ["network", "rm", network] not in fake_docker.calls
    del fake_docker.answers["run -d --rm --init --name"]
    fake_docker.calls.clear()
    await env.set_network_policy(NetworkPolicy(allowed_hosts=("b.example.com",)))
    assert env.network_policy == NetworkPolicy(allowed_hosts=("b.example.com",))
    assert not [call for call in fake_docker.calls if call[:2] == ["network", "create"]]
    proxy_run = next(call for call in fake_docker.calls if call[:2] == ["run", "-d"])
    assert proxy_run[-1] == "b.example.com"


async def test_stop_before_start_calls_no_docker(fake_docker: _FakeDocker) -> None:
    """Make no CLI call when the box never started."""
    await DockerEnvironment(_spec()).stop()
    assert fake_docker.calls == []


async def test_calls_before_start_raise(fake_docker: _FakeDocker) -> None:
    """Refuse exec, upload and download before start."""
    env = DockerEnvironment(_spec())
    with pytest.raises(RuntimeError):
        await env.exec("true")
    with pytest.raises(RuntimeError):
        await env.upload_files({"a": "b"})
    with pytest.raises(RuntimeError):
        await env.download_file("a")
    assert fake_docker.calls == []


async def test_stop_job_sandboxes_counts_only_boxes(fake_docker: _FakeDocker) -> None:
    """Remove boxes and proxies, but count only the boxes."""
    fake_docker.answers["ps"] = _CliResult(0, b"box1 \nproxy1 egress\nbox2 \n", b"")
    fake_docker.answers["network ls"] = _CliResult(0, b"net1\n", b"")
    assert await DockerEnvironment.stop_job_sandboxes("job-1") == 2
    removed = [call[2] for call in fake_docker.calls if call[:2] == ["rm", "-f"]]
    assert removed == ["box1", "proxy1", "box2"]
    assert fake_docker.calls[-1] == ["network", "rm", "net1"]


async def test_stop_job_sandboxes_skips_networks_when_the_list_fails(fake_docker: _FakeDocker) -> None:
    """Remove no network when Docker cannot list them."""
    fake_docker.answers["network ls"] = _CliResult(1, b"net1\n", b"daemon error")
    assert await DockerEnvironment.stop_job_sandboxes("job-1") == 0
    assert not [call for call in fake_docker.calls if call[:2] == ["network", "rm"]]


async def test_proxy_that_exits_fails_the_start_at_once(fake_docker: _FakeDocker) -> None:
    """Stop the ready poll when the proxy container is gone."""
    fake_docker.answers["logs"] = _CliResult(1, b"", b"No such container")
    env = DockerEnvironment(_spec(network_disabled=False, allowed_hosts=("a.example.com",)))
    with pytest.raises(docker_provider.SandboxInfraError, match="stopped"):
        await env.start()
    assert [call[0] for call in fake_docker.calls].count("logs") == 1


async def test_idle_network_sweep_keeps_young_and_used_networks(fake_docker: _FakeDocker) -> None:
    """Remove only networks with no container that are old enough."""
    now = int(time.time())
    fake_docker.answers["network ls"] = _CliResult(0, b"old used young\n", b"")
    fake_docker.answers["network inspect --format {{len .Containers}} {{.Created.Unix}} old"] = _CliResult(
        0, f"0 {now - 3600}".encode(), b""
    )
    fake_docker.answers["network inspect --format {{len .Containers}} {{.Created.Unix}} used"] = _CliResult(
        0, f"1 {now - 3600}".encode(), b""
    )
    fake_docker.answers["network inspect --format {{len .Containers}} {{.Created.Unix}} young"] = _CliResult(
        0, f"0 {now}".encode(), b""
    )
    assert await docker_provider.remove_idle_sandbox_networks() == 1
    assert [call for call in fake_docker.calls if call[:2] == ["network", "rm"]] == [["network", "rm", "old"]]


@needs_docker
class TestDockerSandboxContract(SandboxEnvironmentContract):
    """Run the provider contract against real offline containers."""

    provider_cls = DockerEnvironment

    def make_spec(self, **overrides: object) -> SandboxSpec:
        """Return an offline spec on the local base image with the test label.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return _spec(**overrides)


@needs_docker
async def test_exec_reports_a_timeout() -> None:
    """Mark a command that the timeout killed."""
    async with DockerEnvironment(_spec()) as env:
        result = await env.exec("sleep 30", timeout_sec=1)
    assert result.timed_out
    assert result.exit_code == 124


@needs_docker
@pytest.mark.parametrize("code", [124, 125, 137])
async def test_exit_codes_of_a_timeout_do_not_read_as_a_timeout(code: int) -> None:
    """Report a command that exits with a wrapper exit code by itself as a normal exit."""
    async with DockerEnvironment(_spec()) as env:
        result = await env.exec(f"exit {code}", timeout_sec=30)
    assert result.exit_code == code
    assert not result.timed_out


@needs_docker
async def test_timed_exec_keeps_only_the_command_stderr() -> None:
    """Return only the command's own stderr when the timeout does not fire."""
    async with DockerEnvironment(_spec()) as env:
        result = await env.exec("echo out; echo err >&2", timeout_sec=30)
        quiet = await env.exec("true", timeout_sec=30)
    assert (result.exit_code, result.stdout, result.stderr) == (0, "out\n", "err\n")
    assert quiet.stderr == ""


@needs_docker
async def test_fired_timeout_adds_no_job_notice_to_stderr() -> None:
    """Return only the command's own stderr when the timeout kills it."""
    async with DockerEnvironment(_spec()) as env:
        result = await env.exec("echo err >&2; sleep 30", timeout_sec=1)
    assert result.timed_out
    assert result.stderr == "err\n"


@needs_docker
async def test_timeout_kills_the_whole_process_group() -> None:
    """Kill the children of a command that the timeout stops."""
    async with DockerEnvironment(_spec()) as env:
        result = await env.exec("sleep 301 & sleep 302; wait", timeout_sec=1)
        remaining = await env.exec("ps -eo args")
    assert result.timed_out
    assert "sleep 301" not in remaining.stdout
    assert "sleep 302" not in remaining.stdout


@needs_docker
async def test_relative_paths_land_in_the_workspace() -> None:
    """Write a relative path under /workspace and read it back."""
    async with DockerEnvironment(_spec()) as env:
        await env.upload_files({"deep/a.txt": "one", "b.txt": "two"})
        assert (await env.exec("cat /workspace/deep/a.txt /workspace/b.txt")).stdout == "onetwo"
        assert await env.download_file("deep/a.txt") == "one"


@needs_docker
async def test_offline_box_has_no_network() -> None:
    """Start an offline box with no route out."""
    async with DockerEnvironment(_spec()) as env:
        result = await env.exec("tail -n +2 /proc/net/route | wc -l")
    assert result.stdout.strip() == "0"


@needs_docker
async def test_stop_after_a_cancel_removes_everything() -> None:
    """Remove the box even when the task that stops it is cancelled."""
    env = DockerEnvironment(_spec())
    await env.start()
    stopping = asyncio.ensure_future(env.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert _wait_for_no_leftovers() == ([], [])


@needs_docker
async def test_cancelled_exec_leaves_the_box_running() -> None:
    """Cancel a running command and keep the box usable."""
    async with DockerEnvironment(_spec()) as env:
        command = asyncio.ensure_future(env.exec("sleep 30"))
        await asyncio.sleep(1)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert (await env.exec("echo still")).stdout.strip() == "still"
    assert _wait_for_no_leftovers() == ([], [])


@needs_docker
@pytest.mark.parametrize("sudo", [False, True])
async def test_cancelled_exec_kills_the_command_in_the_box(sudo: bool) -> None:
    """Kill the command and its children in the box, not only the docker exec client."""
    async with DockerEnvironment(_spec()) as env:
        command = asyncio.ensure_future(env.exec("sleep 411 & sleep 412; wait", sudo=sudo))
        await asyncio.sleep(1.5)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        remaining = await env.exec("ps -eo args")
    assert "sleep 411" not in remaining.stdout
    assert "sleep 412" not in remaining.stdout


@needs_docker
async def test_cancel_kills_the_command_when_tmp_is_full() -> None:
    """Find and kill the command's process group after the workload fills /tmp."""
    async with DockerEnvironment(_spec()) as env:
        # The fill goes around the wrapper, which would free a page when it ends.
        script = "head -c 1100M /dev/zero > /tmp/fill; df --output=avail /tmp | tail -n 1"
        filled = _cli("exec", str(env.container_name), "sh", "-c", script)
        assert filled.stdout.strip() == "0", filled.stderr
        command = asyncio.ensure_future(env.exec("sleep 414"))
        await asyncio.sleep(1.5)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        remaining = await env.exec("ps -eo args")
    assert "sleep 414" not in remaining.stdout


@needs_docker
async def test_unrecorded_process_group_kills_the_command() -> None:
    """Kill the command and raise when the box cannot record its process group."""
    async with DockerEnvironment(_spec()) as env:
        name = str(env.container_name)
        # The fill goes around the wrapper: a wrapped command frees the page
        # of its own group-id file when it ends, and the next one reuses it.
        _cli("exec", name, "sh", "-c", f"head -c 2M /dev/zero > {EXEC_STATE_DIR}/fill")
        with pytest.raises(SandboxInfraError, match="process group"):
            await env.exec("sleep 415 & sleep 416; wait", timeout_sec=60)
        remaining = _cli("exec", name, "ps", "-eo", "args")
    assert remaining.returncode == 0, remaining.stderr
    assert "sleep 415" not in remaining.stdout
    assert "sleep 416" not in remaining.stdout


@needs_docker
async def test_exec_state_directory_hides_and_keeps_root_files() -> None:
    """Let a non-root user write exec state, but not list it or remove root files."""
    async with DockerEnvironment(_spec()) as env:
        await env.exec(f": > {EXEC_STATE_DIR}/root-file")
        name = str(env.container_name)
        mode = _cli("exec", name, "stat", "-c", "%a %U", EXEC_STATE_DIR)
        listed = _cli("exec", "-u", "1000", name, "ls", EXEC_STATE_DIR)
        written = _cli("exec", "-u", "1000", name, "touch", f"{EXEC_STATE_DIR}/user-file")
        removed = _cli("exec", "-u", "1000", name, "rm", "-f", f"{EXEC_STATE_DIR}/root-file")
        kept = await env.exec(f"test -e {EXEC_STATE_DIR}/root-file")
    assert mode.stdout.strip() == "1733 root"
    assert listed.returncode != 0
    assert written.returncode == 0, written.stderr
    assert removed.returncode != 0
    assert kept.exit_code == 0


@needs_docker
async def test_cancel_before_the_env_reaches_the_box_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never start a command whose exec was cancelled before its env stream arrived."""
    feed = docker_provider._feed

    async def slow_feed(writer: asyncio.StreamWriter, data: bytes) -> None:
        """Hold the env stream back, as a slow ``docker exec`` attach does.

        Args:
            writer: The stdin pipe.
            data: The bytes to write.
        """
        await asyncio.sleep(3)
        await feed(writer, data)

    async with DockerEnvironment(_spec()) as env:
        monkeypatch.setattr(docker_provider, "_feed", slow_feed)
        command = asyncio.ensure_future(env.exec("touch /workspace/ran; sleep 413"))
        await asyncio.sleep(0.5)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        monkeypatch.setattr(docker_provider, "_feed", feed)
        await asyncio.sleep(3)
        ran = await env.exec("test -e /workspace/ran && echo ran || echo none")
        remaining = await env.exec("ps -eo args")
    assert ran.stdout.strip() == "none"
    assert "sleep 413" not in remaining.stdout


@needs_proxy_image
async def test_proxy_refuses_a_host_that_is_not_listed() -> None:
    """Answer 403 for a host outside the allowlist and block direct egress."""
    spec = _spec(image=PROXY_IMAGE, network_disabled=False, allowed_hosts=("example.com",))
    async with DockerEnvironment(spec) as env:
        refused = await env.exec(f'python3 -c "{PROXY_PROBE}"')
        direct = await env.exec(f'python3 -c "{DIRECT_PROBE}"')
    assert refused.stdout.strip() == "403", refused.stderr
    assert direct.stdout.strip() == "blocked", direct.stderr
    assert _wait_for_no_leftovers() == ([], [])


@pytest.fixture
def host_listener() -> Iterator[int]:
    """Listen on every address of the Docker host in a labelled container.

    Yields:
        The TCP port of the listener.
    """
    port = 20000 + uuid.uuid4().int % 20000
    server = (
        "import socket, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('0.0.0.0', {port}))\n"
        "s.listen()\n"
        "print('listening', flush=True)\n"
        "time.sleep(300)\n"
    )
    name = f"sky413-listener-{TEST_RUN[:8]}"
    assert PROXY_IMAGE is not None
    started = _cli(
        "run",
        "-d",
        "--rm",
        "--network",
        "host",
        "--name",
        name,
        "--label",
        f"{TEST_LABEL}={TEST_RUN}",
        "--entrypoint",
        "python3",
        PROXY_IMAGE,
        "-c",
        server,
    )
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.monotonic() + 15
        while "listening" not in _cli("logs", name).stdout:
            assert time.monotonic() < deadline, "The host listener did not start."
            time.sleep(0.2)
        yield port
    finally:
        _cli("rm", "-f", name)


@needs_proxy_image
async def test_allowlisted_box_cannot_reach_the_docker_host(host_listener: int) -> None:
    """Give the box no route to a service that listens on the Docker host."""
    spec = _spec(image=PROXY_IMAGE, network_disabled=False, allowed_hosts=("example.com",))
    async with DockerEnvironment(spec) as env:
        network = f"{env.container_name}-net"
        gateway = _cli("network", "inspect", "--format", "{{range .IPAM.Config}}{{.Gateway}} {{end}}", network)
        targets = [address for address in gateway.stdout.split() if ":" not in address]
        routes = await env.exec("cat /proc/net/route")
        assert targets, gateway.stderr
        for address in targets:
            probe = (
                "import socket\n"
                "try:\n"
                f"    socket.create_connection(('{address}', {host_listener}), timeout=3).close()\n"
                "    print('open')\n"
                "except OSError:\n"
                "    print('blocked')\n"
            )
            result = await env.exec(f'python3 -c "{probe}"')
            assert result.stdout.strip() == "blocked", (address, routes.stdout, result.stderr)
        refused = await env.exec(f'python3 -c "{PROXY_PROBE}"')
        assert refused.stdout.strip() == "403", refused.stderr


@needs_proxy_image
async def test_network_policy_switches_off_and_back_on() -> None:
    """Drop the proxy on disable, then bring back an allowlist through a new proxy."""
    spec = _spec(image=PROXY_IMAGE, network_disabled=False, allowed_hosts=("example.com",))
    async with DockerEnvironment(spec) as env:
        name = env.container_name
        await env.disable_network()
        assert _cli("inspect", f"{name}-egress").returncode != 0
        assert _cli("network", "inspect", f"{name}-net").returncode != 0
        offline = await env.exec(f'python3 -c "{PROXY_PROBE}"')
        assert offline.exit_code != 0
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("example.net",)))
        assert (await env.exec(f'python3 -c "{PROXY_PROBE}"')).stdout.strip() == "403"
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("example.com", "example.net")))
        assert (await env.exec(f'python3 -c "{PROXY_PROBE}"')).stdout.strip() == "403"
    assert _wait_for_no_leftovers() == ([], [])


@needs_proxy_image
async def test_cancelled_start_leaves_nothing_behind() -> None:
    """Remove the network, the proxy and the box when start is cancelled."""
    spec = _spec(image=PROXY_IMAGE, network_disabled=False, allowed_hosts=("example.com",))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5), DockerEnvironment(spec):
            await asyncio.sleep(60)
    assert _wait_for_no_leftovers() == ([], [])

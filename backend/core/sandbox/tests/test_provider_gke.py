"""Tests for the async GKE sandbox provider.

A fake Kubernetes client replaces the real one. It keeps each "pod" as a
manifest in memory and a directory in ``tmp_path``, and runs each exec
session on this machine inside that directory. Like the v4 exec protocol,
the fake never closes the command's stdin while the session runs. No test
reaches a cluster.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, SandboxEnvironment
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.factory import PROVIDERS, ProductionNotAllowedError, create_environment, load_provider
from core.sandbox.providers import gke
from core.sandbox.providers.gke import GKEEnvironment
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

IMAGE = "registry.example/skynet/task:1"


class FakeApiError(Exception):
    """Stand in for ``kubernetes.client.rest.ApiException``."""

    def __init__(self, status: int, reason: str) -> None:
        """Keep the status and reason, as the real error does.

        Args:
            status: The HTTP status, or 0 for a websocket failure.
            reason: The reason phrase.
        """
        super().__init__(f"({status}) Reason: {reason}\nHTTP response body: secret-body")
        self.status = status
        self.reason = reason


class FakeCluster:
    """Hold the fake cluster's pods, logged calls and injected failures."""

    def __init__(self, root: Path) -> None:
        """Start with no pods.

        Args:
            root: The directory that holds one directory for each pod.
        """
        self.root = root
        self.pods: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.clients: list[FakeApiClient] = []
        self.status: dict[str, Any] = {"phase": "Running", "containerStatuses": [{"name": "main", "ready": True}]}
        self.create_error: FakeApiError | None = None
        self.delete_error: FakeApiError | None = None
        self.config_error: Exception | None = None
        self.lock = threading.Lock()

    def new_client(self, config_file: str | None = None, context: str | None = None, persist_config: bool = True):
        """Stand in for ``kubernetes.config.new_client_from_config``.

        Args:
            config_file: The kubeconfig path; the provider passes none.
            context: The kubeconfig context.
            persist_config: Must be False, so the kubeconfig never changes.

        Returns:
            A fake API client on this cluster.
        """
        assert config_file is None
        assert persist_config is False
        self.log("connect", context)
        if self.config_error is not None:
            raise self.config_error
        client = FakeApiClient(self)
        self.clients.append(client)
        return client

    def log(self, verb: str, detail: Any) -> None:
        """Record one call.

        Args:
            verb: The call name.
            detail: What the call carried.
        """
        with self.lock:
            self.calls.append((verb, detail))

    def verbs(self) -> list[str]:
        """Return the name of every logged call, in order."""
        return [verb for verb, _ in self.calls]


class FakeApiClient:
    """Stand in for ``kubernetes.client.ApiClient``; the configuration is the cluster."""

    def __init__(self, configuration: FakeCluster) -> None:
        """Keep the configuration.

        Args:
            configuration: The fake cluster.
        """
        self.configuration = configuration
        self.closed = False

    def close(self) -> None:
        """Mark the client closed."""
        self.closed = True


class FakeCoreV1Api:
    """Stand in for ``kubernetes.client.CoreV1Api`` with the calls the provider makes."""

    def __init__(self, api_client: FakeApiClient) -> None:
        """Bind to the client's cluster.

        Args:
            api_client: The fake API client.
        """
        self.cluster = api_client.configuration

    def create_namespaced_pod(self, *, namespace: str, body: dict[str, Any], _request_timeout: float) -> None:
        """Store the pod and make its directory.

        Args:
            namespace: The namespace.
            body: The pod manifest.
            _request_timeout: The client timeout.
        """
        assert _request_timeout > 0
        self.cluster.log("create", {"namespace": namespace, "body": body})
        if self.cluster.create_error is not None:
            raise self.cluster.create_error
        name = body["metadata"]["name"]
        self.cluster.pods[name] = json.loads(json.dumps(body))
        (self.cluster.root / name / "tmp").mkdir(parents=True)

    def read_namespaced_pod(self, *, name: str, namespace: str, _preload_content: bool, _request_timeout: float):
        """Return the raw pod JSON.

        Args:
            name: The pod name.
            namespace: The namespace.
            _preload_content: Must be False, so the JSON keeps its keys.
            _request_timeout: The client timeout.

        Returns:
            An object with the JSON bytes in ``data``.
        """
        assert _preload_content is False
        self.cluster.log("read", {"namespace": namespace, "name": name})
        if name not in self.cluster.pods:
            raise FakeApiError(404, "Not Found")
        return SimpleNamespace(data=json.dumps({**self.cluster.pods[name], "status": self.cluster.status}).encode())

    def list_namespaced_pod(
        self, *, namespace: str, label_selector: str, _preload_content: bool, _request_timeout: float
    ):
        """Return the raw list of pods that match one ``key=value`` selector.

        Args:
            namespace: The namespace.
            label_selector: ``key=value``.
            _preload_content: Must be False.
            _request_timeout: The client timeout.

        Returns:
            An object with the JSON bytes in ``data``.
        """
        assert _preload_content is False
        self.cluster.log("list", {"namespace": namespace, "selector": label_selector})
        key, value = label_selector.split("=", 1)
        items = [pod for pod in self.cluster.pods.values() if pod["metadata"]["labels"].get(key) == value]
        return SimpleNamespace(data=json.dumps({"items": items}).encode())

    def delete_namespaced_pod(
        self, *, name: str, namespace: str, grace_period_seconds: int, _request_timeout: float
    ) -> None:
        """Remove the pod.

        Args:
            name: The pod name.
            namespace: The namespace.
            grace_period_seconds: The grace period.
            _request_timeout: The client timeout.
        """
        self.cluster.log("delete", {"namespace": namespace, "name": name})
        if self.cluster.delete_error is not None:
            raise self.cluster.delete_error
        if self.cluster.pods.pop(name, None) is None:
            raise FakeApiError(404, "Not Found")

    def connect_get_namespaced_pod_exec(self, *args: Any, **kwargs: Any) -> None:
        """Refuse a direct call: the provider must go through ``stream``."""
        raise AssertionError("connect_get_namespaced_pod_exec must run through kubernetes.stream.stream")


class FakeSession:
    """Stand in for a binary ``WSClient``: run the command here, never close its stdin early."""

    def __init__(self, root: Path, command: list[str], stdin: bool) -> None:
        """Start the command in the pod directory.

        Args:
            root: The pod directory.
            command: The command and its arguments.
            stdin: Open a stdin pipe.
        """
        env = {"PATH": os.environ["PATH"], "TMPDIR": str(root / "tmp")}
        self.process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        self.ready = threading.Condition()
        self.readers = [
            threading.Thread(target=self._pump, args=(key, pipe), daemon=True)
            for key, pipe in (("stdout", self.process.stdout), ("stderr", self.process.stderr))
        ]
        for reader in self.readers:
            reader.start()

    def _pump(self, key: str, pipe: Any) -> None:
        """Copy one pipe into its buffer.

        Args:
            key: ``stdout`` or ``stderr``.
            pipe: The pipe.
        """
        while chunk := os.read(pipe.fileno(), 4096):
            with self.ready:
                self.buffers[key] += chunk
                self.ready.notify_all()
        with self.ready:
            self.ready.notify_all()

    def _ended(self) -> bool:
        """Say whether both pipes closed and the process exited."""
        return not any(reader.is_alive() for reader in self.readers) and self.process.poll() is not None

    def write_stdin(self, data: bytes) -> None:
        """Write to the command's stdin and keep it open.

        Args:
            data: The bytes.
        """
        assert isinstance(data, bytes)
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def update(self, timeout: float = 0) -> None:
        """Wait up to ``timeout`` seconds for output or the end.

        Args:
            timeout: The wait limit.
        """
        with self.ready:
            if not any(self.buffers.values()) and not self._ended():
                self.ready.wait(min(timeout, 0.05) if timeout else 0)

    def is_open(self) -> bool:
        """Say whether the session still runs."""
        return not self._ended()

    def _take(self, key: str, timeout: float | None) -> bytes:
        """Return and clear one buffer.

        Args:
            key: ``stdout`` or ``stderr``.
            timeout: Must be 0: the real client blocks forever on ``None``.

        Returns:
            The buffered bytes.
        """
        assert timeout == 0
        with self.ready:
            data = bytes(self.buffers[key])
            self.buffers[key].clear()
        return data

    def read_stdout(self, timeout: float | None = None) -> bytes:
        """Return the buffered stdout bytes."""
        return self._take("stdout", timeout)

    def read_stderr(self, timeout: float | None = None) -> bytes:
        """Return the buffered stderr bytes."""
        return self._take("stderr", timeout)

    @property
    def returncode(self) -> int | None:
        """Return the exit code once the session ended."""
        return None if self.is_open() else self.process.returncode

    def close(self) -> None:
        """Close the client side. The remote command keeps running, as on a cluster."""
        if self.process.stdin is not None:
            self.process.stdin.close()


def fake_stream(method: Any, name: str, namespace: str, **kwargs: Any) -> FakeSession:
    """Stand in for ``kubernetes.stream.stream`` on the pod exec call.

    Args:
        method: The bound ``connect_get_namespaced_pod_exec``.
        name: The pod name.
        namespace: The namespace.
        **kwargs: The exec options.

    Returns:
        The running session.
    """
    cluster: FakeCluster = method.__self__.cluster
    assert method.__name__ == "connect_get_namespaced_pod_exec"
    assert kwargs["binary"] is True
    assert kwargs["tty"] is False
    assert kwargs["_preload_content"] is False
    assert kwargs["container"] == "main"
    cluster.log("exec", {"namespace": namespace, "name": name, "command": kwargs["command"]})
    if name not in cluster.pods:
        raise FakeApiError(0, "Handshake status 404 Not Found")
    return FakeSession(cluster.root / name, kwargs["command"], kwargs["stdin"])


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    """Install the fake client and point the provider at it.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Replaces the client modules and the poll interval.

    Returns:
        The fake cluster.
    """
    fake = FakeCluster(tmp_path / "cluster")
    fake.root.mkdir()
    monkeypatch.setattr(gke, "k8s_client", SimpleNamespace(ApiClient=FakeApiClient, CoreV1Api=FakeCoreV1Api))
    monkeypatch.setattr(gke, "k8s_config", SimpleNamespace(new_client_from_config=fake.new_client))
    monkeypatch.setattr(gke, "k8s_stream", fake_stream)
    monkeypatch.setattr(GKEEnvironment, "poll_seconds", 0.0)
    return fake


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec with an image.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), "image": IMAGE, **overrides})


@pytest.mark.usefixtures("cluster")
class TestGKEEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake client."""

    provider_cls = GKEEnvironment

    def make_spec(self, **overrides: Any) -> SandboxSpec:
        """Return the contract spec with the test image.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return super().make_spec(**{"image": IMAGE, **overrides})

    async def test_upload_then_download_round_trips(self, sandbox: SandboxEnvironment) -> None:
        """Read back the exact text, through a path relative to the container's cwd.

        The fake runs on this machine, so ``/tmp`` would be the host's ``/tmp``.
        """
        await sandbox.upload_files({"contract/a.txt": "alpha\r\nבטא\n", "contract/empty.txt": ""})
        assert await sandbox.download_file("contract/a.txt") == "alpha\r\nבטא\n"
        assert await sandbox.download_file("contract/empty.txt") == ""
        assert (await sandbox.exec("cat contract/a.txt")).stdout == "alpha\r\nבטא\n"

    async def test_download_missing_file_returns_none(self, sandbox: SandboxEnvironment) -> None:
        """Return None for a relative path that does not exist."""
        assert await sandbox.download_file("contract/missing.txt") is None


def test_factory_registers_gke_for_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the provider with the ``gke`` extra and refuse it in production."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    entry = PROVIDERS["gke"]
    assert (entry.module, entry.cls, entry.extra, entry.production_allowed) == (
        "core.sandbox.providers.gke",
        "GKEEnvironment",
        "gke",
        False,
    )
    assert load_provider("gke") is GKEEnvironment
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        load_provider("gke")


def test_factory_creates_the_provider_with_its_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass provider kwargs through ``create_environment``."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    env = create_environment("gke", _spec(image=None), image=IMAGE, namespace="team-a", context="gke_p_z_c")
    assert isinstance(env, GKEEnvironment)
    assert (env._image, env._namespace, env._context) == (IMAGE, "team-a", "gke_p_z_c")


def test_preflight_passes_with_the_client_gcloud_and_a_kubeconfig(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Return when the package, ``gcloud`` and a kubeconfig in the ``KUBECONFIG`` list exist."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setenv("KUBECONFIG", os.pathsep.join([str(tmp_path / "missing"), str(kubeconfig)]))
    monkeypatch.setattr(gke.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(factory.settings, "app_environment", "")
    factory.preflight("gke")


def test_preflight_reports_a_missing_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the extra when the kubernetes package is missing."""
    monkeypatch.setattr(gke, "k8s_client", None)
    with pytest.raises(SandboxFatalError, match="`gke` extra"):
        GKEEnvironment.preflight()


def test_preflight_reports_a_missing_gcloud(cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to run without ``gcloud``, which the GKE auth plugin needs."""
    monkeypatch.setattr(gke.shutil, "which", lambda name: None)
    with pytest.raises(SandboxFatalError, match="gcloud"):
        GKEEnvironment.preflight()


def test_preflight_reports_a_missing_kubeconfig(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refuse to run when no kubeconfig file exists."""
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing"))
    monkeypatch.setattr(gke.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SandboxFatalError, match="no kubeconfig"):
        GKEEnvironment.preflight()


def test_an_image_is_required() -> None:
    """Refuse a sandbox with no image: the provider builds none."""
    with pytest.raises(SandboxSpecError, match="needs an image"):
        GKEEnvironment(_spec(image=None))


def test_a_disabled_network_is_refused() -> None:
    """Refuse a spec that asks for no egress: the provider only has open egress."""
    with pytest.raises(SandboxSpecError):
        GKEEnvironment(_spec(network_disabled=True))


def test_the_spec_image_wins_over_the_provider_image() -> None:
    """Use the spec's image when both name one."""
    assert GKEEnvironment(_spec(), image="other:1")._image == IMAGE


async def test_start_uses_the_namespace_and_context_and_bounds_the_pod(cluster: FakeCluster) -> None:
    """Create a root pod with the deadline and job label, through the chosen context and namespace."""
    spec = _spec(lifetime_seconds=90.2, tags={JOB_TAG: "job-1"}, name="My Task", env={"A": "1"})
    async with GKEEnvironment(spec, namespace="team-a", context="gke_p_z_c") as env:
        manifest = cluster.pods[str(env.pod_name)]
    assert cluster.calls[0] == ("connect", "gke_p_z_c")
    assert all(detail["namespace"] == "team-a" for verb, detail in cluster.calls if verb != "connect")
    assert str(env.pod_name).startswith("my-task-")
    assert manifest["metadata"]["labels"] == {"skynet": "sandbox", "skynet.job": "job-1"}
    pod = manifest["spec"]
    assert pod["activeDeadlineSeconds"] == 91
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"] == {"runAsUser": 0, "runAsGroup": 0}
    container = pod["containers"][0]
    assert (container["name"], container["image"]) == ("main", IMAGE)
    assert container["resources"] == {"requests": {"cpu": "1"}}
    assert "env" not in container


async def test_env_values_stay_off_the_command_line_and_the_manifest(cluster: FakeCluster) -> None:
    """Send spec and exec env values on stdin only, through a stdin that never closes."""
    async with GKEEnvironment(_spec(env={"SPEC_SECRET": "spec-value-123"})) as env:
        result = await env.exec('printf "%s %s" "$SPEC_SECRET" "$EXEC_SECRET"', env={"EXEC_SECRET": "exec-value-456"})
    assert result.stdout == "spec-value-123 exec-value-456"
    logged = json.dumps(cluster.calls)
    for value in ("spec-value-123", "exec-value-456"):
        assert value not in logged


async def test_exec_runs_in_the_cwd_and_without_stdin(cluster: FakeCluster) -> None:
    """Change into the working directory, and give the command no stdin to wait on."""
    async with GKEEnvironment(_spec()) as env:
        await env.upload_files({"work dir/x.txt": "x"})
        assert (await env.exec("cat x.txt", cwd="work dir")).stdout == "x"
        started = time.monotonic()
        assert (await env.exec("cat", timeout_sec=10)).stdout == ""
        assert time.monotonic() - started < 5


class SplitFrames:
    """Stand in for a session that sends one Hebrew text split inside its characters."""

    def __init__(self, frames: list[bytes]) -> None:
        """Keep the frames to send, one for each update.

        Args:
            frames: The stdout frames.
        """
        self.frames = list(frames)
        self.pending = b""

    def write_stdin(self, data: bytes) -> None:
        """Ignore the env stream."""

    def update(self, timeout: float = 0) -> None:
        """Receive the next frame."""
        if self.frames:
            self.pending += self.frames.pop(0)

    def is_open(self) -> bool:
        """Say whether frames remain."""
        return bool(self.frames)

    def read_stdout(self, timeout: float | None = None) -> bytes:
        """Return and clear the received bytes."""
        data, self.pending = self.pending, b""
        return data

    def read_stderr(self, timeout: float | None = None) -> bytes:
        """Return no stderr."""
        return b""

    returncode = 0

    def close(self) -> None:
        """Do nothing."""


async def test_a_character_split_across_frames_decodes_whole(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Join UTF-8 bytes that two frames split, in the result and in every streamed chunk."""
    async with GKEEnvironment(_spec()) as env:
        frames = [b"\xd7", b"\x90\xd7", b"\x91\n"]
        monkeypatch.setattr(gke, "k8s_stream", lambda *args, **kwargs: SplitFrames(frames))
        chunks: list[str] = []
        result = await env.exec("ignored", on_output=lambda stream, text: chunks.append(text))
    assert result.stdout == "אב\n"
    assert "".join(chunks) == "אב\n"
    assert all("�" not in chunk for chunk in chunks)


async def test_exec_timeout_kills_the_command_and_reports_timed_out(cluster: FakeCluster) -> None:
    """Kill the command at its limit and return ``timed_out=True``."""
    async with GKEEnvironment(_spec()) as env:
        started = time.monotonic()
        result = await env.exec("sleep 30", timeout_sec=1)
        assert time.monotonic() - started < 10
        assert result.timed_out
        assert result.exit_code == 124
        plain = await env.exec("exit 124", timeout_sec=5)
        assert plain.exit_code == 124
        assert not plain.timed_out


def _alive(pid: int) -> bool:
    """Say whether a process still runs.

    Args:
        pid: The process id.

    Returns:
        True while the process exists.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_a_cancelled_exec_kills_the_command(cluster: FakeCluster) -> None:
    """Kill the command's process group when the caller cancels the exec; closing the session alone does not."""
    async with GKEEnvironment(_spec()) as env:
        pid_file = cluster.root / str(env.pod_name) / "pid"
        task = asyncio.create_task(env.exec("echo $$ > pid; exec sleep 30"))
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        assert _alive(pid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not _alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _alive(pid)
        assert (await env.exec("echo still")).stdout.strip() == "still"


async def test_exec_before_start_raises(cluster: FakeCluster) -> None:
    """Refuse to run a command before start."""
    with pytest.raises(RuntimeError, match="not running"):
        await GKEEnvironment(_spec()).exec("true")


async def test_a_lost_session_is_an_infra_error(cluster: FakeCluster) -> None:
    """Raise SandboxInfraError when the exec websocket cannot reach the pod."""
    async with GKEEnvironment(_spec()) as env:
        cluster.pods.clear()
        with pytest.raises(SandboxInfraError, match="Handshake status 404"):
            await env.exec("true")


async def test_a_hung_connect_is_an_infra_error(cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give up on an exec websocket that does not connect, then keep working."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 0.2)
    monkeypatch.setattr(gke, "_GROUP_KILL_SECONDS", 0.2)
    release = threading.Event()
    hung: list[bool] = []

    def hanging_stream(method: Any, name: str, namespace: str, **kwargs: Any) -> FakeSession:
        """Block the first connect until the test releases it.

        Args:
            method: The bound exec call.
            name: The pod name.
            namespace: The namespace.
            **kwargs: The exec options.

        Returns:
            The running session.
        """
        if not hung:
            hung.append(True)
            release.wait(10)
        return fake_stream(method, name, namespace, **kwargs)

    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "k8s_stream", hanging_stream)
        try:
            started = time.monotonic()
            with pytest.raises(SandboxInfraError, match="did not connect"):
                await env.exec("true", timeout_sec=60)
            assert time.monotonic() - started < 5
            assert (await env.exec("echo still")).stdout.strip() == "still"
        finally:
            release.set()


async def test_a_hung_connect_under_a_short_timeout_says_it_did_not_connect(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report a connect hang as such, not as a command that ran past its timeout."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 5.0)
    monkeypatch.setattr(gke, "_KILL_GRACE_SECONDS", 0)
    monkeypatch.setattr(gke, "_GROUP_KILL_SECONDS", 0.2)
    release = threading.Event()
    hung: list[bool] = []

    def hanging_stream(method: Any, name: str, namespace: str, **kwargs: Any) -> FakeSession:
        """Block the first connect until the test releases it.

        Args:
            method: The bound exec call.
            name: The pod name.
            namespace: The namespace.
            **kwargs: The exec options.

        Returns:
            The running session.
        """
        if not hung:
            hung.append(True)
            release.wait(10)
        return fake_stream(method, name, namespace, **kwargs)

    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "k8s_stream", hanging_stream)
        try:
            with pytest.raises(SandboxInfraError, match="did not connect in 1s") as raised:
                await env.exec("true", timeout_sec=1)
            assert "past its" not in str(raised.value)
        finally:
            release.set()


async def test_a_hung_connect_during_start_stops_the_exec_polls(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail the start after one hung exec probe, on the provider's own session pool."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 0.2)
    monkeypatch.setattr(gke, "_GROUP_KILL_SECONDS", 0.2)
    release = threading.Event()
    threads: list[str] = []

    def hanging_stream(method: Any, name: str, namespace: str, **kwargs: Any) -> FakeSession:
        """Block every connect until the test releases it.

        Args:
            method: The bound exec call.
            name: The pod name.
            namespace: The namespace.
            **kwargs: The exec options.

        Returns:
            The running session.
        """
        threads.append(threading.current_thread().name)
        release.wait(10)
        return fake_stream(method, name, namespace, **kwargs)

    monkeypatch.setattr(gke, "k8s_stream", hanging_stream)
    env = GKEEnvironment(_spec())
    try:
        with pytest.raises(SandboxInfraError, match="did not connect"):
            await env.start()
    finally:
        release.set()
        await env.stop()
    assert len(threads) == 1
    assert threads[0].startswith("skynet-gke-exec")


async def test_a_queued_session_gives_up_at_once_when_the_pool_is_full(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop a session that still waits for a pool thread, with no wait for its thread."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 0.2)
    monkeypatch.setattr(gke, "_GROUP_KILL_SECONDS", 5.0)
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "_SESSION_POOL", pool)
        blocker = pool.submit(release.wait, 10)
        try:
            started = time.monotonic()
            with pytest.raises(SandboxInfraError, match="did not connect"):
                await env._call(env._name, ["true"])
            assert time.monotonic() - started < 2
        finally:
            release.set()
            blocker.result(5)
            pool.shutdown()


async def test_a_connect_at_the_limit_is_not_reported_as_a_command_that_did_not_start(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Say the session connected late when the connect ends just after the limit."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 0.2)
    monkeypatch.setattr(gke, "_GROUP_KILL_SECONDS", 5.0)

    def late_stream(method: Any, name: str, namespace: str, **kwargs: Any) -> FakeSession:
        """Connect just after the connect limit runs out.

        Args:
            method: The bound exec call.
            name: The pod name.
            namespace: The namespace.
            **kwargs: The exec options.

        Returns:
            The running session.
        """
        time.sleep(0.4)
        return fake_stream(method, name, namespace, **kwargs)

    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "k8s_stream", late_stream)
        with pytest.raises(SandboxInfraError, match="connected only after") as raised:
            await env._call(env._name, ["true"])
        assert not isinstance(raised.value, gke._ConnectTimeoutError)


async def test_a_late_connect_never_sends_the_env_stream(cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """Close a session that connects after its limit, so the command never runs without a kill."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 0.2)
    monkeypatch.setattr(gke, "_GROUP_KILL_SECONDS", 5.0)
    sessions: list[FakeSession] = []

    def late_stream(method: Any, name: str, namespace: str, **kwargs: Any) -> FakeSession:
        """Connect the first session just after the connect limit runs out.

        Args:
            method: The bound exec call.
            name: The pod name.
            namespace: The namespace.
            **kwargs: The exec options.

        Returns:
            The running session.
        """
        if not sessions:
            time.sleep(0.4)
        session = fake_stream(method, name, namespace, **kwargs)
        sessions.append(session)
        return session

    async def lost_kill(name: str, state: str) -> None:
        """Drop the kill, as when it cannot reach the pod.

        Args:
            name: The pod name.
            state: The command's state id.
        """

    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "k8s_stream", late_stream)
        monkeypatch.setattr(env, "_kill_group", lost_kill)
        with pytest.raises(SandboxInfraError, match="connected only after"):
            await env.exec("touch ran")
        late = sessions[0].process
        assert late.wait(5) != 0
        assert not (cluster.root / env._name / "ran").exists()


@pytest.mark.parametrize(
    ("stream", "runs"),
    [(b"A=1\0\0", True), (b"A=1\0", False), (b"A=1", False), (b"", False)],
)
def test_the_env_loader_runs_the_command_only_after_the_end_marker(tmp_path: Path, stream: bytes, runs: bool) -> None:
    """Refuse to run the command when stdin ends before the empty pair."""
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", gke._ENV_LOADER, "skynet-env", "sh", "-c", 'echo "$A" > ran'],
        input=stream,
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is runs
    assert (tmp_path / "ran").exists() is runs
    if runs:
        assert (tmp_path / "ran").read_text() == "1\n"


async def test_an_exec_dropped_from_a_full_pool_sends_no_kill(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail an exec that never left the pool queue at once, with no kill that queues again."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 1.0)
    monkeypatch.setattr(gke, "_GROUP_KILL_SECONDS", 5.0)
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    kills: list[str] = []

    async def record_kill(name: str, state: str) -> None:
        """Record a kill instead of sending it.

        Args:
            name: The pod name.
            state: The command's state id.
        """
        kills.append(state)

    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "_SESSION_POOL", pool)
        monkeypatch.setattr(env, "_kill_group", record_kill)
        blocker = pool.submit(release.wait, 10)
        try:
            started = time.monotonic()
            with pytest.raises(gke._DroppedSessionError, match="did not connect"):
                await env.exec("true", timeout_sec=60)
            # A wait for the queued job would last until the 5s kill limit.
            # The margin keeps a slow runner below it.
            assert time.monotonic() - started < 4
            assert kills == []
        finally:
            release.set()
            blocker.result(5)
            pool.shutdown()


async def test_a_cancelled_exec_dropped_from_a_full_pool_sends_no_kill(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel an exec that never left the pool queue at once, with no kill that queues again."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 30.0)
    monkeypatch.setattr(gke, "_GROUP_KILL_SECONDS", 5.0)
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    kills: list[str] = []

    async def record_kill(name: str, state: str) -> None:
        """Record a kill instead of sending it.

        Args:
            name: The pod name.
            state: The command's state id.
        """
        kills.append(state)

    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "_SESSION_POOL", pool)
        monkeypatch.setattr(env, "_kill_group", record_kill)
        blocker = pool.submit(release.wait, 10)
        try:
            task = asyncio.create_task(env.exec("true", timeout_sec=60))
            await asyncio.sleep(0.2)
            started = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # A wait for the queued job would last until the 5s kill limit.
            assert time.monotonic() - started < 3
            assert kills == []
        finally:
            release.set()
            blocker.result(5)
            pool.shutdown()


async def test_a_timeout_scope_over_a_queued_exec_raises_a_timeout(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let ``asyncio.timeout`` turn the cancel of a queued exec into ``TimeoutError``, with no kill."""
    monkeypatch.setattr(gke, "_CONNECT_SECONDS", 30.0)
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    kills: list[str] = []

    async def record_kill(name: str, state: str) -> None:
        """Record a kill instead of sending it.

        Args:
            name: The pod name.
            state: The command's state id.
        """
        kills.append(state)

    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "_SESSION_POOL", pool)
        monkeypatch.setattr(env, "_kill_group", record_kill)
        blocker = pool.submit(release.wait, 10)
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.3):
                    await env.exec("true", timeout_sec=60)
            assert kills == []
        finally:
            release.set()
            blocker.result(5)
            pool.shutdown()


def _stalled_websocket_server(server: socket.socket, done: threading.Event) -> None:
    """Accept one websocket handshake, then send half a frame and go quiet.

    Args:
        server: The listening socket.
        done: Set by the test when the server may close.
    """
    connection, _ = server.accept()
    with connection:
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        key = next(
            line.split(b":", 1)[1].strip()
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key:")
        )
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        connection.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        # A binary frame header that announces 10 bytes, then only 3 of them.
        connection.sendall(bytes([0x82, 10]) + b"abc")
        done.wait(10)


def test_the_websocket_keeps_its_timeout_after_the_connect() -> None:
    """End a read of a frame that stops half way when the scope's timeout runs out."""
    if gke.k8s_ws_client is None:
        pytest.skip("The kubernetes package is not installed.")
    configuration = pytest.importorskip("kubernetes").client.Configuration()
    done = threading.Event()
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        thread = threading.Thread(target=_stalled_websocket_server, args=(server, done), daemon=True)
        thread.start()
        url = f"ws://127.0.0.1:{server.getsockname()[1]}/"
        try:
            with gke._connect_timeout(0.3):
                websocket = gke.k8s_ws_client.create_websocket(configuration, url)
            assert websocket.gettimeout() == 0.3
            started = time.monotonic()
            with pytest.raises(Exception, match="timed out"):
                websocket.recv_data_frame(True)
            assert time.monotonic() - started < 5
            websocket.shutdown()
        finally:
            done.set()
            thread.join(5)


def test_the_websocket_connect_obeys_the_scope_timeout() -> None:
    """End a connect to a server that never answers when the scope's timeout runs out."""
    if gke.k8s_ws_client is None:
        pytest.skip("The kubernetes package is not installed.")
    assert gke.k8s_ws_client.WebSocket is gke._TimedWebSocket
    configuration = pytest.importorskip("kubernetes").client.Configuration()
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        url = f"ws://127.0.0.1:{server.getsockname()[1]}/"
        started = time.monotonic()
        with gke._connect_timeout(0.3), pytest.raises(Exception, match="timed out"):
            gke.k8s_ws_client.create_websocket(configuration, url)
        assert time.monotonic() - started < 5
    assert getattr(gke._connect_scope, "seconds", None) is None


HANDSHAKE_403 = (
    "Handshake status 403 Forbidden -+-+- {'audit-id': 'abc'} -+-+- "
    "b'pods is forbidden: User \"robot@project.iam.gserviceaccount.com\" cannot create pods/exec'"
)


async def test_a_refused_exec_handshake_is_fatal(cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise SandboxFatalError for a 403 exec handshake, without its headers or body."""

    def refused_stream(*args: Any, **kwargs: Any) -> FakeSession:
        """Refuse the handshake as ``websocket_call`` reports it.

        Args:
            *args: The exec call and its target.
            **kwargs: The exec options.

        Raises:
            FakeApiError: Always, with status 0.
        """
        raise FakeApiError(0, HANDSHAKE_403)

    async with GKEEnvironment(_spec()) as env:
        monkeypatch.setattr(gke, "k8s_stream", refused_stream)
        with pytest.raises(SandboxFatalError, match="RBAC") as raised:
            await env.exec("true")
    assert "403 Forbidden" in str(raised.value)
    assert "-+-+-" not in str(raised.value)
    assert "gserviceaccount" not in str(raised.value)


async def test_a_refused_exec_handshake_fails_the_start(cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the start at the first refused exec probe instead of polling until the deadline."""
    calls: list[bool] = []

    def refused_stream(*args: Any, **kwargs: Any) -> FakeSession:
        """Refuse the handshake as ``websocket_call`` reports it.

        Args:
            *args: The exec call and its target.
            **kwargs: The exec options.

        Raises:
            FakeApiError: Always, with status 0.
        """
        calls.append(True)
        raise FakeApiError(0, HANDSHAKE_403)

    monkeypatch.setattr(gke, "k8s_stream", refused_stream)
    with pytest.raises(SandboxFatalError):
        async with GKEEnvironment(_spec()):
            pass
    assert len(calls) == 1
    assert cluster.verbs()[-1] == "delete"


def test_the_status_comes_from_the_wrapped_handshake_error() -> None:
    """Read the status from the error that ``websocket_call`` wrapped."""

    class BadStatusError(Exception):
        """Stand in for websocket-client's ``WebSocketBadStatusException``."""

        status_code = 401

    try:
        try:
            raise BadStatusError("Handshake status 401 Unauthorized")
        except BadStatusError as inner:
            raise FakeApiError(0, f"wrapped {type(inner).__name__}") from None
    except FakeApiError as error:
        wrapped = error
    assert wrapped.__context__ is not None
    assert gke._status(wrapped) == 401
    assert gke._status(FakeApiError(0, "Handshake status 401 Unauthorized")) == 401
    assert gke._status(FakeApiError(0, "Connection refused")) is None
    assert gke._status(FakeApiError(404, "Not Found")) == 404


def test_describe_drops_the_handshake_headers_and_body() -> None:
    """Keep only the status line of a refused handshake."""
    assert gke._describe(FakeApiError(0, HANDSHAKE_403)) == "0 Handshake status 403 Forbidden"
    assert gke._describe(ValueError(HANDSHAKE_403)) == "Handshake status 403 Forbidden"


async def test_upload_refuses_an_empty_path(cluster: FakeCluster) -> None:
    """Refuse an empty sandbox path."""
    async with GKEEnvironment(_spec()) as env:
        with pytest.raises(SandboxSpecError):
            await env.upload_files({"": "x"})


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_fatal(cluster: FakeCluster, status: int) -> None:
    """Raise SandboxFatalError without the response body, and still try the delete."""
    cluster.create_error = FakeApiError(status, "Forbidden")
    with pytest.raises(SandboxFatalError, match="RBAC") as raised:
        async with GKEEnvironment(_spec()):
            pass
    assert "secret-body" not in str(raised.value)
    assert cluster.verbs() == ["connect", "create", "delete"]
    assert all(client.closed for client in cluster.clients)


async def test_other_create_failures_are_infra_errors_without_a_retry(cluster: FakeCluster) -> None:
    """Raise SandboxInfraError once for a refusal that is not about auth."""
    cluster.create_error = FakeApiError(500, "Internal Server Error")
    with pytest.raises(SandboxInfraError, match="500 Internal Server Error"):
        async with GKEEnvironment(_spec()):
            pass
    assert cluster.verbs().count("create") == 1


async def test_a_bad_kubeconfig_is_fatal(cluster: FakeCluster) -> None:
    """Raise SandboxFatalError when the kubeconfig does not load, and create nothing."""
    cluster.config_error = ValueError("context 'nope' not found")
    with pytest.raises(SandboxFatalError, match="kubeconfig"):
        async with GKEEnvironment(_spec(), context="nope"):
            pass
    assert cluster.verbs() == ["connect"]


@pytest.mark.parametrize(
    ("status", "match"),
    [
        ({"phase": "Failed", "reason": "Evicted"}, "phase Failed"),
        (
            {
                "phase": "Pending",
                "containerStatuses": [
                    {"name": "main", "ready": False, "state": {"waiting": {"reason": "ImagePullBackOff"}}}
                ],
            },
            "ImagePullBackOff",
        ),
    ],
)
async def test_a_failed_pod_raises_once_without_a_retry(
    cluster: FakeCluster, status: dict[str, Any], match: str
) -> None:
    """Raise SandboxInfraError for a terminal phase or an image pull failure, after one create."""
    cluster.status = status
    with pytest.raises(SandboxInfraError, match=match):
        async with GKEEnvironment(_spec()):
            pass
    verbs = cluster.verbs()
    assert verbs.count("create") == 1
    assert verbs[-1] == "delete"


async def test_a_pod_that_stays_pending_times_out(cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise StartTimeoutError when the pod is not ready in time."""
    monkeypatch.setattr(GKEEnvironment, "ready_seconds", 0.2)
    monkeypatch.setattr(GKEEnvironment, "poll_seconds", 0.05)
    cluster.status = {"phase": "Pending"}
    with pytest.raises(StartTimeoutError):
        async with GKEEnvironment(_spec()):
            pass
    assert cluster.verbs()[-1] == "delete"


async def test_start_twice_raises(cluster: FakeCluster) -> None:
    """Refuse a second start of the same sandbox."""
    async with GKEEnvironment(_spec()) as env:
        with pytest.raises(RuntimeError, match="already started"):
            await env.start()


async def test_stop_deletes_once_logs_a_failure_and_closes_the_client(
    cluster: FakeCluster, caplog: pytest.LogCaptureFixture
) -> None:
    """Delete the pod once; log a failed delete, do not raise, and close the client."""
    env = GKEEnvironment(_spec())
    await env.start()
    cluster.delete_error = FakeApiError(503, "Service Unavailable")
    await env.stop()
    await env.stop()
    assert cluster.verbs().count("delete") == 1
    assert "503 Service Unavailable" in caplog.text
    assert cluster.clients[0].closed


async def test_stop_job_sandboxes_continues_after_a_failure(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Count only the confirmed deletes, try every pod, and use the given namespace and context."""
    job_id = f"job/{uuid.uuid4().hex}"
    envs = [GKEEnvironment(_spec(tags={JOB_TAG: job_id}), namespace="team-a") for _ in range(3)]
    for env in envs:
        await env.start()
    failing = envs[0].pod_name
    real_delete = gke._delete_pod_sync

    def flaky_delete(api_client: Any, namespace: str, name: str) -> bool:
        """Fail the delete of the first pod only."""
        if name == failing:
            return False
        return real_delete(api_client, namespace, name)

    monkeypatch.setattr(gke, "_delete_pod_sync", flaky_delete)
    assert await GKEEnvironment.stop_job_sandboxes(job_id, namespace="team-a", context="ctx-b") == 2
    assert ("connect", "ctx-b") in cluster.calls
    assert cluster.clients[-1].closed
    monkeypatch.undo()
    for env in envs:
        await env.stop()


async def test_stop_job_sandboxes_logs_a_bad_kubeconfig(cluster: FakeCluster, caplog: pytest.LogCaptureFixture) -> None:
    """Return 0 and log when the kubeconfig does not load."""
    cluster.config_error = ValueError("no config")
    assert await GKEEnvironment.stop_job_sandboxes("job-1") == 0
    assert "no config" in caplog.text


def test_the_real_client_has_the_calls_the_provider_makes() -> None:
    """Check the call names and parameters against the installed kubernetes package."""
    kubernetes = pytest.importorskip("kubernetes")
    stream_module = pytest.importorskip("kubernetes.stream")
    api = kubernetes.client.CoreV1Api
    expected = {
        "create_namespaced_pod": {"namespace", "body"},
        "read_namespaced_pod": {"name", "namespace"},
        "list_namespaced_pod": {"namespace", "label_selector"},
        "delete_namespaced_pod": {"name", "namespace", "grace_period_seconds"},
        "connect_get_namespaced_pod_exec": {"name", "namespace", "command", "container", "stdin", "stdout", "tty"},
    }
    for method, params in expected.items():
        assert params <= set(inspect.signature(getattr(api, method)).parameters), method
    config_params = set(inspect.signature(kubernetes.config.new_client_from_config).parameters)
    assert {"context", "persist_config"} <= config_params
    assert {"write_stdin", "update", "is_open", "read_stdout", "read_stderr", "close"} <= set(
        dir(stream_module.ws_client.WSClient)
    )
    assert callable(stream_module.stream)

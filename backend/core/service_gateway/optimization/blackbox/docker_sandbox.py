"""Local Docker sandbox runtime for the single-user Mac app (local mode).

In local mode the protected parent opens its boxes as containers on this
machine instead of Vercel microVMs. The guest contract stays the same: files
and commands relative to a writable working directory, output streamed while
a command runs (the model mailbox answers guest requests mid-command), and no
network unless the parent asked for an allowlisted setup box. Such a box gets
its own internal Docker network with no route to the internet or to this Mac's
loopback; its only way to the internet is an egress proxy sidecar
(``docker_egress_proxy``) that tunnels HTTPS to the allowlisted hosts alone,
matching the Vercel allowlist. The network's gateway address is the Docker VM
itself, so services listening there stay reachable from the box. Containers run
with a read-only root filesystem, a fixed CPU and memory profile, a wall-time
limit and the ``skynet.job`` label the desktop shell and the worker sweep use
for cleanup. Local sandboxes are never billed, so the budget is never charged.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import posixpath
import shutil
import subprocess
import tarfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ....billing.runtime import BudgetRuntime
from ....config import Settings
from ....exceptions import ServiceError
from ....i18n import t
from . import sandbox_contract
from .docker_egress_proxy import READY_MARKER
from .sandbox_log import logged_command

logger = logging.getLogger(__name__)

# The desktop shell sweeps leftover boxes by this label on quit (plan §1.7).
DOCKER_JOB_LABEL = "skynet.job"
WORKSPACE_DIR = "/workspace"
# The image's root stays read-only; only these tmpfs mounts are writable.
# Docker's tmpfs default is noexec, which would break native venvs and scripts.
_TMPFS_MOUNTS = (
    f"{WORKSPACE_DIR}:rw,exec,nosuid,mode=1777,size=4g",
    "/tmp:rw,exec,nosuid,mode=1777,size=1g",
)
_PIDS_LIMIT = 1024
_CLI_TIMEOUT_SECONDS = 60.0
_PROBE_TIMEOUT_SECONDS = 10.0
# One catalog request asks for availability several times; a short-lived answer
# keeps a stalled daemon from costing one probe timeout per question.
_PROBE_CACHE_SECONDS = 5.0
_probe_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_probe_cache_lock = threading.Lock()
# Reads NUL-separated NAME=value pairs from stdin and runs the command under
# them. Values travel neither on a host command line (any local user can read
# process arguments) nor in the Docker CLI's own environment. `env`, unlike
# bash's `export`, takes any name an environment can hold, as Vercel does.
_ENV_LOADER = 'pairs=(); while IFS= read -r -d "" pair; do pairs+=("$pair"); done; exec env -- "${pairs[@]}" "$@"'
# A login or interactive shell reads ~/.bash_profile, ~/.profile or ~/.bashrc,
# which a version's code can write. Some bash builds (Debian, Homebrew) also read
# ~/.bashrc in a plain `bash -c` when SSH_CLIENT is set. Code planted there would run before every
# later box command: the reset, the metric install and the metric step (E38).
_SHELL = ("bash", "--noprofile", "--norc", "-c")
_MISSING_FILE_EXIT = 3
_READ_FILE_SCRIPT = f'test -f "$1" || exit {_MISSING_FILE_EXIT}; exec cat -- "$1"'
# The proxy sidecar's own way out; the setup box never joins it.
_UPLINK_NETWORK = "bridge"
_EGRESS_PROXY_SOURCE = Path(__file__).with_name("docker_egress_proxy.py")
_EGRESS_PORT = 3128
_EGRESS_READY_SECONDS = 30.0
# Above the port range a non-root user may bind, and without any capability.
_EGRESS_USER = "65534:65534"
# A setup network has no container between ``network create`` and the proxy's
# ``docker run``. That window lasts at most two CLI calls plus the proxy's ready
# wait, so the idle-network sweep leaves younger networks alone.
_IDLE_NETWORK_MIN_AGE_SECONDS = 300.0


def _docker(
    docker_bin: str, args: list[str], *, stdin: bytes | None = None, timeout: float = _CLI_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[bytes]:
    """Run one short Docker CLI command and capture its output.

    Args:
        docker_bin: Docker (or OrbStack) CLI path.
        args: Arguments after the binary.
        stdin: Bytes fed to the command's standard input.
        timeout: Wall-clock limit for the CLI call.

    Returns:
        The completed process with raw stdout and stderr.

    Raises:
        ServiceError: When the CLI is missing, cannot run, or does not answer in time.
    """
    try:
        return subprocess.run([docker_bin, *args], input=stdin, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise ServiceError(t("sandbox.docker_unavailable")) from error
    except OSError as error:
        # APP_DOCKER_BIN can name a directory or a file without the execute
        # bit; report it like a missing CLI so callers degrade, not crash.
        raise ServiceError(t("sandbox.docker_unavailable")) from error
    except subprocess.TimeoutExpired as error:
        raise ServiceError(f"docker {args[0]} did not answer within {timeout:.0f}s.") from error


def _stderr_line(result: subprocess.CompletedProcess[bytes]) -> str:
    """Return the last non-empty line Docker wrote to stderr.

    Args:
        result: A finished Docker CLI call.

    Returns:
        The line, cut to a readable length, or the exit status when stderr is empty.
    """
    lines = [line.strip() for line in result.stderr.decode(errors="replace").splitlines() if line.strip()]
    return lines[-1][:240] if lines else f"exit status {result.returncode}"


def _relative_path(path: str) -> str:
    """Keep a sandbox file path inside the working directory.

    Args:
        path: Path relative to the working directory.

    Returns:
        The normalized relative POSIX path.

    Raises:
        ValueError: When ``path`` is absolute or would escape the working directory.
    """
    normalized = posixpath.normpath(path)
    if not path or "\x00" in path or posixpath.isabs(normalized) or normalized.split("/")[0] == "..":
        raise ValueError(f"sandbox path escapes the working directory: {path!r}")
    return normalized


@dataclass(frozen=True)
class _Egress:
    """The internal network and proxy sidecar behind one allowlisted setup box."""

    network: str
    proxy: str

    @property
    def env(self) -> dict[str, str]:
        """Point the box's package managers at the proxy.

        Returns:
            Proxy variables in the forms pip, uv, npm, yarn, cargo, go and git read.
        """
        url = f"http://{self.proxy}:{_EGRESS_PORT}"
        names = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "npm_config_https_proxy", "npm_config_proxy")
        # The variables outlive disable_network() and the same box later scores
        # the repository, so loopback must never go through the removed proxy.
        loopback = dict.fromkeys(("NO_PROXY", "no_proxy"), "localhost,127.0.0.1,::1")
        return dict.fromkeys(names, url) | loopback


def _remove_egress(docker_bin: str, egress: _Egress) -> None:
    """Remove a setup box's proxy sidecar and then its network. Never raises.

    Args:
        docker_bin: Docker CLI path.
        egress: The sidecar and network to remove.
    """
    for args, missing in (
        (["rm", "-f", egress.proxy], b"No such container"),
        (["network", "rm", egress.network], b"not found"),
    ):
        try:
            result = _docker(docker_bin, args)
        except ServiceError as error:
            logger.warning("could not remove local sandbox egress %s: %s", args[-1], error)
            continue
        if result.returncode != 0 and missing not in result.stderr:
            logger.warning("could not remove local sandbox egress %s: %s", args[-1], _stderr_line(result))


def _label_args(labels: Mapping[str, str]) -> list[str]:
    """Turn labels into Docker ``--label`` arguments.

    Args:
        labels: Label name → value.

    Returns:
        Alternating ``--label`` flags and ``name=value`` items.
    """
    return [item for key, value in labels.items() for item in ("--label", f"{key}={value}")]


def _env_args(env: Mapping[str, str]) -> list[str]:
    """Turn a box's fixed start environment into Docker ``-e`` arguments.

    Args:
        env: The box's non-secret start environment (home, temp dir, proxy).

    Returns:
        Alternating ``-e`` flags and ``NAME=value`` items.
    """
    args: list[str] = []
    for name, value in env.items():
        args += ["-e", f"{name}={value}"]
    return args


def _env_stream(env: Mapping[str, str]) -> str:
    """Encode a command's environment for ``_ENV_LOADER``.

    Args:
        env: The command's environment.

    Returns:
        NUL-terminated ``NAME=value`` pairs.

    Raises:
        ServiceError: When a name or value holds a NUL, which no environment can carry.
    """
    if any("\0" in name or "\0" in value for name, value in env.items()):
        raise ServiceError("Sandbox environment names and values cannot contain NUL characters.")
    return "".join(f"{name}={value}\0" for name, value in env.items())


class DockerSandboxSession:
    """Session over one running container, addressed by its id."""

    def __init__(
        self,
        docker_bin: str,
        container: str,
        *,
        env: Mapping[str, str],
        owner: str | None,
        egress: _Egress | None = None,
    ) -> None:
        """Wrap a started container.

        Args:
            docker_bin: Docker CLI path.
            container: The container id ``docker run -d`` printed.
            env: The box's environment. Every command gets it, and its values are redacted from logged output.
            owner: Job the box belongs to, which its logged output is routed to.
            egress: The proxy sidecar and internal network of an allowlisted setup box.
        """
        self._docker_bin = docker_bin
        self.container = container
        self._env = dict(env)
        self.log_owner = owner
        self.egress = egress
        self._closed = False
        self._lock = threading.Lock()

    def _drop_egress(self) -> None:
        """Remove the box's proxy sidecar and network once; later calls do nothing."""
        with self._lock:
            egress, self.egress = self.egress, None
        if egress is not None:
            _remove_egress(self._docker_bin, egress)

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write text files at paths relative to the working directory, creating parents.

        One tar stream per call keeps a many-file write to a single exec, and
        it runs beside an in-flight command because the model mailbox answers
        guest requests while that command still runs.

        Args:
            files: Relative path → content.

        Raises:
            ServiceError: When the container refuses the files.
        """
        if not files:
            return
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path, text in files.items():
                data = text.encode("utf-8")
                info = tarfile.TarInfo(_relative_path(path))
                info.size = len(data)
                info.mode = 0o644
                info.mtime = int(time.time())
                archive.addfile(info, io.BytesIO(data))
        result = _docker(
            self._docker_bin,
            ["exec", "-i", "-w", WORKSPACE_DIR, self.container, "tar", "-x", "-f", "-", "--no-same-owner"],
            stdin=buffer.getvalue(),
        )
        if result.returncode != 0:
            raise ServiceError(f"Could not write files into the local sandbox: {_stderr_line(result)}")

    @logged_command
    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: sandbox_contract.OutputSink | None = None,
        sudo: bool = False,
    ) -> sandbox_contract.CommandResult:
        """Run ``command`` through ``bash --noprofile --norc -c`` in the working directory and stream its output.

        Args:
            command: Shell command line.
            env: Extra environment for this command.
            timeout_seconds: Kill the command after this long.
            on_output: Receives each line of output as it is written.
            sudo: Run as the container's root user.

        Returns:
            Exit code, captured output and whether the timeout fired.

        Raises:
            ServiceError: When the command outlives its kill deadline or Docker is missing.
        """
        inner = [*_SHELL, command]
        deadline = None
        seconds = None
        if timeout_seconds is not None:
            seconds = max(1, math.ceil(timeout_seconds))
            inner = ["timeout", "--signal=KILL", f"{seconds}s", *inner]
            deadline = seconds + sandbox_contract.KILL_GRACE_SECONDS
        stream = _env_stream({**self._env, **(env or {})})
        loader = [*_SHELL, _ENV_LOADER, "skynet-env"]
        user = ["-u", "root"] if sudo else []
        argv = [self._docker_bin, "exec", "-i", *user, "-w", WORKSPACE_DIR, self.container, *loader, *inner]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as error:
            raise ServiceError(t("sandbox.docker_unavailable")) from error
        # A CLI that exits early (a removed container) closes the pipe; its
        # exit code and stderr then report why.
        with contextlib.suppress(BrokenPipeError):
            process.stdin.write(stream)
        with contextlib.suppress(BrokenPipeError):
            process.stdin.close()
        # One reader per pipe hands each line to the caller as it lands; the
        # model mailbox must see a guest request while the command still runs.
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
        readers = [
            threading.Thread(target=sandbox_contract._pump, args=(name, pipe, captured[name], on_output), daemon=True)
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=deadline)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise ServiceError(
                f"command still running {sandbox_contract.KILL_GRACE_SECONDS:.0f}s past its {seconds}s timeout: {sandbox_contract._brief(command)}"
            ) from error
        finally:
            for reader in readers:
                reader.join()
        exit_code = process.returncode
        return sandbox_contract.CommandResult(
            exit_code=exit_code,
            stdout="".join(captured["stdout"]),
            stderr="".join(captured["stderr"]),
            timed_out=timeout_seconds is not None and exit_code in sandbox_contract._TIMEOUT_EXIT_CODES,
        )

    def read_file(self, path: str) -> str | None:
        """Return the text of ``path`` (relative to the working directory), or ``None`` when absent.

        Args:
            path: Relative file path.

        Returns:
            The file's text, or ``None``.

        Raises:
            ServiceError: When the container cannot be read.
        """
        result = _docker(
            self._docker_bin,
            ["exec", "-w", WORKSPACE_DIR, self.container, "sh", "-c", _READ_FILE_SCRIPT, "sh", _relative_path(path)],
        )
        if result.returncode == _MISSING_FILE_EXIT:
            return None
        if result.returncode != 0:
            raise ServiceError(f"Could not read {path!r} from the local sandbox: {_stderr_line(result)}")
        return result.stdout.decode("utf-8", errors="replace")

    def disable_network(self) -> None:
        """Detach the container from every network, confirm that only loopback is left, and drop the proxy.

        Raises:
            ServiceError: When Docker still reports a network on the container.
        """
        for network in self._networks():
            result = _docker(self._docker_bin, ["network", "disconnect", "--force", network, self.container])
            if result.returncode != 0:
                raise ServiceError(f"Could not switch the local sandbox's network off: {_stderr_line(result)}")
        if self._networks():
            raise ServiceError("Docker did not confirm that the scoring box's network is off.")
        self._drop_egress()

    def _networks(self) -> list[str]:
        """List the networks the container is attached to, other than ``none``.

        Returns:
            Network names.

        Raises:
            ServiceError: When Docker cannot inspect the container.
        """
        result = _docker(
            self._docker_bin, ["inspect", "--format", "{{json .NetworkSettings.Networks}}", self.container]
        )
        if result.returncode != 0:
            raise ServiceError(f"Could not inspect the local sandbox: {_stderr_line(result)}")
        networks = json.loads(result.stdout.decode() or "null") or {}
        return [name for name in networks if name != "none"]

    def close(self) -> None:
        """Remove the container, then its proxy and network. Never raises; local boxes carry no usage to settle."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            result = _docker(self._docker_bin, ["rm", "-f", self.container])
        except ServiceError as error:
            logger.warning("could not remove local sandbox %s: %s", self.container[:12], error)
        else:
            if result.returncode != 0 and b"No such container" not in result.stderr:
                logger.warning("could not remove local sandbox %s: %s", self.container[:12], _stderr_line(result))
        self._drop_egress()


class DockerSandboxRuntime:
    """Sandbox runtime over the local Docker daemon (local mode only)."""

    injects_headers = False

    def __init__(self, image: str, *, docker_bin: str = "docker", budget: BudgetRuntime | None = None) -> None:
        """Bind the runtime to a locally built image.

        Args:
            image: Image id (``sha256:<64 hex>``) the boxes boot from unless their spec names one.
            docker_bin: Docker CLI path; OrbStack works through the same CLI.
            budget: The run's budget when the runtime serves a protected run. It
                only marks the runtime protected: local boxes are never charged.
        """
        self._image = image
        self._docker_bin = docker_bin
        self.protected = budget is not None
        # The broker prices a Vercel box before it opens any; local boxes are free.
        self.billable = False

    def open(self, spec: sandbox_contract.SandboxSpec) -> sandbox_contract.SandboxSession:
        """Start one container per ``spec``.

        Args:
            spec: Lifetime, environment, image, network and labels for the box.

        Returns:
            The session over the new container.

        Raises:
            ServiceError: When the spec asks for something local boxes cannot
                honour, or Docker cannot start the container.
        """
        if not math.isfinite(spec.lifetime_seconds) or spec.lifetime_seconds <= 0:
            raise ServiceError("The sandbox lifetime must be finite and positive.")
        if isinstance(spec.vcpus, bool) or spec.vcpus not in {1, *range(2, 33, 2)}:
            raise ServiceError("The sandbox must use 1 or an even number of vCPUs between 2 and 32.")
        if spec.inject_headers:
            raise ServiceError("A local sandbox has no network edge to inject credentials with.")
        if spec.network_disabled and spec.allowed_hosts:
            raise ServiceError("An offline sandbox cannot reach allowlisted hosts.")
        owner = spec.tags.get(sandbox_contract.JOB_TAG)
        labels = {**sandbox_contract.SANDBOX_TAG, **spec.tags, **({DOCKER_JOB_LABEL: owner} if owner else {})}
        name = sandbox_contract.unique_sandbox_name(spec.name or "skynet-local")
        image = spec.image or self._image
        # The default for a box without hosts is offline: Docker has no edge
        # policy, and its default bridge reaches services that listen only on
        # this Mac's loopback, so an allowlisted box goes out through the proxy.
        egress = self._open_egress(name, spec, labels, image) if spec.allowed_hosts else None
        try:
            container = self._run_box(name, spec, labels, image, egress)
        except BaseException:
            if egress is not None:
                _remove_egress(self._docker_bin, egress)
            raise
        return DockerSandboxSession(self._docker_bin, container, env=spec.env, owner=owner, egress=egress)

    def _run_box(
        self,
        name: str,
        spec: sandbox_contract.SandboxSpec,
        labels: Mapping[str, str],
        image: str,
        egress: _Egress | None,
    ) -> str:
        """Start the box's container.

        Args:
            name: Container name.
            spec: The box's spec.
            labels: Labels for the container.
            image: Image the box boots from.
            egress: The setup box's internal network and proxy, or ``None`` for an offline box.

        Returns:
            The container id.

        Raises:
            ServiceError: When Docker cannot start the container.
        """
        argv = [
            "run",
            "-d",
            "--rm",
            "--init",
            "--name",
            name,
            "--network",
            egress.network if egress else "none",
            "--cpus",
            str(spec.vcpus),
            "--memory",
            f"{spec.vcpus * 2048}m",
            "--pids-limit",
            str(_PIDS_LIMIT),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *[item for mount in _TMPFS_MOUNTS for item in ("--tmpfs", mount)],
            "-w",
            WORKSPACE_DIR,
            # spec.env can hold the gateway key, so each command gets it over stdin.
            *_env_args({"HOME": WORKSPACE_DIR, "TMPDIR": "/tmp", **(egress.env if egress else {})}),
            *_label_args(labels),
            "--entrypoint",
            "sleep",
            image,
            # The box's only process is this timer, so the container (and
            # every command in it) ends when its lifetime does.
            str(math.ceil(spec.lifetime_seconds)),
        ]
        result = _docker(self._docker_bin, argv)
        container = result.stdout.decode().strip()
        if result.returncode != 0 or not container:
            raise ServiceError(f"Could not start the local sandbox: {_stderr_line(result)}")
        return container

    def _open_egress(
        self, name: str, spec: sandbox_contract.SandboxSpec, labels: Mapping[str, str], image: str
    ) -> _Egress:
        """Create a setup box's internal network and start its egress proxy on it and on ``bridge``.

        The proxy runs on the box's own image, which is already on this
        machine, so no other image is pulled; it lives no longer than the box.

        Args:
            name: The box's container name, which the network and proxy names extend.
            spec: The box's spec, with its allowlisted hosts and lifetime.
            labels: Labels for the network and the proxy, so the job sweep finds them.
            image: Image the proxy runs on; it needs ``python3``.

        Returns:
            The network and proxy, ready to tunnel.

        Raises:
            ServiceError: When Docker cannot create either or the proxy does not report ready.
        """
        egress = _Egress(network=f"{name}-net", proxy=f"{name}-egress")
        created = _docker(self._docker_bin, ["network", "create", "--internal", *_label_args(labels), egress.network])
        if created.returncode != 0:
            raise ServiceError(f"Could not create the local sandbox's network: {_stderr_line(created)}")
        try:
            self._start_proxy(egress, spec, labels, image)
        except BaseException:
            _remove_egress(self._docker_bin, egress)
            raise
        return egress

    def _start_proxy(
        self, egress: _Egress, spec: sandbox_contract.SandboxSpec, labels: Mapping[str, str], image: str
    ) -> None:
        """Start the proxy sidecar, give it its way out and wait until it listens.

        Args:
            egress: The network to serve and the sidecar's name.
            spec: The box's spec, with its allowlisted hosts and lifetime.
            labels: Labels for the sidecar.
            image: Image the proxy runs on.

        Raises:
            ServiceError: When the sidecar does not start, connect or report ready in time.
        """
        argv = [
            "run",
            "-d",
            "--rm",
            "--init",
            "--name",
            egress.proxy,
            "--network",
            egress.network,
            "--cpus",
            "1",
            "--memory",
            "256m",
            "--pids-limit",
            "256",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _EGRESS_USER,
            *_label_args(labels),
            "--entrypoint",
            "python3",
            image,
            "-I",
            "-c",
            _EGRESS_PROXY_SOURCE.read_text(encoding="utf-8"),
            str(_EGRESS_PORT),
            str(math.ceil(spec.lifetime_seconds)),
            *sorted(set(spec.allowed_hosts)),
        ]
        started = _docker(self._docker_bin, argv)
        if started.returncode != 0:
            raise ServiceError(f"Could not start the local sandbox's egress proxy: {_stderr_line(started)}")
        connected = _docker(self._docker_bin, ["network", "connect", _UPLINK_NETWORK, egress.proxy])
        if connected.returncode != 0:
            raise ServiceError(f"Could not connect the local sandbox's egress proxy: {_stderr_line(connected)}")
        deadline = time.monotonic() + _EGRESS_READY_SECONDS
        while True:
            logs = _docker(self._docker_bin, ["logs", egress.proxy])
            if READY_MARKER.encode() in logs.stdout:
                return
            if time.monotonic() >= deadline:
                raise ServiceError(f"The local sandbox's egress proxy did not start: {_stderr_line(logs)}")
            time.sleep(0.2)

    def stop_job_sandboxes(self, owner_id: str) -> int:
        """Remove every container still up under ``owner_id``'s label, then the job's networks.

        Args:
            owner_id: The job whose boxes to remove.

        Returns:
            How many containers (boxes and egress proxies) were removed.
        """
        selector = f"label={DOCKER_JOB_LABEL}={owner_id}"
        listed = _docker(self._docker_bin, ["ps", "-q", "--filter", selector])
        if listed.returncode != 0:
            logger.warning("could not list local sandboxes of job %s: %s", owner_id, _stderr_line(listed))
            return 0
        stopped = 0
        for container in listed.stdout.decode().split():
            removed = _docker(self._docker_bin, ["rm", "-f", container])
            if removed.returncode != 0:
                logger.warning("could not remove local sandbox %s of job %s", container, owner_id)
                continue
            stopped += 1
        # A network can only go once no container is attached, so it goes last.
        networks = _docker(self._docker_bin, ["network", "ls", "-q", "--filter", selector])
        if networks.returncode != 0:
            logger.warning("could not list local sandbox networks of job %s: %s", owner_id, _stderr_line(networks))
            return stopped
        for network in networks.stdout.decode().split():
            if _docker(self._docker_bin, ["network", "rm", network]).returncode != 0:
                logger.warning("could not remove local sandbox network %s of job %s", network, owner_id)
        return stopped


def remove_idle_sandbox_networks(docker_bin: str) -> int:
    """Remove every sandbox network that no container uses any more.

    A setup box's internal network has no lifetime of its own: a killed
    backend leaves it behind after ``--rm`` removes the box and its proxy, and
    each one holds a /16 from Docker's small address pool. A network younger
    than ``_IDLE_NETWORK_MIN_AGE_SECONDS`` is skipped, because a box that is
    still opening has a network with no container yet; this keeps the sweep
    safe beside other workers or backends on the same daemon. It never raises:
    a failure is logged and skipped.

    Args:
        docker_bin: Docker (or OrbStack) CLI path.

    Returns:
        How many networks were removed.
    """
    filters = [
        arg for key, value in sandbox_contract.SANDBOX_TAG.items() for arg in ("--filter", f"label={key}={value}")
    ]
    try:
        listed = _docker(docker_bin, ["network", "ls", "-q", *filters])
    except ServiceError as error:
        logger.warning("could not list idle local sandbox networks: %s", error)
        return 0
    if listed.returncode != 0:
        logger.warning("could not list idle local sandbox networks: %s", _stderr_line(listed))
        return 0
    removed = 0
    for network in listed.stdout.decode().split():
        try:
            inspected = _docker(
                docker_bin, ["network", "inspect", "--format", "{{len .Containers}} {{.Created.Unix}}", network]
            )
            if inspected.returncode != 0:
                continue
            attached, _, created = inspected.stdout.decode().strip().partition(" ")
            if attached != "0" or not created.isdigit():
                continue
            if time.time() - int(created) < _IDLE_NETWORK_MIN_AGE_SECONDS:
                continue
            result = _docker(docker_bin, ["network", "rm", network])
        except ServiceError as error:
            logger.warning("could not remove idle local sandbox network %s: %s", network, error)
            continue
        if result.returncode != 0:
            logger.warning("could not remove idle local sandbox network %s: %s", network, _stderr_line(result))
            continue
        removed += 1
    return removed


def docker_sandbox_unavailable_reason(settings: Settings) -> str | None:
    """Explain why local Docker sandboxes cannot start, or return ``None``.

    The answer is reused for a few seconds per Docker CLI and image.

    Args:
        settings: The backend settings with the Docker CLI and local image id.

    Returns:
        A user-facing reason, or ``None`` when the daemon runs and the image exists.
    """
    key = (settings.docker_bin, settings.local_sandbox_image or "")
    now = time.monotonic()
    with _probe_cache_lock:
        cached = _probe_cache.get(key)
    if cached is not None and now - cached[0] < _PROBE_CACHE_SECONDS:
        return cached[1]
    reason = _probe_docker(settings)
    with _probe_cache_lock:
        _probe_cache[key] = (time.monotonic(), reason)
    return reason


def _probe_docker(settings: Settings) -> str | None:
    """Check the Docker CLI, the daemon and the local image once.

    Args:
        settings: The backend settings with the Docker CLI and local image id.

    Returns:
        A user-facing reason, or ``None`` when the daemon runs and the image exists.
    """
    if shutil.which(settings.docker_bin) is None:
        return t("sandbox.docker_unavailable")
    try:
        daemon = _docker(
            settings.docker_bin, ["info", "--format", "{{.ServerVersion}}"], timeout=_PROBE_TIMEOUT_SECONDS
        )
    except ServiceError:
        return t("sandbox.docker_unavailable")
    if daemon.returncode != 0:
        return t("sandbox.docker_unavailable")
    image = settings.local_sandbox_image
    if not image:
        return t("sandbox.docker_image_missing")
    try:
        found = _docker(settings.docker_bin, ["image", "inspect", "--format", "{{.Id}}", image])
    except ServiceError:
        return t("sandbox.docker_unavailable")
    if found.returncode != 0:
        return t("sandbox.docker_image_missing")
    return None


def docker_runtime_from_settings(settings: Settings, *, budget: BudgetRuntime | None = None) -> DockerSandboxRuntime:
    """Build the local Docker runtime from settings.

    Args:
        settings: The backend settings.
        budget: The run's budget when the runtime serves a protected run.

    Returns:
        The runtime over ``settings.local_sandbox_image``.
    """
    return DockerSandboxRuntime(settings.local_sandbox_image, docker_bin=settings.docker_bin, budget=budget)

"""Async OpenShift sandbox provider: one pod for each sandbox, driven by the ``oc`` CLI.

Adapted from Harbor 0.25.0 (Apache-2.0), openshift.py. Changes:

- The pod implements ``SandboxEnvironment``: every ``oc`` call runs through
  ``asyncio.create_subprocess_exec``, and a cancel never leaves a CLI behind.
- No retries. Harbor retries pod starts and file copies with tenacity; the
  Skynet error policy retries at the trial level, so a failed start raises once.
- No image build. Harbor builds the task image with a BuildConfig and
  ``oc start-build``; this provider boots an image that the spec or the
  caller names, which the cluster must be able to pull.
- The pod carries a job label for ``stop_job_sandboxes`` and an
  ``activeDeadlineSeconds`` from the spec lifetime, so the cluster ends a pod
  that a crashed worker leaves behind.
- Environment values reach the command on stdin, never on a command line or
  in the pod manifest, so ``ps`` and ``oc get pod -o yaml`` do not show them.
- Files move through ``oc exec -i`` with the content on stdin, not ``oc cp``,
  which needs ``tar`` in the image.
- A timeout or a cancel kills the command's process group in the pod.
  Killing the ``oc exec`` client alone does not stop the remote command.

Egress is open: the provider sets no NetworkPolicy, as in Harbor. Not for
production; the factory refuses it there.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import json
import logging
import math
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..resources import ResourceMode, SandboxSpec

logger = logging.getLogger(__name__)

OPENSHIFT_CAPABILITIES = EnvironmentCapabilities(
    name="openshift",
    network_modes=frozenset({NetworkMode.OPEN}),
)

DEFAULT_OC_BIN = "oc"
DEFAULT_SERVICE_ACCOUNT = "skynet-task"
CONTAINER_NAME = "main"
JOB_LABEL = "skynet.job"
SANDBOX_LABELS: Mapping[str, str] = {"skynet": "sandbox"}

# Harbor's set: enough for package installs and ``chown`` as root, without
# the default runtime set's NET_RAW, MKNOD or AUDIT_WRITE.
_CAPABILITIES = (
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "NET_BIND_SERVICE",
    "SETGID",
    "SETUID",
    "SYS_CHROOT",
)
# PID 1 must answer SIGTERM, or every delete waits the full grace period.
_IDLE_COMMAND = ("sh", "-c", "trap 'exit 0' TERM; while :; do sleep 3600 & wait $!; done")
_CLI_TIMEOUT_SECONDS = 60.0
_WHOAMI_TIMEOUT_SECONDS = 10.0
_DELETE_GRACE_SECONDS = 10
_KILL_GRACE_SECONDS = 15
_GROUP_KILL_SECONDS = 10.0
_TIMEOUT_EXIT_CODE = 124
_NO_GROUP_EXIT_CODE = 125
_MISSING_FILE_EXIT = 3
_READ_CHUNK_BYTES = 65536
_SHELL = ("bash", "--noprofile", "--norc", "-c")
# Reads NUL-terminated NAME=value pairs from stdin, so secrets never appear
# on a command line that ``ps`` in the pod or on the worker shows.
_ENV_LOADER = 'pairs=(); while IFS= read -r -d "" pair; do pairs+=("$pair"); done; exec env -- "${pairs[@]}" "$@"'
# The state files live under the pod's own TMPDIR. The wrapper runs outside
# the env loader, so a command env that sets TMPDIR cannot move them away
# from the kill and marker scripts. Job control (``set -m``) puts the command
# in its own process group, and keeps its stdin, which carries the env
# stream. Bash prints a job notice on its own stderr when it reaps a killed
# job, so the real stderr stays on fd 3 for the command alone. The timer
# leaves a marker, so a command that exits 124 by itself is not a timeout.
# A cancel can come before the wrapper writes the group id: the kill leaves
# a cancel marker, and the wrapper checks it before and after the write.
_STATE = 'state="${TMPDIR:-/tmp}/skynet-exec-$1"'
_EXEC_WRAPPER = (
    f"{_STATE}; limit=$2; shift 2; timer=; "
    'if [ -e "$state.cancel" ]; then rm -f -- "$state.cancel"; exit 137; fi; '
    'exec 3>&2 2>/dev/null; set -m; "$@" 2>&3 3>&- & child=$!; '
    'if ! echo "$child" > "$state.pid"; then kill -KILL -- "-$child"; wait "$child"; '
    f'rm -f -- "$state.pid" "$state.cancel"; : > "$state.nogroup"; exit {_NO_GROUP_EXIT_CODE}; fi; '
    'if [ -e "$state.cancel" ]; then kill -KILL -- "-$child" 2>/dev/null; fi; '
    'if [ -n "$limit" ]; then '
    '(sleep "$limit"; : > "$state.timeout"; kill -KILL -- "-$child") >/dev/null 2>&1 3>&- & timer=$!; fi; '
    'set +m; wait "$child"; code=$?; '
    'if [ -n "$timer" ]; then kill -KILL -- "-$timer" 2>/dev/null; fi; rm -f -- "$state.pid" "$state.cancel"; '
    f'if [ -e "$state.timeout" ] && [ "$code" = 137 ]; then exit {_TIMEOUT_EXIT_CODE}; fi; '
    'rm -f -- "$state.timeout"; exit "$code"'
)
_KILL_GROUP_SCRIPT = (
    f'{_STATE}; : > "$state.cancel"; '
    'for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$state.pid" ] && break; sleep 0.1; done; '
    'pgid=$(cat -- "$state.pid" 2>/dev/null); rm -f -- "$state.pid" "$state.timeout"; '
    'case $pgid in ""|*[!0-9]*|0|1) exit 0;; esac; kill -KILL -- "-$pgid" 2>/dev/null; exit 0'
)
_MARKER_SCRIPT = f'{_STATE}; test -e "$state.$2" && rm -f -- "$state.$2"'
_WRITE_FILE_SCRIPT = 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
_READ_FILE_SCRIPT = f'test -f "$1" || exit {_MISSING_FILE_EXIT}; exec cat -- "$1"'
_NAME_UNSAFE = re.compile(r"[^a-z0-9-]+")
_NAME_DASHES = re.compile(r"-{2,}")
_NAME_STEM_CHARS = 50
_LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?)?$")
_TERMINAL_PHASES = frozenset({"Failed", "Succeeded", "Unknown"})
_FAILED_WAIT_REASONS = frozenset(
    {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CreateContainerConfigError", "CreateContainerError"}
)
_AUTH_MARKERS = ("unauthorized", "must be logged in", "forbidden")


@dataclass(frozen=True)
class _CliResult:
    """Hold the result of one ``oc`` call."""

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def error_line(self) -> str:
        """Return the last non-empty stderr line, cut to 500 characters."""
        lines = [line for line in self.stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
        return lines[-1][:500] if lines else f"exit code {self.returncode}"


async def _kill(process: asyncio.subprocess.Process) -> None:
    """Kill a CLI process and wait for it to end.

    Args:
        process: The process to kill.
    """
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


async def _oc(
    oc_bin: str, args: Sequence[str], *, stdin: bytes | None = None, cli_seconds: float = _CLI_TIMEOUT_SECONDS
) -> _CliResult:
    """Run one ``oc`` command and capture its output.

    Args:
        oc_bin: The ``oc`` CLI path.
        args: The CLI arguments.
        stdin: Bytes for the command's stdin, or ``None`` for no input.
        cli_seconds: Kill the CLI after this many seconds.

    Returns:
        The exit code and the captured output.

    Raises:
        SandboxInfraError: The CLI is missing or did not finish in time.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            oc_bin,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise SandboxInfraError(f"The oc CLI {oc_bin!r} could not start: {error}") from error
    # A cancel waits for the CLI to end. A killed ``oc apply`` can still
    # create its pod after a delete by name ran, and that pod would then stay.
    communication = asyncio.ensure_future(process.communicate(stdin))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cli_seconds
    cancelled: asyncio.CancelledError | None = None
    while not communication.done() and loop.time() < deadline:
        try:
            await asyncio.wait({communication}, timeout=deadline - loop.time())
        except asyncio.CancelledError as error:
            cancelled = error
    if not communication.done():
        communication.cancel()
        await _kill(process)
        with contextlib.suppress(asyncio.CancelledError):
            await communication
        if cancelled is not None:
            raise cancelled
        raise SandboxInfraError(f"oc {_verb(args)} did not finish in {cli_seconds:.0f}s.")
    if cancelled is not None:
        raise cancelled
    stdout, stderr = communication.result()
    return _CliResult(process.returncode or 0, stdout, stderr)


def _verb(args: Sequence[str]) -> str:
    """Return the ``oc`` verb of an argument list, after any ``-n`` option.

    Args:
        args: The CLI arguments.

    Returns:
        The first argument that is not the namespace option.
    """
    rest = list(args)
    if rest[:1] == ["-n"]:
        rest = rest[2:]
    return rest[0] if rest else ""


def _ns_args(namespace: str | None) -> list[str]:
    """Return the ``-n`` option for a namespace.

    Args:
        namespace: The namespace, or ``None`` for the current ``oc`` project.

    Returns:
        ``["-n", namespace]``, or an empty list.
    """
    return ["-n", namespace] if namespace else []


def _pod_name(stem: str | None) -> str:
    """Return a DNS-1123 pod name with a random suffix.

    Args:
        stem: The readable part of the name, or ``None`` for the default.

    Returns:
        ``<stem>-<8 hex chars>`` in ``[a-z0-9-]``, at most 59 characters.
    """
    safe = _NAME_DASHES.sub("-", _NAME_UNSAFE.sub("-", (stem or "").lower()))
    safe = safe.strip("-")[:_NAME_STEM_CHARS].rstrip("-")
    return f"{safe or 'skynet'}-{uuid.uuid4().hex[:8]}"


def _job_label(job_id: str) -> str:
    """Return a Kubernetes label value for a job id.

    Args:
        job_id: The value of the ``JOB_TAG`` tag.

    Returns:
        The id itself when it is a valid label value, else a stable digest,
        so two different ids never share a label.
    """
    if job_id and _LABEL_VALUE.match(job_id):
        return job_id
    return "sha256-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:56]


def _env_stream(env: Mapping[str, str]) -> bytes:
    """Encode a command's environment for ``_ENV_LOADER``.

    Args:
        env: The command's environment.

    Returns:
        NUL-terminated ``NAME=value`` pairs.

    Raises:
        SandboxSpecError: A name or value holds a NUL, which no environment can carry.
    """
    if any("\0" in name or "\0" in value for name, value in env.items()):
        raise SandboxSpecError("Sandbox environment names and values cannot contain NUL characters.")
    return "".join(f"{name}={value}\0" for name, value in env.items()).encode("utf-8")


def _check_path(path: str) -> str:
    """Refuse a sandbox path that no shell argument can carry.

    Args:
        path: An absolute path, or a path relative to the container's working directory.

    Returns:
        The path, unchanged.

    Raises:
        SandboxSpecError: The path is empty or holds a NUL.
    """
    if not path or "\0" in path:
        raise SandboxSpecError(f"Invalid sandbox path: {path!r}")
    return path


def _resources(spec: SandboxSpec) -> dict[str, dict[str, str]]:
    """Map the spec's resource request onto pod requests and limits.

    Args:
        spec: The sandbox spec.

    Returns:
        The container ``resources`` block; empty for ``IGNORE``.
    """
    request = spec.resources
    if request.mode is ResourceMode.IGNORE:
        return {}
    size = {"cpu": str(request.cpus)}
    if request.memory_mb is not None:
        size["memory"] = f"{request.memory_mb}Mi"
    if request.disk_mb is not None:
        size["ephemeral-storage"] = f"{request.disk_mb}Mi"
    if request.mode is ResourceMode.LIMIT:
        return {"limits": size}
    if request.mode is ResourceMode.GUARANTEE:
        return {"requests": size, "limits": dict(size)}
    return {"requests": size}


def _auth_failure(result: _CliResult) -> bool:
    """Say whether an ``oc`` failure means the login or the RBAC is wrong.

    Args:
        result: The failed CLI result.

    Returns:
        True for an auth, permission or missing ServiceAccount failure.
    """
    text = result.stderr.decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in _AUTH_MARKERS) or ("serviceaccount" in text and "not found" in text)


async def _pump(name: str, reader: asyncio.StreamReader, captured: list[str], on_output: OutputSink | None) -> None:
    """Read one output pipe to its end and hand each chunk to the sink.

    Args:
        name: ``stdout`` or ``stderr``.
        reader: The pipe.
        captured: Receives each decoded chunk.
        on_output: Receives ``(name, text)`` for each chunk, or ``None``.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await reader.read(_READ_CHUNK_BYTES)
        text = decoder.decode(chunk, final=not chunk)
        if text:
            captured.append(text)
            if on_output is not None:
                on_output(name, text)
        if not chunk:
            return


async def _feed(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write bytes to a process's stdin, then close it.

    Args:
        writer: The stdin pipe.
        data: The bytes to write.
    """
    # A CLI that exits early (a deleted pod) closes the pipe; its exit code
    # and stderr then report why.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        writer.write(data)
        await writer.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        writer.close()
        await writer.wait_closed()


class OpenshiftEnvironment(SandboxEnvironment):
    """Run one sandbox as a pod in an OpenShift (or Kubernetes) namespace.

    The ``oc`` CLI must be logged in. The pod runs as root with Harbor's
    small capability set, under a ServiceAccount that the cluster admin
    creates and allows to run as any UID. Development only: the factory never
    allows it in production.
    """

    capabilities = OPENSHIFT_CAPABILITIES
    default_oc_bin: ClassVar[str] = DEFAULT_OC_BIN
    default_namespace: ClassVar[str | None] = None
    ready_seconds: ClassVar[float] = 300.0
    poll_seconds: ClassVar[float] = 1.0

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        image: str | None = None,
        namespace: str | None = None,
        service_account: str = DEFAULT_SERVICE_ACCOUNT,
        oc_bin: str | None = None,
    ) -> None:
        """Check the spec and choose the image and namespace.

        Args:
            spec: The sandbox to open.
            image: The image to boot when the spec names none. The cluster
                must be able to pull it; no image is built.
            namespace: The namespace; the default is the class default, then
                the current ``oc`` project.
            service_account: The ServiceAccount that runs the pod.
            oc_bin: The ``oc`` CLI path; the default is ``default_oc_bin``.

        Raises:
            SandboxSpecError: The spec breaks a provider rule or names no image.
        """
        super().__init__(spec)
        self._image = spec.image or image
        if not self._image:
            raise SandboxSpecError("The OpenShift sandbox needs an image in the spec or the provider.")
        self._namespace = namespace or self.default_namespace
        self._service_account = service_account
        self._oc_bin = oc_bin or self.default_oc_bin
        self._name: str | None = None
        self._stopped = False

    @property
    def pod_name(self) -> str | None:
        """Return the pod name, or ``None`` before start."""
        return self._name

    @classmethod
    def preflight(cls) -> None:
        """Check that ``oc`` is on PATH and logged in.

        Raises:
            SandboxFatalError: ``oc`` is missing, or ``oc whoami`` fails.
        """
        oc_bin = shutil.which(cls.default_oc_bin)
        if oc_bin is None:
            raise SandboxFatalError(f"The OpenShift sandbox needs the oc CLI ({cls.default_oc_bin!r}) on PATH.")
        try:
            result = subprocess.run(
                [oc_bin, "whoami"], capture_output=True, timeout=_WHOAMI_TIMEOUT_SECONDS, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxFatalError(f"oc whoami did not answer: {error}") from error
        if result.returncode != 0:
            detail = _CliResult(result.returncode, result.stdout, result.stderr).error_line
            raise SandboxFatalError(f"The oc CLI is not logged in to a cluster. Run oc login. ({detail})")

    def _manifest(self, name: str) -> dict[str, Any]:
        """Build the pod manifest. It holds no environment value.

        Args:
            name: The pod name.

        Returns:
            The pod object for ``oc apply``.
        """
        owner = self.spec.tags.get(JOB_TAG)
        labels = {**SANDBOX_LABELS, **({JOB_LABEL: _job_label(owner)} if owner else {})}
        container: dict[str, Any] = {
            "name": CONTAINER_NAME,
            "image": self._image,
            "command": list(_IDLE_COMMAND),
            "securityContext": {"capabilities": {"drop": ["ALL"], "add": list(_CAPABILITIES)}},
        }
        resources = _resources(self.spec)
        if resources:
            container["resources"] = resources
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "serviceAccountName": self._service_account,
                "automountServiceAccountToken": False,
                "activeDeadlineSeconds": max(1, math.ceil(self.spec.lifetime_seconds)),
                "securityContext": {"runAsUser": 0},
                "containers": [container],
            },
        }

    async def start(self) -> None:
        """Create the pod and wait until a command can run in it.

        Raises:
            RuntimeError: The sandbox was already started.
            SandboxFatalError: ``oc`` is not logged in, has no permission, or
                the ServiceAccount does not exist.
            SandboxInfraError: The cluster refused the pod, or the pod failed.
            StartTimeoutError: The pod was not ready within ``ready_seconds``.
        """
        if self._name is not None:
            raise RuntimeError("The OpenShift sandbox was already started.")
        name = _pod_name(self.spec.name)
        self._name = name
        manifest = json.dumps(self._manifest(name)).encode("utf-8")
        applied = await _oc(self._oc_bin, [*_ns_args(self._namespace), "apply", "-f", "-"], stdin=manifest)
        if applied.returncode != 0:
            if _auth_failure(applied):
                raise SandboxFatalError(
                    f"OpenShift refused the pod {name}: {applied.error_line}. Check oc login, and that the "
                    f"ServiceAccount {self._service_account!r} exists and may run as any UID."
                )
            raise SandboxInfraError(f"OpenShift refused the pod {name}: {applied.error_line}")
        await self._wait_ready(name)

    async def _wait_ready(self, name: str) -> None:
        """Poll the pod until it runs and a command succeeds in it.

        Args:
            name: The pod name.

        Raises:
            SandboxFatalError: ``oc`` lost its login or permission.
            SandboxInfraError: The pod reached a terminal phase or cannot pull its image.
            StartTimeoutError: The pod was not ready within ``ready_seconds``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_seconds
        while True:
            if await self._pod_ready(name) and await self._exec_ready(name):
                return
            if loop.time() >= deadline:
                raise StartTimeoutError(self.ready_seconds)
            await asyncio.sleep(self.poll_seconds)

    async def _pod_ready(self, name: str) -> bool:
        """Read the pod status once.

        Args:
            name: The pod name.

        Returns:
            True when the pod runs and every container is ready.

        Raises:
            SandboxFatalError: ``oc`` lost its login or permission.
            SandboxInfraError: The status is not readable, the pod reached a
                terminal phase, or a container cannot start.
        """
        result = await _oc(self._oc_bin, [*_ns_args(self._namespace), "get", "pod", name, "-o", "json"])
        if result.returncode != 0:
            if _auth_failure(result):
                raise SandboxFatalError(f"oc cannot read the pod {name}: {result.error_line}")
            raise SandboxInfraError(f"oc cannot read the pod {name}: {result.error_line}")
        try:
            status = json.loads(result.stdout).get("status") or {}
        except (ValueError, AttributeError) as error:
            raise SandboxInfraError(f"oc sent an unreadable status for the pod {name}.") from error
        phase = status.get("phase")
        if phase in _TERMINAL_PHASES:
            reason = status.get("reason") or status.get("message") or "no reason"
            raise SandboxInfraError(f"The pod {name} ended in phase {phase} before it was ready: {reason}")
        containers = status.get("containerStatuses") or []
        for container in containers:
            waiting = (container.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in _FAILED_WAIT_REASONS:
                raise SandboxInfraError(
                    f"The pod {name} cannot start its container: {waiting.get('reason')}: "
                    f"{str(waiting.get('message') or '')[:300]}"
                )
        return phase == "Running" and bool(containers) and all(item.get("ready") for item in containers)

    async def _exec_ready(self, name: str) -> bool:
        """Say whether ``oc exec`` reaches the container yet.

        A Ready pod can refuse ``exec`` for a short time after it starts.

        Args:
            name: The pod name.

        Returns:
            True when ``true`` runs in the container.
        """
        result = await _oc(self._oc_bin, self._exec_args(name, "true"))
        return result.returncode == 0

    def _exec_args(self, name: str, *argv: str, stdin: bool = False) -> list[str]:
        """Build the ``oc exec`` arguments for one command in the container.

        Args:
            name: The pod name.
            *argv: The command and its arguments.
            stdin: Pass the client's stdin to the command.

        Returns:
            The CLI arguments.
        """
        interactive = ["-i"] if stdin else []
        return [*_ns_args(self._namespace), "exec", *interactive, name, "-c", CONTAINER_NAME, "--", *argv]

    async def stop(self) -> None:
        """Delete the pod. Do nothing before start or after a stop.

        A cancel waits until the delete ends, then reaches the caller, so a
        cancelled stop never skips the delete. A failure is logged; the pod's
        ``activeDeadlineSeconds`` still ends it.

        Raises:
            asyncio.CancelledError: The task was cancelled during the delete.
        """
        if self._name is None or self._stopped:
            return
        self._stopped = True
        removal = asyncio.ensure_future(_delete_pod(self._oc_bin, self._namespace, self._name))
        cancelled: asyncio.CancelledError | None = None
        while not removal.done():
            try:
                await asyncio.wait({removal})
            except asyncio.CancelledError as error:
                cancelled = error
        if cancelled is not None:
            raise cancelled
        removal.result()

    def _require_running(self) -> str:
        """Return the pod name of a started, not stopped sandbox.

        Returns:
            The pod name.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._name is None or self._stopped:
            raise RuntimeError("The OpenShift sandbox is not running.")
        return self._name

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> ExecResult:
        """Run one shell command in the pod and stream its output.

        Args:
            command: The shell command, run by ``bash --noprofile --norc -c``.
            cwd: The working directory; the default is the image's.
            env: Extra environment variables for this command.
            timeout_sec: After this long, kill the command's process group and
                return ``timed_out=True``; ``None`` sets no limit.
            on_output: Receives ``(stream, text)`` chunks while the command runs.
            sudo: No effect: the pod already runs as root.

        Returns:
            The exit code, the captured output and whether the timeout fired.

        Raises:
            SandboxInfraError: The command ran past its kill deadline, the pod
                could not record the command's process group, or ``oc`` is
                missing. A timeout, a cancel or an error kills the command's
                process group in the pod first.
        """
        del sudo
        name = self._require_running()
        state = uuid.uuid4().hex
        limit = ""
        deadline: float | None = None
        if timeout_sec is not None:
            seconds = max(1, math.ceil(timeout_sec))
            limit = str(seconds)
            deadline = seconds + _KILL_GRACE_SECONDS
        stream = _env_stream({**self.spec.env, **(env or {})})
        script = f"cd -- {shlex.quote(cwd)} && {command}" if cwd else command
        argv = [*_SHELL, _EXEC_WRAPPER, "skynet-exec", state, limit, *_SHELL, _ENV_LOADER, "skynet-env"]
        args = self._exec_args(name, *argv, *_SHELL, script, stdin=True)
        try:
            process = await asyncio.create_subprocess_exec(
                self._oc_bin,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise SandboxInfraError(f"The oc CLI {self._oc_bin!r} could not start: {error}") from error
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
        try:
            async with asyncio.timeout(deadline):
                await asyncio.gather(
                    _feed(process.stdin, stream),
                    _pump("stdout", process.stdout, captured["stdout"], on_output),
                    _pump("stderr", process.stderr, captured["stderr"], on_output),
                )
                exit_code = await process.wait()
        except TimeoutError as error:
            await self._stop_command(process, name, state)
            raise SandboxInfraError(
                f"The command still ran {_KILL_GRACE_SECONDS}s past its {timeout_sec}s timeout: {command[:200]}"
            ) from error
        except BaseException:
            await self._stop_command(process, name, state)
            raise
        if exit_code == _NO_GROUP_EXIT_CODE and await self._take_marker(name, state, "nogroup"):
            raise SandboxInfraError(
                f"The pod could not record the command's process group, so the command was killed: {command[:200]}"
            )
        timed_out = bool(limit) and exit_code == _TIMEOUT_EXIT_CODE and await self._take_marker(name, state, "timeout")
        return ExecResult(
            exit_code=exit_code,
            stdout="".join(captured["stdout"]),
            stderr="".join(captured["stderr"]),
            timed_out=timed_out,
        )

    async def _stop_command(self, process: asyncio.subprocess.Process, name: str, state: str) -> None:
        """Kill a command's process group in the pod, then its ``oc exec`` client.

        The group kill is best effort and bounded; a failure is logged, so the
        error or cancel that stopped the command still reaches the caller.

        Args:
            process: The ``oc exec`` client.
            name: The pod name.
            state: The command's state id.
        """
        try:
            await _oc(
                self._oc_bin,
                self._exec_args(name, *_SHELL, _KILL_GROUP_SCRIPT, "skynet-kill", state),
                cli_seconds=_GROUP_KILL_SECONDS,
            )
        except Exception as error:
            logger.warning("Could not kill a command in the OpenShift sandbox %s: %s", name, error)
        finally:
            await _kill(process)

    async def _take_marker(self, name: str, state: str, marker: str) -> bool:
        """Read and remove one marker file that the exec wrapper left.

        Args:
            name: The pod name.
            state: The command's state id.
            marker: The marker suffix: ``timeout`` or ``nogroup``.

        Returns:
            True when the marker was there.
        """
        result = await _oc(self._oc_bin, self._exec_args(name, *_SHELL, _MARKER_SCRIPT, "skynet-marker", state, marker))
        return result.returncode == 0

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the pod, creating parent directories.

        Args:
            files: Maps each sandbox path to its text content. A relative
                path is relative to the container's working directory.

        Raises:
            SandboxInfraError: The pod refused a file.
            SandboxSpecError: A path is empty or holds a NUL.
        """
        name = self._require_running()
        for path, text in files.items():
            args = self._exec_args(name, *_SHELL, _WRITE_FILE_SCRIPT, "skynet-write", _check_path(path), stdin=True)
            result = await _oc(self._oc_bin, args, stdin=text.encode("utf-8"))
            if result.returncode != 0:
                raise SandboxInfraError(f"Could not write {path!r} into the OpenShift sandbox: {result.error_line}")

    async def download_file(self, path: str) -> str | None:
        """Read one text file from the pod.

        Args:
            path: The sandbox path. A relative path is relative to the
                container's working directory.

        Returns:
            The file content, or ``None`` when no regular file is there.

        Raises:
            SandboxInfraError: The pod could not be read.
            SandboxSpecError: The path is empty or holds a NUL.
        """
        name = self._require_running()
        args = self._exec_args(name, *_SHELL, _READ_FILE_SCRIPT, "skynet-read", _check_path(path))
        result = await _oc(self._oc_bin, args)
        if result.returncode == _MISSING_FILE_EXIT:
            return None
        if result.returncode != 0:
            raise SandboxInfraError(f"Could not read {path!r} from the OpenShift sandbox: {result.error_line}")
        return result.stdout.decode("utf-8", errors="replace")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str, *, oc_bin: str | None = None, namespace: str | None = None) -> int:
        """Delete every pod of a job. A failure on one pod is logged and skipped.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.
            oc_bin: The ``oc`` CLI path; the default is ``default_oc_bin``.
            namespace: The namespace; the default is ``default_namespace``.

        Returns:
            The number of pods deleted.
        """
        oc_bin = oc_bin or cls.default_oc_bin
        namespace = namespace or cls.default_namespace
        selector = f"{JOB_LABEL}={_job_label(job_id)}"
        try:
            listed = await _oc(oc_bin, [*_ns_args(namespace), "get", "pods", "-l", selector, "-o", "name"])
        except SandboxInfraError as error:
            logger.warning("Could not list the OpenShift sandboxes of job %s: %s", job_id, error)
            return 0
        if listed.returncode != 0:
            logger.warning("Could not list the OpenShift sandboxes of job %s: %s", job_id, listed.error_line)
            return 0
        stopped = 0
        for line in listed.stdout.decode("utf-8", errors="replace").split():
            pod = line.removeprefix("pod/")
            if pod and await _delete_pod(oc_bin, namespace, pod):
                stopped += 1
        return stopped


async def _delete_pod(oc_bin: str, namespace: str | None, name: str) -> bool:
    """Delete one pod and log a failure. Never raises an Exception.

    Args:
        oc_bin: The ``oc`` CLI path.
        namespace: The namespace, or ``None`` for the current project.
        name: The pod name.

    Returns:
        True when ``oc`` confirmed the delete, or the pod was already gone.
    """
    args = [
        *_ns_args(namespace),
        "delete",
        "pod",
        name,
        "--ignore-not-found",
        f"--grace-period={_DELETE_GRACE_SECONDS}",
        "--wait=false",
    ]
    try:
        result = await _oc(oc_bin, args)
    except SandboxInfraError as error:
        logger.warning("Could not delete the OpenShift sandbox %s: %s", name, error)
        return False
    if result.returncode != 0:
        logger.warning("Could not delete the OpenShift sandbox %s: %s", name, result.error_line)
        return False
    return True

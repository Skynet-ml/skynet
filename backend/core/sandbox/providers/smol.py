"""The async smolmachines sandbox provider, for dev and evaluation only.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/smol.py: the
``AsyncMachine.create`` call with a ``MachineConfig`` and a ``ResourceSpec``,
the ``/bin/sh`` exec wrapper, the network split into hosts and CIDRs, the
refusal of wildcard hosts, the machine name rule, the one-GPU local limit,
the disk size in whole GB and the delete that ignores a machine that is
already gone come from it. Changes: the provider implements the SBX-6 async
contract, the SDK calls never retry (SKY-406 D3, D10), the factory refuses
the provider in production (SKY-406 D1), and the job sweep stops the
machines that this process started, because the SDK has no list by tag.
Harbor's golden checkpoints, fork batching, SSH streaming, Dockerfile
checks and directory transfer are left out.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import re
import shlex
import uuid
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, ClassVar, Literal

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import RunTimeoutError, SandboxFatalError
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from smol.async_machine import AsyncMachine
    from smol.errors import SmolError
    from smol.types import ConnectOptions, ExecOptions, MachineConfig, ResourceSpec
except ImportError:
    AsyncMachine = None
    SmolError = None
    ConnectOptions = None
    ExecOptions = None
    MachineConfig = None
    ResourceSpec = None

logger = logging.getLogger(__name__)

# smolmachines fixes egress at create time, so a policy change after start is
# not possible. GPUs work on the local target only; __init__ checks that.
SMOL_CAPABILITIES = EnvironmentCapabilities(
    name="smol",
    network_modes=frozenset(NetworkMode),
    gpus=True,
)

DEFAULT_IMAGE = "python:3.11-slim"
DEFAULT_READY_TIMEOUT_SEC = 120.0
# Prefer bash when the image has it, as Harbor does; "$1" is the command.
_SHELL_EXEC = 'if [ -x /bin/bash ]; then shell=/bin/bash; else shell=/bin/sh; fi; exec "$shell" -lc "$1"'
_SHELL_NAME = "skynet-smol"
_GONE_CODES = frozenset({"NOT_FOUND", "VM_NOT_FOUND"})
# The SDK exec timeout ends the command first; this bound only stops a lost
# connection from blocking exec past the limit.
_TIMEOUT_GRACE_SEC = 30.0
# How long a cancelled start or stop waits for an open SDK call before the cancel passes on.
_CANCEL_GRACE_SEC = 0.5

# The machines of this process, for the job sweep.
_LIVE: set[SmolEnvironment] = set()
_LATE_DELETES: set[asyncio.Future[None]] = set()


class SmolEnvironment(SandboxEnvironment):
    """Run a sandbox as one smolmachines VM created from a registry image."""

    capabilities: ClassVar[EnvironmentCapabilities] = SMOL_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        target: Literal["local", "cloud"] = "local",
        api_key: str | None = None,
        base_url: str | None = None,
        timeouts: PhaseTimeouts | None = None,
    ) -> None:
        """Create a stopped smolmachines sandbox.

        smolmachines has no machine lifetime, so ``spec.lifetime_seconds``
        is not sent; ``stop()`` and the job sweep delete the machine.

        Args:
            spec: The sandbox to open. ``spec.image`` is a registry image;
                ``None`` uses ``DEFAULT_IMAGE``.
            target: ``local`` runs the VM on this host, ``cloud`` in Smol Cloud.
            api_key: The Smol Cloud key, or ``None`` for the SDK default.
            base_url: The Smol Cloud URL, or ``None`` for the SDK default.
            timeouts: The phase timeouts. The start phase bounds the machine
                readiness wait; the run phase is the default ``exec`` limit.

        Raises:
            SandboxSpecError: The spec asks for a target, a GPU count or an
                allowlist entry that smolmachines cannot apply.
        """
        super().__init__(spec)
        if target not in ("local", "cloud"):
            raise SandboxSpecError("The smol sandbox target must be 'local' or 'cloud'.")
        if spec.resources.gpu > 1:
            raise SandboxSpecError("The smol sandbox supports at most one GPU.")
        if spec.resources.gpu and target == "cloud":
            raise SandboxSpecError("The smol sandbox supports GPUs on the local target only.")
        if any(host.startswith("*") for host in spec.allowed_hosts):
            raise SandboxSpecError("The smol sandbox does not support wildcard hosts.")
        self._target = target
        self._api_key = api_key
        self._base_url = base_url
        self._ready_timeout = timeouts.seconds(Phase.START) if timeouts is not None else DEFAULT_READY_TIMEOUT_SEC
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._machine: Any = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the smolmachines SDK is present.

        Raises:
            SandboxFatalError: The ``smolmachines`` package is missing.
        """
        _require_sdk()

    async def start(self) -> None:
        """Create the machine. Do nothing when the machine already runs.

        A cancel during the create waits for the create for at most
        ``_CANCEL_GRACE_SEC``, also through repeat cancels. A machine that
        the create returns in that time gets a delete. A create or delete
        that is still open after its grace time goes on in the background,
        so no machine leaks and the caller's start timeout stays bounded.

        Raises:
            asyncio.CancelledError: The task was cancelled during the create.
        """
        if self._machine is not None:
            return
        _require_sdk()
        create = asyncio.ensure_future(self._create())
        try:
            machine = await asyncio.shield(create)
        except asyncio.CancelledError:
            await _wait_through_cancels(create, _CANCEL_GRACE_SEC)
            if not create.done():
                create.add_done_callback(_delete_late_machine)
                raise
            if create.cancelled() or create.exception() is not None:
                raise
            await _delete_bounded(create.result())
            raise
        self._machine = machine
        _LIVE.add(self)

    async def stop(self) -> None:
        """Delete the machine. A machine that is already gone counts as deleted.

        A cancel during the delete waits for the delete for at most
        ``_CANCEL_GRACE_SEC``, also through repeat cancels, then passes the
        cancel on with the delete still running in the background.

        Raises:
            asyncio.CancelledError: The task was cancelled during the delete.
        """
        machine = self._machine
        if machine is None:
            return
        self._machine = None
        _LIVE.discard(self)
        delete = asyncio.ensure_future(_delete(machine))
        try:
            await asyncio.shield(delete)
        except asyncio.CancelledError:
            await _wait_through_cancels(delete, _CANCEL_GRACE_SEC)
            if not delete.done():
                _keep_late_delete(delete)
            elif not delete.cancelled() and delete.exception() is not None:
                logger.warning("Could not delete a smol machine during a cancelled stop.", exc_info=delete.exception())
            raise

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
        """Run one shell command in the machine.

        The SDK returns the output when the command ends, so the sink gets
        one stdout chunk and one stderr chunk at the end.

        Args:
            command: The shell command.
            cwd: The working directory, or the image default.
            env: Extra environment variables.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` chunks; ``None`` uses the
                sink of the current task.
            sudo: Run the command as root instead of the image user.

        Returns:
            The exit code and the captured output.

        Raises:
            RuntimeError: The machine is not running.
            RunTimeoutError: The command ran past the time limit.
        """
        machine = self._require_running()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        sink = on_output or current_output_sink()
        options = ExecOptions(
            env={**self.spec.env, **(env or {})} or None,
            workdir=cwd,
            timeout=None if limit is None else max(1, math.ceil(limit)),
            user="root" if sudo else None,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            async with asyncio.timeout(None if limit is None else limit + _TIMEOUT_GRACE_SEC):
                result = await machine.exec(_argv(command), options)
        except Exception:
            # The SDK reports an expired exec as an error with no documented
            # code, so a failure at or past the limit counts as a timeout.
            if limit is not None and loop.time() - started >= limit:
                raise RunTimeoutError(limit) from None
            raise
        stdout, stderr = result.stdout or "", result.stderr or ""
        if sink is not None:
            if stdout:
                sink("stdout", stdout)
            if stderr:
                sink("stderr", stderr)
        return ExecResult(exit_code=result.exit_code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the machine and create their parent directories.

        Args:
            files: Maps each absolute sandbox path to its text content.

        Raises:
            RuntimeError: The machine is not running, or a parent directory
                could not be created.
        """
        machine = self._require_running()
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {"", ".", "/"})
        if parents:
            made = await machine.exec(
                _argv("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents)), ExecOptions(user="root")
            )
            if made.exit_code != 0:
                raise RuntimeError(f"The smol sandbox could not create the upload directories: {made.stderr}")
        for path, text in files.items():
            await machine.write_file(path, text.encode("utf-8"))

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the machine.

        Args:
            path: The absolute sandbox path.

        Returns:
            The exact file content, or ``None`` when the file does not exist.

        Raises:
            RuntimeError: The machine is not running.
        """
        machine = self._require_running()
        # The SDK has no documented not-found code for files, and a missing
        # machine must not read as a missing file, so check the path first.
        probe = await machine.exec(_argv(f"test -f {shlex.quote(path)}"), ExecOptions(user="root"))
        if probe.exit_code != 0:
            return None
        return (await machine.read_file(path)).decode("utf-8")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Delete every machine of this process that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of machines that the sweep deleted.
        """
        stopped = 0
        for env in [env for env in _LIVE if env.spec.tags.get(JOB_TAG) == job_id]:
            try:
                await env.stop()
            except Exception:
                logger.exception("Could not delete a smol machine of job %s.", job_id)
                continue
            stopped += 1
        return stopped

    async def _create(self) -> Any:
        """Create one machine from the spec.

        Returns:
            The SDK machine.
        """
        spec = self.spec
        name = machine_name(spec.name or f"skynet-{uuid.uuid4().hex[:8]}")
        return await AsyncMachine.create(
            MachineConfig(
                name=name,
                image=spec.image or DEFAULT_IMAGE,
                resources=resource_spec(spec, self._network_policy or NetworkPolicy(disabled=True)),
                ready_timeout_seconds=self._ready_timeout,
                env=dict(spec.env) or None,
            ),
            ConnectOptions(target=self._target, api_key=self._api_key, base_url=self._base_url),
        )

    def _require_running(self) -> Any:
        """Return the SDK machine, or raise when the machine is stopped.

        Returns:
            The SDK machine.

        Raises:
            RuntimeError: The machine is not running.
        """
        if self._machine is None:
            raise RuntimeError("The smol sandbox is not running.")
        return self._machine


def machine_name(value: str) -> str:
    """Normalize a name to the lowercase letters, digits and dashes that smolmachines accepts.

    Args:
        value: The wanted name.

    Returns:
        The name, at most 128 characters long.
    """
    normalized = re.sub(r"[^A-Za-z0-9-]+", "-", value).strip("-").lower()
    return (normalized or "skynet")[:128].rstrip("-")


def network_args(policy: NetworkPolicy) -> tuple[bool, list[str], list[str]]:
    """Map an egress policy to the smolmachines network flag, host list and CIDR list.

    Args:
        policy: The egress policy.

    Returns:
        ``(network, allow_hosts, allow_cidrs)``. Open egress sends empty lists.
    """
    if policy.disabled:
        return False, [], []
    hosts: list[str] = []
    cidrs: list[str] = []
    for host in policy.allowed_hosts:
        try:
            cidrs.append(str(ipaddress.ip_network(host, strict=False)))
        except ValueError:
            hosts.append(host)
    return True, hosts, cidrs


def resource_spec(spec: SandboxSpec, policy: NetworkPolicy) -> Any:
    """Build the smolmachines ``ResourceSpec`` for a spec and an egress policy.

    smolmachines applies one size per resource, so every mode except
    ``IGNORE`` sends the requested sizes.

    Args:
        spec: The sandbox spec.
        policy: The egress policy.

    Returns:
        The SDK ``ResourceSpec``.
    """
    request = spec.resources
    network, hosts, cidrs = network_args(policy)
    sized = request.mode is not ResourceMode.IGNORE
    return ResourceSpec(
        cpus=request.cpus if sized else None,
        memory_mb=request.memory_mb if sized else None,
        storage_gb=math.ceil(request.disk_mb / 1024) if sized and request.disk_mb else None,
        network=network,
        allow_hosts=hosts or None,
        allow_cidrs=cidrs or None,
        cuda=request.gpu == 1,
        gpu=False,
    )


def _argv(command: str) -> list[str]:
    """Wrap a shell command in the argv that the SDK exec takes.

    Args:
        command: The shell command.

    Returns:
        The argv list.
    """
    return ["/bin/sh", "-c", _SHELL_EXEC, _SHELL_NAME, command]


def _require_sdk() -> None:
    """Raise when the smolmachines SDK is not installed.

    Raises:
        SandboxFatalError: The ``smolmachines`` package is missing.
    """
    if AsyncMachine is None:
        raise SandboxFatalError("The smol sandbox needs the smolmachines package: install the `smol` extra.")


async def _delete(machine: Any) -> None:
    """Delete one machine, and ignore a machine that is already gone.

    Args:
        machine: The SDK machine.

    Raises:
        SmolError: The delete failed for another reason.
    """
    try:
        await machine.delete()
    except SmolError as error:
        if getattr(error, "code", None) not in _GONE_CODES:
            raise


async def _delete_bounded(machine: Any) -> None:
    """Delete a machine of a cancelled start, and wait at most ``_CANCEL_GRACE_SEC``.

    Args:
        machine: The SDK machine.
    """
    delete = asyncio.ensure_future(_delete(machine))
    await _wait_through_cancels(delete, _CANCEL_GRACE_SEC)
    if not delete.done():
        _keep_late_delete(delete)
    elif not delete.cancelled() and delete.exception() is not None:
        # The cancel must still reach the caller, so the error goes to the log.
        logger.error("Could not delete a smol machine after a cancelled start.", exc_info=delete.exception())


async def _wait_through_cancels(task: asyncio.Future[Any], grace_sec: float) -> None:
    """Wait until the task ends or the grace time passes, and absorb each cancel meanwhile.

    The task itself is never cancelled, because smolmachines may already
    have done the work on its side.

    Args:
        task: The task to wait for.
        grace_sec: The longest wait in seconds.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_sec
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            # The caller re-raises its own cancel after this wait.
            continue


def _delete_late_machine(create: asyncio.Future[Any]) -> None:
    """Delete the machine of a create that ended after its start was cancelled.

    A done callback must never raise, so every error goes to the log.

    Args:
        create: The finished create task.
    """
    try:
        if create.cancelled() or create.exception() is not None:
            return
        delete = asyncio.ensure_future(_delete(create.result()))
    except Exception:
        logger.exception("Could not delete a smol machine that arrived after a cancelled start.")
        return
    _keep_late_delete(delete)


def _keep_late_delete(delete: asyncio.Future[None]) -> None:
    """Keep a background delete alive until it ends, and log its error then.

    Args:
        delete: The running delete task.
    """
    # The loop holds tasks only by weak reference, so keep this one until it ends.
    _LATE_DELETES.add(delete)
    delete.add_done_callback(_finish_late_delete)


def _finish_late_delete(delete: asyncio.Future[None]) -> None:
    """Forget a finished late delete and log its error.

    Args:
        delete: The finished delete task.
    """
    _LATE_DELETES.discard(delete)
    if not delete.cancelled() and delete.exception() is not None:
        logger.error("Could not delete a smol machine after a cancelled start or stop.", exc_info=delete.exception())

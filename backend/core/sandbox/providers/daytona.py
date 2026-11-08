"""The async Daytona sandbox provider, for dev use only.

Adapted from Harbor 0.25.0 (Apache-2.0), ``harbor/environments/daytona/environment.py``
and ``harbor/environments/daytona/utils.py``. Changes:

- The provider runs on the SBX-6 async contract in ``..environment``.
- No retries (SKY-406 D3, D10). Each SDK error maps to one sandbox error
  group, and the caller decides whether to retry.
- No snapshots, no Dockerfile builds, no compose, no SSH and no GPU types.
  A box always starts from an image.
- ``exec`` runs one synchronous session command, so stdout and stderr stay
  apart and no status polling is needed.
- ``stop_job_sandboxes`` finds the boxes of a job by their ``JOB_TAG`` label.
- The factory refuses this provider in production (SKY-406 D1).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import math
import os
import posixpath
import shlex
import uuid
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

from ..capabilities import EnvironmentCapabilities, NetworkMode, network_mode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import (
    RunTimeoutError,
    SandboxError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
)
from ..resources import ResourceMode, SandboxSpec
from ..timeouts import Phase, PhaseTimeouts

try:
    from daytona import (
        AsyncDaytona,
        CreateSandboxFromImageParams,
        DaytonaAuthenticationError,
        DaytonaAuthorizationError,
        DaytonaError,
        DaytonaForbiddenError,
        DaytonaNotFoundError,
        DaytonaRateLimitError,
        ListSandboxesQuery,
        Resources,
        SessionExecuteRequest,
    )
except ImportError:
    AsyncDaytona = None
    CreateSandboxFromImageParams = None
    DaytonaAuthenticationError = None
    DaytonaAuthorizationError = None
    DaytonaError = None
    DaytonaForbiddenError = None
    DaytonaNotFoundError = None
    DaytonaRateLimitError = None
    ListSandboxesQuery = None
    Resources = None
    SessionExecuteRequest = None

logger = logging.getLogger(__name__)

# Daytona applies a domain allowlist, a CIDR allowlist and block-all, and can
# change them on a running box. It has no egress header injection, and this
# port leaves out its GPU types, so neither is offered. Daytona publishes no
# fixed per-box size limits, so none are set here; the API rejects a size
# above the organization quota.
DAYTONA_CAPABILITIES = EnvironmentCapabilities(
    name="daytona",
    billable=True,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)

DEFAULT_IMAGE = "python:3.12-slim"
CREATE_TIMEOUT_SEC = 300.0
DELETE_TIMEOUT_SEC = 60.0

_TRANSIENT_MARKERS = ("limit exceeded", "too many requests", "capacity", "rate limit")
_BUILD_MARKERS = ("build_failed", "failed to build")


def _client() -> Any:
    """Create a Daytona client from the ``DAYTONA_*`` environment variables.

    Tests replace this function with a fake client factory.

    Returns:
        A new ``AsyncDaytona`` client.
    """
    return AsyncDaytona()


class DaytonaEnvironment(SandboxEnvironment):
    """Run a sandbox as one ephemeral Daytona box."""

    capabilities: ClassVar[EnvironmentCapabilities] = DAYTONA_CAPABILITIES

    def __init__(self, spec: SandboxSpec, *, timeouts: PhaseTimeouts | None = None) -> None:
        """Check the spec and create a stopped Daytona sandbox.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` timeout. ``None`` means no default timeout.

        Raises:
            SandboxSpecError: The provider cannot open this spec.
            SandboxFatalError: The ``daytona`` extra is not installed.
        """
        super().__init__(spec)
        _require_sdk()
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._client: Any = None
        self._sandbox: Any = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK is installed and that credentials are set.

        Raises:
            SandboxFatalError: The extra or the credentials are missing.
        """
        _require_sdk()
        if os.environ.get("DAYTONA_API_KEY"):
            return
        if os.environ.get("DAYTONA_JWT_TOKEN") and os.environ.get("DAYTONA_ORGANIZATION_ID"):
            return
        raise SandboxFatalError(
            "The Daytona sandbox needs DAYTONA_API_KEY, or DAYTONA_JWT_TOKEN and DAYTONA_ORGANIZATION_ID."
        )

    async def start(self) -> None:
        """Create the Daytona box. Do nothing when the sandbox already runs.

        Raises:
            SandboxError: Daytona refused or failed the create call.
        """
        if self._sandbox is not None:
            return
        if self._client is None:
            self._client = _client()
        params = CreateSandboxFromImageParams(
            image=self.spec.image or DEFAULT_IMAGE,
            resources=_resources(self.spec),
            env_vars=dict(self.spec.env) or None,
            labels=dict(self.spec.tags) or None,
            # ephemeral deletes the box when it stops, and the TTL deletes a
            # box that a crashed worker never stopped.
            ephemeral=True,
            auto_stop_interval=0,
            ttl_minutes=max(1, math.ceil(self.spec.lifetime_seconds / 60)),
            **_network_kwargs(self._network_policy or NetworkPolicy(), clear=False),
        )
        create = asyncio.ensure_future(self._create(params))
        try:
            self._sandbox = await asyncio.shield(create)
        except asyncio.CancelledError:
            # A cancel must not lose a box that Daytona already made: wait for
            # the create to end, keep the box, so stop() can delete it.
            with contextlib.suppress(Exception):
                self._sandbox = await create
            raise

    async def stop(self) -> None:
        """Delete the box and close the client. Safe before start and when called twice.

        A cancel during the delete waits for the delete to finish, then
        passes the cancel on.
        """
        sandbox, client = self._sandbox, self._client
        self._sandbox = None
        self._client = None
        if sandbox is None and client is None:
            return
        cleanup = asyncio.ensure_future(_delete_and_close(sandbox, client))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
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
        """Run one shell command in the box.

        Args:
            command: The shell command, run by ``bash -c``.
            cwd: The working directory in the box.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives the stdout and stderr text when the command ends.
            sudo: Not supported.

        Returns:
            The exit code and the output of the command.

        Raises:
            SandboxRejectedError: ``sudo`` is set.
            RunTimeoutError: The command ran past its time limit.
            SandboxError: Daytona failed the call.
        """
        if sudo:
            raise SandboxRejectedError("The Daytona sandbox does not run commands with sudo.")
        sandbox = self._require_running()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        shell = _shell_command(command, cwd=cwd, env=env, timeout_sec=limit)
        session_id = f"skynet-{uuid.uuid4().hex}"
        try:
            async with asyncio.timeout(limit) as deadline:
                with _translated("exec"):
                    # The session is not deleted: Daytona kills every child of a
                    # deleted session, also the background ones. The box delete
                    # removes it.
                    await sandbox.process.create_session(session_id)
                    response = await sandbox.process.execute_session_command(
                        session_id,
                        SessionExecuteRequest(command=shell, run_async=False),
                        timeout=None if limit is None else math.ceil(limit),
                    )
        except TimeoutError:
            if deadline.expired():
                raise RunTimeoutError(limit) from None
            raise
        result = ExecResult(
            exit_code=int(response.exit_code if response.exit_code is not None else -1),
            stdout=response.stdout or "",
            stderr=response.stderr or "",
        )
        if on_output is not None:
            if result.stdout:
                on_output("stdout", result.stdout)
            if result.stderr:
                on_output("stderr", result.stderr)
        return result

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the box as UTF-8.

        Args:
            files: Maps each box path to its text content.

        Raises:
            SandboxError: Daytona failed a call or a parent folder could not be made.
        """
        sandbox = self._require_running()
        parents = sorted({posixpath.dirname(path) for path in files} - {"", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents))
            if not made.ok:
                raise SandboxInfraError(f"The Daytona sandbox could not make folders: {made.stderr.strip()[:500]}")
        for path, text in files.items():
            with _translated("upload"):
                # bytes, not str: the SDK reads a str argument as a local file path.
                await sandbox.fs.upload_file(text.encode("utf-8"), path)

    async def download_file(self, path: str) -> str | None:
        """Read a UTF-8 text file from the box.

        Args:
            path: The box path.

        Returns:
            The content, or ``None`` when the file does not exist.

        Raises:
            SandboxError: Daytona failed the call.
        """
        sandbox = self._require_running()
        try:
            with _translated("download"):
                data = await sandbox.fs.download_file(path)
        except _MissingError:
            return None
        return data.decode("utf-8")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Delete every Daytona box that carries this job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` label.

        Returns:
            The number of boxes that were deleted.
        """
        _require_sdk()
        client = _client()
        deleted = 0
        try:
            boxes = [box async for box in client.list(ListSandboxesQuery(labels={JOB_TAG: job_id}))]
            for box in boxes:
                try:
                    with _translated("delete"):
                        await box.delete(timeout=DELETE_TIMEOUT_SEC)
                except _MissingError:
                    continue
                except Exception:
                    logger.exception("The Daytona sandbox %s of job %s did not delete.", box.id, job_id)
                    continue
                deleted += 1
        finally:
            await client.close()
        return deleted

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a new egress policy to the running box.

        Args:
            policy: The checked policy.

        Raises:
            SandboxError: Daytona failed the call.
        """
        sandbox = self._require_running()
        with _translated("network"):
            await sandbox.update_network_settings(**_network_kwargs(policy, clear=True))

    async def _create(self, params: Any) -> Any:
        """Ask Daytona for a new box.

        Args:
            params: The create parameters.

        Returns:
            The new ``AsyncSandbox``.

        Raises:
            SandboxError: Daytona refused or failed the create call.
        """
        with _translated("create"):
            return await self._client.create(params, timeout=CREATE_TIMEOUT_SEC)

    def _require_running(self) -> Any:
        """Return the running box.

        Returns:
            The ``AsyncSandbox``.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._sandbox is None:
            raise RuntimeError("The Daytona sandbox is not running.")
        return self._sandbox


class _MissingError(SandboxFatalError):
    """Report that Daytona does not know the box or the file."""


def _require_sdk() -> None:
    """Raise when the ``daytona`` extra is not installed.

    Raises:
        SandboxFatalError: The SDK did not import.
    """
    if AsyncDaytona is None:
        raise SandboxFatalError("The Daytona sandbox needs the `daytona` package: install the `daytona` extra.")


async def _delete_and_close(sandbox: Any, client: Any) -> None:
    """Delete a box and close its client; a box that is already gone counts as deleted.

    Args:
        sandbox: The box, or ``None``.
        client: The client, or ``None``.

    Raises:
        SandboxError: Daytona failed the delete call.
    """
    try:
        if sandbox is not None:
            try:
                with _translated("delete"):
                    await sandbox.delete(timeout=DELETE_TIMEOUT_SEC)
            except _MissingError:
                pass
    finally:
        if client is not None:
            await client.close()


@contextlib.contextmanager
def _translated(action: str) -> Iterator[None]:
    """Turn a Daytona SDK error into a sandbox error group, without a retry.

    Args:
        action: The short name of the call, for the error message.

    Yields:
        Nothing.

    Raises:
        SandboxError: The mapped error, chained to the SDK error.
    """
    try:
        yield
    except Exception as error:
        if DaytonaError is None or not isinstance(error, DaytonaError):
            raise
        raise _classify(error, action) from error


def _classify(error: Exception, action: str) -> SandboxError:
    """Return the sandbox error for one Daytona SDK error.

    Args:
        error: The SDK error.
        action: The short name of the call.

    Returns:
        The sandbox error in the matching group.
    """
    text = str(error)
    message = f"Daytona {action} failed: {text[:500]}"
    lowered = text.lower()
    if isinstance(error, DaytonaNotFoundError):
        return _MissingError(message)
    if isinstance(error, (DaytonaAuthenticationError, DaytonaAuthorizationError, DaytonaForbiddenError)):
        return SandboxFatalError(message)
    if isinstance(error, DaytonaRateLimitError) or any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return SandboxTransientError(message)
    if any(marker in lowered for marker in _BUILD_MARKERS):
        return SandboxRejectedError(message)
    return SandboxInfraError(message)


def _resources(spec: SandboxSpec) -> Any:
    """Build the Daytona resource request from the spec.

    Args:
        spec: The sandbox spec.

    Returns:
        A ``Resources`` object, or ``None`` when the spec ignores sizes.
    """
    request = spec.resources
    if request.mode is ResourceMode.IGNORE:
        return None
    sizes: dict[str, int] = {"cpu": request.cpus}
    # Daytona sizes memory and disk in whole GiB; round up so the box is
    # never smaller than the request.
    if request.memory_mb is not None:
        sizes["memory"] = math.ceil(request.memory_mb / 1024)
    if request.disk_mb is not None:
        sizes["disk"] = math.ceil(request.disk_mb / 1024)
    return Resources(**sizes)


def _network_kwargs(policy: NetworkPolicy, *, clear: bool) -> dict[str, Any]:
    """Return the Daytona network fields for one egress policy.

    Args:
        policy: The egress policy.
        clear: True for a change on a running box. Daytona keeps a stored
            list until it is cleared, so the unused lists are sent empty.

    Returns:
        The keyword arguments for the create or the network update call.
    """
    mode = network_mode(disabled=policy.disabled, allowed_hosts=policy.allowed_hosts)
    if mode is NetworkMode.DENY_ALL:
        return {"network_block_all": True}
    kwargs: dict[str, Any] = {"network_block_all": False}
    if clear:
        kwargs["domain_allow_list"] = ""
        kwargs["network_allow_list"] = ""
    if mode is NetworkMode.ALLOWLIST:
        domains, cidrs = _split_hosts(policy.allowed_hosts)
        if domains:
            kwargs["domain_allow_list"] = ",".join(domains)
        if cidrs:
            kwargs["network_allow_list"] = ",".join(cidrs)
    return kwargs


def _split_hosts(hosts: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Split allowed hosts into domain names and CIDR blocks.

    Args:
        hosts: The allowed hosts; an IP address or a CIDR block is a network.

    Returns:
        The domain names and the CIDR blocks, in their input order.
    """
    domains: list[str] = []
    cidrs: list[str] = []
    for host in hosts:
        try:
            cidrs.append(str(ipaddress.ip_network(host, strict=False)))
        except ValueError:
            domains.append(host)
    return domains, cidrs


def _shell_command(
    command: str,
    *,
    cwd: str | None,
    env: Mapping[str, str] | None,
    timeout_sec: float | None,
) -> str:
    """Wrap a command for one Daytona session call.

    Args:
        command: The shell command.
        cwd: The working directory, or ``None``.
        env: Extra environment variables, or ``None``.
        timeout_sec: The time limit, or ``None``.

    Returns:
        The full command line.
    """
    line = f"bash -c {shlex.quote(command)}"
    if env:
        line = "env " + " ".join(f"{shlex.quote(f'{key}={value}')}" for key, value in env.items()) + f" {line}"
    if timeout_sec is not None:
        # The in-box timeout ends the command when the client deadline cancels
        # the call, so the process does not keep running in the box.
        line = f"timeout {math.ceil(timeout_sec)} {line}"
    if cwd:
        line = f"cd {shlex.quote(cwd)} && {line}"
    return line

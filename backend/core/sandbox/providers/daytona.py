"""The async Daytona sandbox provider, for dev use only.

Adapted from Harbor 0.25.0 (Apache-2.0), ``harbor/environments/daytona/environment.py``
and ``harbor/environments/daytona/utils.py``. Changes:

- The provider runs on the SBX-6 async contract in ``..environment``.
- No retries (SKY-406 D3, D10). Each SDK error maps to one sandbox error
  group, and the caller decides whether to retry.
- No snapshots, no Dockerfile builds, no compose, no SSH and no GPU types.
  A box always starts from an image. A user image pinned by digest
  (SKY-889) goes to Daytona as it is, and Daytona pulls it on its side.
  Daytona takes no pull credentials with a create call: a private registry
  needs a registry entry in the Daytona organization (for ECR, an IAM role
  that Daytona assumes), so this provider takes none.
- ``exec`` runs one synchronous session command, so stdout and stderr stay
  apart and no status polling is needed.
- ``stop_job_sandboxes`` finds the boxes of a job by their ``JOB_TAG`` label.
- The factory refuses this provider in production (SKY-406 D1).
- A protected box reserves its whole funded lifetime before the create call
  and settles after the delete (SKY-545). Its TTL is fixed at create, so the
  lifetime never grows and the box never extends its reservation.
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
from collections.abc import Callable, Iterator, Mapping
from typing import Any, ClassVar

from core.billing.budgets import BudgetError
from core.billing.daytona_usage import DaytonaUsageReservation
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, network_mode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import (
    SandboxError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
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
        DaytonaTimeoutError,
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
    DaytonaTimeoutError = None
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

DEFAULT_IMAGE = "python:3.12.15-slim"
CREATE_TIMEOUT_SEC = 300.0
DELETE_TIMEOUT_SEC = 60.0
# The list call can lag a new box; a second lookup after this wait must
# also find nothing before a refusal releases the hold.
LOOKUP_RETRY_SEC = 3.0
# Daytona's default box size, from https://www.daytona.io/docs/en/sandboxes
# (read 2026-11-24). A protected box sends these sizes when the spec leaves
# them open, so the box gets its usual size and the quote prices it exactly.
DEFAULT_MEMORY_GIB = 1
DEFAULT_DISK_GIB = 3

_TRANSIENT_MARKERS = ("limit exceeded", "too many requests", "capacity", "rate limit")
_BUILD_MARKERS = ("build_failed", "failed to build")
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124


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

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = DaytonaUsageReservation,
    ) -> None:
        """Check the spec and create a stopped Daytona sandbox.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` timeout. ``None`` means no default timeout.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: The provider cannot open this spec, or a
                protected box has no operation key or ignores sizes.
            SandboxFatalError: The ``daytona`` extra is not installed.
        """
        super().__init__(spec)
        _require_sdk()
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Daytona sandbox needs an operation key.")
            if spec.resources.mode is ResourceMode.IGNORE:
                raise SandboxSpecError("A protected Daytona sandbox needs explicit sizes to price.")
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._client: Any = None
        self._sandbox: Any = None
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any = None
        self._create_error: BaseException | None = None
        self._create_sent = False
        # A label that only this box carries, so a failed create can still
        # find the box it made.
        self._box_label = {"skynet-box": uuid.uuid4().hex} if budget is not None else {}

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
            StartTimeoutError: The box did not become ready in time.
            SandboxError: Daytona refused or failed the create call.
        """
        if self._sandbox is not None:
            return
        ttl_minutes = max(1, math.ceil(self.spec.lifetime_seconds / 60))
        await self._reserve(ttl_minutes)
        if self._client is None:
            self._client = _client()
        params = CreateSandboxFromImageParams(
            image=self.spec.image or DEFAULT_IMAGE,
            resources=_resources(self.spec, protected=self._budget is not None),
            env_vars=dict(self.spec.env) or None,
            labels={**self.spec.tags, **self._box_label} or None,
            # ephemeral deletes the box when it stops, and the TTL deletes a
            # box that a crashed worker never stopped.
            ephemeral=True,
            auto_stop_interval=0,
            ttl_minutes=ttl_minutes,
            **_network_kwargs(self._network_policy or NetworkPolicy(), clear=False),
        )
        create = asyncio.ensure_future(self._create(params))
        try:
            self._sandbox = await asyncio.shield(create)
        except asyncio.CancelledError:
            # A cancel must not lose a box that Daytona already made: wait for
            # the create to end, also through more cancels, and keep the box,
            # so stop() can delete it. asyncio.wait does not raise the create
            # error, so a failed create still passes the cancel on.
            while not create.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({create})
            if not create.cancelled() and create.exception() is None:
                self._sandbox = create.result()
            elif not create.cancelled():
                self._create_error = create.exception()
            raise
        except BaseException as error:
            self._create_error = error
            raise
        if self._usage is not None:
            # A size mismatch fails the start; the caller's stop() then
            # deletes the box and settles it.
            await asyncio.shield(asyncio.to_thread(self._usage.confirm_created, self._sandbox))

    async def _reserve(self, ttl_minutes: int) -> None:
        """Reserve the whole funded lifetime of a protected box before the create call.

        Args:
            ttl_minutes: The box TTL that Daytona enforces.
        """
        if self._budget is None or self._usage is not None:
            return
        reserving = asyncio.ensure_future(
            asyncio.to_thread(
                self._usage_factory,
                self._budget,
                self._usage_request(ttl_minutes),
                operation_key=self.spec.operation_key,
            )
        )
        try:
            self._usage = await asyncio.shield(reserving)
            self._usage.box_label = dict(self._box_label)
        except asyncio.CancelledError:
            # The thread cannot be cancelled and may still reserve; keep its
            # result so stop() releases it instead of leaking it.
            while not reserving.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({reserving})
            if not reserving.cancelled() and reserving.exception() is None:
                self._usage = reserving.result()
                self._usage.box_label = dict(self._box_label)
            raise

    def _usage_request(self, ttl_minutes: int) -> dict[str, Any]:
        """Return the create parameters that the reservation prices.

        The request has no per-box name, so pool lease slices for equal boxes
        share one fingerprint.

        Args:
            ttl_minutes: The box TTL that Daytona enforces.

        Returns:
            The funded lifetime, sizes and image of the box.
        """
        sizes = _sizes(self.spec, protected=True)
        return {
            # The TTL counts from the create; the delete timeout covers a box
            # whose delete runs at the end of the TTL.
            "lifetime_ms": ttl_minutes * 60_000 + math.ceil(DELETE_TIMEOUT_SEC * 1000),
            "vcpus": sizes["cpu"],
            "memory_gib": sizes["memory"],
            "disk_gib": sizes["disk"],
            "image": self.spec.image or DEFAULT_IMAGE,
        }

    async def stop(self) -> None:
        """Delete the box, close the client and settle the reservation. Safe before start and when called twice.

        A cancel during the delete waits for the delete to finish, also
        through more cancels, then passes the cancel on.

        Raises:
            SandboxError: Daytona failed the delete call; the reservation stays pending.
            UsagePendingError: The settle failed, so the reservation stays pending.
            BudgetError: The usage evidence breaks the reserved bounds.
        """
        sandbox, client, usage = self._sandbox, self._client, self._usage
        create_error, create_sent = self._create_error, self._create_sent
        self._sandbox = None
        self._client = None
        self._usage = None
        self._create_error = None
        self._create_sent = False
        if sandbox is None and client is None and usage is None:
            return
        cleanup = asyncio.ensure_future(
            _delete_and_settle(
                sandbox,
                client,
                usage,
                create_error=create_error,
                create_sent=create_sent,
                box_label=self._box_label,
            )
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            while not cleanup.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({cleanup})
            if not cleanup.cancelled() and cleanup.exception() is not None:
                logger.warning("The Daytona delete failed during a cancelled stop.", exc_info=cleanup.exception())
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
            The exit code and the output of the command. When the command runs
            past its time limit, the result has exit code 124 and ``timed_out``
            set.

        Raises:
            SandboxRejectedError: ``sudo`` is set.
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
                return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
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
            StartTimeoutError: The box did not become ready in time.
            SandboxError: Daytona refused or failed the create call.
        """
        with _translated("create"):
            try:
                self._create_sent = True
                return await self._client.create(params, timeout=CREATE_TIMEOUT_SEC)
            except DaytonaTimeoutError as error:
                # The SDK timeout is not a TimeoutError, and the box can
                # already exist. Keep it, so stop() deletes it.
                await self._keep_timed_out_box(error.sandbox_id)
                raise StartTimeoutError(CREATE_TIMEOUT_SEC) from error

    async def _keep_timed_out_box(self, sandbox_id: str | None) -> None:
        """Find the box that a timed-out create made, so stop() can delete it.

        A failed lookup only logs: the box TTL deletes a box that stays lost,
        and the caller needs the timeout error, not the lookup error.

        Args:
            sandbox_id: The box id from the SDK timeout, or ``None``.
        """
        if not sandbox_id:
            return
        try:
            self._sandbox = await self._client.get(sandbox_id)
        except Exception:
            logger.warning("Daytona could not find the timed-out box %s.", sandbox_id, exc_info=True)

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


async def _delete_and_settle(
    sandbox: Any,
    client: Any,
    usage: Any,
    *,
    create_error: BaseException | None,
    create_sent: bool,
    box_label: Mapping[str, str],
) -> None:
    """Delete a box, then settle or release its reservation.

    Args:
        sandbox: The box, or ``None`` when no box handle came back.
        client: The client, or ``None``.
        usage: The reservation of a protected box, or ``None``.
        create_error: The create failure, or ``None``.
        create_sent: True once the create call was sent to Daytona.
        box_label: The label that only this box carries.

    Raises:
        SandboxError: Daytona failed the delete call.
        UsagePendingError: The settle failed, so the reservation stays pending.
        BudgetError: The usage evidence breaks the reserved bounds.
    """
    looked_up = False
    if usage is not None and sandbox is None and create_sent and client is not None:
        # The create can make a box and still fail or lose its answer, so
        # look for the box by its own label before the hold is released.
        try:
            sandbox = await asyncio.wait_for(_find_box(client, box_label), DELETE_TIMEOUT_SEC)
            if sandbox is None and create_error is not None and _refused(create_error):
                # A refusal can also come from the status poll after the box
                # exists, and the list can lag that box; look once more.
                await asyncio.sleep(LOOKUP_RETRY_SEC)
                sandbox = await asyncio.wait_for(_find_box(client, box_label), DELETE_TIMEOUT_SEC)
            looked_up = True
        except Exception as error:
            logger.warning("The sandbox lookup after a failed create failed: %s", type(error).__name__)
    try:
        await _delete_and_close(sandbox, client)
    except BaseException:
        if usage is not None:
            # The box may still run until its TTL, so keep the whole hold.
            try:
                await asyncio.to_thread(usage.pending)
            except Exception as error:
                logger.warning("The sandbox reservation did not mark pending: %s", type(error).__name__)
        raise
    if usage is None:
        return
    try:
        if sandbox is not None:
            await asyncio.to_thread(usage.bind, sandbox)
            await asyncio.to_thread(usage.settle)
        else:
            error = create_error or SandboxInfraError("The sandbox was never created.")
            refused = not create_sent or (looked_up and _refused(error))
            await asyncio.to_thread(usage.fail, error, refused=refused)
    except (UsagePendingError, BudgetError):
        raise
    except Exception as error:
        # The text of a database error can hold details the run must not see.
        raise UsagePendingError("Sandbox usage is not yet settled; the reservation remains active.") from error


async def _find_box(client: Any, box_label: Mapping[str, str]) -> Any:
    """Find the box that carries a label.

    Args:
        client: The Daytona client.
        box_label: The label that only one box carries.

    Returns:
        The box, or ``None`` when Daytona lists no such box.

    Raises:
        SandboxError: Daytona failed the list call.
    """
    with _translated("list"):
        async for box in client.list(ListSandboxesQuery(labels=dict(box_label))):
            return box
    return None


def _refused(error: BaseException) -> bool:
    """Report whether Daytona answered the create call with a refusal.

    Only the SDK error types of a 401, 403 or 429 answer count. The caller
    also checks that no box carries the box label, because the same errors
    can come from a status read after the box exists.

    Args:
        error: The create failure.

    Returns:
        True for an authentication, permission or rate-limit answer.
    """
    cause = error.__cause__
    refusals = tuple(
        kind
        for kind in (
            DaytonaAuthenticationError,
            DaytonaAuthorizationError,
            DaytonaForbiddenError,
            DaytonaRateLimitError,
        )
        if kind is not None
    )
    return bool(refusals) and isinstance(cause, refusals)


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


def _sizes(spec: SandboxSpec, *, protected: bool) -> dict[str, int]:
    """Return the Daytona box sizes for the spec.

    Args:
        spec: The sandbox spec.
        protected: True for a metered box, which always sends every size.

    Returns:
        The CPU count, and memory and disk in GiB when known.
    """
    request = spec.resources
    sizes: dict[str, int] = {"cpu": request.cpus}
    # Daytona sizes memory and disk in whole GiB; round up so the box is
    # never smaller than the request.
    if request.memory_mb is not None:
        sizes["memory"] = math.ceil(request.memory_mb / 1024)
    elif protected:
        sizes["memory"] = DEFAULT_MEMORY_GIB
    if request.disk_mb is not None:
        sizes["disk"] = math.ceil(request.disk_mb / 1024)
    elif protected:
        sizes["disk"] = DEFAULT_DISK_GIB
    return sizes


def _resources(spec: SandboxSpec, *, protected: bool = False) -> Any:
    """Build the Daytona resource request from the spec.

    Args:
        spec: The sandbox spec.
        protected: True for a metered box, which always sends every size.

    Returns:
        A ``Resources`` object, or ``None`` when the spec ignores sizes.
    """
    if spec.resources.mode is ResourceMode.IGNORE:
        return None
    return Resources(**_sizes(spec, protected=protected))


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

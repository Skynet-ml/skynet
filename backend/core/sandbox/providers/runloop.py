"""The async Runloop devbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), runloop: the devbox create, the
command wrapper, the file calls and the named network policy flow come from
``harbor/environments/runloop.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- The SDK client never retries (``max_retries=0``) and the provider adds no
  retry of its own (SKY-406 D3, D10). An SDK error maps to one sandbox
  error group.
- The start and run phase timeouts bound the create and each command. A
  command that runs past its limit or whose ``exec`` is cancelled is killed.
- Each devbox carries a box tag in its metadata. A stop during the create,
  or after a create that timed out, finds the box by that tag and shuts it
  down, so a lost create does not leak a box.
- The spec sizes map to Runloop custom sizes, and a memory request that
  Runloop cannot give is rejected at construction.
- ``stop_job_sandboxes`` finds the devboxes of a job by their metadata.
- With a budget the devbox is protected: it reserves its whole keep-alive
  lifetime before the create call and settles after the shutdown, from
  Runloop's usage report or else from the measured time
  (``core.billing.runloop_usage``).
- Harbor's blueprint and snapshot builds, storage-object uploads, directory
  transfer and user switching are left out. The devbox runs as root.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import math
import os
import re
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

from core.billing.budgets import BudgetError
from core.billing.operation_pricing import json_fingerprint
from core.billing.runloop_usage import DEFAULT_DISK_GB, RunloopUsageReservation
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, network_mode
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
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
    from runloop_api_client import (
        APIConnectionError,
        APIStatusError,
        AsyncRunloopSDK,
        AuthenticationError,
        BadRequestError,
        ConflictError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
        RunloopError,
        UnprocessableEntityError,
    )
    from runloop_api_client.lib.polling import PollingConfig
except ImportError:
    APIConnectionError = None
    APIStatusError = None
    AsyncRunloopSDK = None
    AuthenticationError = None
    BadRequestError = None
    ConflictError = None
    InternalServerError = None
    NotFoundError = None
    PermissionDeniedError = None
    RateLimitError = None
    RunloopError = None
    UnprocessableEntityError = None
    PollingConfig = None

logger = logging.getLogger(__name__)

API_KEY_ENV = "RUNLOOP_API_KEY"
BOX_TAG = "skynet_box"
STOP_TIMEOUT = 60.0
KILL_TIMEOUT = 30.0
POLL_INTERVAL = 1.0
_LIST_LIMIT = 5000
# The asyncio phase timeouts bound every wait, so the SDK poll limit must
# never end a wait first.
_UNBOUNDED_POLLS = 10**9
_MAX_KEEP_ALIVE_SECONDS = 172_800
_MAX_MEMORY_GB = 64
_ENDED_STATUSES = frozenset({"failure", "shutdown"})
# Answers that refuse a request before Runloop acts on it. Other 4xx codes,
# 5xx answers and transport errors leave the outcome unknown.
_REFUSED_STATUSES = frozenset({400, 401, 403, 404, 409, 422, 429})
# The SDK's devbox.create POSTs here, then polls wait_for_status while the box
# boots. Only a refusal status from this POST shows that no devbox was made.
_CREATE_PATH = "/v1/devboxes/create_and_await_running"
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Runloop custom sizes: 1 or an even number of vCPUs up to 16, even GiB of
# disk up to 64, and 2 to 8 GiB of memory for each vCPU (checked in
# ``_launch_parameters``, because the capability check has no lower bound).
RUNLOOP_CAPABILITIES = EnvironmentCapabilities(
    name="runloop",
    network_modes=frozenset(NetworkMode),
    max_vcpus=16,
    even_vcpus=True,
    max_disk_mb=64 * 1024,
    billable=True,
)


def _classes(*classes: type[BaseException] | None) -> tuple[type[BaseException], ...]:
    """Return the SDK error classes that exist.

    Args:
        *classes: SDK error classes, ``None`` when the SDK is not installed.

    Returns:
        The classes that are not ``None``, for use in an ``except`` clause.
    """
    return tuple(cls for cls in classes if cls is not None)


@contextlib.contextmanager
def _mapped(action: str) -> Iterator[None]:
    """Turn an SDK error into the sandbox error of its group.

    Args:
        action: What the provider was doing, for the error message.

    Yields:
        Nothing.

    Raises:
        SandboxTransientError: A connection error, a rate limit or a 5xx answer.
        SandboxFatalError: The API key is wrong or has no permission.
        SandboxRejectedError: Runloop rejected the request as invalid.
        SandboxInfraError: Any other SDK error.
    """
    try:
        yield
    except _classes(APIConnectionError, RateLimitError, InternalServerError) as error:
        raise SandboxTransientError(f"Runloop could not {action}: {type(error).__name__}.") from error
    except _classes(AuthenticationError, PermissionDeniedError) as error:
        raise SandboxFatalError(f"Runloop refused the credentials to {action}: {type(error).__name__}.") from error
    except _classes(BadRequestError, UnprocessableEntityError) as error:
        raise SandboxRejectedError(f"Runloop rejected the request to {action}: {type(error).__name__}.") from error
    except _classes(APIStatusError, RunloopError) as error:
        raise SandboxInfraError(f"Runloop could not {action}: {type(error).__name__}.") from error


def _polling() -> Any:
    """Return an SDK polling config that the asyncio timeouts end.

    Returns:
        A ``PollingConfig`` with no attempt or time limit of its own.
    """
    return PollingConfig(interval_seconds=POLL_INTERVAL, max_attempts=_UNBOUNDED_POLLS, timeout_seconds=None)


def _even_up(value: int) -> int:
    """Round a whole number up to the next even number.

    Args:
        value: The number.

    Returns:
        ``value`` when it is even, else ``value + 1``.
    """
    return value + value % 2


def _launch_parameters(spec: SandboxSpec) -> dict[str, Any]:
    """Map the spec to Runloop launch parameters.

    Args:
        spec: The sandbox to open. ``validate_spec`` already checked it.

    Returns:
        The ``launch_parameters`` of the create call.

    Raises:
        SandboxSpecError: Runloop cannot give the requested memory to this
            number of vCPUs.
    """
    params: dict[str, Any] = {
        "architecture": "x86_64",
        "keep_alive_time_seconds": min(math.ceil(spec.lifetime_seconds), _MAX_KEEP_ALIVE_SECONDS),
        "user_parameters": {"uid": 0, "username": "root"},
    }
    request = spec.resources
    if request.mode is ResourceMode.IGNORE:
        return params
    cpus = request.cpus
    asked_gb = math.ceil(request.memory_mb / 1024) if request.memory_mb is not None else 0
    memory_gb = _even_up(max(asked_gb, 2 * cpus))
    if memory_gb > min(_MAX_MEMORY_GB, 8 * cpus):
        raise SandboxSpecError(
            f"The runloop sandbox gives at most {min(_MAX_MEMORY_GB, 8 * cpus)} GiB of memory to {cpus} vCPUs."
        )
    params.update(resource_size_request="CUSTOM_SIZE", custom_cpu_cores=cpus, custom_gb_memory=memory_gb)
    if request.disk_mb is not None:
        params["custom_disk_size"] = _even_up(max(2, math.ceil(request.disk_mb / 1024)))
    return params


def _policy_name(hosts: list[str]) -> str:
    """Name the shared Runloop network policy for one set of hosts.

    Args:
        hosts: The sorted, lower-case allowed hosts; empty means no network.

    Returns:
        A stable name, so boxes with the same egress share one policy.
    """
    if not hosts:
        return "skynet-network-policy-no-network-v1"
    digest = hashlib.sha256("\n".join(hosts).encode()).hexdigest()[:16]
    return f"skynet-network-policy-allowlist-{digest}-v1"


def _policy_matches(policy: Any, hosts: list[str]) -> bool:
    """Tell whether a Runloop network policy allows exactly these hosts.

    Args:
        policy: A Runloop network policy view.
        hosts: The sorted allowed hosts.

    Returns:
        True when the policy allows these hosts and nothing else.
    """
    # Hostnames are case-insensitive, and Runloop may return them in another
    # order or more than once.
    wanted = {host.lower() for host in hosts}
    egress = getattr(policy, "egress", None)
    if egress is None:
        return False
    return (
        getattr(egress, "allow_all", None) is False
        and getattr(egress, "allow_agent_gateway", False) is False
        and getattr(egress, "allow_devbox_to_devbox", False) is False
        and getattr(egress, "allow_mcp_gateway", False) is False
        and getattr(egress, "allow_runloop_mirrors", False) is False
        and not list(getattr(egress, "allowed_cidrs", None) or [])
        and {str(host).lower() for host in getattr(egress, "allowed_hostnames", None) or []} == wanted
    )


def _refused_create(error: SandboxError) -> bool:
    """Tell whether Runloop refused the create POST itself.

    Args:
        error: The mapped error of ``client.devbox.create``.

    Returns:
        True when the SDK error is a refusal status from the create POST,
        not from the status poll that follows it once the devbox exists.
    """
    if getattr(error.__cause__, "status_code", None) not in _REFUSED_STATUSES:
        return False
    request = getattr(error.__cause__, "request", None)
    path = getattr(getattr(request, "url", None), "path", None)
    return isinstance(path, str) and path.endswith(_CREATE_PATH)


def _settle_failure(error: Exception) -> Exception:
    """Return the error that a failed settle raises from ``stop()``.

    Args:
        error: The settle failure.

    Returns:
        A billing error unchanged. Any other failure becomes a
        ``UsagePendingError``, because the hold stays pending and the
        original text can hold database details.
    """
    if isinstance(error, UsagePendingError | BudgetError):
        return error
    pending = UsagePendingError("Sandbox usage is not yet settled; the reservation remains active.")
    pending.__cause__ = error
    return pending


async def _bounded(awaitable: Awaitable[Any], action: str) -> bool:
    """Wait for a cleanup call for at most ``STOP_TIMEOUT`` seconds.

    Args:
        awaitable: The cleanup call.
        action: What the call does, for the log.

    Returns:
        True when the call finished without an error.
    """
    try:
        await asyncio.wait_for(awaitable, STOP_TIMEOUT)
    except Exception as error:
        logger.warning("Runloop could not %s: %s", action, type(error).__name__)
        return False
    return True


class RunloopEnvironment(SandboxEnvironment):
    """Run commands in one Runloop devbox."""

    capabilities = RUNLOOP_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = RunloopUsageReservation,
    ) -> None:
        """Check the spec and prepare the create call.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; ``None`` sets no start or run limit.
            budget: The ``BudgetRuntime`` that funds the devbox, or ``None``.
            usage_factory: Makes the usage reservation of a protected devbox.

        Raises:
            SandboxSpecError: Runloop cannot open this spec, or a protected
                spec has no operation key or no explicit size.
        """
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Runloop sandbox needs an operation key.")
            if spec.resources.mode is ResourceMode.IGNORE:
                # Runloop picks the size in this mode, so no price bounds the box.
                raise SandboxSpecError("A protected Runloop sandbox needs an explicit size.")
            self.capabilities = dataclasses.replace(RUNLOOP_CAPABILITIES, protected=True)
        super().__init__(spec)
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        self._box_tag = uuid.uuid4().hex
        self._create_params: dict[str, Any] = {
            "name": spec.name or f"skynet-{self._box_tag[:12]}",
            "metadata": {**spec.tags, BOX_TAG: self._box_tag},
            "environment_variables": dict(spec.env),
            "launch_parameters": _launch_parameters(spec),
        }
        self._client: Any = None
        self._devbox: Any = None
        self._starting: asyncio.Future[None] | None = None
        self._create_dispatched = False
        self._started = False
        self._stop_task: asyncio.Future[None] | None = None
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._binding: asyncio.Future[Any] | None = None
        self._create_sent_at: float | None = None
        self._create_refused: SandboxError | None = None
        self._settle_error: Exception | None = None
        self._orphan_id: str | None = None
        self._orphan_launch: Any = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and the API key exist.

        Raises:
            SandboxFatalError: The SDK or the ``RUNLOOP_API_KEY`` variable is missing.
        """
        if AsyncRunloopSDK is None:
            raise SandboxFatalError("The runloop-api-client package is not installed: install the `runloop` extra.")
        if not os.environ.get(API_KEY_ENV):
            raise SandboxFatalError(f"The Runloop sandbox needs the {API_KEY_ENV} variable.")

    @staticmethod
    def _make_client() -> Any:
        """Make the async SDK client, which reads ``RUNLOOP_API_KEY``.

        Returns:
            An ``AsyncRunloopSDK`` with the SDK retries off.
        """
        RunloopEnvironment.preflight()
        return AsyncRunloopSDK(max_retries=0)

    async def start(self) -> None:
        """Create the devbox and wait until it runs.

        Any failure or cancel stops the box before the error goes on.

        Raises:
            StartTimeoutError: The devbox did not run within the start timeout.
            SandboxInfraError: A stop ended the start, or the sandbox already
                started once.
            SandboxError: The create call failed; see ``_mapped``.
        """
        if self._started:
            # The box tag is the create idempotency key, so a second create
            # would get the first box back.
            raise SandboxInfraError("The Runloop sandbox already started once; it cannot start again.")
        self._started = True
        # A stop before the start leaves a finished stop task; drop it so the
        # stop after this start shuts the box down.
        self._stop_task = None
        self._client = self._make_client()
        self._starting = asyncio.ensure_future(self._create())
        try:
            await self._starting
        except BaseException as error:
            # The start error goes on; a settle error stays for the next stop().
            await self._stop_once()
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The Runloop sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    def _usage_request(self) -> dict[str, Any]:
        """Build the usage request that the reservation prices.

        It leaves out the box name and tag, so every box of one pool sends the
        same request.

        Returns:
            The size, lifetime and network parameters of the create.
        """
        launch = self._create_params["launch_parameters"]
        hosts = sorted({host.lower() for host in self.spec.allowed_hosts})
        return {
            "lifetime_ms": launch["keep_alive_time_seconds"] * 1000,
            "vcpus": launch.get("custom_cpu_cores"),
            "memory_gb": launch.get("custom_gb_memory"),
            "disk_gb": launch.get("custom_disk_size", DEFAULT_DISK_GB),
            "network_disabled": self.spec.network_disabled,
            **({"allowed_hosts": hosts} if hosts else {}),
            "environment_fingerprint": json_fingerprint(dict(self.spec.env)),
        }

    async def _reserve(self) -> None:
        """Reserve the whole lifetime of a protected devbox before the create call."""
        reserving = asyncio.ensure_future(
            asyncio.to_thread(
                self._usage_factory, self._budget, self._usage_request(), operation_key=self.spec.operation_key
            )
        )
        try:
            self._usage = await asyncio.shield(reserving)
        except asyncio.CancelledError:
            # The thread cannot be cancelled and may still reserve; keep its
            # result so the teardown releases it instead of leaking it.
            while not reserving.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(reserving)
            if not reserving.cancelled() and reserving.exception() is None:
                self._usage = reserving.result()
            raise

    async def _create(self) -> None:
        """Reserve the budget, find the network policy, then create the devbox and keep it.

        Raises:
            StartTimeoutError: The devbox did not run within the start timeout.
            UsagePendingError: A protected devbox does not have its funded size.
        """
        if self._budget is not None and self._usage is None:
            await self._reserve()
        client = self._client
        params = dict(self._create_params)
        limit = self._start_limit
        try:
            async with asyncio.timeout(limit) as deadline:
                policy_id = await self._network_policy_id(client)
                if policy_id is not None:
                    params["launch_parameters"] = {**params["launch_parameters"], "network_policy_id": policy_id}
                self._create_dispatched = True
                self._create_sent_at = time.monotonic()
                try:
                    with _mapped("create the devbox"):
                        # The devbox is kept here, not in start(), so a stop that
                        # cancels the start right after the create still sees it.
                        self._devbox = await client.devbox.create(
                            **params, polling_config=_polling(), idempotency_key=self._box_tag
                        )
                except SandboxError as error:
                    if _refused_create(error):
                        self._create_refused = error
                    raise
                if self._usage is not None:
                    # The SDK keeps only the id of the created devbox, so read
                    # its view to check the size that Runloop gave it.
                    with _mapped("read the devbox"):
                        view = await self._devbox.get_info()
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(limit) from None
        if self._usage is not None:
            # The teardown waits for this call, so it never settles an unbound hold.
            self._binding = asyncio.ensure_future(
                asyncio.to_thread(self._usage.confirm, self._devbox.id, view.launch_parameters)
            )
            await asyncio.shield(self._binding)

    async def _network_policy_id(self, client: Any) -> str | None:
        """Find or create the shared network policy that the spec needs.

        Args:
            client: The SDK client.

        Returns:
            The policy id, or ``None`` for open egress.

        Raises:
            SandboxFatalError: A policy with the same name allows other egress.
        """
        mode = network_mode(disabled=self.spec.network_disabled, allowed_hosts=tuple(self.spec.allowed_hosts))
        if mode is NetworkMode.OPEN:
            return None
        # Hostnames are case-insensitive, so "A.example" and "a.example" share
        # one policy name and one policy.
        hosts = [] if mode is NetworkMode.DENY_ALL else sorted({host.lower() for host in self.spec.allowed_hosts})
        name = _policy_name(hosts)
        with _mapped("set up the network policy"):
            found = await self._find_policy(client, name, hosts)
            if found is not None:
                return found
            try:
                created = await client.api.network_policies.create(
                    name=name,
                    allow_all=False,
                    allow_agent_gateway=False,
                    allow_devbox_to_devbox=False,
                    allow_mcp_gateway=False,
                    allow_runloop_mirrors=False,
                    allowed_hostnames=hosts,
                    description="Managed by Skynet. Every Skynet box with this egress shares it.",
                    idempotency_key=name,
                )
            except _classes(ConflictError):
                # Another box created the same policy at the same time.
                found = await self._find_policy(client, name, hosts)
                if found is not None:
                    return found
                raise
        return created.id

    @staticmethod
    async def _find_policy(client: Any, name: str, hosts: list[str]) -> str | None:
        """Return the id of the named policy when it allows exactly these hosts.

        Args:
            client: The SDK client.
            name: The policy name.
            hosts: The sorted allowed hosts.

        Returns:
            The policy id, or ``None`` when no policy has this name.

        Raises:
            SandboxFatalError: The named policy allows other egress. Using it
                would give the box an egress that the spec did not ask for.
        """
        async for policy in client.api.network_policies.list(name=name, limit=100):
            if getattr(policy, "name", None) != name:
                continue
            if _policy_matches(policy, hosts):
                return policy.id
            raise SandboxFatalError(f"The Runloop network policy {name!r} exists with a different egress.")
        return None

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
        """Run one shell command in the devbox and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory; the devbox home by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, line)`` pairs while the command runs.
            sudo: Has no effect: the devbox already runs commands as root.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, it is killed first, and the result has exit code
            124 and ``timed_out`` set.

        Raises:
            SandboxInfraError: The devbox is not running, or Runloop gave no
                exit code.
            SandboxRejectedError: An environment variable name is not valid.
        """
        devbox, client = self._devbox, self._client
        if devbox is None or client is None:
            raise SandboxInfraError("The Runloop sandbox is not running.")
        wrapped = self._wrap(command, cwd, env)
        callbacks: dict[str, Any] = {}
        if on_output is not None:
            callbacks = {
                "stdout": lambda line: on_output("stdout", line),
                "stderr": lambda line: on_output("stderr", line),
            }
        with _mapped("start the command"):
            execution = await devbox.cmd.exec_async(wrapped, **callbacks)
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        finished = False
        timed_out = False
        try:
            async with asyncio.timeout(limit) as deadline:
                with _mapped("wait for the command"):
                    await client.api.devboxes.executions.await_completed(
                        execution.execution_id, devbox_id=devbox.id, polling_config=_polling()
                    )
                    result = await execution.result()
                    stdout = await result.stdout()
                    stderr = await result.stderr()
            finished = True
        except TimeoutError:
            if not deadline.expired():
                raise
            timed_out = True
        finally:
            if not finished:
                # A timeout, a cancel or a lost wait must not leave the command running.
                await asyncio.shield(_kill(execution))
        if timed_out:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        if result.exit_code is None:
            raise SandboxInfraError("Runloop gave no exit code for the command.")
        return ExecResult(exit_code=result.exit_code, stdout=stdout, stderr=stderr)

    @staticmethod
    def _wrap(command: str, cwd: str | None, env: Mapping[str, str] | None) -> str:
        """Build the shell line that runs the command with its cwd and env.

        Args:
            command: The shell command.
            cwd: The working directory, or ``None``.
            env: Extra environment variables, or ``None``.

        Returns:
            The quoted shell line.

        Raises:
            SandboxRejectedError: An environment variable name is not valid.
        """
        parts: list[str] = []
        for key, value in (env or {}).items():
            if not _ENV_KEY.fullmatch(key):
                raise SandboxRejectedError(f"{key!r} is not a valid environment variable name.")
            parts.append(f"{key}={shlex.quote(value)}")
        line = " ".join([*parts, "bash", "-c", shlex.quote(command)])
        if cwd is not None:
            line = f"cd {shlex.quote(cwd)} && {line}"
        return line

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the devbox, making parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The devbox is not running, or the parent
                directories could not be made.
        """
        devbox = self._devbox
        if devbox is None:
            raise SandboxInfraError("The Runloop sandbox is not running.")
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {".", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents))
            if not made.ok:
                raise SandboxInfraError("Runloop could not make the upload directories.")
        for path, text in files.items():
            with _mapped("write a file"):
                await devbox.file.write(file_path=path, contents=text)

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the devbox.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The devbox is not running.
        """
        devbox = self._devbox
        if devbox is None:
            raise SandboxInfraError("The Runloop sandbox is not running.")
        try:
            with _mapped("read a file"):
                data = await devbox.file.download(path=path)
        except SandboxInfraError as error:
            if NotFoundError is not None and isinstance(error.__cause__, NotFoundError):
                return None
            raise
        return data.decode("utf-8")

    async def stop(self) -> None:
        """Shut the devbox down once, settle its hold and close the client. Safe to call again.

        Raises:
            UsagePendingError: The settle failed, so the hold stays pending.
            BudgetError: The usage violates the reserved bounds.
        """
        await self._stop_once()
        # Like Vercel, only one stop caller gets a settle failure.
        settle_error, self._settle_error = self._settle_error, None
        if settle_error is not None:
            raise settle_error

    async def _stop_once(self) -> None:
        """Run the teardown once, shielded from the caller's cancel."""
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)

    async def _teardown(self) -> None:
        """End a running start, shut the devbox down and close the client."""
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the create first, so no devbox
            # appears after this teardown has looked for it.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        client, devbox = self._client, self._devbox
        self._client = self._devbox = None
        if client is None:
            return
        box_id = devbox.id if devbox is not None else None
        stopped = False
        try:
            if devbox is not None:
                stopped = await _bounded(devbox.shutdown(), "shut the devbox down")
            elif self._create_dispatched:
                stopped = await _bounded(self._shut_down_orphan(client), "shut the lost devbox down")
                box_id = self._orphan_id
            stopped_at = time.monotonic()
            if self._usage is not None:
                await self._settle(client, box_id, stopped_at if stopped else None)
        finally:
            await _bounded(client.aclose(), "close the client")

    async def _settle(self, client: Any, box_id: str | None, stopped_at: float | None) -> None:
        """Settle or release the budget hold of a protected devbox.

        A failure to bind or settle a devbox that exists is kept for
        ``stop()`` to raise; the hold stays pending. When no devbox was
        found, a billing failure is only logged.

        Args:
            client: The SDK client.
            box_id: The devbox id, or ``None`` when no devbox was found.
            stopped_at: ``time.monotonic()`` of the confirmed shutdown or the
                clean tag search, or ``None`` when neither is confirmed.
        """
        usage = self._usage
        if self._binding is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(self._binding)
        try:
            if box_id is None:
                if not self._create_dispatched:
                    await asyncio.to_thread(usage.release, "The create call was never sent.")
                elif self._create_refused is not None and stopped_at is not None:
                    # The tag search ran without an error and found no box.
                    await asyncio.to_thread(usage.release, str(self._create_refused))
                else:
                    # A lost create may still have made a devbox that the tag
                    # search did not find, so the hold waits for reconciliation.
                    await asyncio.to_thread(usage.pending)
                return
            if self._orphan_launch is not None:
                # A lost create never returned the view; the tag search did.
                usage.record_sizes(self._orphan_launch)
            if usage.devbox_id is None:
                await asyncio.to_thread(usage.bind, box_id)
            if stopped_at is not None and self._create_sent_at is not None:
                usage.record_stop(math.ceil((stopped_at - self._create_sent_at) * 1000))
            # Ask even after an unconfirmed shutdown: a final report still
            # settles the box at what it used.
            try:
                report = await asyncio.wait_for(client.api.devboxes.retrieve_resource_usage(box_id), STOP_TIMEOUT)
                usage.record_usage(report)
            except Exception as error:
                # The measured time, if any, still bounds the bill from above.
                logger.warning("Runloop gave no usage report for devbox %s: %s", box_id, type(error).__name__)
            await asyncio.to_thread(usage.settle)
        except Exception as error:
            logger.warning("Runloop devbox %s usage stays pending: %s", box_id, type(error).__name__)
            if box_id is not None:
                self._settle_error = _settle_failure(error)

    async def _shut_down_orphan(self, client: Any) -> None:
        """Shut down the devbox of a create that ended before it returned.

        Args:
            client: The SDK client.
        """
        async for box in client.api.devboxes.list(limit=_LIST_LIMIT):
            if (box.metadata or {}).get(BOX_TAG) == self._box_tag:
                self._orphan_id = box.id
                self._orphan_launch = getattr(box, "launch_parameters", None)
                if box.status not in _ENDED_STATUSES:
                    await client.api.devboxes.shutdown(box.id)
                return

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Shut down every running devbox that carries this job tag.

        Args:
            job_id: The job tag value.

        Returns:
            The number of devboxes that were shut down. A list that runs past
            ``STOP_TIMEOUT`` is logged, and only the devboxes found so far
            are shut down.
        """
        client = cls._make_client()
        try:
            ids: list[str] = []
            try:
                async with asyncio.timeout(STOP_TIMEOUT):
                    with _mapped("list the devboxes"):
                        # A loop, not a comprehension, keeps the ids found
                        # before a list timeout.
                        async for box in client.api.devboxes.list(limit=_LIST_LIMIT):
                            if (box.metadata or {}).get(JOB_TAG) == job_id and box.status not in _ENDED_STATUSES:
                                ids.append(box.id)  # noqa: PERF401
            except TimeoutError:
                logger.warning("Runloop did not list the devboxes of job %s in time.", job_id)
            stopped = 0
            for box_id in ids:
                if await _bounded(client.api.devboxes.shutdown(box_id), "shut a job devbox down"):
                    stopped += 1
            return stopped
        finally:
            await _bounded(client.aclose(), "close the client")


async def _kill(execution: Any) -> None:
    """Kill a running command and log, not raise, a failure.

    Args:
        execution: The SDK execution handle.
    """
    try:
        await asyncio.wait_for(execution.kill(), KILL_TIMEOUT)
    except Exception as error:
        logger.warning("Runloop could not kill the command: %s", type(error).__name__)

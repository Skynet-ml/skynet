"""The async Prime Intellect sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), prime: the sandbox create request,
the command call, the file calls and the network policy flow come from
``harbor/environments/prime.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- The provider adds no retry of its own (SKY-406 D3, D10), with two
  exceptions in the teardown: it sends the delete again after a failure,
  and it reads the deleted sandbox again until Prime shows its end, both
  within ``STOP_TIMEOUT``. The prime-sandboxes SDK keeps internal retries
  that it does not let a caller turn off. An SDK error maps to one sandbox
  error group.
- The start and run phase timeouts bound the create and each command. The
  SDK command timeout is set to the run limit, so Prime also ends a command
  that runs past it.
- Each sandbox carries a box label. A stop during the create, or after a
  create that timed out, finds the sandbox by that label and deletes it, so
  a lost create does not leak a sandbox. The box label is also the create
  idempotency key.
- ``stop_job_sandboxes`` finds the sandboxes of a job by their labels.
- The network policy can change after the start (``set_network``), and the
  provider waits until Prime applies it.
- With a budget the sandbox is protected. It reserves its whole timeout
  before the create call, checks the created size, and settles from
  Prime's own ``created_at`` and ``terminated_at`` after the delete
  (``core.billing.prime_usage``). Prime has no call that makes a running
  sandbox live longer, so a reservation never extends.
- Harbor's Compose mode (and its PyYAML use), GPUs, user switching and
  directory transfer are left out. The sandbox runs commands as root.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import os
import re
import shlex
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from core.billing.prime_usage import PrimeUsageReservation
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, network_mode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import (
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from ..resources import ResourceMode, SandboxSpec
from ..timeouts import Phase, PhaseTimeouts

try:
    from prime_sandboxes import (
        APIError,
        APITimeoutError,
        AsyncSandboxClient,
        CommandTimeoutError,
        CreateSandboxRequest,
        PaymentRequiredError,
        SandboxFileNotFoundError,
        SandboxNotRunningError,
        UnauthorizedError,
        UploadTimeoutError,
    )
except ImportError:
    APIError = None
    APITimeoutError = None
    AsyncSandboxClient = None
    CommandTimeoutError = None
    CreateSandboxRequest = None
    PaymentRequiredError = None
    SandboxFileNotFoundError = None
    SandboxNotRunningError = None
    UnauthorizedError = None
    UploadTimeoutError = None

logger = logging.getLogger(__name__)

API_KEY_ENV = "PRIME_API_KEY"
BOX_TAG = "skynet_box"
DEFAULT_IMAGE = "python:3.12.15-slim"
# prime-sandboxes 0.5.8 CreateSandboxRequest has disk_size_gb=None, and its
# validate_restore_source validator sets 5.0 GB for an image create. The create
# sends this size, so the quote prices the same disk that Prime gives.
DEFAULT_DISK_GB = 5.0
STOP_TIMEOUT = 60.0
POLL_INTERVAL = 1.0
# Prime may list a new sandbox some time after its create; a refused create
# is searched for again after this delay before its hold is released.
SEARCH_AGAIN_DELAY = 10.0
NETWORK_APPLY_TIMEOUT = 30.0
_LIST_PAGE = 100
_NAME_LIMIT = 100
# A command with no limit still needs an SDK timeout; Harbor uses one day.
_NO_LIMIT_SECONDS = 24 * 60 * 60
# The asyncio start timeout bounds the wait, so the SDK budget must never end it first.
_UNBOUNDED_WAIT = 10**6
_ENDED_STATUSES = frozenset({"ERROR", "TERMINATED", "TIMEOUT"})
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
# The SDK gateway upload and read calls say "failed" or "timed out" for a
# connection error or a timeout that carries no HTTP status.
_NO_STATUS_TRANSIENT = re.compile(r"\b(?:failed|timed out)\b")
_FATAL_STATUSES = frozenset({401, 402})
_REJECTED_STATUSES = frozenset({400, 404, 409, 422})
_USAGE_FIELDS = ("cpu_cores", "memory_gb", "disk_size_gb", "timeout_minutes", "gpu_count")
_RECORD_FIELDS = ("id", "status", *_USAGE_FIELDS, "created_at", "started_at", "terminated_at")

PRIME_CAPABILITIES = EnvironmentCapabilities(
    name="prime",
    billable=True,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)


def _classes(*classes: type[BaseException] | None) -> tuple[type[BaseException], ...]:
    """Return the SDK error classes that exist.

    Args:
        *classes: SDK error classes, ``None`` when the SDK is not installed.

    Returns:
        The classes that are not ``None``, for use in an ``except`` clause.
    """
    return tuple(cls for cls in classes if cls is not None)


def _status(error: BaseException) -> int | None:
    """Read the HTTP status from a Prime API error message.

    Args:
        error: The SDK error.

    Returns:
        The status code, or ``None`` when the message has none.
    """
    # The SDK puts the status only in the message, after a prefix for the
    # gateway calls: "Upload failed: HTTP <code> <method> <url>: <detail>".
    match = _HTTP_STATUS.search(str(error))
    return int(match.group(1)) if match else None


@contextlib.contextmanager
def _mapped(action: str) -> Iterator[None]:
    """Turn an SDK error into the sandbox error of its group.

    Args:
        action: What the provider was doing, for the error message.

    Yields:
        Nothing.

    Raises:
        SandboxFatalError: The API key is wrong, or the account cannot pay
            (also HTTP 401 or 402 from a gateway call).
        SandboxTransientError: A connection error, an API, upload or read
            timeout, a rate limit or a 5xx answer.
        SandboxRejectedError: Prime rejected the request as invalid.
        SandboxInfraError: The sandbox stopped, or any other SDK error.
    """
    try:
        yield
    except _classes(UnauthorizedError, PaymentRequiredError) as error:
        raise SandboxFatalError(f"Prime refused the request to {action}: {type(error).__name__}.") from error
    except _classes(APITimeoutError, UploadTimeoutError) as error:
        raise SandboxTransientError(f"Prime could not {action}: {type(error).__name__}.") from error
    except _classes(APIError) as error:
        status = _status(error)
        name = type(error).__name__
        if status in _FATAL_STATUSES:
            raise SandboxFatalError(f"Prime refused the request to {action}: HTTP {status}.") from error
        if (status is None and _NO_STATUS_TRANSIENT.search(str(error))) or status == 429:
            raise SandboxTransientError(f"Prime could not {action}: {name}.") from error
        if status is not None and status >= 500:
            raise SandboxTransientError(f"Prime could not {action}: HTTP {status}.") from error
        if status in _REJECTED_STATUSES:
            raise SandboxRejectedError(f"Prime rejected the request to {action}: HTTP {status}.") from error
        raise SandboxInfraError(f"Prime could not {action}: {name}.") from error
    except _classes(SandboxNotRunningError) as error:
        raise SandboxInfraError(f"Prime could not {action}: {type(error).__name__}.") from error


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
        logger.warning("Prime could not %s: %s", action, type(error).__name__)
        return False
    return True


async def _labelled(client: Any, label: str, *, ended: bool = False) -> AsyncIterator[Any]:
    """Yield the sandboxes that carry one label, page by page.

    Args:
        client: The SDK client.
        label: The ``key=value`` label.
        ended: Also yield the sandboxes that already ended.

    Yields:
        Each sandbox that has the label, and has not ended unless ``ended`` is set.
    """
    cursor: str | None = None
    while True:
        # The SDK sends any value that is not None as ``is_active``, and
        # ``is_active=false`` lists only ended boxes. None lists every box.
        page = await client.list(
            labels=[label], exclude_terminated=None if ended else True, per_page=_LIST_PAGE, cursor=cursor
        )
        for sandbox in page.sandboxes:
            # The label filter is a server query; check it here too, so a
            # wider answer never deletes another sandbox.
            if label in (sandbox.labels or []) and (ended or sandbox.status not in _ENDED_STATUSES):
                yield sandbox
        if not page.has_next or not page.next_cursor:
            return
        cursor = page.next_cursor


def _refused(error: BaseException | None) -> bool:
    """Tell whether Prime refused a create with an answer that can mean no box was made.

    prime-sandboxes 0.5.8 sends the create with ``idempotent_post=True``, so
    it sends it up to 3 times after a ReadError, a RemoteProtocolError, a
    ConnectError or a 502, 503 or 504 answer. A 4xx answer can thus answer a
    retry whose first try made a box. A refusal alone never releases the
    hold: only label searches that find no box can show that none exists. A 409 is an idempotency
    conflict, which can mean that a box exists, so it is never a refusal.

    Args:
        error: The start failure, or ``None``.

    Returns:
        True for a 4xx refusal other than 409.
    """
    return isinstance(error, SandboxRejectedError | SandboxFatalError) and _status(error.__cause__) != 409


def _record(sandbox: Any) -> dict[str, Any]:
    """Copy the billing fields of an SDK sandbox into a plain record.

    Args:
        sandbox: The SDK ``Sandbox``.

    Returns:
        The id, status, size, timeout and timestamps, with ISO text for each time.
    """
    record = {name: getattr(sandbox, name, None) for name in _RECORD_FIELDS}
    return {name: value.isoformat() if isinstance(value, datetime) else value for name, value in record.items()}


def _create_fields(spec: SandboxSpec, box_tag: str) -> dict[str, Any]:
    """Map the spec to the fields of a Prime create request.

    Args:
        spec: The sandbox to open. ``validate_spec`` already checked it.
        box_tag: The unique tag of this sandbox.

    Returns:
        The ``CreateSandboxRequest`` keyword arguments.
    """
    labels = [f"{BOX_TAG}={box_tag}", *(f"{key}={value}" for key, value in spec.tags.items())]
    mode = network_mode(disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts))
    fields: dict[str, Any] = {
        "name": (spec.name or f"skynet-{box_tag[:12]}")[:_NAME_LIMIT],
        "docker_image": spec.image or DEFAULT_IMAGE,
        "timeout_minutes": max(1, math.ceil(spec.lifetime_seconds / 60)),
        "labels": labels,
        "idempotency_key": box_tag,
        # Prime reads an empty allowlist as no egress, and None as open egress.
        "network_allowlist": None if mode is NetworkMode.OPEN else list(dict.fromkeys(spec.allowed_hosts)),
    }
    if spec.env:
        # Harbor sends the start environment as secrets, so Prime does not show the values.
        fields["secrets"] = dict(spec.env)
    request = spec.resources
    if request.mode is not ResourceMode.IGNORE:
        fields["cpu_cores"] = float(request.cpus)
        if request.memory_mb is not None:
            fields["memory_gb"] = request.memory_mb / 1024
        if request.disk_mb is not None:
            fields["disk_size_gb"] = request.disk_mb / 1024
    fields.setdefault("disk_size_gb", DEFAULT_DISK_GB)
    return fields


class PrimeEnvironment(SandboxEnvironment):
    """Run commands in one Prime Intellect sandbox."""

    capabilities = PRIME_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = PrimeUsageReservation,
    ) -> None:
        """Check the spec and prepare the create request.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; ``None`` sets no start or run limit.
            budget: The ``BudgetRuntime`` that funds the sandbox, or ``None``.
            usage_factory: Makes the usage reservation of a protected sandbox.

        Raises:
            SandboxSpecError: Prime cannot open this spec, or a protected
                sandbox has no operation key.
        """
        if budget is not None:
            if not spec.operation_key:
                raise SandboxSpecError("A protected Prime sandbox needs an operation key.")
            self.capabilities = dataclasses.replace(PRIME_CAPABILITIES, protected=True)
        super().__init__(spec)
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._billing_call: asyncio.Future[Any] | None = None
        self._settle_error: Exception | None = None
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        self._box_tag = uuid.uuid4().hex
        self._create_fields = _create_fields(spec, self._box_tag)
        self._client: Any = None
        self._sandbox_id: str | None = None
        self._starting: asyncio.Future[None] | None = None
        self._create_dispatched = False
        self._create_sent_at: float | None = None
        self._create_error: BaseException | None = None
        self._deleted_at: float | None = None
        self._started = False
        self._stop_task: asyncio.Future[None] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and the API key exist.

        Raises:
            SandboxFatalError: The SDK or the ``PRIME_API_KEY`` variable is missing.
        """
        if AsyncSandboxClient is None:
            raise SandboxFatalError("The prime-sandboxes package is not installed: install the `prime` extra.")
        if not os.environ.get(API_KEY_ENV):
            raise SandboxFatalError(f"The Prime sandbox needs the {API_KEY_ENV} variable.")

    @staticmethod
    def _make_client() -> Any:
        """Make the async SDK client with the key from ``PRIME_API_KEY``.

        Returns:
            An ``AsyncSandboxClient``.
        """
        PrimeEnvironment.preflight()
        # Pass the key, so the SDK never falls back to a key in a local config file.
        return AsyncSandboxClient(api_key=os.environ[API_KEY_ENV])

    async def start(self) -> None:
        """Create the sandbox and wait until it runs.

        Any failure or cancel deletes the sandbox and settles, keeps or
        releases its reservation before the error goes on.

        Raises:
            SandboxSpecError: The SDK rejected the create request fields.
            StartTimeoutError: The sandbox did not run within the start timeout.
            SandboxInfraError: A stop ended the start, or the sandbox already
                started once.
            SandboxError: The create call failed; see ``_mapped``.
        """
        if self._started:
            # The box tag is the create idempotency key, so a second create
            # would get the first sandbox back.
            raise SandboxInfraError("The Prime sandbox already started once; it cannot start again.")
        self._started = True
        # A stop before the start leaves a finished stop task; drop it so the
        # stop after this start deletes the sandbox.
        self._stop_task = None
        self._client = self._make_client()
        self._starting = asyncio.ensure_future(self._create())
        try:
            await self._starting
        except BaseException as error:
            # The teardown gets the start error, so it can release the hold
            # of a create that Prime refused.
            if self._stop_task is None:
                self._stop_task = asyncio.ensure_future(self._teardown(error))
            await asyncio.shield(self._stop_task)
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The Prime sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _create(self) -> None:
        """Create the sandbox, and keep any create error for the teardown.

        Raises:
            SandboxSpecError: The SDK rejected the create request fields.
            StartTimeoutError: The sandbox did not run within the start timeout.
            UsagePendingError: Prime created a sandbox larger than funded.
        """
        try:
            await self._create_box()
        except BaseException as error:
            # Kept here, not in start(): a stop that runs before start() sees
            # the error must still know that Prime refused the create.
            self._create_error = error
            raise

    async def _create_box(self) -> None:
        """Reserve the budget, create the sandbox, keep its id and wait until it runs.

        Raises:
            SandboxSpecError: The SDK rejected the create request fields.
            StartTimeoutError: The sandbox did not run within the start timeout.
            UsagePendingError: Prime created a sandbox larger than funded.
        """
        client = self._client
        limit = self._start_limit
        request = self._create_request()
        await self._reserve(request)
        try:
            async with asyncio.timeout(limit) as deadline:
                # Set only now: a request that the SDK refuses never reaches
                # Prime, so the stop has no lost sandbox to look for.
                self._create_dispatched = True
                self._create_sent_at = time.monotonic()
                with _mapped("create the sandbox"):
                    sandbox = await client.create(request)
                    # The id is kept here, not in start(), so a stop that
                    # cancels the start right after the create still sees it.
                    self._sandbox_id = sandbox.id
                    if self._usage is not None:
                        await self._billing(self._usage.confirm_created, _record(sandbox))
                    await client.wait_for_creation(sandbox.id, max_attempts=_UNBOUNDED_WAIT)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(limit) from None

    async def _reserve(self, request: Any) -> None:
        """Reserve the whole timeout of a protected sandbox before its create.

        Args:
            request: The SDK create request, with the SDK defaults filled in.
        """
        if self._budget is None:
            return
        fields = {name: getattr(request, name) for name in _USAGE_FIELDS}
        reserving = asyncio.ensure_future(
            asyncio.to_thread(self._usage_factory, self._budget, fields, operation_key=self.spec.operation_key)
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

    async def _billing(self, call: Callable[..., Any], *args: Any) -> Any:
        """Run one reservation call in a thread that a cancel cannot cut short.

        The teardown awaits the same call before it settles, so a settle never
        races a confirm that is still running.

        Args:
            call: The reservation method.
            *args: Its arguments.

        Returns:
            The result of the call.
        """
        self._billing_call = asyncio.ensure_future(asyncio.to_thread(call, *args))
        return await asyncio.shield(self._billing_call)

    def _create_request(self) -> Any:
        """Build the SDK create request from the prepared fields.

        Returns:
            A ``CreateSandboxRequest``.

        Raises:
            SandboxSpecError: The SDK rejected a field, for example an
                allowlist host with a port, a scheme or a wildcard.
        """
        try:
            return CreateSandboxRequest(**self._create_fields)
        except ValueError:
            pass
        # Raised outside the except block, so neither the cause nor the
        # context keeps the SDK error, whose text holds the secret values.
        raise SandboxSpecError("Prime cannot open this spec: the SDK rejected the create request fields.")

    def _running(self) -> tuple[Any, str]:
        """Return the client and the sandbox id of the running sandbox.

        Returns:
            The SDK client and the sandbox id.

        Raises:
            SandboxInfraError: The sandbox is not running.
        """
        client, sandbox_id = self._client, self._sandbox_id
        if client is None or sandbox_id is None or self._starting is not None:
            raise SandboxInfraError("The Prime sandbox is not running.")
        return client, sandbox_id

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
        """Run one shell command in the sandbox and wait for it.

        Args:
            command: The shell command. The SDK runs it with ``bash -c``.
            cwd: The working directory; the sandbox default by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives the stdout and the stderr text once the
                command ends. Prime does not stream this call.
            sudo: Has no effect: the sandbox already runs commands as root.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, the result has exit code 124 and ``timed_out`` set.

        Raises:
            SandboxInfraError: The sandbox is not running.
            SandboxRejectedError: An environment variable name is not valid.
        """
        client, sandbox_id = self._running()
        for key in env or {}:
            if not _ENV_KEY.fullmatch(key):
                raise SandboxRejectedError(f"{key!r} is not a valid environment variable name.")
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        sdk_timeout = math.ceil(limit) if limit is not None else _NO_LIMIT_SECONDS
        try:
            async with asyncio.timeout(limit) as deadline:
                with _mapped("run the command"):
                    response = await client.execute_command(
                        sandbox_id, command, working_dir=cwd, env=dict(env) if env else None, timeout=sdk_timeout
                    )
        except TimeoutError:
            if not deadline.expired():
                raise
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        except _classes(CommandTimeoutError):
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        stdout, stderr = response.stdout or "", response.stderr or ""
        if on_output is not None:
            if stdout:
                on_output("stdout", stdout)
            if stderr:
                on_output("stderr", stderr)
        return ExecResult(exit_code=response.exit_code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the sandbox, making parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The sandbox is not running, or the parent
                directories could not be made.
            SandboxTransientError: An upload ran past the SDK upload timeout.
        """
        client, sandbox_id = self._running()
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {".", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents))
            if not made.ok:
                raise SandboxInfraError("Prime could not make the upload directories.")
        for path, text in files.items():
            with _mapped("write a file"):
                await client.upload_bytes(sandbox_id, path, text.encode("utf-8"), PurePosixPath(path).name)

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the sandbox.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The sandbox is not running.
        """
        client, sandbox_id = self._running()
        with _mapped("read a file"):
            try:
                data = await client.read_file(sandbox_id, path)
            except _classes(SandboxFileNotFoundError):
                return None
        return data.content

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Replace the egress rules of the sandbox and wait until Prime applies them.

        Args:
            policy: The checked policy.

        Raises:
            SandboxSpecError: The SDK rejected a host of the policy.
            SandboxInfraError: The sandbox is not running, or Prime did not
                apply the rules within ``NETWORK_APPLY_TIMEOUT``.
        """
        client, sandbox_id = self._running()
        if policy.disabled:
            rules: dict[str, list[str]] = {"deny": ["*"]}
        elif policy.allowed_hosts:
            rules = {"allow": list(dict.fromkeys(policy.allowed_hosts))}
        else:
            rules = {"allow": ["*"]}
        with _mapped("change the network policy"):
            try:
                status = await client.set_network(sandbox_id, **rules)
            except ValueError:
                # The SDK checks the hosts before it sends the request.
                raise SandboxSpecError("Prime cannot apply this network policy: the SDK rejected a host.") from None
            try:
                async with asyncio.timeout(NETWORK_APPLY_TIMEOUT):
                    while not status.applied:
                        await asyncio.sleep(POLL_INTERVAL)
                        status = await client.get_network(sandbox_id)
            except TimeoutError:
                raise SandboxInfraError("Prime did not apply the network policy in time.") from None

    async def stop(self) -> None:
        """Delete the sandbox once, settle its reservation and close the client. Safe to call again.

        Raises:
            UsagePendingError: The settle failed, so the reservation stays pending.
            BudgetError: The ledger refused the settlement.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown(None))
        await asyncio.shield(self._stop_task)
        # Only one stop caller gets a settle failure, like the Vercel box.
        settle_error, self._settle_error = self._settle_error, None
        if settle_error is not None:
            raise settle_error

    async def _teardown(self, error: BaseException | None) -> None:
        """End a running start, delete the sandbox, settle its reservation and close the client.

        Args:
            error: The start failure, or ``None`` for a normal stop.
        """
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the create first, so no sandbox
            # appears after this teardown has looked for it.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        if self._billing_call is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(self._billing_call)
        client, sandbox_id = self._client, self._sandbox_id
        self._client = self._sandbox_id = None
        if client is None:
            return
        if error is None:
            error = self._create_error
        try:
            searched = False
            if sandbox_id is not None:
                await self._delete(client, sandbox_id)
            elif self._create_dispatched:
                found, searched = await self._search_orphan(client)
                if found is None and searched and _refused(error):
                    # Release needs two empty searches, so a box that Prime
                    # lists late still settles.
                    await asyncio.sleep(SEARCH_AGAIN_DELAY)
                    found, searched = await self._search_orphan(client)
                if found is not None:
                    sandbox_id = found.id
                    if found.status not in _ENDED_STATUSES:
                        # A failed delete keeps the id, so the box still settles.
                        await self._delete(client, sandbox_id)
            await self._settle(client, sandbox_id, error, searched=searched)
        finally:
            await _bounded(client.aclose(), "close the client")

    async def _settle(
        self, client: Any, sandbox_id: str | None, error: BaseException | None, *, searched: bool
    ) -> None:
        """Settle, keep or release the reservation of a deleted sandbox.

        A create with no sandbox found is released only when it never left,
        or when Prime refused it (4xx other than 409) and two label searches,
        ended sandboxes included, finished and found nothing. A 409, a 5xx
        answer, a create timeout or a cancel during the create stays pending
        on purpose, also after an empty search: Prime may still make the box,
        and only a person can check that. A found box, running or ended, is
        always settled, never released.

        Args:
            client: The SDK client.
            sandbox_id: The deleted sandbox, or ``None`` when none was found.
            error: The start failure, or ``None`` for a normal stop.
            searched: True when the last label search for a lost sandbox finished.
        """
        usage = self._usage
        if usage is None:
            return
        record: dict[str, Any] = {}
        try:
            if sandbox_id is None:
                if not self._create_dispatched or (searched and _refused(error)):
                    await asyncio.to_thread(usage.release, type(error).__name__ if error else "not created")
                else:
                    await asyncio.to_thread(usage.pending)
                return
            record, seconds = await self._final_record(client, sandbox_id, usage.request["timeout_minutes"] * 60)
            if usage.sandbox_id is None:
                # A box larger than funded still binds its id first, and then
                # settles at its larger size, clamped to the hold.
                with contextlib.suppress(UsagePendingError):
                    await asyncio.to_thread(usage.confirm_created, {"id": sandbox_id, **record})
            await asyncio.to_thread(usage.settle, record, seconds=seconds)
        except Exception as billing_error:
            logger.warning("Prime sandbox usage stays pending: %s", type(billing_error).__name__)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(usage.pending, record)
            if sandbox_id is not None:
                self._settle_error = billing_error

    async def _delete(self, client: Any, sandbox_id: str) -> None:
        """Delete the sandbox, and try again after a failure until ``STOP_TIMEOUT``.

        A 4xx answer ends the tries: the box is gone, or Prime will not
        delete it now. Prime still ends the box at its timeout. A delete
        that Prime accepts keeps the local time of its answer.

        Args:
            client: The SDK client.
            sandbox_id: The sandbox to delete.
        """
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                while True:
                    try:
                        with _mapped("delete the sandbox"):
                            await client.delete(sandbox_id)
                        self._deleted_at = time.monotonic()
                        return
                    except (SandboxRejectedError, SandboxFatalError):
                        raise
                    except Exception as error:
                        logger.warning("Prime could not delete the sandbox, trying again: %s", type(error).__name__)
                    await asyncio.sleep(POLL_INTERVAL)
        except Exception as error:
            logger.warning("Prime could not delete the sandbox: %s", type(error).__name__)

    async def _final_record(self, client: Any, sandbox_id: str, full_seconds: int) -> tuple[dict[str, Any], int | None]:
        """Read the sandbox until Prime shows its end time and an ended status.

        Args:
            client: The SDK client.
            sandbox_id: The deleted sandbox.
            full_seconds: The sandbox's whole funded lifetime.

        Returns:
            The last record read, and the seconds to charge when the record
            cannot give them. That is ``None`` when Prime showed the end time.
            It is the local time since the create was sent when Prime no
            longer knows the box (HTTP 404). When Prime accepted the delete,
            it is the local time from the create sent to the delete answer.
            Otherwise it is the whole funded lifetime: the box exists, and
            Prime ends it at its timeout.
        """
        record: dict[str, Any] = {}
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                while True:
                    try:
                        with _mapped("read the sandbox"):
                            record = _record(await client.get(sandbox_id))
                    except SandboxRejectedError as read_error:
                        if _status(read_error.__cause__) != 404 or self._create_sent_at is None:
                            raise
                        # The box ended before this answer, so the time since
                        # the create was sent covers its whole lifetime.
                        return record, math.ceil(time.monotonic() - self._create_sent_at)
                    except SandboxTransientError as read_error:
                        logger.warning("Prime could not read the sandbox, trying again: %s", type(read_error).__name__)
                    if record.get("terminated_at") is not None and record.get("status") in _ENDED_STATUSES:
                        return record, None
                    await asyncio.sleep(POLL_INTERVAL)
        except Exception as read_error:
            logger.warning("Prime did not show the sandbox end: %s", type(read_error).__name__)
        if self._deleted_at is not None and self._create_sent_at is not None:
            # The accepted delete ended the box, so it ran no longer than this.
            return record, math.ceil(self._deleted_at - self._create_sent_at)
        return record, full_seconds

    async def _search_orphan(self, client: Any) -> tuple[Any | None, bool]:
        """Look for the sandbox of a create that ended before it returned.

        The search lists ended sandboxes too: a box that already ended
        still ran and must be settled.

        Args:
            client: The SDK client.

        Returns:
            The first sandbox under this box label, or ``None``, and True
            when the search finished within ``STOP_TIMEOUT``.
        """

        async def first() -> Any | None:
            """Return the first sandbox under this box label.

            Returns:
                The sandbox, or ``None`` when none was found.
            """
            async for sandbox in _labelled(client, f"{BOX_TAG}={self._box_tag}", ended=True):
                return sandbox
            return None

        search = asyncio.ensure_future(first())
        searched = await _bounded(search, "find the lost sandbox")
        return (search.result() if searched else None), searched

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Delete every active sandbox that carries this job tag.

        Args:
            job_id: The job tag value.

        Returns:
            The number of sandboxes that were deleted. A list that runs past
            ``STOP_TIMEOUT`` is logged, and only the sandboxes found so far
            are deleted.
        """
        client = cls._make_client()
        try:
            ids: list[str] = []
            try:
                async with asyncio.timeout(STOP_TIMEOUT):
                    with _mapped("list the sandboxes"):
                        # A loop, not a comprehension, keeps the ids found
                        # before a list timeout.
                        async for sandbox in _labelled(client, f"{JOB_TAG}={job_id}"):
                            ids.append(sandbox.id)  # noqa: PERF401
            except TimeoutError:
                logger.warning("Prime did not list the sandboxes of job %s in time.", job_id)
            stopped = 0
            for sandbox_id in ids:
                if await _bounded(client.delete(sandbox_id), "delete a job sandbox"):
                    stopped += 1
            return stopped
        finally:
            await _bounded(client.aclose(), "close the client")

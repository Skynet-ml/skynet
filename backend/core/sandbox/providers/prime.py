"""The async Prime Intellect sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), prime: the sandbox create request,
the command call, the file calls and the network policy flow come from
``harbor/environments/prime.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- The provider adds no retry of its own (SKY-406 D3, D10). The
  prime-sandboxes SDK keeps internal retries that it does not let a caller
  turn off. An SDK error maps to one sandbox error group.
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
- Harbor's Compose mode (and its PyYAML use), GPUs, user switching and
  directory transfer are left out. The sandbox runs commands as root.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import shlex
import uuid
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

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
DEFAULT_IMAGE = "python:3.12-slim"
STOP_TIMEOUT = 60.0
POLL_INTERVAL = 1.0
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

PRIME_CAPABILITIES = EnvironmentCapabilities(
    name="prime",
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


async def _labelled(client: Any, label: str) -> AsyncIterator[Any]:
    """Yield the active sandboxes that carry one label, page by page.

    Args:
        client: The SDK client.
        label: The ``key=value`` label.

    Yields:
        Each sandbox that has the label and has not ended.
    """
    cursor: str | None = None
    while True:
        page = await client.list(labels=[label], exclude_terminated=True, per_page=_LIST_PAGE, cursor=cursor)
        for sandbox in page.sandboxes:
            # The label filter is a server query; check it here too, so a
            # wider answer never deletes another sandbox.
            if label in (sandbox.labels or []) and sandbox.status not in _ENDED_STATUSES:
                yield sandbox
        if not page.has_next or not page.next_cursor:
            return
        cursor = page.next_cursor


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
    return fields


class PrimeEnvironment(SandboxEnvironment):
    """Run commands in one Prime Intellect sandbox."""

    capabilities = PRIME_CAPABILITIES

    def __init__(self, spec: SandboxSpec, *, timeouts: PhaseTimeouts | None = None) -> None:
        """Check the spec and prepare the create request.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; ``None`` sets no start or run limit.

        Raises:
            SandboxSpecError: Prime cannot open this spec.
        """
        super().__init__(spec)
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        self._box_tag = uuid.uuid4().hex
        self._create_fields = _create_fields(spec, self._box_tag)
        self._client: Any = None
        self._sandbox_id: str | None = None
        self._starting: asyncio.Future[None] | None = None
        self._create_dispatched = False
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

        Any failure or cancel deletes the sandbox before the error goes on.

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
            await self.stop()
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The Prime sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _create(self) -> None:
        """Create the sandbox, keep its id and wait until it runs.

        Raises:
            SandboxSpecError: The SDK rejected the create request fields.
            StartTimeoutError: The sandbox did not run within the start timeout.
        """
        client = self._client
        limit = self._start_limit
        request = self._create_request()
        try:
            async with asyncio.timeout(limit) as deadline:
                # Set only now: a request that the SDK refuses never reaches
                # Prime, so the stop has no lost sandbox to look for.
                self._create_dispatched = True
                with _mapped("create the sandbox"):
                    sandbox = await client.create(request)
                    # The id is kept here, not in start(), so a stop that
                    # cancels the start right after the create still sees it.
                    self._sandbox_id = sandbox.id
                    await client.wait_for_creation(sandbox.id, max_attempts=_UNBOUNDED_WAIT)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(limit) from None

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
        """Delete the sandbox once and close the client. Safe to call again."""
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)

    async def _teardown(self) -> None:
        """End a running start, delete the sandbox and close the client."""
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the create first, so no sandbox
            # appears after this teardown has looked for it.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        client, sandbox_id = self._client, self._sandbox_id
        self._client = self._sandbox_id = None
        if client is None:
            return
        try:
            if sandbox_id is not None:
                await _bounded(client.delete(sandbox_id), "delete the sandbox")
            elif self._create_dispatched:
                await _bounded(self._delete_orphan(client), "delete the lost sandbox")
        finally:
            await _bounded(client.aclose(), "close the client")

    async def _delete_orphan(self, client: Any) -> None:
        """Delete the sandbox of a create that ended before it returned.

        Args:
            client: The SDK client.
        """
        async for sandbox in _labelled(client, f"{BOX_TAG}={self._box_tag}"):
            await client.delete(sandbox.id)
            return

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

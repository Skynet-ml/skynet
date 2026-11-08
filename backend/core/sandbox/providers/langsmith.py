"""A sandbox provider on LangSmith sandboxes, through the async LangSmith SDK, for dev and tests only.

Adapted from Harbor 0.25.0 (Apache-2.0), langsmith.py. Changes:

- The provider implements the Skynet async contract and uses the SDK's
  ``AsyncSandboxClient`` directly, instead of the sync client in threads.
- No Docker Compose mode, no dockerd, no Dockerfile snapshot builds and no
  directory transfer: a sandbox runs one image and moves UTF-8 text files.
- No tenacity retries, and the SDK's own HTTP retries are off
  (``max_retries=0``): the Skynet retry policy decides on a retry from the
  error group.
- No profile or header auth and no URL helpers: the SDK reads the API key
  and derives the endpoint itself. ``LANGSMITH_SANDBOX_API_URL`` can
  override the endpoint.
- Every SDK error maps to a Skynet error group, and a timeout raises the
  phase timeout error.
- The spec network mode maps to the sandbox proxy access list. A policy
  cannot change after start.

The factory refuses this provider in production (SKY-406 D1).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import re
import uuid
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

from ..capabilities import EnvironmentCapabilities, NetworkMode, network_mode
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import (
    RunTimeoutError,
    SandboxFatalError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from langsmith.sandbox import (
        AsyncSandboxClient,
        CommandTimeoutError,
        DataplaneNotConfiguredError,
        QuotaExceededError,
        ResourceCreationError,
        ResourceNotFoundError,
        ResourceTimeoutError,
        SandboxAPIError,
        SandboxAuthenticationError,
        SandboxConnectionError,
        SandboxNotReadyError,
        SandboxOperationError,
        ValidationError,
    )
    from langsmith.utils import LangSmithError
except ImportError:
    AsyncSandboxClient = None
    CommandTimeoutError = None
    DataplaneNotConfiguredError = None
    QuotaExceededError = None
    ResourceCreationError = None
    ResourceNotFoundError = None
    ResourceTimeoutError = None
    SandboxAPIError = None
    SandboxAuthenticationError = None
    SandboxConnectionError = None
    SandboxNotReadyError = None
    SandboxOperationError = None
    ValidationError = None
    LangSmithError = None

logger = logging.getLogger(__name__)

# The sandbox proxy applies deny-all and allowlist access lists at create
# time; it cannot change them on a running sandbox.
LANGSMITH_CAPABILITIES = EnvironmentCapabilities(name="langsmith", network_modes=frozenset(NetworkMode))

_API_URL_ENV = "LANGSMITH_SANDBOX_API_URL"
_API_KEY_ENVS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")
_DEFAULT_DELETE_AFTER_STOP_SECONDS = 7200
_DEFAULT_IDLE_TTL_SECONDS = 1800
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 300
_DEFAULT_EXEC_TIMEOUT_SECONDS = 3600
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 900
_ONE_MIB = 1024 * 1024
# The server ends a command at its own timeout; the local deadline is a little
# later, so a lost stream cannot hang exec.
_EXEC_TIMEOUT_GRACE_SECONDS = 10
_EXEC_CLEANUP_TIMEOUT_SECONDS = 5
_MIN_COMMAND_TTL_SECONDS = 600
_DELETE_TIMEOUT_SECONDS = 60


class LangSmithEnvironment(SandboxEnvironment):
    """Run a sandbox as one LangSmith sandbox.

    Each sandbox gets a unique name. ``stop`` deletes the sandbox by that
    name, so a sandbox that a failed start left behind is deleted too. The
    idle and delete-after-stop TTLs remove a sandbox that a crashed worker
    never stopped.
    """

    capabilities: ClassVar[EnvironmentCapabilities] = LANGSMITH_CAPABILITIES
    _running: ClassVar[set[LangSmithEnvironment]] = set()

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        snapshot: str | None = None,
        idle_ttl_seconds: int = _DEFAULT_IDLE_TTL_SECONDS,
        delete_after_stop_seconds: int = _DEFAULT_DELETE_AFTER_STOP_SECONDS,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        timeouts: PhaseTimeouts | None = None,
    ) -> None:
        """Create a stopped LangSmith sandbox.

        Args:
            spec: The sandbox to open. The image is the snapshot name when
                ``snapshot`` is not set. The lifetime is not applied; the TTLs
                bound an orphaned sandbox.
            api_key: The LangSmith API key; ``None`` lets the SDK read
                ``LANGSMITH_API_KEY``.
            api_url: The sandbox API endpoint; ``None`` reads
                ``LANGSMITH_SANDBOX_API_URL``, then lets the SDK derive it.
            snapshot: The snapshot to boot; ``None`` uses the spec image, then
                the server default.
            idle_ttl_seconds: Idle time before the server stops the sandbox; a
                multiple of 60.
            delete_after_stop_seconds: Time after a stop before the server
                deletes the sandbox; a multiple of 60.
            request_timeout_seconds: The HTTP timeout of one SDK request.
            startup_timeout_seconds: The time limit of ``start``.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` time limit. ``None`` means one hour.

        Raises:
            SandboxSpecError: The spec asks for a feature that this provider
                cannot apply.
            SandboxFatalError: The ``langsmith`` extra is not installed.
            ValueError: A TTL is negative or not a multiple of 60.
        """
        super().__init__(spec)
        if AsyncSandboxClient is None:
            raise SandboxFatalError("The LangSmith sandbox needs the `langsmith` extra.")
        self._idle_ttl = _validate_ttl_seconds("idle_ttl_seconds", idle_ttl_seconds)
        self._delete_after_stop = _validate_ttl_seconds("delete_after_stop_seconds", delete_after_stop_seconds)
        self._api_key = api_key
        self._api_url = api_url or os.environ.get(_API_URL_ENV) or None
        self._snapshot = snapshot or spec.image
        self._request_timeout = request_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._client: Any = None
        self._box: Any = None
        self._name: str | None = None
        self._stop_task: asyncio.Future[None] | None = None

    @property
    def name(self) -> str | None:
        """Return the LangSmith sandbox name, or ``None`` before the first start."""
        return self._name

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK is installed and an API key is set, without a network call.

        Raises:
            SandboxFatalError: The ``langsmith`` extra is missing, or no API key
                is set.
        """
        if AsyncSandboxClient is None:
            raise SandboxFatalError("The LangSmith sandbox needs the `langsmith` extra.")
        if not any(os.environ.get(name) for name in _API_KEY_ENVS):
            raise SandboxFatalError("The LangSmith sandbox needs LANGSMITH_API_KEY.")

    async def start(self) -> None:
        """Create the sandbox and wait until it is ready. Do nothing when it already runs.

        Raises:
            StartTimeoutError: The sandbox was not ready in time, by the local
                deadline or the server. It is deleted first.
            SandboxFatalError: The API key or the account setup is not valid.
            SandboxTransientError: The quota is full, no capacity is free, or
                the server or the connection failed.
            SandboxRejectedError: The server refused the request, or the image
                did not start.
        """
        if self._box is not None:
            return
        self._name = _k8s_name("skynet", f"{self.spec.name or 'box'}-{uuid.uuid4().hex[:8]}")
        # The client exists before the create call, so stop() can delete by
        # name a sandbox that the server made before the create failed.
        self._client = AsyncSandboxClient(
            api_endpoint=self._api_url,
            api_key=self._api_key,
            timeout=self._request_timeout,
            max_retries=0,
        )
        try:
            async with asyncio.timeout(self._startup_timeout) as deadline:
                with _mapped_errors(start_timeout=self._startup_timeout):
                    box = await self._client.create_sandbox(**self._create_kwargs())
        except TimeoutError:
            await self.stop()
            if not deadline.expired():
                raise
            raise StartTimeoutError(self._startup_timeout) from None
        except BaseException:
            await self.stop()
            raise
        self._box = box
        type(self)._running.add(self)

    async def stop(self) -> None:
        """Delete the sandbox and close the client.

        A cancel during the delete waits for the delete to finish, then
        passes the cancel on, so the sandbox never outlives ``stop``.
        """
        client = self._client
        if client is None:
            if self._stop_task is not None:
                await asyncio.shield(self._stop_task)
            return
        name = self._name
        self._client = None
        self._box = None
        type(self)._running.discard(self)
        self._stop_task = asyncio.ensure_future(_teardown(client, name))
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            await self._stop_task
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
        """Run one shell command in the sandbox and stream its output.

        Args:
            command: The shell command.
            cwd: The working directory; the sandbox default when ``None``.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout,
                then one hour.
            on_output: Receives ``(stream, text)`` chunks while the command
                runs; the active output sink when ``None``.
            sudo: Not supported; the provider never asks for root.

        Returns:
            The exit code and the captured output.

        Raises:
            RuntimeError: The sandbox is not running.
            SandboxRejectedError: ``sudo`` is set, or the server refused the command.
            RunTimeoutError: The command ran past the time limit. It is killed first.
            SandboxFatalError: The API key is not valid, or the sandbox is gone.
            SandboxTransientError: The quota is full, or the server or the
                connection failed.
        """
        box = self._require_running()
        if sudo:
            raise SandboxRejectedError("A LangSmith sandbox cannot run commands as root.")
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        remote = _exec_timeout_seconds(limit)
        merged = {**self.spec.env, **(env or {})}
        sink = on_output or current_output_sink()
        handle: Any = None
        finished = False
        try:
            async with asyncio.timeout(remote + _EXEC_TIMEOUT_GRACE_SECONDS) as deadline:
                with _mapped_errors():
                    handle = await box.run(
                        command,
                        timeout=remote,
                        env=merged or None,
                        cwd=cwd,
                        idle_timeout=-1,
                        kill_on_disconnect=True,
                        ttl_seconds=max(_MIN_COMMAND_TTL_SECONDS, remote + 60),
                        wait=False,
                    )
                    async for chunk in handle:
                        if sink is not None:
                            sink(chunk.stream, chunk.data)
                    result = await handle.result
            finished = True
        except TimeoutError:
            # The output sink can raise its own TimeoutError; only an expired
            # deadline is a run timeout.
            if not deadline.expired():
                raise
            raise RunTimeoutError(limit if limit is not None else remote) from None
        except CommandTimeoutError:
            raise RunTimeoutError(limit if limit is not None else remote) from None
        finally:
            if handle is not None and not finished:
                await asyncio.shield(_kill(handle))
        return ExecResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the sandbox.

        Args:
            files: Maps each sandbox path to its text content.

        Raises:
            RuntimeError: The sandbox is not running.
            SandboxRejectedError: The sandbox refused the write.
            SandboxTransientError: The server or the connection failed.
        """
        box = self._require_running()
        with _mapped_errors():
            for path, text in files.items():
                await box.write(path, text.encode("utf-8"), timeout=int(self._request_timeout))

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the sandbox.

        Args:
            path: The sandbox path.

        Returns:
            The exact file content, or ``None`` when the file does not exist.

        Raises:
            RuntimeError: The sandbox is not running.
            SandboxRejectedError: The file is not UTF-8 text, or the sandbox
                refused the read.
            SandboxTransientError: The server or the connection failed.
        """
        box = self._require_running()
        with _mapped_errors():
            try:
                data = await box.read(path, timeout=int(self._request_timeout))
            except ResourceNotFoundError:
                return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise SandboxRejectedError(f"The sandbox file {path} is not UTF-8 text.") from None

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every running LangSmith sandbox in this process that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandboxes that the sweep stopped.
        """
        stopped = 0
        for sandbox in list(cls._running):
            if sandbox.spec.tags.get(JOB_TAG) != job_id:
                continue
            try:
                await sandbox.stop()
            except Exception:
                logger.exception("Could not stop a LangSmith sandbox of job %s.", job_id)
                continue
            stopped += 1
        return stopped

    def _require_running(self) -> Any:
        """Return the SDK sandbox, or raise when the sandbox is stopped.

        Returns:
            The SDK ``AsyncSandbox``.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._box is None:
            raise RuntimeError("The LangSmith sandbox is not running.")
        return self._box

    def _create_kwargs(self) -> dict[str, Any]:
        """Build the ``create_sandbox`` arguments from the spec.

        Returns:
            The keyword arguments.
        """
        kwargs: dict[str, Any] = {
            "name": self._name,
            "timeout": math.ceil(self._startup_timeout),
            "idle_ttl_seconds": self._idle_ttl,
            "delete_after_stop_seconds": self._delete_after_stop,
        }
        if self._snapshot:
            kwargs["snapshot"] = self._snapshot
        resources = self.spec.resources
        if resources.mode is not ResourceMode.IGNORE:
            kwargs["vcpus"] = resources.cpus
            if resources.memory_mb is not None:
                kwargs["mem_bytes"] = resources.memory_mb * _ONE_MIB
            if resources.disk_mb is not None:
                kwargs["fs_capacity_bytes"] = resources.disk_mb * _ONE_MIB
        proxy = _proxy_config(self.spec)
        if proxy is not None:
            kwargs["proxy_config"] = proxy
        return kwargs


def _proxy_config(spec: SandboxSpec) -> dict[str, Any] | None:
    """Map the spec network mode to a sandbox proxy config.

    Args:
        spec: The sandbox spec.

    Returns:
        The proxy config, or ``None`` for open egress.
    """
    mode = network_mode(disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts))
    if mode is NetworkMode.DENY_ALL:
        return {"rules": [], "no_proxy": [], "access_control": {"deny_list": ["*"]}}
    if mode is NetworkMode.ALLOWLIST:
        return {"rules": [], "no_proxy": [], "access_control": {"allow_list": list(spec.allowed_hosts)}}
    return None


@contextlib.contextmanager
def _mapped_errors(*, start_timeout: float | None = None) -> Iterator[None]:
    """Map every LangSmith SDK error to a Skynet error group.

    ``CommandTimeoutError`` passes through, so that ``exec`` raises the run
    timeout. The except order matters: the SDK errors share base classes.

    Args:
        start_timeout: The ``start`` time limit; ``None`` outside ``start``.

    Raises:
        StartTimeoutError: The server said that the sandbox was not ready in
            time, during ``start``.
        SandboxFatalError: The API key or the account setup is not valid, or
            the sandbox is gone.
        SandboxTransientError: The quota is full, no capacity is free, or the
            server or the connection failed.
        SandboxRejectedError: The server refused the request, the image did
            not start, or the sandbox refused the operation.
    """
    try:
        yield
    except CommandTimeoutError:
        raise
    except SandboxAuthenticationError as exc:
        raise SandboxFatalError(f"LangSmith refused the API key: {type(exc).__name__}.") from exc
    except ResourceTimeoutError as exc:
        if start_timeout is not None:
            raise StartTimeoutError(start_timeout) from exc
        raise SandboxTransientError(f"LangSmith sandbox call timed out: {type(exc).__name__}.") from exc
    except (ResourceNotFoundError, DataplaneNotConfiguredError) as exc:
        raise SandboxFatalError(f"LangSmith sandbox call failed: {type(exc).__name__}.") from exc
    except ResourceCreationError as exc:
        # Unschedulable (HTTP 503) means no capacity now. ImagePull, CrashLoop
        # and SandboxConfig fail again on a retry with the same spec.
        if exc.error_type == "Unschedulable" or _http_status(exc) == 503:
            raise SandboxTransientError(f"LangSmith has no sandbox capacity: {exc.error_type}.") from exc
        raise SandboxRejectedError(f"LangSmith could not create the sandbox: {exc.error_type}.") from exc
    except (QuotaExceededError, SandboxConnectionError, SandboxAPIError, SandboxNotReadyError) as exc:
        raise SandboxTransientError(f"LangSmith sandbox call failed: {type(exc).__name__}.") from exc
    except ValidationError as exc:
        raise SandboxRejectedError(f"LangSmith refused the sandbox request: {exc}") from exc
    except SandboxOperationError as exc:
        raise SandboxRejectedError(f"The LangSmith sandbox refused the operation: {exc.error_type}.") from exc
    except LangSmithError as exc:
        # The SDK raises its base class for an HTTP status it has no class for.
        status = _http_status(exc)
        if status is not None and (status >= 500 or status in (408, 429)):
            raise SandboxTransientError(f"LangSmith sandbox call failed: HTTP {status}.") from exc
        raise SandboxRejectedError(f"LangSmith refused the sandbox call: {type(exc).__name__}.") from exc


def _http_status(exc: BaseException) -> int | None:
    """Return the HTTP status of the response that caused an SDK error.

    Args:
        exc: The SDK error; the SDK chains the ``httpx.HTTPStatusError``.

    Returns:
        The status code, or ``None`` when no HTTP response caused the error.
    """
    response = getattr(exc.__cause__, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


async def _teardown(client: Any, name: str | None) -> None:
    """Delete one sandbox by name and close the client, and log a failure instead of raising it.

    Args:
        client: The SDK ``AsyncSandboxClient``.
        name: The sandbox name; ``None`` when no create call ran.
    """
    try:
        if name is not None:
            await asyncio.wait_for(client.delete_sandbox(name), _DELETE_TIMEOUT_SECONDS)
    except ResourceNotFoundError:
        pass
    except Exception as exc:
        # Only the class name: SDK messages can carry request details.
        logger.warning("Could not delete the LangSmith sandbox %s: %s.", name, type(exc).__name__)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.aclose(), _DELETE_TIMEOUT_SECONDS)


async def _kill(handle: Any) -> None:
    """Kill a running command, bounded in time, and ignore a failure.

    Args:
        handle: The SDK ``AsyncCommandHandle``.
    """
    with contextlib.suppress(Exception):
        await asyncio.wait_for(handle.kill(), _EXEC_CLEANUP_TIMEOUT_SECONDS)


def _k8s_name(prefix: str, value: str, *, max_length: int = 63) -> str:
    """Build a DNS-safe sandbox name with a hash suffix that keeps it unique.

    Args:
        prefix: The fixed name prefix.
        value: The free-form part of the name.
        max_length: The longest name allowed.

    Returns:
        A lowercase name of letters, digits and hyphens.
    """
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "sandbox"
    suffix = f"-{hashlib.sha256(value.encode()).hexdigest()[:8]}"
    available = max_length - len(prefix) - len(suffix) - 1
    trimmed = normalized[:available].strip("-") or "sandbox"
    return f"{prefix}-{trimmed}{suffix}"


def _exec_timeout_seconds(timeout_sec: float | None) -> int:
    """Return the server command timeout in whole seconds.

    Args:
        timeout_sec: The time limit; ``None`` means one hour.

    Returns:
        The timeout, rounded up and at least one second.
    """
    if timeout_sec is None:
        return _DEFAULT_EXEC_TIMEOUT_SECONDS
    return max(1, math.ceil(timeout_sec))


def _validate_ttl_seconds(field: str, value: int) -> int:
    """Check that a TTL is a non-negative multiple of 60, as the server requires.

    Args:
        field: The argument name for the error message.
        value: The TTL in seconds.

    Returns:
        The TTL.

    Raises:
        ValueError: The TTL is negative or not a multiple of 60.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value % 60:
        raise ValueError(f"{field} must be a non-negative multiple of 60 seconds.")
    return value

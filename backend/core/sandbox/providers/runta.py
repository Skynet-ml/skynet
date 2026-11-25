"""The async Runta sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), runta: the direct runtime create,
the disk size floor, the egress policy map, the bash command form, the
output limit and the delete on stop come from
``harbor/environments/runta/environment.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- A box with a budget is protected (SKY-938). It reserves its whole
  lifetime before the create, binds the created runtime to the hold, and
  settles the measured wall time after the delete, also after a failed or
  cancelled start. One teardown task owns the settle.
- Runta publishes no maximum lifetime. Every runtime gets Runta's
  running-session limit (``auto_shutdown_after_secs``) at the spec lifetime
  and a short retention (``auto_delete_after_secs``), and the provider also
  deletes the box itself when the lifetime ends.
- A closed or allowlist network policy also sends an empty
  ``tcp_allowlist``, because Runta denies raw TCP only for an empty list.
  Harbor sends no TCP list.
- The requests and the limits of vCPU and memory are equal, so the priced
  size is the billed size. Harbor sends no limits.
- A spec image is refused: the direct path runs Runta's default runtime
  image, and Harbor's image builds are left out.
- Commands go as a ``bash -c`` argv with the ``cwd`` and ``timeout``
  arguments of the SDK. Runta runs commands as root, so ``sudo`` changes
  nothing.
- The create is sent with ``wait=False`` under a shield, as in Harbor. A
  create that a timeout or a stop cancels gets a short grace to return its
  handle. A create that is still running after the grace goes to a
  background task that deletes and settles its box when the handle arrives,
  or looks up its name when the late create fails.
- After a failed create the provider looks up the runtime by its unique
  name. A found runtime is deleted and settled. The hold is released only
  when Runta clearly refused the create and the lookup finds no runtime.
- ``stop_job_sandboxes`` stops the runtimes that this process started for
  the job. Runtimes of other processes end at their own session limit.
- Harbor's Dockerfile and compose builds, checkpoints, the DinD strategy,
  directory transfer and attach are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import shlex
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from core.billing.budgets import BudgetError
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runta_usage import BOOT_LIMIT_SEC, MIN_DISK_GIB, RETENTION_SEC, USAGE, quote_runta_runtime
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, network_mode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxError, SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from runta import ApiError, AsyncRunta, ConfigError, EgressMode, EgressPolicy
except ImportError:
    ApiError = None
    AsyncRunta = None
    ConfigError = None
    EgressMode = None
    EgressPolicy = None

logger = logging.getLogger(__name__)

STOP_TIMEOUT = 60.0
# Harbor waits this long for a cancelled create, so a runtime made during
# the request still has a handle to delete.
CREATE_GRACE = 30.0
# The SDK timeout should end a command first; this margin only catches a
# hung SDK call.
EXEC_GRACE = 30.0
# Harbor's output limit for one command.
MAX_OUTPUT_BYTES = 1024 * 1024
# Harbor's memory default for a runtime.
DEFAULT_MEMORY_MIB = 2048
# The SDK runs the command under GNU timeout, which exits 124.
_TIMEOUT_EXIT_CODE = 124
# Answers of the control plane that make no runtime. 409 and 429 are left
# out: they can come after an earlier attempt made the runtime. A refused
# create still releases its hold only when the name lookup finds no runtime.
_REFUSED_CREATE_STATUSES = frozenset({400, 401, 402, 403, 404, 413, 422})
_DEFAULT_CONFIG = Path("~/.config/runta/config.toml")

RUNTA_CAPABILITIES = EnvironmentCapabilities(
    name="runta",
    billable=True,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)

# Process-local: the sweep sees only the boxes that this process started.
_JOB_BOXES: dict[str, set[RuntaEnvironment]] = {}
# Strong references to the tasks that outlive their box, so the event loop
# does not drop them.
_LATE_TASKS: set[asyncio.Future[Any]] = set()
# Tests swap this for a fake clock.
_cap_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@contextlib.contextmanager
def _mapped(action: str) -> Iterator[None]:
    """Turn an SDK error into a sandbox error.

    Args:
        action: What the provider was doing, for the error message.

    Yields:
        Nothing.

    Raises:
        SandboxInfraError: The SDK call raised an error that is not a sandbox error.
    """
    try:
        yield
    except SandboxError:
        raise
    except Exception as error:
        raise SandboxInfraError(f"Runta could not {action}: {type(error).__name__}.") from error


def _refused_create(error: BaseException | None) -> bool:
    """Tell whether a create failure shows that Runta made no runtime.

    This check alone never frees a hold; the name lookup must also find no
    runtime.

    Args:
        error: The SDK error of the create, or ``None``.

    Returns:
        True when no runtime can exist for the create.
    """
    if error is None:
        return False
    # The SDK checks the resources argument before any request.
    if isinstance(error, ValueError):
        return True
    return ApiError is not None and isinstance(error, ApiError) and error.status_code in _REFUSED_CREATE_STATUSES


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
        logger.warning("Runta could not %s: %s", action, type(error).__name__)
        return False
    return True


def _keep_late(task: asyncio.Future[Any]) -> None:
    """Keep a background task alive until it ends, and log its failure.

    Args:
        task: The background task.
    """
    _LATE_TASKS.add(task)
    task.add_done_callback(_on_late_done)


def _on_late_done(task: asyncio.Future[Any]) -> None:
    """Forget a finished background task and log its failure.

    Args:
        task: The finished task.
    """
    _LATE_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("A late Runta task failed: %s", type(task.exception()).__name__)


async def _release_late(reserving: asyncio.Future[Any]) -> None:
    """Release the hold of a reserve that outlives its teardown.

    The create starts only after the reserve returns, so no runtime exists
    for this hold.

    Args:
        reserving: The reserve call.
    """
    usage = await reserving
    await asyncio.to_thread(usage.fail)


async def _list_named(client: Any, name: str) -> list[Any]:
    """List the runtimes with this exact name, and delete every match but the first.

    Args:
        client: The Runta client.
        name: The unique name of the runtime.

    Returns:
        A list with the first match, or an empty list.

    Raises:
        Exception: The list call failed or timed out.
    """
    listed = await asyncio.wait_for(client.runtimes.list(), STOP_TIMEOUT)
    # Only a runtime with this exact name can belong to this create; never
    # touch another runtime that the list returns.
    found = [runtime for runtime in listed if runtime.name == name]
    for extra in found[1:]:
        await _bounded(extra.delete(), "delete a duplicate runtime")
    return found[:1]


async def _delete_late(create: asyncio.Future[Any], usage: Any, client: Any, name: str) -> None:
    """Delete and settle the runtime of a create that outlived its grace.

    This task owns the hold. A failed create is looked up by name, as in the
    grace. The delete always runs once a runtime is known. A failed lookup,
    a failed delete or a failed confirm keeps the hold pending with the
    runtime name as evidence.

    Args:
        create: The create task.
        usage: The usage reservation of the box, or ``None``.
        client: The Runta client, closed at the end.
        name: The unique name of the runtime.

    Raises:
        UsagePendingError: The lookup, the confirm or the delete failed.
    """
    try:
        try:
            runtime = await create
        except Exception as create_error:
            try:
                found = await _list_named(client, name)
            except Exception as error:
                raise UsagePendingError(
                    "Runta could not list runtimes after a late failed create, so the hold stays."
                ) from error
            if not found:
                if usage is not None:
                    await asyncio.to_thread(usage.fail, _refused_create(create_error))
                return
            runtime = found[0]
        confirmed = True
        if usage is not None and usage.runtime_id is None:
            try:
                await asyncio.to_thread(usage.confirm_created, runtime)
            except Exception as error:
                # The delete must still run, or the box runs to Runta's own shutdown.
                confirmed = False
                logger.warning("Runta could not confirm the late runtime %s: %s", name, type(error).__name__)
        if not await _bounded(runtime.delete(), "delete a late runtime"):
            if usage is not None:
                await asyncio.to_thread(usage.pending)
            raise UsagePendingError("A late Runta runtime did not delete, so its hold stays pending.")
        if usage is None:
            return
        if not confirmed:
            await asyncio.to_thread(usage.pending)
            raise UsagePendingError("A late Runta runtime has no confirmed id, so its hold stays pending.")
        usage.stopped()
        await asyncio.to_thread(usage.settle)
    finally:
        await _bounded(client.close(), "close the client")


def _egress_policy(*, disabled: bool, allowed_hosts: tuple[str, ...]) -> Any:
    """Map a network policy to a Runta egress policy.

    Args:
        disabled: True when the box has no egress.
        allowed_hosts: The hosts that egress may reach; empty means any host.

    Returns:
        The Runta egress policy.
    """
    mode = network_mode(disabled=disabled, allowed_hosts=allowed_hosts)
    if mode is NetworkMode.DENY_ALL:
        return EgressPolicy(mode=EgressMode.ALLOWLIST, allowed_hosts=[])
    if mode is NetworkMode.ALLOWLIST:
        return EgressPolicy(mode=EgressMode.ALLOWLIST, allowed_hosts=list(dict.fromkeys(allowed_hosts)))
    return EgressPolicy(mode=EgressMode.DENYLIST, denied_hosts=[])


def _tcp_create(*, disabled: bool, allowed_hosts: tuple[str, ...]) -> dict[str, list[str]]:
    """Return the raw TCP argument of the create for a network policy.

    The egress policy covers only HTTP hosts; Runta denies raw TCP on a new
    runtime only when the create sends an empty ``tcp_allowlist``.

    Args:
        disabled: True when the box has no egress.
        allowed_hosts: The hosts that egress may reach; empty means any host.

    Returns:
        ``{"tcp_allowlist": []}`` for a closed or allowlist policy, else no argument.
    """
    if network_mode(disabled=disabled, allowed_hosts=allowed_hosts) is NetworkMode.OPEN:
        return {}
    return {"tcp_allowlist": []}


def _runtime_request(spec: SandboxSpec) -> dict[str, int]:
    """Map the spec to the lifetime and the size that the create sends and the hold prices.

    Args:
        spec: The sandbox to open.

    Returns:
        ``lifetime_ms``, ``vcpus``, ``memory_mib`` and ``disk_gib``.
    """
    request = spec.resources
    sized = request.mode is not ResourceMode.IGNORE
    disk_mb = request.disk_mb if sized else None
    return {
        "lifetime_ms": math.ceil(spec.lifetime_seconds) * 1000,
        "vcpus": max(1, math.ceil(request.cpus)) if sized else 1,
        "memory_mib": (request.memory_mb if sized else None) or DEFAULT_MEMORY_MIB,
        "disk_gib": max(MIN_DISK_GIB, math.ceil(disk_mb / 1024)) if disk_mb else MIN_DISK_GIB,
    }


class RuntaEnvironment(SandboxEnvironment):
    """Run commands in one Runta runtime."""

    capabilities = RUNTA_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = USAGE.reserve,
    ) -> None:
        """Check the spec and prepare the create call.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; ``None`` sets no start or run limit.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: The spec breaks a provider rule, Skynet cannot
                price it, or a protected box has no operation key.
        """
        if spec.image is not None:
            raise SandboxSpecError("The Runta sandbox runs Runta's default image; it cannot run a spec image.")
        if budget is not None and spec.operation_key is None:
            raise SandboxSpecError("A protected Runta sandbox needs an operation key.")
        super().__init__(spec)
        self._request = _runtime_request(spec)
        if budget is not None:
            try:
                quote_runta_runtime(self._request)
            except UnpricedOperationError as error:
                raise SandboxSpecError(f"Skynet cannot fund this Runta runtime: {error}") from error
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._reserving: asyncio.Future[Any] | None = None
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        # The lookup after a failed create needs a name that no other runtime has.
        prefix = spec.name or "skynet"
        self._name = f"{prefix}-{uuid.uuid4().hex[:12]}"
        self._job_id = spec.tags.get(JOB_TAG)
        self._client: Any = None
        self._runtime: Any = None
        self._sent = False
        self._create_task: asyncio.Future[Any] | None = None
        self._create_error: BaseException | None = None
        self._cap_task: asyncio.Future[None] | None = None
        self._capped = False
        self._confirming: asyncio.Future[None] | None = None
        self._starting: asyncio.Future[None] | None = None
        self._started = False
        self._deleted = False
        self._stop_task: asyncio.Future[list[Exception]] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and a Runta token or config file exist.

        Raises:
            SandboxFatalError: The SDK is not installed, or no credentials exist.
        """
        if AsyncRunta is None:
            raise SandboxFatalError("The runta package is not installed: install the `runta` extra.")
        config = Path(os.environ.get("RUNTA_CONFIG") or _DEFAULT_CONFIG).expanduser()
        if not os.environ.get("RUNTA_TOKEN") and not config.is_file():
            raise SandboxFatalError("The Runta sandbox found no RUNTA_TOKEN and no Runta config file.")

    async def start(self) -> None:
        """Reserve the box, create the runtime and wait until it runs.

        Any failure or cancel deletes the runtime and settles the hold
        before the error goes on.

        Raises:
            StartTimeoutError: The runtime did not run within the start timeout.
            SandboxInfraError: A stop ended the start, the sandbox already
                started once, or an SDK call failed.
            SandboxFatalError: The SDK or the credentials are missing.
        """
        if self._started:
            raise SandboxInfraError("The Runta sandbox already started once; it cannot start again.")
        self._started = True
        # A stop before the start leaves a finished stop task; drop it so the
        # stop after this start deletes the runtime.
        self._stop_task = None
        self.preflight()
        self._starting = asyncio.ensure_future(self._create())
        try:
            await self._starting
        except BaseException as error:
            # Wait for the teardown but not through stop(): a settle failure
            # would hide the start error. It stays for a later stop() call.
            if self._stop_task is None:
                self._stop_task = asyncio.ensure_future(self._teardown())
            await asyncio.wait({self._stop_task})
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The Runta sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _create(self) -> None:
        """Reserve the hold, send the create and wait until the runtime runs.

        Raises:
            StartTimeoutError: The runtime did not run within the start timeout.
            SandboxInfraError: An SDK call failed.
            SandboxFatalError: Runta found no credentials.
        """
        spec = self.spec
        limit = self._start_limit
        await self._reserve()
        try:
            self._client = AsyncRunta()
        except Exception as error:
            if ConfigError is not None and isinstance(error, ConfigError):
                raise SandboxFatalError("The Runta sandbox found no credentials.") from error
            raise SandboxInfraError(f"Runta could not open a client: {type(error).__name__}.") from error
        request = self._request
        deadline = asyncio.timeout(limit)
        try:
            async with deadline:
                with _mapped("prepare the runtime"):
                    params: dict[str, Any] = {
                        "name": self._name,
                        "resources": {
                            "requests": {
                                "vcpus": request["vcpus"],
                                "memory_mib": request["memory_mib"],
                                "disk_gib": request["disk_gib"],
                            },
                            "limits": {"vcpus": request["vcpus"], "memory_mib": request["memory_mib"]},
                        },
                        "egress_policy": _egress_policy(
                            disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts)
                        ),
                        "auto_shutdown_after_secs": request["lifetime_ms"] // 1000,
                        **_tcp_create(disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts)),
                        "auto_delete_after_secs": RETENTION_SEC,
                        "wait": False,
                    }
                self._sent = True
                if self._usage is not None:
                    self._usage.sent(self._name)
                self._cap_task = asyncio.ensure_future(self._cap(request["lifetime_ms"] / 1000))
                create = self._create_task = asyncio.ensure_future(self._client.runtimes.create(**params))
                try:
                    with _mapped("create the runtime"):
                        # The shield keeps the create running through a cancel,
                        # so the teardown grace can still get the handle.
                        runtime = await asyncio.shield(create)
                except SandboxInfraError as error:
                    self._create_error = error.__cause__
                    raise
                self._keep(runtime)
                if self._usage is not None:
                    # A cancel cannot stop the ledger thread; the teardown
                    # waits for this future before it settles.
                    self._confirming = asyncio.ensure_future(asyncio.to_thread(self._usage.confirm_created, runtime))
                    await asyncio.shield(self._confirming)
                ready_limit = BOOT_LIMIT_SEC if limit is None else min(limit, BOOT_LIMIT_SEC)
                with _mapped("wait for the runtime"):
                    try:
                        await runtime.wait_until_ready(timeout=ready_limit)
                    except TimeoutError:
                        raise StartTimeoutError(ready_limit) from None
                    except RuntimeError as error:
                        raise SandboxInfraError(f"The Runta runtime failed to start: {error}") from error
        except TimeoutError:
            if limit is None or not deadline.expired():
                raise
            raise StartTimeoutError(limit) from None

    async def _reserve(self) -> None:
        """Reserve the whole lifetime of a protected box before the create.

        Raises:
            BudgetReached: The budget cannot pay for the box.
            UsagePendingError: The box was already dispatched elsewhere.
        """
        if self._budget is None:
            return
        reserving = self._reserving = asyncio.ensure_future(
            asyncio.to_thread(self._usage_factory, self._budget, self._request, operation_key=self.spec.operation_key)
        )
        # The thread cannot be cancelled; the teardown waits for it and
        # releases what it returns.
        self._usage = await asyncio.shield(reserving)

    async def _cap(self, seconds: float) -> None:
        """Delete the runtime when its funded lifetime ends.

        Args:
            seconds: The lifetime from the create call.
        """
        await _cap_sleep(seconds)
        # The teardown must not cancel this task, or the warning below is lost.
        self._capped = True
        logger.warning("Runta sandbox %s reached its lifetime; it is deleted.", self._name)
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        errors = await asyncio.shield(self._stop_task)
        for error in errors:
            logger.warning("Runta sandbox %s did not settle at its lifetime: %s", self._name, type(error).__name__)

    def _keep(self, runtime: Any) -> None:
        """Keep the runtime handle and list the box under its job.

        Args:
            runtime: The SDK runtime handle.
        """
        self._runtime = runtime
        if self._job_id is not None:
            _JOB_BOXES.setdefault(self._job_id, set()).add(self)

    def _forget(self) -> None:
        """Remove the box from its job after the runtime is deleted."""
        boxes = _JOB_BOXES.get(self._job_id) if self._job_id is not None else None
        if boxes is not None:
            boxes.discard(self)
            if not boxes:
                del _JOB_BOXES[self._job_id]

    def _require_runtime(self) -> Any:
        """Return the runtime handle.

        Returns:
            The SDK runtime handle.

        Raises:
            SandboxInfraError: The runtime is not running.
        """
        if self._runtime is None or self._stop_task is not None:
            raise SandboxInfraError("The Runta sandbox is not running.")
        return self._runtime

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
        """Run one shell command in the runtime and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory; the runtime default by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` pairs. Runta buffers the
                output, so each stream arrives once, after the command ends.
            sudo: Run the command as root. Runta always runs as root.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, the result has exit code 124 and ``timed_out`` set.

        Raises:
            SandboxInfraError: The runtime is not running, or the SDK call failed.
        """
        runtime = self._require_runtime()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        try:
            async with asyncio.timeout(limit + EXEC_GRACE if limit is not None else None):
                with _mapped("run the command"):
                    result = await runtime.exec(
                        ["bash", "-c", command],
                        timeout=max(1, math.ceil(limit)) if limit is not None else None,
                        env={**self.spec.env, **(env or {})},
                        cwd=cwd,
                        max_output_bytes=MAX_OUTPUT_BYTES,
                    )
        except TimeoutError:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        stdout, stderr = result.stdout_text, result.stderr_text
        if limit is not None and result.exit_code == _TIMEOUT_EXIT_CODE:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, stdout=stdout, stderr=stderr, timed_out=True)
        if result.stdout_truncated or result.stderr_truncated:
            logger.warning("Runta kept only %d MiB of a command output stream.", MAX_OUTPUT_BYTES >> 20)
        sink = on_output or current_output_sink()
        if sink is not None:
            for stream, text in (("stdout", stdout), ("stderr", stderr)):
                if text:
                    sink(stream, text)
        return ExecResult(exit_code=result.exit_code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the runtime, making parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The runtime is not running, the parent
                directories could not be made, or a write failed.
        """
        runtime = self._require_runtime()
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {".", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents))
            if not made.ok:
                raise SandboxInfraError("Runta could not make the upload directories.")
        for path, text in files.items():
            with _mapped("write a file"):
                await runtime.files.write(path, text.encode("utf-8"))

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the runtime.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The runtime is not running, the read failed
                for a file that exists, or the file is not UTF-8 text.
        """
        runtime = self._require_runtime()
        try:
            with _mapped("read a file"):
                data = await runtime.files.read(path)
        except SandboxInfraError:
            # The SDK error for a missing file is not documented, so ask the box.
            probe = await self.exec(f"test -e {shlex.quote(path)}")
            if probe.exit_code == 1:
                return None
            raise
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            # The message names no bytes, so no file content reaches the logs.
            raise SandboxInfraError("Runta read a file that is not UTF-8 text.") from None

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a new egress policy to the running runtime.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The runtime is not running, or Runta refused the policy.
        """
        runtime = self._require_runtime()
        with _mapped("change the egress policy"):
            await runtime.egress.set_policy(
                _egress_policy(disabled=policy.disabled, allowed_hosts=policy.allowed_hosts)
            )
            # An open policy cannot list every TCP host, so it keeps the raw TCP list as it is.
            if _tcp_create(disabled=policy.disabled, allowed_hosts=policy.allowed_hosts):
                await runtime.egress.set_tcp_allowlist([])

    async def stop(self) -> None:
        """Delete the runtime once and settle its hold. Safe to call again and before start.

        Raises:
            UsagePendingError: The runtime did not delete, or the ledger call
                failed, so the hold stays pending.
            BudgetError: The ledger refused the settlement.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        task = self._stop_task
        try:
            errors = await asyncio.shield(task)
        except asyncio.CancelledError:
            # The teardown owns the hold and goes on; the caller's cancel goes on too.
            _keep_late(task)
            raise
        # The callers of one teardown share its list, so only the first one
        # raises its failure.
        if errors:
            raise errors.pop()

    async def _teardown(self) -> list[Exception]:
        """End a running start, delete the runtime, then settle or release its hold.

        This task is the only owner of the settle, unless it hands a late
        create to ``_delete_late``.

        Returns:
            The failure that keeps the hold pending, or an empty list.
        """
        starting = self._starting
        if starting is not None and not starting.done():
            starting.cancel()
            await asyncio.wait({starting})
        reserving, self._reserving = self._reserving, None
        if reserving is not None and self._usage is None:
            await asyncio.wait({reserving}, timeout=CREATE_GRACE + STOP_TIMEOUT)
            if not reserving.done():
                _keep_late(asyncio.ensure_future(_release_late(reserving)))
            elif not reserving.cancelled() and reserving.exception() is None:
                self._usage = reserving.result()
        cap = self._cap_task
        if cap is not None and not cap.done() and not self._capped:
            cap.cancel()
        confirming = self._confirming
        if confirming is not None:
            await asyncio.wait({confirming})
            if not confirming.cancelled() and confirming.exception() is not None:
                # _end_usage tries the confirm again when no runtime id came back.
                logger.warning("Runta sandbox %s did not confirm its runtime.", self._name)
        create = self._create_task
        if self._runtime is None and create is not None:
            await asyncio.wait({create}, timeout=CREATE_GRACE)
            if not create.done():
                return await self._hand_off_late(create)
            if not create.cancelled() and create.exception() is None:
                self._keep(create.result())
            elif self._create_error is None and not create.cancelled():
                self._create_error = create.exception()
        try:
            await self._end_usage()
        except Exception as error:
            if isinstance(error, UsagePendingError | BudgetError):
                return [error]
            pending = UsagePendingError("Sandbox usage is not yet settled; the reservation remains active.")
            pending.__cause__ = error
            return [pending]
        return []

    async def _hand_off_late(self, create: asyncio.Future[Any]) -> list[Exception]:
        """Give a create that outlived its grace, and its hold, to a background task.

        Args:
            create: The create task that still runs.

        Returns:
            An empty list: the background task owns the hold now.
        """
        usage, self._usage = self._usage, None
        client, self._client = self._client, None
        if usage is not None:
            logger.warning(
                "Runta deletes the late runtime %s of the pending hold %s.", self._name, self.spec.operation_key
            )
            await asyncio.to_thread(usage.pending)
        _keep_late(asyncio.ensure_future(_delete_late(create, usage, client, self._name)))
        return []

    async def _end_usage(self) -> None:
        """Delete the runtime, or look it up after a failed create, then settle or release the hold.

        Raises:
            UsagePendingError: The runtime did not delete, or the lookup failed.
            BudgetError: The ledger refused the settlement.
        """
        usage = self._usage
        runtime = self._runtime
        if runtime is None and self._sent:
            runtime = await self._find_failed_create()
            if runtime is None:
                self._usage = None
                if usage is not None:
                    await asyncio.to_thread(usage.fail, _refused_create(self._create_error))
                await self._close()
                return
        if runtime is None:
            # The create was never sent, so no runtime can exist.
            self._usage = None
            if usage is not None:
                await asyncio.to_thread(usage.fail)
            await self._close()
            return
        if usage is not None and usage.runtime_id is None:
            try:
                await asyncio.to_thread(usage.confirm_created, runtime)
            except UsagePendingError as error:
                # The hold keeps the name as evidence; the delete still goes on.
                logger.warning("Runta sandbox %s has no runtime id: %s", self._name, error)
        if not await _bounded(runtime.delete(), "delete the runtime"):
            # The runtime can still run. Keep the handle and the hold, so a
            # later stop() or the job sweep tries the delete again.
            self._keep(runtime)
            try:
                if usage is not None:
                    await asyncio.to_thread(usage.pending)
            finally:
                # Reset only now: a stop() during the pending call must share
                # this teardown, not start a second owner of the hold.
                self._stop_task = None
            raise UsagePendingError("The Runta runtime did not delete, so its hold stays pending.")
        self._runtime = None
        self._deleted = True
        self._forget()
        await self._close()
        self._usage = None
        if usage is not None:
            usage.stopped()
            await asyncio.to_thread(usage.settle)

    async def _find_failed_create(self) -> Any:
        """Look up the runtime of a failed create by its name, and delete every match but the first.

        Returns:
            The SDK handle of the runtime, or ``None`` when Runta lists no runtime with the name.

        Raises:
            UsagePendingError: The lookup failed, so the hold stays pending.
        """
        try:
            found = await _list_named(self._client, self._name)
        except Exception as error:
            try:
                if self._usage is not None:
                    await asyncio.to_thread(self._usage.pending)
            finally:
                # Reset only now: a stop() during the pending call must share
                # this teardown, not start a second owner of the hold.
                self._stop_task = None
            raise UsagePendingError(
                "Runta could not list runtimes after a failed create, so the hold stays."
            ) from error
        return found[0] if found else None

    async def _close(self) -> None:
        """Close the Runta client once no runtime of this box can be left."""
        client, self._client = self._client, None
        if client is not None:
            await _bounded(client.close(), "close the client")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every runtime that this process started for the job, all at the same time.

        Args:
            job_id: The job tag value.

        Returns:
            The number of runtimes that were deleted. A box that fails to
            delete or settle is logged and the sweep goes on.

        Raises:
            Exception: The first stop error that is not a usage or budget error.
        """
        envs = list(_JOB_BOXES.get(job_id, ()))
        results = await asyncio.gather(*(env.stop() for env in envs), return_exceptions=True)
        unexpected: BaseException | None = None
        for env, result in zip(envs, results, strict=True):
            if isinstance(result, UsagePendingError | BudgetError):
                logger.warning("Runta sandbox %s usage stays pending: %s", env._name, type(result).__name__)
            elif isinstance(result, BaseException) and unexpected is None:
                unexpected = result
        if unexpected is not None:
            raise unexpected
        return sum(1 for env in envs if env._deleted)

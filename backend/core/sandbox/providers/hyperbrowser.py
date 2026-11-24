"""The async Hyperbrowser sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), hyperbrowser: the sandbox create
parameters, the 2 GiB disk floor, the network policy map, the bash command
form, the exec timeout check, the ``/dev/fd`` and ``/dev/shm`` runtime fix,
the root file calls and the stop come from
``harbor/environments/hyperbrowser/environment.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- A box with a budget is protected. It reserves its whole lifetime before
  the create and settles its measured runtime after the stop, also after a
  failed or cancelled start. The create has no name or tag, so a lost box
  cannot be found again: a failed create releases its hold only when the
  create was never sent, or when Hyperbrowser answered it with a refusal
  status. Any other failed create keeps its hold pending.
- Every create sends ``timeout_minutes`` from the spec lifetime, so
  Hyperbrowser ends a box that a lost process never stops. The provider
  refuses a box that comes back with a longer timeout, and it also stops
  the box itself when the spec lifetime ends.
- The provider adds no retry of its own (SKY-406 D3, D10). The SDK never
  retries the create.
- A create that a cancel interrupts gets a short grace to return its
  handle. Unlike Harbor, a create that is still open when the grace ends is
  not dropped: a callback stops its box when the handle arrives.
- Each command's output goes to the output sink when the command ends,
  because the SDK exec call returns the whole output at once.
- ``stop_job_sandboxes`` stops the boxes that this process started for the
  job. The SDK cannot list boxes by a tag, so boxes of other processes end
  only at their timeout.
- Harbor's Dockerfile and compose builds, the managed image builds, the DinD
  strategy, the process polling API, directory transfer, mounts, region
  pinning and attach are left out. ``spec.image`` names an image that
  Hyperbrowser already has.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import os
import posixpath
import shlex
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from core.billing.budgets import BudgetError
from core.billing.hyperbrowser_usage import HyperbrowserUsageReservation, quote_hyperbrowser_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError
from ..resources import SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from hyperbrowser import AsyncHyperbrowser, HyperbrowserError
    from hyperbrowser.models import CreateSandboxParams
except ImportError:
    AsyncHyperbrowser = None
    HyperbrowserError = None
    CreateSandboxParams = None

logger = logging.getLogger(__name__)

# Hyperbrowser filters egress by host and can replace the policy of a running
# sandbox. Its pricing pages publish no egress charge, so a protected box may
# open egress. A box with a budget meters its runtime against the run budget.
HYPERBROWSER_CAPABILITIES = EnvironmentCapabilities(
    name="hyperbrowser",
    billable=True,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)

# Harbor's DinD mode starts boxes from this Hyperbrowser image.
DEFAULT_IMAGE = "default"
# Hyperbrowser publishes no maximum timeout; the usage quote refuses more.
_MAX_LIFETIME_SEC = 24 * 60 * 60
# A box must name its memory so its bill has a ceiling; with no memory in the
# spec it gets 2 GiB for each core.
_MEMORY_MIB_PER_CPU = 2048
# Harbor never asks for a smaller disk.
_MIN_DISK_MIB = 2048
# Harbor raises the SDK's 30 s request timeout, because parallel creates and
# stops can take longer.
_SDK_REQUEST_TIMEOUT_SEC = 120
# Hyperbrowser's own exec timeout ends the command first; this bound only
# stops a lost connection from blocking exec past the limit.
_TIMEOUT_GRACE_SEC = 30.0
# How long a cancelled start or stop waits before the cancel passes on.
_CANCEL_GRACE_SEC = 0.5
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124
_FIX_RUNTIME_TIMEOUT_SEC = 60
# The SDK sends the create as one POST and never retries it (hyperbrowser
# 1.15.6, transport/async_transport.py retries only GET), so one of these
# answers to that POST means Hyperbrowser made no box. A 5xx, a timeout or a
# lost connection may come after the box exists, so they never count.
_REFUSED_CREATE_STATUSES = frozenset({400, 401, 402, 403, 404, 409, 413, 422, 429})
# Hyperbrowser recreates /dev for each box: bash process substitution needs
# /dev/fd, and POSIX semaphores (Python multiprocessing locks) need /dev/shm.
_FIX_RUNTIME_COMMAND = "([ -e /dev/fd ] || ln -sf /proc/self/fd /dev/fd) && mkdir -p /dev/shm && chmod 1777 /dev/shm"

# Process-local: the sweep sees only the boxes that this process started.
_JOB_BOXES: dict[str, set[HyperbrowserEnvironment]] = {}
# The loop holds tasks only by weak reference, so background stops live here.
_LATE_TASKS: set[asyncio.Future[Any]] = set()
# The lifetime cap sleeps through this name, so tests can use a fake clock.
_sleep = asyncio.sleep


class HyperbrowserEnvironment(SandboxEnvironment):
    """Run a sandbox as one Hyperbrowser sandbox."""

    capabilities: ClassVar[EnvironmentCapabilities] = HYPERBROWSER_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = HyperbrowserUsageReservation,
    ) -> None:
        """Create a stopped Hyperbrowser sandbox.

        With a budget the box is protected: it reserves its whole lifetime
        before the create and settles its measured runtime after it stops.

        Args:
            spec: The sandbox to open. ``spec.image`` is a Hyperbrowser image
                name; ``None`` uses ``DEFAULT_IMAGE``.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` time limit. ``None`` means no default limit.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: The spec asks for a lifetime longer than 24
                hours, or a protected box has no operation key or no priced size.
        """
        super().__init__(spec)
        if spec.lifetime_seconds > _MAX_LIFETIME_SEC:
            raise SandboxSpecError(f"The hyperbrowser sandbox lives at most {_MAX_LIFETIME_SEC} seconds.")
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._client: Any = None
        self._sandbox: Any = None
        self._cap: asyncio.Task[None] | None = None
        self._stopped = False
        self._job_id = (spec.tags or {}).get(JOB_TAG)
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Hyperbrowser sandbox needs an operation key.")
            try:
                quote_hyperbrowser_sandbox(self._usage_request())
            except UnpricedOperationError as error:
                raise SandboxSpecError(str(error)) from error

    @property
    def usage(self) -> Any | None:
        """Return the usage reservation of a protected box, once it exists."""
        return self._usage

    @classmethod
    def preflight(cls) -> None:
        """Check that the Hyperbrowser SDK and API key are present.

        Raises:
            SandboxFatalError: The ``hyperbrowser`` package or ``HYPERBROWSER_API_KEY`` is missing.
        """
        _require_sdk()
        if not os.environ.get("HYPERBROWSER_API_KEY"):
            raise SandboxFatalError("The hyperbrowser sandbox needs HYPERBROWSER_API_KEY.")

    async def start(self) -> None:
        """Create the Hyperbrowser sandbox. Do nothing when the sandbox already runs.

        A cancel during the create waits for the create for at most
        ``_CANCEL_GRACE_SEC``, also through repeat cancels. A sandbox that
        the create returns in that time is stopped and settled. A create
        that is still open after the grace goes on in the background, and a
        callback stops its box when it returns.

        A protected box reserves its budget before the create. A create that
        fails releases the hold only when no box can exist; otherwise the
        hold stays pending. A protected box cannot start again after it
        stopped, because its hold is settled.

        Raises:
            asyncio.CancelledError: The task was cancelled during the create.
            SandboxInfraError: The protected box already stopped, the box came
                back with a longer timeout, or the runtime fix failed.
        """
        if self._sandbox is not None:
            return
        if self._stopped and self._budget is not None:
            raise SandboxInfraError("The protected Hyperbrowser sandbox is stopped; it cannot start again.")
        _require_sdk()
        usage = await self._reserve()
        create = asyncio.ensure_future(self._create(usage))
        try:
            client, sandbox = await asyncio.shield(create)
        except asyncio.CancelledError:
            await _wait_through_cancels(create, _CANCEL_GRACE_SEC)
            self._usage = None
            if not create.done():
                create.add_done_callback(functools.partial(_stop_late_sandbox, usage=usage))
                raise
            if create.cancelled() or create.exception() is not None:
                refused = not create.cancelled() and _refused(create.exception())
                await _fail_usage_through_cancels(usage, refused)
                raise
            stopping = asyncio.ensure_future(_stop_and_settle(*create.result(), usage))
            await _wait_through_cancels(stopping, _CANCEL_GRACE_SEC)
            if not stopping.done():
                _keep_late_task(stopping)
            elif not stopping.cancelled() and stopping.exception() is not None:
                logger.exception(
                    "Could not stop a hyperbrowser sandbox after a cancelled start.", exc_info=stopping.exception()
                )
            raise
        except Exception as error:
            self._usage = None
            if await _fail_usage_through_cancels(usage, _refused(error)):
                # A cancel is never swallowed; the create error stays as its cause.
                raise asyncio.CancelledError from error
            raise
        self._client = client
        self._sandbox = sandbox
        if self._job_id is not None:
            _JOB_BOXES.setdefault(self._job_id, set()).add(self)
        self._cap = asyncio.ensure_future(self._enforce_lifetime())

    async def stop(self) -> None:
        """Stop the Hyperbrowser sandbox, then settle its usage.

        A cancel during the stop waits for it for at most
        ``_CANCEL_GRACE_SEC``, also through repeat cancels, then passes the
        cancel on. A stop that is still open after the grace goes on in the
        background. A protected box settles its usage also when the stop
        fails, at its whole funded lifetime.

        Raises:
            asyncio.CancelledError: The task was cancelled during the stop.
            UsagePendingError: The usage could not settle; the hold stays pending.
            BudgetError: The usage broke the reserved bounds.
        """
        cap, self._cap = self._cap, None
        if cap is not None and cap is not asyncio.current_task():
            cap.cancel()
        sandbox = self._sandbox
        if sandbox is None:
            return
        usage, self._usage = self._usage, None
        client, self._client = self._client, None
        self._sandbox = None
        self._stopped = True
        boxes = _JOB_BOXES.get(self._job_id) if self._job_id is not None else None
        if boxes is not None:
            boxes.discard(self)
            if not boxes:
                del _JOB_BOXES[self._job_id]
        stopping = asyncio.ensure_future(_stop_and_settle(client, sandbox, usage))
        try:
            await asyncio.shield(stopping)
        except asyncio.CancelledError:
            await _wait_through_cancels(stopping, _CANCEL_GRACE_SEC)
            if not stopping.done():
                _keep_late_task(stopping)
            elif not stopping.cancelled() and stopping.exception() is not None:
                logger.exception(
                    "Could not stop a hyperbrowser sandbox during a cancelled stop.", exc_info=stopping.exception()
                )
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
        """Run one bash command in the Hyperbrowser sandbox.

        Args:
            command: The shell command.
            cwd: The working directory, or the image default.
            env: Extra environment variables, added to the spec env.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` when the command ends;
                ``None`` uses the sink of the current task.
            sudo: Run the command as root.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, the result has exit code 124 and ``timed_out`` set.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        sandbox = self._require_running()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        sdk_limit = None if limit is None else max(1, math.ceil(limit))
        sink = on_output or current_output_sink()
        merged_env = {**(self.spec.env or {}), **(env or {})}
        try:
            async with asyncio.timeout(None if limit is None else limit + _TIMEOUT_GRACE_SEC) as deadline:
                # The SDK runs a command string with /bin/sh; bash keeps the contract's shell.
                result = await sandbox.exec(
                    f"bash -c {shlex.quote(command)}",
                    cwd=cwd,
                    env=merged_env or None,
                    timeout_sec=sdk_limit,
                    run_as="root" if sudo else None,
                )
        except TimeoutError:
            if limit is not None and deadline.expired():
                return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
            raise
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if sink is not None:
            if stdout:
                sink("stdout", stdout)
            if stderr:
                sink("stderr", stderr)
        if _reached_timeout(result, sdk_limit):
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, stdout=stdout, stderr=stderr, timed_out=True)
        exit_code = result.exit_code if result.exit_code is not None else 1
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the sandbox, and create their parent directories.

        Args:
            files: Maps each absolute sandbox path to its text content.

        Raises:
            RuntimeError: The sandbox is not running.
            SandboxInfraError: A parent directory could not be created.
        """
        sandbox = self._require_running()
        files_api = sandbox.files.with_run_as("root")
        for path, text in files.items():
            parent = posixpath.dirname(path)
            if parent and parent != "/":
                made = await sandbox.exec(f"mkdir -p {shlex.quote(parent)}", run_as="root")
                if made.exit_code != 0:
                    raise SandboxInfraError(f"Could not create {parent} in the hyperbrowser sandbox.")
            await files_api.upload(path, text.encode("utf-8"))

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the sandbox.

        Args:
            path: The absolute sandbox path.

        Returns:
            The exact file content, or ``None`` when the file does not exist.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        sandbox = self._require_running()
        files_api = sandbox.files.with_run_as("root")
        if not await files_api.exists(path):
            return None
        data = await files_api.download(path)
        return bytes(data).decode("utf-8")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every sandbox that this process started for the job.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandboxes that the sweep stopped.

        Raises:
            Exception: The first stop error that is not a usage or budget error.
        """
        envs = list(_JOB_BOXES.get(job_id, ()))
        results = await asyncio.gather(*(env.stop() for env in envs), return_exceptions=True)
        stopped = 0
        unexpected: BaseException | None = None
        for result in results:
            if isinstance(result, UsagePendingError | BudgetError):
                # The hold stays pending for reconciliation; the sweep goes on.
                logger.warning("A hyperbrowser sandbox of job %s has a pending usage hold.", job_id)
                stopped += 1
            elif isinstance(result, BaseException):
                unexpected = unexpected or result
            else:
                stopped += 1
        if unexpected is not None:
            raise unexpected
        return stopped

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Replace the egress policy of the running sandbox.

        Args:
            policy: The checked policy.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        sandbox = self._require_running()
        await sandbox.update_network(network_kwargs(policy, for_update=True))

    async def _enforce_lifetime(self) -> None:
        """Stop the box when the spec lifetime ends, in case Hyperbrowser's timeout does not."""
        try:
            await _sleep(self.spec.lifetime_seconds)
            logger.warning("A hyperbrowser sandbox reached its lifetime; stopping it.")
            await self.stop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not stop a hyperbrowser sandbox at the end of its lifetime.")

    async def _reserve(self) -> Any | None:
        """Reserve the whole lifetime of a protected box.

        Returns:
            The usage reservation, or ``None`` for an unprotected box.
        """
        if self._budget is None or self._usage is not None:
            return self._usage
        reserving = asyncio.ensure_future(
            asyncio.to_thread(
                self._usage_factory, self._budget, self._usage_request(), operation_key=self.spec.operation_key
            )
        )
        try:
            self._usage = await asyncio.shield(reserving)
        except asyncio.CancelledError:
            # The thread cannot be cancelled and may still reserve; release
            # its result instead of leaking the hold.
            await _wait_through_cancels(reserving, _CANCEL_GRACE_SEC)
            if not reserving.done():
                reserving.add_done_callback(_release_late_reservation)
            elif not reserving.cancelled() and reserving.exception() is None:
                await _fail_usage_through_cancels(reserving.result())
            raise
        return self._usage

    def _usage_request(self) -> dict[str, Any]:
        """Build the usage request that the reservation prices.

        Returns:
            The image, the timeout and the size of the box.
        """
        spec = self.spec
        resources = spec.resources
        memory = resources.memory_mb if resources.memory_mb is not None else resources.cpus * _MEMORY_MIB_PER_CPU
        return {
            "image": spec.image or DEFAULT_IMAGE,
            "timeout_minutes": max(1, math.ceil(spec.lifetime_seconds / 60)),
            "vcpus": resources.cpus,
            "memory_mib": memory,
        }

    def _create_params(self) -> dict[str, Any]:
        """Build the Hyperbrowser create parameters.

        Returns:
            The image, size, timeout and network parameters of the box.
        """
        request = self._usage_request()
        params: dict[str, Any] = {
            "image_name": request["image"],
            "cpu": request["vcpus"],
            "memory_mib": request["memory_mib"],
            "timeout_minutes": request["timeout_minutes"],
            **network_kwargs(self._network_policy or NetworkPolicy(disabled=True)),
        }
        if self.spec.resources.disk_mb is not None:
            params["disk_mib"] = max(self.spec.resources.disk_mb, _MIN_DISK_MIB)
        return params

    async def _create(self, usage: Any | None) -> tuple[Any, Any]:
        """Create one sandbox, check its timeout and fix its runtime.

        Args:
            usage: The usage reservation of a protected box, or ``None``.

        Returns:
            The SDK client and the sandbox handle.

        Raises:
            SandboxInfraError: The box came back with a longer timeout, or the runtime fix failed.
        """
        params = self._create_params()
        client = AsyncHyperbrowser(timeout=_SDK_REQUEST_TIMEOUT_SEC)
        try:
            # A local check error here made no request, so its hold is released.
            body = CreateSandboxParams(**params) if CreateSandboxParams is not None else params
            if usage is not None:
                usage.sent()
            sandbox = await client.sandboxes.create(body)
        except BaseException:
            await _close_client(client)
            raise
        try:
            if usage is not None:
                await asyncio.to_thread(usage.confirm_created, sandbox)
            granted = getattr(sandbox, "timeout_minutes", None)
            if (
                not isinstance(granted, int)
                or isinstance(granted, bool)
                or not 0 < granted <= params["timeout_minutes"]
            ):
                raise SandboxInfraError(
                    f"The hyperbrowser sandbox got timeout_minutes={granted!r}, not {params['timeout_minutes']}."
                )
            fixed = await sandbox.exec(
                _FIX_RUNTIME_COMMAND, cwd="/", timeout_sec=_FIX_RUNTIME_TIMEOUT_SEC, run_as="root"
            )
            if fixed.exit_code != 0:
                raise SandboxInfraError(
                    f"Could not prepare the hyperbrowser sandbox runtime: {fixed.stderr or fixed.stdout}"
                )
        except BaseException:
            try:
                await _stop_and_settle(client, sandbox, usage)
            except Exception:
                # The first error goes on; the settle error keeps the hold pending.
                logger.exception("Could not stop a hyperbrowser sandbox whose start failed.")
            raise
        return client, sandbox

    def _require_running(self) -> Any:
        """Return the sandbox handle, or raise when the sandbox is stopped.

        Returns:
            The sandbox handle.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._sandbox is None:
            raise RuntimeError("The hyperbrowser sandbox is not running.")
        return self._sandbox


def network_kwargs(policy: NetworkPolicy, *, for_update: bool = False) -> dict[str, Any]:
    """Map an egress policy to the Hyperbrowser network parameters.

    Args:
        policy: The egress policy.
        for_update: Also clear the lists that the mode leaves unset, so an
            update replaces the whole earlier policy.

    Returns:
        The ``allow_internet_access``, ``allow_out`` and ``deny_out`` parameters.
    """
    if policy.disabled or not policy.allowed_hosts:
        kwargs: dict[str, Any] = {"allow_internet_access": not policy.disabled}
        if for_update:
            kwargs.update({"allow_out": [], "deny_out": []})
        return kwargs
    kwargs = {"allow_internet_access": False, "allow_out": list(policy.allowed_hosts)}
    if for_update:
        kwargs["deny_out"] = []
    return kwargs


def _require_sdk() -> None:
    """Raise when the Hyperbrowser SDK is not installed.

    Raises:
        SandboxFatalError: The ``hyperbrowser`` package is missing.
    """
    if AsyncHyperbrowser is None:
        raise SandboxFatalError("The hyperbrowser sandbox needs the hyperbrowser package: install the extra.")


def _refused(error: BaseException | None) -> bool:
    """Return True when Hyperbrowser answered the create with a refusal, so it made no box.

    Args:
        error: The create error.

    Returns:
        True for an SDK error with a refusal status.
    """
    return (
        HyperbrowserError is not None
        and isinstance(error, HyperbrowserError)
        and getattr(error, "status_code", None) in _REFUSED_CREATE_STATUSES
    )


def _reached_timeout(result: Any, timeout_sec: int | None) -> bool:
    """Return True when the SDK result shows that the command hit its time limit.

    Args:
        result: The SDK process result.
        timeout_sec: The limit sent to the SDK, or ``None``.

    Returns:
        True for a ``timed_out`` status, or for a ``killed`` status that ran
        at least the limit.
    """
    status = getattr(result, "status", None)
    if status == "timed_out":
        return True
    if status != "killed" or timeout_sec is None:
        return False
    # Hyperbrowser reports its own limit as a kill; only the runtime tells it
    # apart from a command that killed itself.
    started = getattr(result, "started_at", None)
    completed = getattr(result, "completed_at", None)
    return (
        isinstance(started, int | float)
        and isinstance(completed, int | float)
        and completed - started >= timeout_sec * 1000
    )


async def _close_client(client: Any) -> None:
    """Close the SDK client and log a failure.

    Args:
        client: The SDK client, or ``None``.
    """
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        logger.debug("Could not close a hyperbrowser client.", exc_info=True)


async def _vendor_usage(sandbox: Any) -> dict[str, Any] | None:
    """Read the usage that Hyperbrowser reports for a stopped box, as evidence.

    Args:
        sandbox: The sandbox handle.

    Returns:
        The reported usage fields, or ``None`` when the read failed.
    """
    try:
        detail = await sandbox.info()
    except Exception:
        logger.debug("Could not read the usage of a stopped hyperbrowser sandbox.", exc_info=True)
        return None
    fields = ("credits_used", "duration", "start_time", "end_time", "close_reason", "status")
    return {field: getattr(detail, field, None) for field in fields}


async def _stop_and_settle(client: Any, sandbox: Any, usage: Any | None) -> None:
    """Stop one sandbox, settle its usage and close its client.

    A stop that fails still settles: the usage then bills the whole funded
    lifetime, because Hyperbrowser ends the box at its timeout.

    Args:
        client: The SDK client of the box.
        sandbox: The sandbox handle.
        usage: The usage reservation of a protected box, or ``None``.
    """
    try:
        try:
            await sandbox.stop()
        except BaseException:
            if usage is not None:
                try:
                    await asyncio.to_thread(usage.settle)
                except Exception:
                    # The stop error goes on; the settle error keeps the hold pending.
                    logger.exception("Could not settle the usage of a hyperbrowser sandbox whose stop failed.")
            raise
        if usage is not None:
            usage.stopped()
            usage.report(await _vendor_usage(sandbox))
            await asyncio.to_thread(usage.settle)
    finally:
        await _close_client(client)


async def _fail_usage(usage: Any | None, refused: bool = False) -> None:
    """Release or keep the hold of a protected box whose create returned no sandbox.

    Args:
        usage: The usage reservation, or ``None``.
        refused: True when Hyperbrowser refused the create, so no box exists.
    """
    if usage is None:
        return
    try:
        await asyncio.to_thread(usage.fail, refused)
    except Exception:
        logger.exception("Could not release or keep the usage hold of a hyperbrowser sandbox.")


async def _fail_usage_through_cancels(usage: Any | None, refused: bool = False) -> bool:
    """Run :func:`_fail_usage` so that a cancel cannot stop it halfway.

    Args:
        usage: The usage reservation, or ``None``.
        refused: True when Hyperbrowser refused the create, so no box exists.

    Returns:
        True when a cancel arrived during the wait, so the caller must raise it.
    """
    cleanup = asyncio.ensure_future(_fail_usage(usage, refused))
    cancelled = await _wait_through_cancels(cleanup, _CANCEL_GRACE_SEC)
    if not cleanup.done():
        _keep_late_task(cleanup)
    return cancelled


async def _wait_through_cancels(task: asyncio.Future[Any], grace_sec: float) -> bool:
    """Wait until the task ends or the grace time passes, and absorb each cancel meanwhile.

    The task itself is never cancelled, because Hyperbrowser may already have
    done the work on its side.

    Args:
        task: The task to wait for.
        grace_sec: The longest wait in seconds.

    Returns:
        True when a cancel arrived during the wait, so the caller must raise it.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_sec
    cancelled = False
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            # The caller raises the cancel after this wait.
            cancelled = True
    return cancelled


def _release_late_reservation(reserving: asyncio.Future[Any]) -> None:
    """Release the hold of a reservation that finished after its start was cancelled.

    A done callback must never raise, so every error goes to the log.

    Args:
        reserving: The finished reservation task.
    """
    try:
        if reserving.cancelled() or reserving.exception() is not None:
            return
        _keep_late_task(asyncio.ensure_future(_fail_usage(reserving.result())))
    except Exception:
        logger.exception("Could not release a hyperbrowser usage hold that arrived after a cancelled start.")


def _stop_late_sandbox(create: asyncio.Future[Any], *, usage: Any | None = None) -> None:
    """Stop the sandbox of a create that ended after its start was cancelled.

    A done callback must never raise, so every error goes to the log.

    Args:
        create: The finished create task.
        usage: The usage reservation of a protected box, or ``None``.
    """
    try:
        if create.cancelled() or create.exception() is not None:
            refused = not create.cancelled() and _refused(create.exception())
            _keep_late_task(asyncio.ensure_future(_fail_usage(usage, refused)))
            return
        _keep_late_task(asyncio.ensure_future(_stop_and_settle(*create.result(), usage)))
    except Exception:
        logger.exception("Could not stop a hyperbrowser sandbox that arrived after a cancelled start.")


def _keep_late_task(task: asyncio.Future[Any]) -> None:
    """Keep a background task alive until it ends, and log its error then.

    Args:
        task: The running task.
    """
    _LATE_TASKS.add(task)
    task.add_done_callback(_finish_late_task)


def _finish_late_task(task: asyncio.Future[Any]) -> None:
    """Forget a finished background task and log its error.

    Args:
        task: The finished task.
    """
    _LATE_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("A background hyperbrowser stop or release failed.", exc_info=task.exception())

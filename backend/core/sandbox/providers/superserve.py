"""The async Superserve sandbox provider, for development only.

Adapted from Harbor (Apache-2.0), harbor-framework/harbor main at commit
26f95e0ebd, ``src/harbor/environments/superserve.py``: the template that
fixes the box size and mounts ``/tmp`` on disk, the template name hash, the
template lookup and build, the create call with an attempt label in its
metadata, the lookup by that label after a failed create, the egress map and
the kill come from it. Harbor 0.25.0 has no Superserve provider. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- A box with a budget is protected (SKY-940). It reserves its whole funded
  lifetime before the create, checks the created box against its funded
  size, and settles its measured runtime at the published rates after the
  kill, also after a failed or cancelled start.
- Every box gets ``timeout_seconds`` from the spec lifetime, at least
  60 s, so Superserve pauses a box that a lost process never stops, and a
  short ``auto_delete_seconds``, so Superserve then deletes it. The hold
  funds that vendor bound. The provider also enforces the spec lifetime
  itself: a cap task kills the box at the lifetime.
- SDK 0.14.1 calls ``timeout_seconds`` the auto-pause timeout
  (``async_sandbox.py`` lines 876-877). It is a separate knob from
  ``idle_timeout_seconds``, which pauses a box after no activity (line 260)
  and which the provider never sends. The SDK does not say whether the
  auto-pause timer counts wall time from the create. If Superserve counts
  only active time, a busy box outlives its hold when the Skynet process
  dies, and the cap task is then the only bound. The SDK has no other
  absolute-lifetime knob.
- The provider adds no retry of its own (SKY-406 D3, D10). The SDK sends the
  create once. A create that Superserve answered with a definite refusal and
  for which the label lookup finds no box releases its hold; any other
  failed create keeps its hold pending.
- Commands run in the foreground through ``commands.run``. Harbor's
  detached jobs, the job polling and the transient retries are left out.
- A hostname allowlist relies on Superserve's own resolver: the provider
  adds no public DNS resolver, because ``NetworkConfig`` has no ports and
  such a rule would open every port of that address.
- Harbor's Dockerfile and compose builds, the DinD strategy, directory
  transfer, attach, secrets, template eviction, the build concurrency limit
  and pause-on-stop are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import math
import os
import shlex
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any, ClassVar

from core.billing.operation_pricing import UnpricedOperationError
from core.billing.superserve_usage import USAGE, quote_superserve_sandbox
from core.billing.usage_tags import BOX_KEY

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, network_mode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxError, SandboxFatalError, SandboxInfraError
from ..resources import SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from superserve import (
        AsyncSandbox,
        AsyncTemplate,
        ConflictError,
        NetworkConfig,
        NotFoundError,
        RunStep,
        SandboxTimeoutError,
    )
    from superserve import SandboxError as SuperserveError
except ImportError:
    AsyncSandbox = None
    AsyncTemplate = None
    ConflictError = None
    NetworkConfig = None
    NotFoundError = None
    RunStep = None
    SandboxTimeoutError = None
    SuperserveError = None

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "python:3.12-slim"
STOP_TIMEOUT = 60.0
# Harbor waits this long for a cancelled create, so a box made during the
# request still has a handle to kill.
CREATE_GRACE = 30.0
# The SDK timeout should end a command first; this margin only catches a
# hung SDK call.
EXEC_GRACE = 30.0
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124
# Superserve publishes no range for timeout_seconds; this is Skynet's own
# ceiling, equal to the longest idle timeout that Superserve accepts.
_MAX_LIFETIME_SEC = 604_800
# The idle timeout's documented floor; the provider assumes timeout_seconds
# has the same floor, so a short lifetime cannot get a 400.
_MIN_TIMEOUT_SEC = 60
# A template build pulls one registry image and runs one short step; a
# build still running after this is treated as stuck, so start() cannot
# hold a dispatched budget forever. The create is not sent yet, so the
# hold is released.
_BUILD_TIMEOUT_SEC = 900
# Harbor's file transfer timeout.
_TRANSFER_TIMEOUT_SEC = 1800
# The download limit of the other providers (sbx, kernel).
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
# Seconds a paused box is kept before Superserve deletes it. A paused box
# bills only its disk, which the usage margin covers.
_AUTO_DELETE_SEC = 60
_DEFAULT_MEMORY_MB_PER_VCPU = 2048
_DEFAULT_DISK_MB = 10 * 1024
_ATTEMPT_KEY = "skynet.attempt"
# Harbor's template build step: /tmp on the root filesystem, as Docker keeps it.
_TMP_ON_DISK = "umount /tmp 2>/dev/null || umount -l /tmp"
# Bumping this rebuilds every template the provider made.
_TEMPLATE_VERSION = 1
_DENY_ALL = "0.0.0.0/0"
# The SDK sends the create POST once and never retries it, so these
# statuses are definite answers that made no box.
_REFUSED_STATUSES = frozenset({400, 401, 402, 403, 404, 409, 429})

SUPERSERVE_CAPABILITIES = EnvironmentCapabilities(
    name="superserve",
    billable=True,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)

# Strong references to background kills and cleanups, so the event loop does not drop them.
_LATE_TASKS: set[asyncio.Future[Any]] = set()

# A test replaces this sleep so the lifetime cap fires at once.
_cap_sleep = asyncio.sleep


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
        raise SandboxInfraError(f"Superserve could not {action}: {type(error).__name__}.") from error


def _require_sdk() -> None:
    """Raise when the Superserve SDK is not installed.

    Raises:
        SandboxFatalError: The ``superserve`` package is missing.
    """
    if AsyncSandbox is None:
        raise SandboxFatalError("The superserve sandbox needs the superserve package: install the `superserve` extra.")


def _refused_create(error: BaseException | None) -> bool:
    """Tell whether a create failure is a definite answer that made no box.

    A timeout, a network error, a missing access token after the POST and
    any 5xx answer leave the outcome unknown.

    Args:
        error: The create error, or ``None``.

    Returns:
        True for an SDK error with a refusal status.
    """
    if SuperserveError is None or not isinstance(error, SuperserveError):
        return False
    if SandboxTimeoutError is not None and isinstance(error, SandboxTimeoutError):
        return False
    return getattr(error, "status_code", None) in _REFUSED_STATUSES


def _network_config(policy: NetworkPolicy) -> Any:
    """Map a network policy to Superserve egress rules.

    Args:
        policy: The egress policy.

    Returns:
        The ``NetworkConfig``.
    """
    mode = network_mode(disabled=policy.disabled, allowed_hosts=policy.allowed_hosts)
    if mode is NetworkMode.OPEN:
        return NetworkConfig(allow_out=[], deny_out=[])
    if mode is NetworkMode.DENY_ALL:
        return NetworkConfig(allow_out=[], deny_out=[_DENY_ALL])
    return NetworkConfig(allow_out=list(dict.fromkeys(policy.allowed_hosts)), deny_out=[_DENY_ALL])


def _template_name(spec: dict[str, Any]) -> str:
    """Name a template so that the name changes whenever its content does.

    Args:
        spec: The image, sizes and steps baked into the template.

    Returns:
        The template name.
    """
    digest = hashlib.sha256(json.dumps({"version": _TEMPLATE_VERSION, **spec}, sort_keys=True).encode()).hexdigest()[
        :16
    ]
    return f"skynet/{digest}"


async def _bounded(awaitable: Any, action: str) -> bool:
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
        logger.warning("Superserve could not %s: %s", action, type(error).__name__)
        return False
    return True


async def _kill_and_settle(sandbox_id: str, usage: Any | None, handle: Any | None = None) -> None:
    """Kill one box, then settle its usage.

    A kill that fails still settles: the usage then bills the whole funded
    lifetime, because Superserve pauses the box at its timeout and deletes it
    soon after.

    Args:
        sandbox_id: The Superserve sandbox id.
        usage: The usage reservation of a protected box, or ``None``.
        handle: The SDK sandbox handle, or ``None`` for a box found by a lookup.
            Its ``kill`` also closes the handle's HTTP client.

    Raises:
        SandboxInfraError: The kill failed or timed out.
    """
    try:
        with _mapped("kill the box"):
            kill = handle.kill() if handle is not None else AsyncSandbox.kill_by_id(sandbox_id)
            await asyncio.wait_for(kill, STOP_TIMEOUT)
    except BaseException:
        if usage is not None:
            try:
                await asyncio.to_thread(usage.settle)
            except Exception:
                # The kill error goes on; the settle error keeps the hold pending.
                logger.exception("Could not settle the usage of a Superserve box whose kill failed.")
        raise
    if usage is not None:
        usage.stopped()
        await asyncio.to_thread(usage.settle)


async def _find_boxes(attempt: str | None) -> list[Any] | None:
    """Look up the boxes that a create attempt made.

    A box in status ``failed`` or ``deleted`` is not running or is gone, so
    it does not count: it is never bound or settled. The SDK does not say
    that Superserve deletes a failed box or stops billing its disk, so a
    failed box gets a best-effort kill.

    Args:
        attempt: The attempt label of the create.

    Returns:
        The live boxes, or ``None`` when the lookup failed.
    """
    if attempt is None:
        return None
    try:
        boxes = await AsyncSandbox.list(metadata={_ATTEMPT_KEY: attempt})
    except Exception:
        logger.exception("Could not look up a Superserve box after a failed create; its hold stays pending.")
        return None
    live = []
    for box in boxes:
        status = getattr(box.status, "value", box.status)
        if status == "failed":
            await _bounded(AsyncSandbox.kill_by_id(box.id), "kill a failed box of a failed create")
        elif status != "deleted":
            live.append(box)
    return live


async def _fail_usage(usage: Any | None, refused: bool = False) -> None:
    """Release or keep the hold of a protected box whose create returned no box.

    A sent create may still have made a box. A lookup by the attempt label
    finds it, and the box is killed and settled. The hold is released only
    when the create was never sent, or when Superserve refused it and the
    lookup found no box. A failed lookup keeps the hold pending.

    Args:
        usage: The usage reservation, or ``None``.
        refused: True when Superserve gave a definite refusal.
    """
    if usage is None:
        return
    absent = False
    if usage.sent_at is not None and usage.sandbox_id is None:
        found = await _find_boxes(usage.attempt)
        if found:
            try:
                await asyncio.to_thread(usage.bind, found[0].id)
            except Exception:
                logger.exception("Could not bind a Superserve box that a failed create made.")
            for box in found[1:]:
                await _bounded(AsyncSandbox.kill_by_id(box.id), "kill an extra box of a failed create")
            try:
                await _kill_and_settle(found[0].id, usage)
            except Exception:
                logger.exception("Could not stop a Superserve box that a failed create made.")
            return
        absent = refused and found == []
    try:
        await asyncio.to_thread(usage.fail, absent)
    except Exception:
        logger.exception("Could not release the usage hold of a Superserve box that was never created.")


async def _wait_through_cancels(task: asyncio.Future[Any], grace_sec: float) -> bool:
    """Wait until the task ends or the grace time passes, and absorb each cancel meanwhile.

    The task itself is never cancelled, because Superserve may already have
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


async def _fail_usage_through_cancels(usage: Any | None, refused: bool = False) -> bool:
    """Run :func:`_fail_usage` so that a cancel cannot stop it halfway.

    Args:
        usage: The usage reservation, or ``None``.
        refused: True when Superserve gave a definite refusal.

    Returns:
        True when a cancel arrived during the wait, so the caller must raise it.
    """
    cleanup = asyncio.ensure_future(_fail_usage(usage, refused))
    cancelled = await _wait_through_cancels(cleanup, STOP_TIMEOUT)
    if not cleanup.done():
        _keep(cleanup)
    return cancelled


def _keep(task: asyncio.Future[Any]) -> None:
    """Keep a background task alive until it ends, and log its error then.

    Args:
        task: The running task.
    """
    _LATE_TASKS.add(task)
    task.add_done_callback(_forget)


def _forget(task: asyncio.Future[Any]) -> None:
    """Forget a finished background task and log its error.

    Args:
        task: The finished task.
    """
    _LATE_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("A Superserve background cleanup failed.", exc_info=task.exception())


def _on_late_create(usage: Any | None, create: asyncio.Future[Any]) -> None:
    """Clean up after a create that ended after its start was cancelled.

    A done callback must never raise, so every error goes to the log.

    Args:
        usage: The usage reservation of a protected box, or ``None``.
        create: The finished create task.
    """
    try:
        if create.cancelled() or create.exception() is not None:
            refused = not create.cancelled() and _refused_create(create.exception().__cause__)
            _keep(asyncio.ensure_future(_fail_usage(usage, refused)))
            return
        _keep(asyncio.ensure_future(_kill_and_settle(create.result().id, usage, create.result())))
    except Exception:
        logger.exception("Could not stop a Superserve box that arrived after a cancelled start.")


def _on_late_reservation(reserving: asyncio.Future[Any]) -> None:
    """Release the hold of a reservation that finished after its start was cancelled.

    Args:
        reserving: The finished reservation task.
    """
    try:
        if reserving.cancelled() or reserving.exception() is not None:
            return
        _keep(asyncio.ensure_future(_fail_usage(reserving.result())))
    except Exception:
        logger.exception("Could not release a Superserve hold that arrived after a cancelled start.")


class SuperserveEnvironment(SandboxEnvironment):
    """Run a sandbox as one Superserve VM made from a fixed-size template."""

    capabilities: ClassVar[EnvironmentCapabilities] = SUPERSERVE_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = USAGE.reserve,
    ) -> None:
        """Create a stopped Superserve sandbox.

        Args:
            spec: The sandbox to open. ``spec.image`` is a registry image;
                ``None`` uses ``DEFAULT_IMAGE``.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` time limit. ``None`` means no default limit.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: The lifetime is longer than seven days, or a
                protected box has no operation key or no priced size.
        """
        super().__init__(spec)
        if spec.lifetime_seconds > _MAX_LIFETIME_SEC:
            raise SandboxSpecError(f"The superserve sandbox lives at most {_MAX_LIFETIME_SEC} seconds.")
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._sandbox: Any = None
        self._stopped = False
        self._stop_task: asyncio.Future[None] | None = None
        self._cap_task: asyncio.Task[None] | None = None
        self._sent_at: float | None = None
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Superserve sandbox needs an operation key.")
            try:
                quote_superserve_sandbox(self._usage_request())
            except UnpricedOperationError as error:
                raise SandboxSpecError(str(error)) from error

    @property
    def usage(self) -> Any | None:
        """Return the usage reservation of a protected box, once it exists."""
        return self._usage

    @classmethod
    def preflight(cls) -> None:
        """Check that the Superserve SDK and its API key are present.

        Raises:
            SandboxFatalError: The package is missing, or ``SUPERSERVE_API_KEY`` is not set.
        """
        _require_sdk()
        if not os.environ.get("SUPERSERVE_API_KEY"):
            raise SandboxFatalError("The superserve sandbox needs SUPERSERVE_API_KEY.")

    def _size(self) -> tuple[int, int, int]:
        """Return the vCPUs, memory and disk of the box.

        Returns:
            Whole vCPUs, memory in MiB and disk in MiB.
        """
        request = self.spec.resources
        vcpu = max(1, math.ceil(request.cpus))
        memory = request.memory_mb or vcpu * _DEFAULT_MEMORY_MB_PER_VCPU
        disk = request.disk_mb or _DEFAULT_DISK_MB
        return vcpu, memory, disk

    def _usage_request(self) -> dict[str, Any]:
        """Build the usage request that the reservation prices.

        Returns:
            The image, lifetime and size of the box.
        """
        vcpu, memory, disk = self._size()
        return {
            "image": self.spec.image or DEFAULT_IMAGE,
            # The hold funds Superserve's own bound, which can be longer than the lifetime.
            "lifetime_ms": self._timeout_seconds() * 1000,
            "vcpu": vcpu,
            "memory_mib": memory,
            "disk_mib": disk,
            "gpu": bool(self.spec.resources.gpu or self.spec.gpu_type),
        }

    def _timeout_seconds(self) -> int:
        """Return the ``timeout_seconds`` that the create sends.

        Returns:
            The spec lifetime in whole seconds, at least ``_MIN_TIMEOUT_SEC``.
        """
        return max(_MIN_TIMEOUT_SEC, math.ceil(self.spec.lifetime_seconds))

    async def start(self) -> None:
        """Create the Superserve box. Do nothing when the box already runs.

        A protected box reserves its whole lifetime before the template
        lookup and the create. A cancel during the create waits for it for
        at most ``CREATE_GRACE`` seconds; a box that arrives later is killed
        and settled in the background. A failed create releases or keeps the
        hold by :func:`_fail_usage`. The cancel always reaches the caller.

        Raises:
            asyncio.CancelledError: The task was cancelled during the create.
            SandboxInfraError: The protected box already stopped, or an SDK call failed.
        """
        if self._sandbox is not None:
            return
        if self._stopped and self._budget is not None:
            raise SandboxInfraError("The protected Superserve sandbox is stopped; it cannot start again.")
        _require_sdk()
        usage = await self._reserve()
        create = asyncio.ensure_future(self._create(usage))
        try:
            sandbox = await asyncio.shield(create)
        except asyncio.CancelledError:
            await _wait_through_cancels(create, CREATE_GRACE)
            self._usage = None
            if not create.done():
                create.add_done_callback(functools.partial(_on_late_create, usage))
                raise
            if create.cancelled() or create.exception() is not None:
                refused = not create.cancelled() and _refused_create(create.exception().__cause__)
                await _fail_usage_through_cancels(usage, refused)
                raise
            kill = asyncio.ensure_future(_kill_and_settle(create.result().id, usage, create.result()))
            await _wait_through_cancels(kill, STOP_TIMEOUT)
            if not kill.done():
                _keep(kill)
            elif not kill.cancelled() and kill.exception() is not None:
                logger.exception("Could not stop a Superserve box after a cancelled start.", exc_info=kill.exception())
            raise
        except Exception as error:
            self._usage = None
            if await _fail_usage_through_cancels(usage, _refused_create(error.__cause__)):
                # A cancel is never swallowed; the create error stays as its cause.
                raise asyncio.CancelledError from error
            raise
        self._sandbox = sandbox
        self._stop_task = None
        deadline = (self._sent_at or asyncio.get_running_loop().time()) + self.spec.lifetime_seconds
        self._cap_task = asyncio.ensure_future(self._enforce_cap(deadline))

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
            # The thread cannot be cancelled and may still reserve; release its result.
            await _wait_through_cancels(reserving, STOP_TIMEOUT)
            if not reserving.done():
                reserving.add_done_callback(_on_late_reservation)
            elif not reserving.cancelled() and reserving.exception() is None:
                await _fail_usage_through_cancels(reserving.result())
            raise
        return self._usage

    async def _ensure_template(self) -> str:
        """Find or build the fixed-size template of the box and wait until it is ready.

        Returns:
            The template name.

        Raises:
            SandboxInfraError: An SDK call failed, the build failed, or the
                build ran past ``_BUILD_TIMEOUT_SEC``.
        """
        vcpu, memory, disk = self._size()
        image = self.spec.image or DEFAULT_IMAGE
        name = _template_name({"image": image, "vcpu": vcpu, "memory": memory, "disk": disk, "steps": [_TMP_ON_DISK]})
        with _mapped("prepare the template"):
            found = [info for info in await AsyncTemplate.list(name_prefix=name) if info.name == name]
            status = getattr(found[0].status, "value", found[0].status) if found else None
            if status == "ready":
                return name
            if not found:
                try:
                    template = await AsyncTemplate.create(
                        name=name,
                        from_=image,
                        vcpu=vcpu,
                        memory_mib=memory,
                        disk_mib=disk,
                        steps=[RunStep(run=_TMP_ON_DISK)],
                    )
                except ConflictError:
                    # Another process created the same template first.
                    template = await AsyncTemplate.connect(name)
            else:
                template = await AsyncTemplate.connect(found[0].id)
                if status == "failed":
                    await template.rebuild()
                    # A handle keeps the build id it saw at connect time, so the old
                    # handle would wait on the failed build; a new one sees the rebuild.
                    template = await AsyncTemplate.connect(found[0].id)
            async with asyncio.timeout(_BUILD_TIMEOUT_SEC):
                await template.wait_until_ready()
        return name

    async def _create(self, usage: Any | None) -> Any:
        """Prepare the template, then create one box from it.

        The template work comes before the create is marked as sent, so a
        failed build releases the hold.

        Args:
            usage: The usage reservation of a protected box, or ``None``.

        Returns:
            The SDK sandbox handle.
        """
        spec = self.spec
        template = await self._ensure_template()
        attempt = uuid.uuid4().hex
        name = (spec.name or f"skynet-{attempt}")[:64]
        metadata = {**spec.tags, _ATTEMPT_KEY: attempt}
        if usage is not None:
            # The box tag ties the box to its hold for a reconcile.
            metadata[BOX_KEY] = usage.box
        arguments = {
            "name": name,
            "from_template": template,
            "timeout_seconds": self._timeout_seconds(),
            "auto_delete_seconds": _AUTO_DELETE_SEC,
            "metadata": metadata,
            "env_vars": dict(spec.env) or None,
            "network": _network_config(self._network_policy or NetworkPolicy(disabled=True)),
        }
        # The clock starts only when nothing can fail before the create call.
        if usage is not None:
            usage.sent(name, attempt)
        self._sent_at = asyncio.get_running_loop().time()
        with _mapped("create the box"):
            sandbox = await AsyncSandbox.create(**arguments)
        if usage is not None:
            try:
                await asyncio.to_thread(usage.bind, sandbox.id)
                with _mapped("read the box"):
                    info = await sandbox.get_info()
                usage.check_size(info)
            except BaseException:
                try:
                    await _kill_and_settle(sandbox.id, usage, sandbox)
                except Exception:
                    # The confirm error goes on; the sweep can stop a leaked box.
                    logger.exception("Could not stop a Superserve box whose usage was not confirmed.")
                raise
        return sandbox

    async def _enforce_cap(self, deadline: float) -> None:
        """Kill the box at the end of its lifetime.

        Args:
            deadline: The event loop time at which the lifetime ends.
        """
        await _cap_sleep(max(0.0, deadline - asyncio.get_running_loop().time()))
        logger.warning("The Superserve box reached its lifetime; Skynet kills it.")
        try:
            await self.stop()
        except Exception:
            logger.exception("Could not stop a Superserve box at its lifetime.")

    async def stop(self) -> None:
        """Kill the box once and settle its usage. Safe to call again and before start.

        Every caller waits for the same kill. A cancel during the kill waits
        for it for at most ``STOP_TIMEOUT`` seconds, then passes on while the
        kill goes on in the background.

        Raises:
            asyncio.CancelledError: The task was cancelled during the kill.
            SandboxInfraError: The kill failed or timed out.
            UsagePendingError: The usage could not settle; the hold stays pending.
            BudgetError: The usage broke the reserved bounds.
        """
        cap, self._cap_task = self._cap_task, None
        if cap is not None and cap is not asyncio.current_task():
            cap.cancel()
        if self._stop_task is None:
            sandbox = self._sandbox
            if sandbox is None:
                return
            usage, self._usage = self._usage, None
            self._sandbox = None
            self._stopped = True
            self._stop_task = asyncio.ensure_future(_kill_and_settle(sandbox.id, usage, sandbox))
        kill = self._stop_task
        try:
            await asyncio.shield(kill)
        except asyncio.CancelledError:
            await _wait_through_cancels(kill, STOP_TIMEOUT)
            if not kill.done():
                _keep(kill)
            raise

    def _require_running(self) -> Any:
        """Return the SDK sandbox handle.

        Returns:
            The handle.

        Raises:
            SandboxInfraError: The box is not running.
        """
        if self._sandbox is None:
            raise SandboxInfraError("The Superserve sandbox is not running.")
        return self._sandbox

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
        """Run one shell command in the box and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory; the image default by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` pairs. Each stream arrives
                once, after the command ends.
            sudo: Run the command as root.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, the result has exit code 124 and ``timed_out`` set.

        Raises:
            SandboxInfraError: The box is not running, or the SDK call failed.
        """
        sandbox = self._require_running()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        # The SDK waits 30 s for a command with no timeout, so always send one.
        sdk_timeout = math.ceil(limit) if limit is not None else math.ceil(self.spec.lifetime_seconds)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            async with asyncio.timeout(limit + EXEC_GRACE if limit is not None else None):
                with _mapped("run the command"):
                    result = await sandbox.commands.run(
                        command,
                        cwd=cwd,
                        env={**self.spec.env, **(env or {})} or None,
                        user="root" if sudo else None,
                        timeout_seconds=max(1, sdk_timeout),
                    )
        except TimeoutError:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        except SandboxInfraError as error:
            if (
                limit is not None
                and SandboxTimeoutError is not None
                and isinstance(error.__cause__, SandboxTimeoutError)
            ):
                return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
            raise
        # Superserve does not document the exit code of a command it ended at its timeout.
        if limit is not None and result.exit_code != 0 and loop.time() - started >= limit:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, stdout=result.stdout, stderr=result.stderr, timed_out=True)
        if getattr(result, "truncated", False):
            logger.warning("Superserve truncated a command output.")
        sink = on_output or current_output_sink()
        if sink is not None:
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                if text:
                    sink(stream, text)
        return ExecResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the box, making parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The box is not running, the parent directories
                could not be made, or a write failed.
        """
        sandbox = self._require_running()
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {".", "/"})
        if parents:
            # Harbor makes the directories as root, so a path outside the user's home works.
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents), sudo=True)
            if not made.ok:
                raise SandboxInfraError("Superserve could not make the upload directories.")
        for path, text in files.items():
            with _mapped("write a file"):
                await sandbox.files.write(path, text.encode("utf-8"), timeout=_TRANSFER_TIMEOUT_SEC)

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the box.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The box is not running, the read failed, or
                the file is not UTF-8 text, or it is larger than ``MAX_DOWNLOAD_BYTES``.
        """
        sandbox = self._require_running()
        try:
            with _mapped("read a file"):
                data = await sandbox.files.read(path, timeout=_TRANSFER_TIMEOUT_SEC, max_bytes=MAX_DOWNLOAD_BYTES)
        except SandboxInfraError as error:
            if NotFoundError is not None and isinstance(error.__cause__, NotFoundError):
                return None
            raise
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            # The message names no bytes, so no file content reaches the logs.
            raise SandboxInfraError("Superserve read a file that is not UTF-8 text.") from None

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a new egress policy to the running box.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The box is not running, or Superserve refused the policy.
        """
        sandbox = self._require_running()
        with _mapped("change the egress policy"):
            await sandbox.update(network=_network_config(policy))

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Kill every Superserve box that carries the job tag.

        The sweep does not settle holds: the owner's ``stop()`` kills again
        (the kill is idempotent) and settles. A hold whose owner is gone
        stays pending with its box id.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of boxes that the sweep killed.
        """
        _require_sdk()
        with _mapped("list the job boxes"):
            boxes = await AsyncSandbox.list(metadata={JOB_TAG: job_id})
        stopped = 0
        for box in boxes:
            if getattr(box.status, "value", box.status) == "deleted":
                continue
            if await _bounded(AsyncSandbox.kill_by_id(box.id), f"kill a box of job {job_id}"):
                stopped += 1
        return stopped

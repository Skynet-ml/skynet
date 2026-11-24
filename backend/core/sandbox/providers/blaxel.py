"""The async Blaxel sandbox provider, for dev and evaluation only.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/blaxel.py: the
``SandboxInstance.create`` config (name, image, memory, ttl, network), the
egress allowlist with an unresolvable sentinel domain for a box with no
network, the ``bash -c`` command through ``process.exec`` and
``process.wait``, the kill of a command that runs past its time, the
``mkdir -p`` before a file write, ``fs.write_binary`` and ``fs.read_binary``,
the delete by name and the credentials preflight come from it. Changes: the
provider implements the SBX-6 async contract, the factory refuses it in
production (SKY-406 D1), a protected box reserves its whole funded lifetime
before the create and settles its measured runtime after the delete
(SKY-936), the ``ttl`` is the spec lifetime instead of a fixed 24 hours, the
memory comes from the vCPU count, each create gets a new unique name, a
failed create looks up its box by that name, a cancel never leaks a box or
a hold, and the job sweep stops the boxes that this process started.
Harbor's Dockerfile and compose builds, Docker-in-Docker, directory
transfer, region pinning and attach are left out.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import shlex
import uuid
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any, ClassVar

from core.billing.blaxel_usage import BlaxelUsageReservation, quote_blaxel_sandbox
from core.billing.budgets import BudgetError
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, resolve_resources
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from blaxel.core import (
        ResponseError,
        SandboxAPIError,
        SandboxCreationTimeoutError,
        SandboxInstance,
        get_credentials,
    )
except ImportError:
    ResponseError = None
    SandboxAPIError = None
    SandboxCreationTimeoutError = None
    SandboxInstance = None
    get_credentials = None

logger = logging.getLogger(__name__)

# Blaxel gives each 2048 MB of memory one vCPU and bills per GB of memory, so
# the memory size follows the vCPU count. Egress is included in the price,
# so a protected box may use every network mode. Blaxel sets the egress
# policy only at create time.
BLAXEL_CAPABILITIES = EnvironmentCapabilities(
    name="blaxel",
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=False,
    memory_mb_per_vcpu=2048,
    billable=True,
)

DEFAULT_IMAGE = "blaxel/base-image:latest"
_MB_PER_VCPU = 2048
# The Blaxel SDK default size: 2 vCPUs.
_DEFAULT_MEMORY_MB = 4096
# Blaxel refuses a ttl below 5 minutes, and SKY-936 caps it at 30 days.
_MIN_TTL_SEC = 5 * 60
_MAX_LIFETIME_SEC = 30 * 24 * 60 * 60
_NAME_PREFIX = "skynet-"
# Blaxel enforces egress through an allowlist, so a box with no network gets
# an allowlist with one domain that never resolves (from Harbor).
_NO_NETWORK_DOMAIN = "skynet-no-network.invalid"
# The Blaxel create docs (SDK 0.4.28) list these statuses as answers that make
# no box. 409 and 429 are left out: 409 means the unique name already has a
# box, and a 429 says nothing about an earlier attempt. A
# SandboxCreationTimeoutError (408) is left out too: the docs say the control
# plane releases its box, but not whether that box was billed. A refused
# create still releases its hold only when the name lookup finds no box.
_REFUSED_CREATE_STATUSES = frozenset({400, 401, 402, 403, 404, 413, 422})
_NOT_FOUND = 404
# The command's own server-side timeout only cleans up; the provider's wait
# ends first and kills the command.
_TIMEOUT_GRACE_SEC = 30
_WAIT_INTERVAL_MS = 1000
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124
# How long a cancelled start or stop waits for an open Blaxel call before the cancel passes on.
_CANCEL_GRACE_SEC = 0.5

# Process-local: the sweep sees only the boxes that this process started.
_JOB_BOXES: dict[str, set[BlaxelEnvironment]] = {}
# Strong references to the tasks that outlive their start or stop, so the
# event loop does not drop them.
_LATE_TASKS: set[asyncio.Future[Any]] = set()


class BlaxelEnvironment(SandboxEnvironment):
    """Run a sandbox as one Blaxel sandbox that Blaxel deletes at its ttl."""

    capabilities: ClassVar[EnvironmentCapabilities] = BLAXEL_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = BlaxelUsageReservation,
    ) -> None:
        """Create a stopped Blaxel sandbox.

        With a budget the box is protected: it reserves its whole funded
        lifetime before the create and settles its measured runtime after
        the delete.

        Args:
            spec: The sandbox to open. ``spec.image`` is a Blaxel sandbox
                image; ``None`` uses ``DEFAULT_IMAGE``.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` time limit. ``None`` means no default limit.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: The spec asks for a lifetime over 30 days or a
                disk size. A protected box also needs an operation key.
        """
        super().__init__(spec)
        if spec.lifetime_seconds > _MAX_LIFETIME_SEC:
            raise SandboxSpecError(f"The blaxel sandbox lives at most {_MAX_LIFETIME_SEC} seconds.")
        if spec.resources.disk_mb is not None and spec.resources.mode is not ResourceMode.IGNORE:
            raise SandboxSpecError("The blaxel sandbox cannot set a disk size.")
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._memory_mb = _memory_mb(spec)
        self._ttl_sec = max(_MIN_TTL_SEC, math.ceil(spec.lifetime_seconds))
        self._job_id = (spec.tags or {}).get(JOB_TAG)
        self._name: str | None = None
        self._sandbox: Any = None
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._stopped = False
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Blaxel sandbox needs an operation key.")
            try:
                quote_blaxel_sandbox(self._usage_request())
            except UnpricedOperationError as error:
                raise SandboxSpecError(str(error)) from error

    @property
    def usage(self) -> Any | None:
        """Return the usage reservation of a protected box, once it exists."""
        return self._usage

    @classmethod
    def preflight(cls) -> None:
        """Check that the Blaxel SDK and Blaxel credentials are present.

        Raises:
            SandboxFatalError: The ``blaxel`` package is missing, or no
                workspace credentials are set.
        """
        _require_sdk()
        credentials = get_credentials()
        if credentials is None or not getattr(credentials, "workspace", None):
            raise SandboxFatalError("The blaxel sandbox needs BL_WORKSPACE and BL_API_KEY, or a `bl login`.")

    async def start(self) -> None:
        """Create the Blaxel sandbox. Do nothing when the sandbox already runs.

        A protected box reserves its budget before the create. A create that
        fails or is cancelled with no sandbox releases or keeps the hold; a
        sandbox that a cancel deletes settles its usage. A cancel waits for
        the open call for at most ``_CANCEL_GRACE_SEC``, also through repeat
        cancels; a call that is still open then goes on in the background,
        so no box or hold leaks and the caller's start timeout stays bounded.
        A protected box cannot start again after it stopped.

        Raises:
            asyncio.CancelledError: The task was cancelled during the create.
            SandboxInfraError: The protected box already stopped.
        """
        if self._sandbox is not None:
            return
        if self._stopped and self._budget is not None:
            raise SandboxInfraError("The protected Blaxel sandbox is stopped; it cannot start again.")
        _require_sdk()
        usage = await self._reserve()
        name = f"{_NAME_PREFIX}{uuid.uuid4().hex}"
        create = asyncio.ensure_future(self._create(name, usage))
        try:
            sandbox = await asyncio.shield(create)
        except asyncio.CancelledError:
            await _wait_through_cancels(create, _CANCEL_GRACE_SEC)
            self._usage = None
            if not create.done():
                create.add_done_callback(functools.partial(_finish_late_create, name=name, usage=usage))
                raise
            if create.cancelled() or create.exception() is not None:
                refused = not create.cancelled() and _refused(create.exception())
                await _run_through_cancels(_fail_usage(usage, refused))
                raise
            await _run_through_cancels(_delete_and_settle(name, usage))
            raise
        except Exception as error:
            self._usage = None
            if await _run_through_cancels(_fail_usage(usage, _refused(error))):
                # A cancel is never swallowed; the create error stays as its cause.
                raise asyncio.CancelledError from error
            raise
        self._name = name
        self._sandbox = sandbox
        if self._job_id is not None:
            _JOB_BOXES.setdefault(self._job_id, set()).add(self)

    async def stop(self) -> None:
        """Delete the Blaxel sandbox.

        A protected box settles its usage after the delete, also when the
        delete fails. A cancel waits for the delete for at most
        ``_CANCEL_GRACE_SEC``, also through repeat cancels, then passes on;
        the delete and the settle go on in the background.

        Raises:
            asyncio.CancelledError: The task was cancelled during the delete.
            UsagePendingError: The usage could not settle; the hold stays pending.
            BudgetError: The usage broke the reserved bounds.
        """
        if self._sandbox is None or self._name is None:
            return
        usage, self._usage = self._usage, None
        name = self._name
        self._sandbox = None
        self._stopped = True
        boxes = _JOB_BOXES.get(self._job_id) if self._job_id is not None else None
        if boxes is not None:
            boxes.discard(self)
            if not boxes:
                del _JOB_BOXES[self._job_id]
        delete = asyncio.ensure_future(_delete_and_settle(name, usage))
        try:
            await asyncio.shield(delete)
        except asyncio.CancelledError:
            await _wait_through_cancels(delete, _CANCEL_GRACE_SEC)
            if not delete.done():
                _keep_late_task(delete)
            elif not delete.cancelled() and delete.exception() is not None:
                logger.exception(
                    "Could not stop a blaxel sandbox during a cancelled stop.", exc_info=delete.exception()
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
        """Run one bash command in the Blaxel sandbox and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory, or ``/``.
            env: Extra environment variables, on top of the spec env.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` once the command ends;
                ``None`` uses the sink of the current task.
            sudo: Ignored: the Blaxel sandbox API runs commands as root.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, it is killed and the result has exit code 124 and
            ``timed_out`` set.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        sandbox = self._require_running()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        sink = on_output or current_output_sink()
        process = await sandbox.process.exec(
            {
                # The process API runs a command string with /bin/sh; bash keeps the contract's shell.
                "command": f"bash -c {shlex.quote(command)}",
                "working_dir": cwd or "/",
                "env": {**(self.spec.env or {}), **(env or {})},
                "keep_alive": True,
                "timeout": 0 if limit is None else math.ceil(limit) + _TIMEOUT_GRACE_SEC,
            }
        )
        process_id = getattr(process, "pid", None) or getattr(process, "name", None)
        result = process
        if process_id:
            try:
                result = await sandbox.process.wait(
                    process_id,
                    max_wait=-1 if limit is None else math.ceil(limit * 1000),
                    interval=_WAIT_INTERVAL_MS,
                )
            except TimeoutError:
                try:
                    await sandbox.process.kill(process_id)
                except Exception:
                    logger.warning("Could not kill a blaxel command that ran past its time limit.", exc_info=True)
                return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        exit_code = getattr(result, "exit_code", None)
        if exit_code is None:
            exit_code = 0 if str(getattr(result, "status", "")) == "completed" else 1
        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        if sink is not None:
            for stream, text in (("stdout", stdout), ("stderr", stderr)):
                if text:
                    sink(stream, text)
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the sandbox, and make their parent directories first.

        Args:
            files: Maps each absolute sandbox path to its text content.

        Raises:
            RuntimeError: The sandbox is not running.
            SandboxInfraError: The parent directories could not be made.
        """
        sandbox = self._require_running()
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {"", "."})
        if parents:
            result = await self.exec(f"mkdir -p {' '.join(shlex.quote(parent) for parent in parents)}")
            if not result.ok:
                raise SandboxInfraError(f"Could not make the blaxel upload directories: {result.stderr.strip()[:500]}")
        for path, text in files.items():
            await sandbox.fs.write_binary(path, text.encode("utf-8"))

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
        try:
            data = await sandbox.fs.read_binary(path)
        except Exception as error:
            if ResponseError is not None and isinstance(error, ResponseError) and error.status == _NOT_FOUND:
                return None
            raise
        return bytes(data).decode("utf-8")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every Blaxel sandbox of the job that this process started.

        Blaxel cannot list sandboxes by label, so the sweep uses a
        process-local registry. Each box stops through ``stop()``, so a
        protected box also settles.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandboxes that the sweep stopped.

        Raises:
            Exception: The first stop error that is not a usage or budget error.
        """
        envs = list(_JOB_BOXES.get(job_id, ()))
        results = await asyncio.gather(*(env.stop() for env in envs), return_exceptions=True)
        unexpected: BaseException | None = None
        for env, result in zip(envs, results, strict=True):
            if isinstance(result, UsagePendingError | BudgetError):
                # The hold stays pending for reconciliation; the sweep goes on.
                logger.warning("Blaxel sandbox %s usage stays pending: %s", env._name, type(result).__name__)
            elif isinstance(result, BaseException) and unexpected is None:
                unexpected = result
        if unexpected is not None:
            raise unexpected
        return len(envs)

    async def _reserve(self) -> Any | None:
        """Reserve the whole funded lifetime of a protected box.

        Returns:
            The usage reservation, or ``None`` for an unprotected box.
        """
        if self._budget is None:
            return None
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
                await _run_through_cancels(_fail_usage(reserving.result()))
            raise
        return self._usage

    def _usage_request(self) -> dict[str, Any]:
        """Build the usage request that the reservation prices.

        Returns:
            The ttl in milliseconds and the memory size of the box.
        """
        return {
            "image": self.spec.image or DEFAULT_IMAGE,
            "lifetime_ms": self._ttl_sec * 1000,
            "memory_mb": self._memory_mb,
        }

    def _create_config(self, name: str) -> dict[str, Any]:
        """Build the ``SandboxInstance.create`` config.

        Args:
            name: The unique sandbox name.

        Returns:
            The create config with the funded ttl and the egress policy.
        """
        config: dict[str, Any] = {
            "name": name,
            "image": self.spec.image or DEFAULT_IMAGE,
            "memory": self._memory_mb,
            # Blaxel deletes the box at its ttl, so the funded lifetime bounds the box.
            "ttl": f"{self._ttl_sec}s",
        }
        network = network_config(
            NetworkPolicy(disabled=self.spec.network_disabled, allowed_hosts=self.spec.allowed_hosts)
        )
        if network is not None:
            config["network"] = network
        return config

    async def _create(self, name: str, usage: Any | None) -> Any:
        """Create one sandbox, and confirm it to the usage reservation.

        Args:
            name: The unique sandbox name.
            usage: The usage reservation of a protected box, or ``None``.

        Returns:
            The Blaxel ``SandboxInstance``.
        """
        config = self._create_config(name)
        if usage is not None:
            usage.sent(name)
        sandbox = await SandboxInstance.create(config, safe=True)
        if usage is not None:
            try:
                await asyncio.to_thread(usage.confirm_created, sandbox)
            except BaseException:
                try:
                    await _delete_and_settle(name, usage)
                except Exception:
                    # The confirm error goes on; the ttl ends a leaked box.
                    logger.exception("Could not stop a blaxel sandbox whose usage was not confirmed.")
                raise
        return sandbox

    def _require_running(self) -> Any:
        """Return the Blaxel sandbox, or raise when the sandbox is stopped.

        Returns:
            The Blaxel ``SandboxInstance``.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._sandbox is None:
            raise RuntimeError("The blaxel sandbox is not running.")
        return self._sandbox


def _memory_mb(spec: SandboxSpec) -> int:
    """Return the memory size that the create sends and the quote prices.

    Args:
        spec: The sandbox spec.

    Returns:
        2048 MB for each vCPU, the spec memory rounded up to whole vCPUs when
        the resources are ignored, or the SDK default size.
    """
    request = resolve_resources(spec.resources, BLAXEL_CAPABILITIES)
    if request.memory_mb is None:
        return _DEFAULT_MEMORY_MB
    # Blaxel derives whole vCPUs from the memory size, so a size between two
    # vCPU steps is sent and priced at the next step.
    return math.ceil(request.memory_mb / _MB_PER_VCPU) * _MB_PER_VCPU


def network_config(policy: NetworkPolicy) -> dict[str, Any] | None:
    """Map an egress policy to the Blaxel create ``network`` config.

    Args:
        policy: The egress policy.

    Returns:
        ``None`` for open egress, or an allowlist config. A box with no
        network gets an allowlist with one domain that never resolves.
    """
    if policy.disabled:
        return {"allowedDomains": [_NO_NETWORK_DOMAIN], "proxy": {"routing": []}}
    if not policy.allowed_hosts:
        return None
    hosts = list(policy.allowed_hosts)
    return {"allowedDomains": hosts, "proxy": {"routing": [], "bypass": hosts}}


def _require_sdk() -> None:
    """Raise when the Blaxel SDK is not installed.

    Raises:
        SandboxFatalError: The ``blaxel`` package is missing.
    """
    if SandboxInstance is None:
        raise SandboxFatalError("The blaxel sandbox needs the blaxel package: install the `blaxel` extra.")


def _status_code(error: BaseException | None) -> int | None:
    """Return the HTTP status of a Blaxel API error.

    Args:
        error: The error, or ``None``.

    Returns:
        The status, or ``None`` when the error is not a ``SandboxAPIError``.
    """
    if SandboxAPIError is None or not isinstance(error, SandboxAPIError):
        return None
    return getattr(error, "status_code", None)


def _refused(error: BaseException | None) -> bool:
    """Return True when Blaxel refused the create with an answer that makes no box.

    Args:
        error: The create error.

    Returns:
        True for a ``SandboxAPIError`` with a refused status that is not a creation timeout.
    """
    if SandboxCreationTimeoutError is not None and isinstance(error, SandboxCreationTimeoutError):
        return False
    return _status_code(error) in _REFUSED_CREATE_STATUSES


async def _delete(name: str) -> None:
    """Delete one Blaxel sandbox by name; a box that is already gone counts as deleted.

    Args:
        name: The sandbox name.
    """
    try:
        await SandboxInstance.delete(name)
    except Exception as error:
        if _status_code(error) != _NOT_FOUND:
            raise


async def _delete_and_settle(name: str, usage: Any | None) -> None:
    """Delete one Blaxel sandbox, then settle its usage.

    A delete that fails still settles a confirmed box: the usage then bills
    the whole funded lifetime, because Blaxel deletes the box at its ttl.

    Args:
        name: The sandbox name.
        usage: The usage reservation of a protected box, or ``None``.
    """
    try:
        await _delete(name)
    except BaseException:
        if usage is not None:
            try:
                await asyncio.to_thread(usage.settle)
            except Exception:
                # The delete error goes on; the settle error keeps the hold pending.
                logger.exception("Could not settle the usage of a blaxel sandbox whose delete failed.")
        raise
    if usage is not None:
        usage.stopped()
        await asyncio.to_thread(usage.settle)


async def _find_box(name: str) -> tuple[Any | None, bool]:
    """Look up a Blaxel sandbox by name after a failed create.

    Args:
        name: The sandbox name that the create sent.

    Returns:
        The box or ``None``, and True when the answer is certain: a box, or a 404.
    """
    try:
        return await SandboxInstance.get(name), True
    except Exception as error:
        if _status_code(error) == _NOT_FOUND:
            return None, True
        logger.exception("Could not look up a blaxel sandbox after a failed create; its hold stays pending.")
        return None, False


async def _fail_usage(usage: Any | None, refused: bool = False) -> None:
    """Release or keep the hold of a protected box whose create returned no sandbox.

    A sent create may still have made a box. A lookup by its unique name
    finds it, and the box is deleted and settled. The hold is released only
    when the create was never sent, or when Blaxel refused it and the lookup
    answered 404. Otherwise it stays pending with the box name as evidence,
    and the ttl ends a box that Blaxel makes later.

    Args:
        usage: The usage reservation, or ``None``.
        refused: True when Blaxel refused the create with an answer that makes no box.
    """
    if usage is None:
        return
    absent = False
    if usage.sent_at is not None and usage.sandbox_id is None:
        box, certain = await _find_box(usage.sandbox_name)
        if box is not None:
            try:
                await asyncio.to_thread(usage.confirm_created, box)
            except Exception:
                logger.exception("Could not confirm a blaxel sandbox that a failed create made.")
            try:
                await _delete_and_settle(usage.sandbox_name, usage)
            except Exception:
                logger.exception("Could not stop a blaxel sandbox that a failed create made.")
            return
        absent = refused and certain
    try:
        await asyncio.to_thread(usage.fail, absent)
    except Exception:
        logger.exception("Could not release the usage hold of a blaxel sandbox that was never created.")


async def _run_through_cancels(cleanup_coro: Any) -> bool:
    """Run a cleanup so that a cancel cannot stop it halfway.

    The cleanup waits at most ``_CANCEL_GRACE_SEC``, also through repeat
    cancels, then goes on in the background, so the caller stays bounded.

    Args:
        cleanup_coro: The cleanup coroutine.

    Returns:
        True when a cancel arrived during the wait, so the caller must raise it.
    """
    cleanup = asyncio.ensure_future(cleanup_coro)
    cancelled = await _wait_through_cancels(cleanup, _CANCEL_GRACE_SEC)
    if not cleanup.done():
        _keep_late_task(cleanup)
    elif not cleanup.cancelled() and cleanup.exception() is not None:
        logger.error("Could not clean up a blaxel sandbox.", exc_info=cleanup.exception())
    return cancelled


async def _wait_through_cancels(task: asyncio.Future[Any], grace_sec: float) -> bool:
    """Wait until the task ends or the grace time passes, and absorb each cancel meanwhile.

    The task itself is never cancelled, because Blaxel may already have done
    the work on its side.

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
        logger.exception("Could not release a blaxel usage hold that arrived after a cancelled start.")


def _finish_late_create(create: asyncio.Future[Any], *, name: str, usage: Any | None) -> None:
    """Delete the box of a create that ended after its start was cancelled, or settle its hold.

    A done callback must never raise, so every error goes to the log.

    Args:
        create: The finished create task.
        name: The sandbox name that the create sent.
        usage: The usage reservation of a protected box, or ``None``.
    """
    try:
        if create.cancelled() or create.exception() is not None:
            refused = not create.cancelled() and _refused(create.exception())
            _keep_late_task(asyncio.ensure_future(_fail_usage(usage, refused)))
            return
        _keep_late_task(asyncio.ensure_future(_delete_and_settle(name, usage)))
    except Exception:
        logger.exception("Could not stop a blaxel sandbox that arrived after a cancelled start.")


def _keep_late_task(task: asyncio.Future[Any]) -> None:
    """Keep a background task alive until it ends, and log its error then.

    Args:
        task: The running task.
    """
    # The loop holds tasks only by weak reference, so keep this one until it ends.
    _LATE_TASKS.add(task)
    task.add_done_callback(_finish_late_task)


def _finish_late_task(task: asyncio.Future[Any]) -> None:
    """Forget a finished late task and log its error.

    Args:
        task: The finished task.
    """
    _LATE_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("A background blaxel cleanup failed.", exc_info=task.exception())

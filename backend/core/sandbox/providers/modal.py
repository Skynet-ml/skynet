"""The async Modal sandbox provider, for dev and evaluation only.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/modal.py: the
``Sandbox.create`` call, the CPU and memory request and limit tuples, the GPU
string, the egress allowlist split into domains and CIDRs, the dynamic
network policy, the exit-137 out-of-memory check and the terminate flow come
from it. Changes: the provider implements the SBX-6 async contract, the SDK
calls never retry (SKY-406 D3, D10), the factory refuses the provider in
production (SKY-406 D1), and the job sweep lists sandboxes by the job tag.
Harbor's Docker-in-Docker compose mode, SSH streaming, volumes, Dockerfile
and registry builds, directory transfer and region pinning are left out.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import math
import os
import re
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from core.billing.modal_usage import ModalUsageReservation, quote_modal_sandbox
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.usage_tags import BOX_KEY

from ..capabilities import MODAL_GPU_TYPES, EnvironmentCapabilities, NetworkMode, SandboxSpecError, modal_gpu_name
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from modal import App, Image, Sandbox, Secret
except ImportError:
    App = None
    Image = None
    Sandbox = None
    Secret = None

try:
    from modal.config import config as modal_config
except ImportError:
    modal_config = None

try:
    from modal.exception import ExecTimeoutError, SandboxFilesystemNotFoundError
except ImportError:
    ExecTimeoutError = None
    SandboxFilesystemNotFoundError = None

try:
    from modal.exception import AuthError, PermissionDeniedError, UnimplementedError
except ImportError:
    AuthError = None
    PermissionDeniedError = None
    UnimplementedError = None

logger = logging.getLogger(__name__)

# Modal filters egress by domain and CIDR and can replace the policy of a
# running sandbox, so all three modes work. Modal sets no hard vCPU limit here.
# A box with a budget meters its runtime against the run budget (SKY-546).
MODAL_CAPABILITIES = EnvironmentCapabilities(
    name="modal",
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
    gpus=True,
    gpu_types=True,
    billable=True,
)

DEFAULT_IMAGE = "python:3.11-slim"
DEFAULT_APP_NAME = "skynet-sandbox"
# Modal ends every sandbox after 24 hours.
_MAX_LIFETIME_SEC = 24 * 60 * 60
_DEFAULT_CPU_REQUEST_CORES = 0.125
_DEFAULT_MEMORY_REQUEST_MB = 128
_PROTECTED_EGRESS_REFUSAL = (
    "A protected Modal sandbox cannot open egress: Modal neither caps nor reports the egress of one sandbox."
)
# A protected box must name its memory so its bill has a ceiling; with no
# memory in the spec it gets 2 GiB for each core.
_PROTECTED_MEMORY_MB_PER_CORE = 2048
_OOM_EXIT_CODE = 137
# Modal's own exec timeout ends the command first; this bound only stops a
# lost connection from blocking exec past the limit.
_TIMEOUT_GRACE_SEC = 30.0
# How long a cancelled start waits for an open create before the cancel passes on.
_CANCEL_GRACE_SEC = 0.5
# Modal's wait() returns -1 instead of raising when its exec deadline passes.
_MODAL_TIMEOUT_EXIT_CODE = -1
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124

_NOT_FOUND_ERRORS: tuple[type[BaseException], ...] = tuple(
    error for error in (SandboxFilesystemNotFoundError, FileNotFoundError) if error is not None
)
_EXEC_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = tuple(
    error for error in (ExecTimeoutError,) if error is not None
)

# Sandbox.list sends include_finished=False and has no option to change it
# (modal 1.6.3 sandbox/_sandbox.py, Sandbox._list), so an empty lookup misses
# a box that already ended. An empty lookup therefore never releases a hold
# alone; only a create that Modal refused does. These exact error types count
# as a refusal only on the V2 create, which modal 1.6.3 uses when the create
# has no GPU and the "sandbox_v2" setting is on (sandbox/_sandbox.py,
# impl_class_for_create_args). That create raises them from a definite status
# of its one SandboxCreateV2 call, so no box exists. The call retries transient
# errors with one idempotency key (_utils/grpc_utils.py, _retry_transient_errors),
# so a retry after a first attempt made the box can answer ALREADY_EXISTS or
# NOT_FOUND: those types never count. InvalidError never counts either: after
# the call, Sandbox._new_hydrated (_object.py) raises it for an id without the
# "sb-" prefix, when the box exists. The V1 create can raise any error from its
# task-id poll after SandboxCreate made the box. ServiceError, InternalError,
# timeouts and subclasses never count.
_REFUSAL_ERRORS: frozenset[type[BaseException]] = frozenset(
    error for error in (AuthError, PermissionDeniedError, UnimplementedError) if error is not None
)
# A failed create may still make its box late, so the lookup that can stop
# a running box is repeated once. 30 s is an estimate, not a measured bound:
# a box that Modal makes later than this is the documented residual case, and
# it runs until its timeout while its hold stays pending.
_RECHECK_DELAY_SEC = 30.0

# modal 1.6.3 refuses other env names (sandbox/_common.py, _SECRET_KEYNAME_REGEX).
# The check runs here, before any hold, because the V2 create does not check
# the keys of a Secret.from_dict and a server refusal of the sent create would
# leave the hold pending.
_ENV_KEY = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_LATE_TERMINATES: set[asyncio.Future[None]] = set()


class ModalEnvironment(SandboxEnvironment):
    """Run a sandbox as one Modal sandbox that sleeps until commands arrive."""

    capabilities: ClassVar[EnvironmentCapabilities] = MODAL_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        app_name: str = DEFAULT_APP_NAME,
        idle_timeout_sec: int | None = None,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = ModalUsageReservation,
    ) -> None:
        """Create a stopped Modal sandbox.

        With a budget the box is protected: it reserves its whole lifetime
        before the create, pins its CPU and memory requests to their limits,
        and settles its measured runtime after it stops.

        Args:
            spec: The sandbox to open. ``spec.image`` is a registry image;
                ``None`` uses ``DEFAULT_IMAGE``.
            app_name: The Modal app that owns the sandboxes.
            idle_timeout_sec: Modal ends the sandbox after this many idle
                seconds; ``None`` keeps the Modal default.
            timeouts: The phase timeouts; the run phase is the default
                ``exec`` time limit. ``None`` means no default limit.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: The spec asks for a lifetime, a disk size or a
                GPU type that Modal cannot apply, for an env name that Modal
                refuses, or for another unsupported feature. A protected box also needs an operation key and a
                priced size, and cannot use a GPU or any egress.
        """
        super().__init__(spec)
        if spec.lifetime_seconds > _MAX_LIFETIME_SEC:
            raise SandboxSpecError(f"The modal sandbox lives at most {_MAX_LIFETIME_SEC} seconds.")
        if spec.resources.disk_mb is not None and spec.resources.mode is not ResourceMode.IGNORE:
            raise SandboxSpecError("The modal sandbox cannot set a disk size.")
        modal_gpu_type(spec)
        bad_keys = sorted(key for key in (spec.env or {}) if not _ENV_KEY.match(key))
        if bad_keys:
            raise SandboxSpecError(f"The modal sandbox refuses these env names: {', '.join(bad_keys)}.")
        self._app_name = app_name
        self._idle_timeout_sec = idle_timeout_sec
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._sandbox: Any = None
        # Modal reports an explicit terminate as exit 137 too, so a failure
        # after our own terminate must not read as an out-of-memory kill.
        self._termination_requested = False
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._stopped = False
        # True while the last create used the V2 path, where an exact refusal proves no box.
        self._v2_create = False
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Modal sandbox needs an operation key.")
            if not spec.network_disabled or spec.allowed_hosts:
                raise SandboxSpecError(_PROTECTED_EGRESS_REFUSAL)
            try:
                quote_modal_sandbox(self._usage_request())
            except UnpricedOperationError as error:
                raise SandboxSpecError(str(error)) from error

    @property
    def usage(self) -> Any | None:
        """Return the usage reservation of a protected box, once it exists."""
        return self._usage

    @classmethod
    def preflight(cls) -> None:
        """Check that the Modal SDK and Modal credentials are present.

        Raises:
            SandboxFatalError: The ``modal`` package is missing, or no token
                is in the environment or in ``~/.modal.toml``.
        """
        _require_sdk()
        has_env_token = bool(os.environ.get("MODAL_TOKEN_ID")) and bool(os.environ.get("MODAL_TOKEN_SECRET"))
        if not has_env_token and not (Path.home() / ".modal.toml").is_file():
            raise SandboxFatalError(
                "The modal sandbox needs MODAL_TOKEN_ID and MODAL_TOKEN_SECRET, or a ~/.modal.toml file."
            )

    async def start(self) -> None:
        """Create the Modal sandbox. Do nothing when the sandbox already runs.

        A cancel during the create waits for the create for at most
        ``_CANCEL_GRACE_SEC``, also through repeat cancels. A sandbox that
        the create returns in that time gets a terminate, and the cancel
        waits for that terminate for at most ``_CANCEL_GRACE_SEC`` more. A
        create or terminate that is still open after its grace time goes on
        in the background, so no sandbox leaks and the caller's start
        timeout stays bounded.

        A protected box reserves its budget before the create. A create
        that fails or is cancelled with no sandbox releases or keeps the hold;
        a sandbox that a cancel terminates settles its usage. A protected box
        cannot start again after it stopped, because its hold is settled.

        Raises:
            asyncio.CancelledError: The task was cancelled during the create.
            SandboxInfraError: The protected box already stopped.
        """
        if self._sandbox is not None:
            return
        if self._stopped and self._budget is not None:
            raise SandboxInfraError("The protected Modal sandbox is stopped; it cannot start again.")
        _require_sdk()
        usage = await self._reserve()
        create = asyncio.ensure_future(self._create(usage))
        try:
            sandbox = await asyncio.shield(create)
        except asyncio.CancelledError:
            await _wait_through_cancels(create, _CANCEL_GRACE_SEC)
            self._usage = None
            if not create.done():
                create.add_done_callback(
                    functools.partial(
                        _terminate_late_sandbox, usage=usage, app_name=self._app_name, refused=self._refused
                    )
                )
                raise
            if create.cancelled() or create.exception() is not None:
                refused = not create.cancelled() and self._refused(create.exception())
                await _fail_usage_through_cancels(usage, self._app_name, refused)
                raise
            terminate = asyncio.ensure_future(_terminate_and_settle(create.result(), usage))
            await _wait_through_cancels(terminate, _CANCEL_GRACE_SEC)
            if not terminate.done():
                _keep_late_terminate(terminate)
                raise
            if not terminate.cancelled() and terminate.exception() is not None:
                # The cancel must still reach the caller; the sweep can stop a leaked box.
                logger.exception(
                    "Could not stop a modal sandbox after a cancelled start.", exc_info=terminate.exception()
                )
            raise
        except Exception as error:
            self._usage = None
            if await _fail_usage_through_cancels(usage, self._app_name, self._refused(error)):
                # A cancel is never swallowed; the create error stays as its cause.
                raise asyncio.CancelledError from error
            raise
        self._sandbox = sandbox
        self._termination_requested = False

    async def stop(self) -> None:
        """Terminate the Modal sandbox and wait for it to end.

        A cancel during the terminate waits for the terminate for at most
        ``_CANCEL_GRACE_SEC``, also through repeat cancels, then passes the
        cancel on. A terminate that is still open after the grace time goes
        on in the background, so the caller's stop timeout stays bounded.

        A protected box settles its usage after the terminate, also when the
        terminate fails.

        Raises:
            asyncio.CancelledError: The task was cancelled during the terminate.
            UsagePendingError: The usage could not settle; the hold stays pending.
            BudgetError: The usage broke the reserved bounds.
        """
        sandbox = self._sandbox
        if sandbox is None:
            return
        usage, self._usage = self._usage, None
        self._sandbox = None
        self._stopped = True
        self._termination_requested = True
        terminate = asyncio.ensure_future(_terminate_and_settle(sandbox, usage))
        try:
            await asyncio.shield(terminate)
        except asyncio.CancelledError:
            await _wait_through_cancels(terminate, _CANCEL_GRACE_SEC)
            if not terminate.done():
                _keep_late_terminate(terminate)
                raise
            if not terminate.cancelled() and terminate.exception() is not None:
                logger.exception(
                    "Could not stop a modal sandbox during a cancelled stop.", exc_info=terminate.exception()
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
        """Run one bash command in the Modal sandbox.

        Args:
            command: The shell command.
            cwd: The working directory, or the image default.
            env: Extra environment variables, sent as a Modal secret.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` chunks while the command
                runs; ``None`` uses the sink of the current task.
            sudo: Ignored: Modal runs commands as root.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, the result has exit code 124 and ``timed_out`` set.

        Raises:
            RuntimeError: The sandbox is not running.
            SandboxInfraError: The sandbox died with exit 137, likely out of memory.
        """
        sandbox = self._require_running()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        sink = on_output or current_output_sink()
        try:
            async with asyncio.timeout(None if limit is None else limit + _TIMEOUT_GRACE_SEC) as deadline:
                exit_code, stdout, stderr = await self._run(sandbox, command, cwd, env, limit, sink)
        except Exception as exc:
            # Modal's ExecTimeoutError derives from modal.exception.Error, not
            # from the builtin TimeoutError, so it needs its own check.
            timed_out = isinstance(exc, _EXEC_TIMEOUT_ERRORS) or (isinstance(exc, TimeoutError) and deadline.expired())
            if limit is not None and timed_out:
                return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
            await self._raise_if_out_of_memory(exc)
            raise
        if limit is not None and exit_code == _MODAL_TIMEOUT_EXIT_CODE:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, stdout=stdout, stderr=stderr, timed_out=True)
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the sandbox. Modal creates the parent directories.

        Args:
            files: Maps each absolute sandbox path to its text content.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        sandbox = self._require_running()
        with tempfile.TemporaryDirectory(prefix="skynet-modal-") as tmp:
            for index, (path, text) in enumerate(files.items()):
                local = Path(tmp) / str(index)
                local.write_bytes(text.encode("utf-8"))
                await sandbox.filesystem.copy_from_local.aio(local, path)

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
        with tempfile.TemporaryDirectory(prefix="skynet-modal-") as tmp:
            local = Path(tmp) / "file"
            try:
                await sandbox.filesystem.copy_to_local.aio(path, local)
            except _NOT_FOUND_ERRORS:
                return None
            # Bytes, not read_text: universal newlines would turn "\r\n" into "\n".
            return local.read_bytes().decode("utf-8")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str, *, app_name: str = DEFAULT_APP_NAME) -> int:
        """Terminate every Modal sandbox of the app that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.
            app_name: The Modal app that owns the sandboxes.

        Returns:
            The number of sandboxes that the sweep stopped.
        """
        _require_sdk()
        app = await App.lookup.aio(app_name, create_if_missing=True)
        stopped = 0
        async for sandbox in Sandbox.list.aio(app_id=app.app_id, tags={JOB_TAG: job_id}):
            try:
                await _terminate(sandbox)
            except Exception:
                logger.exception("Could not stop a modal sandbox of job %s.", job_id)
                continue
            stopped += 1
        return stopped

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Replace the egress policy of the running sandbox.

        Args:
            policy: The checked policy.

        Raises:
            RuntimeError: The sandbox is not running.
            SandboxSpecError: The box is protected and the policy opens egress.
        """
        if self._budget is not None and not policy.disabled:
            raise SandboxSpecError(_PROTECTED_EGRESS_REFUSAL)
        sandbox = self._require_running()
        await sandbox._experimental_set_outbound_network_policy.aio(**egress_kwargs(policy))

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
            # its result instead of leaking the hold. A slow database must not
            # hold the cancel, so a late result is released in the background.
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
            The lifetime and size of the box.
        """
        spec = self.spec
        request = spec.resources
        memory = request.memory_mb if request.memory_mb is not None else request.cpus * _PROTECTED_MEMORY_MB_PER_CORE
        return {
            "image": spec.image or DEFAULT_IMAGE,
            "lifetime_ms": math.ceil(spec.lifetime_seconds) * 1000,
            "cpu_cores": request.cpus,
            "memory_mib": memory,
            "gpu": bool(request.gpu or spec.gpu_type),
        }

    def _refused(self, error: BaseException | None) -> bool:
        """Return True when Modal refused the last create, so it surely made no box.

        Args:
            error: The create error.

        Returns:
            True for an exact refusal error type on the V2 create.
        """
        return self._v2_create and type(error) in _REFUSAL_ERRORS

    async def _create(self, usage: Any | None = None) -> Any:
        """Look up the app and create one sandbox that sleeps until it is stopped.

        Args:
            usage: The usage reservation of a protected box, or ``None``.

        Returns:
            The Modal sandbox.
        """
        spec = self.spec
        app = await App.lookup.aio(self._app_name, create_if_missing=True)
        policy = self._network_policy or NetworkPolicy(disabled=True)
        kwargs: dict[str, Any] = {**resource_kwargs(spec), **egress_kwargs(policy)}
        if usage is not None:
            # Modal bills max(request, usage), so equal requests and limits make
            # the priced size the billed size.
            request = self._usage_request()
            kwargs["cpu"] = (request["cpu_cores"], request["cpu_cores"])
            kwargs["memory"] = (request["memory_mib"], request["memory_mib"])
        if spec.tags:
            kwargs["tags"] = dict(spec.tags)
        if spec.env:
            kwargs["secrets"] = [Secret.from_dict(dict(spec.env))]
        if spec.name:
            kwargs["name"] = spec.name
        if self._idle_timeout_sec is not None:
            kwargs["idle_timeout"] = self._idle_timeout_sec
        image = Image.from_registry(spec.image or DEFAULT_IMAGE)
        if usage is not None:
            # The tag lets a failed create find the box it may have made.
            kwargs["tags"] = {**kwargs.get("tags", {}), BOX_KEY: usage.box}
            # Sandbox.create builds the image before its create call. A build
            # here makes a build failure an unsent create, which releases the hold.
            # Modal's docs do not say if a build is billed, so the clock counts it.
            usage.building()
            await image.build.aio(app)
            # Sandbox.create still does local work before its one RPC (option
            # checks, the auth token fetch). An error there made no box, but
            # nothing outside the SDK can tell it apart, so its hold stays pending.
            usage.sent()
        self._v2_create = kwargs.get("gpu") is None and bool(modal_config.get("sandbox_v2"))
        sandbox = await Sandbox.create.aio(
            "sleep",
            "infinity",
            app=app,
            image=image,
            timeout=math.ceil(spec.lifetime_seconds),
            **kwargs,
        )
        if usage is not None:
            try:
                await asyncio.to_thread(usage.confirm_created, sandbox)
            except BaseException:
                try:
                    await _terminate_and_settle(sandbox, usage)
                except Exception:
                    # The confirm error goes on; the sweep can stop a leaked box.
                    logger.exception("Could not stop a modal sandbox whose usage was not confirmed.")
                raise
        return sandbox

    async def _run(
        self,
        sandbox: Any,
        command: str,
        cwd: str | None,
        env: Mapping[str, str] | None,
        limit: float | None,
        sink: OutputSink | None,
    ) -> tuple[int, str, str]:
        """Start one command, collect both streams and wait for the exit code.

        Args:
            sandbox: The running Modal sandbox.
            command: The shell command.
            cwd: The working directory, or ``None``.
            env: Extra environment variables, or ``None``.
            limit: The time limit in seconds, or ``None``.
            sink: Receives output chunks, or ``None``.

        Returns:
            The exit code, the stdout text and the stderr text.
        """
        process = await sandbox.exec.aio(
            "bash",
            "-c",
            command,
            workdir=cwd,
            secrets=[Secret.from_dict(dict(env))] if env else [],
            timeout=None if limit is None else max(1, math.ceil(limit)),
        )
        try:
            async with asyncio.TaskGroup() as group:
                stdout_task = group.create_task(_collect(process.stdout, "stdout", sink))
                stderr_task = group.create_task(_collect(process.stderr, "stderr", sink))
        except BaseExceptionGroup as errors:
            # The task group cancels the other reader; the caller expects the
            # reader's own error, not the group.
            raise errors.exceptions[0] from None
        stdout, stderr = stdout_task.result(), stderr_task.result()
        exit_code = await process.wait.aio()
        return exit_code, stdout, stderr

    async def _raise_if_out_of_memory(self, cause: BaseException) -> None:
        """Raise an infra error when a failed SDK call comes from a sandbox that died with 137.

        Args:
            cause: The error of the failed SDK call.

        Raises:
            SandboxInfraError: The sandbox ended with exit 137 and nobody asked
                for its termination.
        """
        sandbox = self._sandbox
        if sandbox is None or self._termination_requested:
            return
        try:
            exit_code = await sandbox.poll.aio()
        except Exception:
            # The original error is the better signal when Modal cannot report a status.
            logger.debug("Could not read the modal sandbox status after a failed call.", exc_info=True)
            return
        if exit_code == _OOM_EXIT_CODE:
            memory = self.spec.resources.memory_mb
            raise SandboxInfraError(
                f"The modal sandbox likely ran out of memory (exit code 137, memory {memory or 'default'} MB)."
            ) from cause

    def _require_running(self) -> Any:
        """Return the Modal sandbox, or raise when the sandbox is stopped.

        Returns:
            The Modal sandbox.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._sandbox is None:
            raise RuntimeError("The modal sandbox is not running.")
        return self._sandbox


def modal_gpu_type(spec: SandboxSpec) -> str | None:
    """Return the spec's GPU type as the Modal name.

    Args:
        spec: The sandbox spec.

    Returns:
        The upper-case Modal GPU name, or ``None`` when the spec names no GPU type.

    Raises:
        SandboxSpecError: The type is empty or is not a Modal GPU name.
    """
    if spec.gpu_type is None:
        return None
    name = modal_gpu_name(spec.gpu_type)
    if name is None:
        raise SandboxSpecError(
            f"The modal sandbox has no {spec.gpu_type!r} GPU. Use one of: {', '.join(sorted(MODAL_GPU_TYPES))}."
        )
    return name


def resource_kwargs(spec: SandboxSpec) -> dict[str, Any]:
    """Map the requested sizes to the Modal ``cpu``, ``memory`` and ``gpu`` arguments.

    Args:
        spec: The sandbox spec.

    Returns:
        The keyword arguments for ``Sandbox.create``.

    Raises:
        SandboxSpecError: The GPU type is not a Modal GPU name.
    """
    request = spec.resources
    kwargs: dict[str, Any] = {}
    gpu_type = modal_gpu_type(spec)
    if gpu_type is not None:
        # A named type with no count asks for one GPU of that type.
        kwargs["gpu"] = f"{gpu_type}:{request.gpu}" if request.gpu else gpu_type
    elif request.gpu:
        kwargs["gpu"] = f"any:{request.gpu}"
    if request.mode is ResourceMode.IGNORE:
        return kwargs
    cpus = request.cpus
    if request.mode is ResourceMode.REQUEST:
        kwargs["cpu"] = cpus
    elif request.mode is ResourceMode.LIMIT:
        # A small scheduling request with a hard ceiling at the requested size.
        kwargs["cpu"] = (min(_DEFAULT_CPU_REQUEST_CORES, cpus), cpus)
    else:
        kwargs["cpu"] = (cpus, cpus)
    memory = request.memory_mb
    if memory is not None:
        if request.mode in (ResourceMode.AUTO, ResourceMode.REQUEST):
            kwargs["memory"] = memory
        elif request.mode is ResourceMode.LIMIT:
            kwargs["memory"] = (min(_DEFAULT_MEMORY_REQUEST_MB, memory), memory)
        else:
            kwargs["memory"] = (memory, memory)
    return kwargs


def egress_kwargs(policy: NetworkPolicy) -> dict[str, list[str]]:
    """Map an egress policy to the Modal domain and CIDR allowlists.

    Every mode sends both lists, so the sandbox stays in Modal's allowlist
    mode and a later policy change can always replace it.

    Args:
        policy: The egress policy.

    Returns:
        The ``outbound_domain_allowlist`` and ``outbound_cidr_allowlist``
        arguments.
    """
    if policy.disabled:
        return {"outbound_domain_allowlist": [], "outbound_cidr_allowlist": []}
    if not policy.allowed_hosts:
        return {"outbound_domain_allowlist": ["*"], "outbound_cidr_allowlist": ["0.0.0.0/0"]}
    domains: list[str] = []
    cidrs: list[str] = []
    for host in policy.allowed_hosts:
        try:
            cidrs.append(str(ipaddress.ip_network(host, strict=False)))
        except ValueError:
            domains.append(host)
    return {"outbound_domain_allowlist": domains, "outbound_cidr_allowlist": cidrs}


def _require_sdk() -> None:
    """Raise when the Modal SDK is not installed.

    Raises:
        SandboxFatalError: The ``modal`` package is missing.
    """
    if Sandbox is None:
        raise SandboxFatalError("The modal sandbox needs the modal package: install the `modal` extra.")


async def _terminate(sandbox: Any) -> None:
    """Terminate one Modal sandbox and wait until it ends.

    Args:
        sandbox: The Modal sandbox.
    """
    await sandbox.terminate.aio()
    await sandbox.wait.aio(raise_on_termination=False)


async def _terminate_and_settle(sandbox: Any, usage: Any | None) -> None:
    """Terminate one Modal sandbox, then settle its usage.

    A terminate that fails still settles: the usage then bills the whole
    funded lifetime, because Modal ends the sandbox at its timeout.

    Args:
        sandbox: The Modal sandbox.
        usage: The usage reservation of a protected box, or ``None``.
    """
    try:
        await _terminate(sandbox)
    except BaseException:
        if usage is not None:
            try:
                await asyncio.to_thread(usage.settle)
            except Exception:
                # The terminate error goes on; the settle error keeps the hold pending.
                logger.exception("Could not settle the usage of a modal sandbox whose terminate failed.")
        raise
    if usage is not None:
        usage.stopped()
        await asyncio.to_thread(usage.settle)


async def _find_boxes(usage: Any, app_name: str) -> list[Any] | None:
    """Look up the running sandboxes that carry the box tag of a protected box.

    Args:
        usage: The usage reservation of the box.
        app_name: The Modal app of the box.

    Returns:
        The running sandboxes, or ``None`` when the lookup failed.
    """
    try:
        app = await App.lookup.aio(app_name, create_if_missing=True)
        return [box async for box in Sandbox.list.aio(app_id=app.app_id, tags={BOX_KEY: usage.box})]
    except Exception:
        logger.exception("Could not look up a modal sandbox after a failed create; its hold stays pending.")
        return None


async def _fail_usage(usage: Any | None, app_name: str | None = None, refused: bool = False) -> None:
    """Release or keep the hold of a protected box whose create returned no sandbox.

    A sent create may still have made a sandbox. A lookup by the box tag
    finds a running box, which is terminated and settled. The lookup cannot
    see a box that already ended, so only a create that Modal refused
    releases the hold, also when its lookup fails. Otherwise the hold goes
    to pending, the lookup is repeated once after ``_RECHECK_DELAY_SEC`` to
    stop a box that appears late, and the hold stays pending.

    Args:
        usage: The usage reservation, or ``None``.
        app_name: The Modal app of the box; ``None`` skips the lookup and keeps a sent hold pending.
        refused: True when Modal refused the create, so no box exists.
    """
    if usage is None:
        return
    absent = False
    # A create that returned a handle sends its box to the terminate and
    # settle path, and fail() never releases its hold, so a lookup adds nothing.
    if app_name is not None and usage.sent_at is not None and usage.sandbox_id is None and not usage.returned:
        found = await _find_boxes(usage, app_name)
        if found == [] and not refused:
            try:
                await asyncio.to_thread(usage.pending)
            except Exception:
                logger.exception("Could not mark a modal usage hold pending before the second lookup.")
            await asyncio.sleep(_RECHECK_DELAY_SEC)
            found = await _find_boxes(usage, app_name)
        if found:
            try:
                await asyncio.to_thread(usage.confirm_created, found[0])
            except Exception:
                logger.exception("Could not confirm a modal sandbox that a failed create made.")
            for box in found:
                try:
                    await _terminate_and_settle(box, usage)
                except Exception:
                    logger.exception("Could not stop a modal sandbox that a failed create made.")
            return
        # A refused create made no box, so a failed lookup does not keep its hold.
        absent = refused
    try:
        await asyncio.to_thread(usage.fail, absent)
    except Exception:
        logger.exception("Could not release the usage hold of a modal sandbox that was never created.")


async def _fail_usage_through_cancels(usage: Any | None, app_name: str | None = None, refused: bool = False) -> bool:
    """Run :func:`_fail_usage` so that a cancel cannot stop it halfway.

    The cleanup waits at most ``_CANCEL_GRACE_SEC``, also through repeat
    cancels, then goes on in the background, so the caller stays bounded.

    Args:
        usage: The usage reservation, or ``None``.
        app_name: The Modal app of the box, for the lookup after a failed create.
        refused: True when Modal refused the create, so no box exists.

    Returns:
        True when a cancel arrived during the wait, so the caller must raise it.
    """
    cleanup = asyncio.ensure_future(_fail_usage(usage, app_name, refused))
    cancelled = await _wait_through_cancels(cleanup, _CANCEL_GRACE_SEC)
    if not cleanup.done():
        _keep_late_terminate(cleanup)
    elif not cleanup.cancelled() and cleanup.exception() is not None:
        logger.error("Could not clean up a failed modal create.", exc_info=cleanup.exception())
    return cancelled


async def _wait_through_cancels(task: asyncio.Future[Any], grace_sec: float) -> bool:
    """Wait until the task ends or the grace time passes, and absorb each cancel meanwhile.

    The task itself is never cancelled, because Modal may already have done
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
        _keep_late_terminate(asyncio.ensure_future(_fail_usage(reserving.result())))
    except Exception:
        logger.exception("Could not release a modal usage hold that arrived after a cancelled start.")


def _terminate_late_sandbox(
    create: asyncio.Future[Any],
    *,
    usage: Any | None = None,
    app_name: str | None = None,
    refused: Callable[[BaseException | None], bool] | None = None,
) -> None:
    """Terminate the sandbox of a create that ended after its start was cancelled.

    A done callback must never raise, so every error goes to the log.

    Args:
        create: The finished create task.
        usage: The usage reservation of a protected box, or ``None``.
        app_name: The Modal app of the box, for the lookup after a failed create.
        refused: Return True when Modal refused the create, so no box exists.
    """
    try:
        if create.cancelled() or create.exception() is not None:
            if usage is not None:
                was_refused = refused is not None and not create.cancelled() and refused(create.exception())
                _keep_late_terminate(asyncio.ensure_future(_fail_usage(usage, app_name, was_refused)))
            return
        terminate = asyncio.ensure_future(_terminate_and_settle(create.result(), usage))
    except Exception:
        logger.exception("Could not stop a modal sandbox that arrived after a cancelled start.")
        return
    _keep_late_terminate(terminate)


def _keep_late_terminate(terminate: asyncio.Future[None]) -> None:
    """Keep a background terminate alive until it ends, and log its error then.

    Args:
        terminate: The running terminate task.
    """
    # The loop holds tasks only by weak reference, so keep this one until it ends.
    _LATE_TERMINATES.add(terminate)
    terminate.add_done_callback(_finish_late_terminate)


def _finish_late_terminate(terminate: asyncio.Future[None]) -> None:
    """Forget a finished late terminate and log its error.

    Args:
        terminate: The finished terminate task.
    """
    _LATE_TERMINATES.discard(terminate)
    if not terminate.cancelled() and terminate.exception() is not None:
        logger.error("Could not stop a modal sandbox after a cancelled start or stop.", exc_info=terminate.exception())


async def _collect(stream: AsyncIterator[str], name: str, sink: OutputSink | None) -> str:
    """Read one output stream to its end and pass each chunk to the sink.

    Args:
        stream: The Modal stream reader.
        name: ``stdout`` or ``stderr``.
        sink: Receives ``(name, chunk)``, or ``None``.

    Returns:
        The whole stream text.
    """
    chunks: list[str] = []
    async for chunk in stream:
        chunks.append(chunk)
        if sink is not None:
            sink(name, chunk)
    return "".join(chunks)

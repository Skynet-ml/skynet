"""The async Sail sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), sail: the Sailbox create, the size
fit, the egress policy map, the bash command form and the file calls come
from ``harbor/environments/sail.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- A box with a budget is protected (SKY-554). It reserves its whole
  lifetime before the create, checks the created box against its funding,
  and settles Sail's reported spend after the terminate, also after a
  failed or cancelled start. The SDK retries a create under one
  idempotency key, so a create error alone does not prove that no box
  exists. After a failed create the provider looks up the box by its name,
  which gets a random suffix: a found box is terminated and settled, and the
  hold is released only when Sail clearly refused the create and the lookup
  finds no box. A failed start returns its error once the box is gone; the
  spend wait goes on in the stop task. Sail has no lifetime extension, so
  the box never extends its funding.
- The provider adds no retry of its own (SKY-406 D3, D10). An SDK error
  maps to ``SandboxInfraError``, because the SDK error classes are not known.
- The start and run phase timeouts bound the create and each command. A
  create that a timeout or a stop cancels gets a short grace to return its
  handle, as in Harbor, so the box can still be terminated. Unlike Harbor,
  a create that is still running when the grace ends, or when a second
  cancel comes, is not dropped: a callback terminates its box when the
  handle arrives.
- Every Sailbox gets ``max_lifetime_seconds`` from the spec lifetime, so Sail
  ends a box that a lost process never stops.
- The spec sizes map to ``Sailbox.size_for``. The spec image comes from a
  registry; ``DEFAULT_IMAGE`` is used when the spec names none.
- ``stop_job_sandboxes`` stops the Sailboxes that this process started for
  the job at the same time, so their spend waits overlap. Sail has no list
  call in the Harbor code, so boxes of other processes end only at their
  lifetime limit.
- Harbor's Dockerfile and compose builds, the DinD strategy, directory
  transfer, attach and sleep-on-stop are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import shlex
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

from core.billing.budgets import BudgetError
from core.billing.runtime import UsagePendingError
from core.billing.sail_usage import USAGE

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, network_mode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxError, SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from sail import App, Config, EgressPolicy, Image, Sailbox
except ImportError:
    App = None
    Config = None
    EgressPolicy = None
    Image = None
    Sailbox = None

logger = logging.getLogger(__name__)

APP_NAME = "skynet"
DEFAULT_IMAGE = "python:3.12.15-slim"
STOP_TIMEOUT = 60.0
# Harbor waits this long for a cancelled create, so a box made during the
# request still has a handle to terminate.
CREATE_GRACE = 30.0
# The SDK timeout should end a command first; this margin only catches a
# hung SDK call.
EXEC_GRACE = 30.0
OUTPUT_BUFFER_BYTES = 64 * 1024 * 1024
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124

SAIL_CAPABILITIES = EnvironmentCapabilities(
    name="sail",
    billable=True,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)

# Process-local: the sweep sees only the boxes that this process started.
_JOB_BOXES: dict[str, set[SailEnvironment]] = {}
# The Sail create docs list these statuses as answers that make no box. 429
# and 409 are left out: the SDK retries a create under one idempotency key,
# so an earlier attempt can still run or have made the box when a retry gets
# the 429 or the 409. A refused create still releases its hold only when the
# name lookup finds no box.
_REFUSED_CREATE_STATUSES = frozenset({400, 401, 402, 403, 404, 413, 422})
# InvalidArgumentError comes from the SDK's argument checks, and
# ImageBuildError from the image build, both before the create request. A
# TypeError is left out: it can also come from reading the answer after
# Sail made the box.
_REFUSED_CREATE_ERRORS = frozenset(
    {"InvalidArgumentError", "PermissionDeniedError", "NotFoundError", "ImageBuildError"}
)
# Strong references to the creates that outlive their start, and to the
# terminate calls of their late boxes, so the event loop does not drop them.
_LATE_TASKS: set[asyncio.Future[Any]] = set()


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
        raise SandboxInfraError(f"Sail could not {action}: {type(error).__name__}.") from error


def _refused_create(error: BaseException | None) -> bool:
    """Tell whether a create failure shows that Sail made no box.

    The SDK maps no HTTP status to an error class, so the check uses the
    class name and the ``status_code`` attribute. This check alone never
    frees a hold; the name lookup must also find no box.

    Args:
        error: The SDK error of the create, or ``None``.

    Returns:
        True when no box can exist for the create.
    """
    if error is None:
        return False
    if type(error).__name__ in _REFUSED_CREATE_ERRORS:
        return True
    return getattr(error, "status_code", None) in _REFUSED_CREATE_STATUSES


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
        logger.warning("Sail could not %s: %s", action, type(error).__name__)
        return False
    return True


def _terminate_late(create: asyncio.Future[Any]) -> None:
    """Terminate the box of a create that outlives its start, when it arrives.

    Args:
        create: The create task. It can still run or be done already.
    """
    _LATE_TASKS.add(create)
    create.add_done_callback(_on_late_create)


def _on_late_create(create: asyncio.Future[Any]) -> None:
    """Start the terminate call for the box of a late create.

    Args:
        create: The finished create task.
    """
    _LATE_TASKS.discard(create)
    if create.cancelled():
        return
    error = create.exception()
    if error is not None:
        logger.warning("A late Sailbox create failed: %s", type(error).__name__)
        return
    terminate = asyncio.ensure_future(_bounded(create.result().terminate.aio(), "terminate a late Sailbox"))
    _LATE_TASKS.add(terminate)
    terminate.add_done_callback(_LATE_TASKS.discard)


def _egress_policy(*, disabled: bool, allowed_hosts: tuple[str, ...]) -> Any:
    """Map a network policy to a Sail egress policy.

    Args:
        disabled: True when the box has no egress.
        allowed_hosts: The hosts that egress may reach; empty means any host.

    Returns:
        The Sail egress policy document.
    """
    mode = network_mode(disabled=disabled, allowed_hosts=allowed_hosts)
    if mode is NetworkMode.DENY_ALL:
        return EgressPolicy.no_network()
    if mode is NetworkMode.ALLOWLIST:
        return EgressPolicy.allow_only(*dict.fromkeys(allowed_hosts))
    return EgressPolicy.allow_all()


def _size_parameters(spec: SandboxSpec) -> dict[str, Any]:
    """Map the spec sizes to the Sail size arguments of the create call.

    Args:
        spec: The sandbox to open.

    Returns:
        The ``size``, ``memory_limit_gib`` and ``disk_limit_gib`` arguments, or
        nothing when the spec ignores sizes.
    """
    request = spec.resources
    if request.mode is ResourceMode.IGNORE:
        return {}
    fit = Sailbox.size_for(
        cpus=request.cpus,
        memory_gib=request.memory_mb / 1024 if request.memory_mb else None,
        disk_gib=request.disk_mb / 1024 if request.disk_mb else None,
    )
    return {"size": fit.size, "memory_limit_gib": fit.memory_limit_gib, "disk_limit_gib": fit.disk_limit_gib}


def _bash_argv(command: str, cwd: str | None) -> list[str]:
    """Build the argv that runs a shell command in a working directory.

    Args:
        command: The shell command.
        cwd: The working directory, or ``None``.

    Returns:
        The ``bash -c`` argv.
    """
    if cwd is not None:
        command = f"cd {shlex.quote(cwd)} && exec bash -c {shlex.quote(command)}"
    return ["bash", "-c", command]


class SailEnvironment(SandboxEnvironment):
    """Run commands in one Sailbox."""

    capabilities = SAIL_CAPABILITIES

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
            SandboxSpecError: The spec breaks a provider rule, or a protected
                box has no operation key.
        """
        if budget is not None and spec.operation_key is None:
            raise SandboxSpecError("A protected Sail sandbox needs an operation key.")
        super().__init__(spec)
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._billing_call: asyncio.Future[Any] | None = None
        self._box_gone = asyncio.Event()
        self._settle_error: Exception | None = None
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        # Sail lists old terminated boxes too, so the lookup after a failed
        # create needs a name that no earlier box of the same spec has.
        if spec.name is None:
            self._name = f"skynet-{uuid.uuid4().hex[:12]}"
        elif budget is not None:
            self._name = f"{spec.name}-{uuid.uuid4().hex[:8]}"
        else:
            self._name = spec.name
        self._job_id = spec.tags.get(JOB_TAG)
        self._sailbox: Any = None
        self._create_task: asyncio.Future[Any] | None = None
        self._starting: asyncio.Future[None] | None = None
        self._settling = False
        self._started = False
        self._terminated = False
        self._create_failed = False
        self._create_refused = False
        self._stop_task: asyncio.Future[None] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and the Sail credentials exist.

        Raises:
            SandboxFatalError: The SDK is not installed, or Sail finds no credentials.
        """
        if Sailbox is None:
            raise SandboxFatalError("The sail package is not installed: install the `sail` extra.")
        try:
            Config.from_env()
        except ValueError as error:
            raise SandboxFatalError("The Sail sandbox found no credentials in the environment.") from error

    async def start(self) -> None:
        """Create the Sailbox and wait until it runs.

        Any failure or cancel stops the box before the error goes on.

        Raises:
            StartTimeoutError: The Sailbox did not run within the start timeout.
            SandboxInfraError: A stop ended the start, the sandbox already
                started once, or the create call failed.
            SandboxFatalError: The SDK or the credentials are missing.
        """
        if self._started:
            raise SandboxInfraError("The Sail sandbox already started once; it cannot start again.")
        self._started = True
        # A stop before the start leaves a finished stop task; drop it so the
        # stop after this start terminates the box.
        self._stop_task = None
        self.preflight()
        self._starting = asyncio.ensure_future(self._create())
        try:
            await self._starting
        except BaseException as error:
            # Wait for the teardown but not through stop(): a settle failure
            # would hide the start error. It stays for a later stop() call.
            # The spend wait takes minutes, so the error returns once the box
            # is gone and the settle goes on in the stop task.
            if self._stop_task is None:
                self._stop_task = asyncio.ensure_future(self._teardown())
            gone = asyncio.ensure_future(self._box_gone.wait())
            try:
                await asyncio.wait({self._stop_task, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                gone.cancel()
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The Sail sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _create(self) -> None:
        """Find the Sail app, then create the Sailbox and keep its handle.

        Raises:
            StartTimeoutError: The Sailbox did not run within the start timeout.
            SandboxInfraError: An SDK call failed.
        """
        spec = self.spec
        limit = self._start_limit
        await self._reserve()
        try:
            async with asyncio.timeout(limit) as deadline:
                with _mapped("find the Sail app"):
                    app = await App.find.aio(APP_NAME, mint_if_missing=True)
                with _mapped("prepare the Sailbox"):
                    params: dict[str, Any] = {
                        "app": app,
                        "image": Image.from_registry(spec.image or DEFAULT_IMAGE),
                        "name": self._name,
                        "max_lifetime_seconds": math.ceil(spec.lifetime_seconds),
                        "egress_policy": _egress_policy(
                            disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts)
                        ),
                        **_size_parameters(spec),
                    }
                    if limit is not None:
                        params["image_build_timeout"] = math.ceil(limit)
                if self._usage is not None:
                    self._usage.begin_create()
                try:
                    with _mapped("create the Sailbox"):
                        # Inside the mapping, so an argument error that the SDK
                        # raises before any request is seen as a refused create.
                        self._create_task = asyncio.ensure_future(Sailbox.create.aio(**params))
                        # The shield keeps the create running through a cancel,
                        # so the grace below can still get the box handle.
                        self._keep(await asyncio.shield(self._create_task))
                except asyncio.CancelledError:
                    await self._settle_create()
                    raise
                except SandboxInfraError as error:
                    self._create_failed = True
                    self._create_refused = _refused_create(error.__cause__)
                    raise
                if self._usage is not None:
                    await self._confirm(self._usage)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(limit) from None

    async def _reserve(self) -> None:
        """Reserve the whole lifetime of a protected box before the create.

        Raises:
            BudgetReached: The budget cannot pay for the box.
            UsagePendingError: The box was already dispatched elsewhere.
        """
        if self._budget is None or self._usage is not None:
            return
        reserving = asyncio.ensure_future(
            asyncio.to_thread(
                self._usage_factory, self._budget, self._usage_request(), operation_key=self.spec.operation_key
            )
        )
        # The thread cannot be cancelled and may still reserve after a stop,
        # so the teardown waits for this call and settles what it returns.
        self._billing_call = reserving
        self._usage = await asyncio.shield(reserving)

    def _usage_request(self) -> dict[str, Any]:
        """Build the request that the reservation prices.

        Returns:
            The lifetime, the size arguments of the create call, the image and
            the network of the box.
        """
        spec = self.spec
        return {
            "lifetime_ms": math.ceil(spec.lifetime_seconds) * 1000,
            "size": None,
            "memory_limit_gib": None,
            "disk_limit_gib": None,
            **_size_parameters(spec),
            "image": spec.image or DEFAULT_IMAGE,
            "network_disabled": spec.network_disabled,
            "allowed_hosts": sorted(spec.allowed_hosts),
        }

    async def _confirm(self, usage: Any) -> None:
        """Check the created box against its funding.

        The create handle carries no limits, so the full box is read first.

        Args:
            usage: The usage reservation of the box.

        Raises:
            UsagePendingError: The box is larger than its funding.
            SandboxInfraError: Sail could not read the box.
        """
        with _mapped("read the Sailbox"):
            sailbox = await Sailbox.get.aio(self._sailbox.sailbox_id)
        await asyncio.to_thread(usage.confirm_created, sailbox)

    async def _settle_create(self) -> None:
        """Give a cancelled create ``CREATE_GRACE`` seconds to return the box.

        A create that is still running when the grace ends, or when a second
        cancel comes, goes to ``_terminate_late``. The server can still make
        the box after the client gives up, so a cancel of the create would
        lose the handle while the box runs until its lifetime limit.
        """
        task = self._create_task
        if task is None:
            return
        self._settling = True
        try:
            sailbox = await asyncio.wait_for(asyncio.shield(task), CREATE_GRACE)
        except asyncio.CancelledError:
            _terminate_late(task)
            logger.warning("A second cancel came during the Sailbox create grace.")
        except TimeoutError:
            _terminate_late(task)
            logger.warning("Sail did not return a cancelled Sailbox create within the grace.")
        except Exception as error:
            # The failure does not prove that no box exists, so the settle
            # looks the box up by its name. It is never a refusal.
            self._create_failed = True
            logger.warning("A cancelled Sailbox create failed: %s", type(error).__name__)
        else:
            self._keep(sailbox)
        finally:
            self._settling = False

    def _keep(self, sailbox: Any) -> None:
        """Keep the Sailbox handle and list the box under its job.

        Args:
            sailbox: The SDK Sailbox handle.
        """
        self._sailbox = sailbox
        if self._job_id is not None:
            _JOB_BOXES.setdefault(self._job_id, set()).add(self)

    def _require_sailbox(self) -> Any:
        """Return the Sailbox handle.

        Returns:
            The SDK Sailbox handle.

        Raises:
            SandboxInfraError: The Sailbox is not running.
        """
        if self._sailbox is None:
            raise SandboxInfraError("The Sail sandbox is not running.")
        return self._sailbox

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
        """Run one shell command in the Sailbox and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory; the Sailbox default by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` pairs. Sail does not
                stream, so each stream arrives once, after the command ends.
            sudo: Run the command as root.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, the result has exit code 124, ``timed_out`` set and
            the output that Sail kept.

        Raises:
            SandboxInfraError: The Sailbox is not running, or the SDK call failed.
        """
        sailbox = self._require_sailbox()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        try:
            async with asyncio.timeout(limit + EXEC_GRACE if limit is not None else None):
                with _mapped("run the command"):
                    result = await sailbox.run.aio(
                        _bash_argv(command, cwd),
                        env={**self.spec.env, **(env or {})},
                        timeout=math.ceil(limit) if limit is not None else None,
                        user="root" if sudo else None,
                        output_buffer_bytes=OUTPUT_BUFFER_BYTES,
                    )
        except TimeoutError:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        if result.timed_out:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, stdout=result.stdout, stderr=result.stderr, timed_out=True)
        if result.stdout_truncated or result.stderr_truncated:
            logger.warning("Sail kept only the last %d MiB of a command output stream.", OUTPUT_BUFFER_BYTES >> 20)
        sink = on_output or current_output_sink()
        if sink is not None:
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                if text:
                    sink(stream, text)
        return ExecResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the Sailbox, making parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The Sailbox is not running, the parent
                directories could not be made, or a write failed.
        """
        sailbox = self._require_sailbox()
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {".", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents))
            if not made.ok:
                raise SandboxInfraError("Sail could not make the upload directories.")
        for path, text in files.items():
            with _mapped("write a file"):
                await sailbox.fs.write.aio(path, io.BytesIO(text.encode("utf-8")))

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the Sailbox.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The Sailbox is not running, the read failed
                for a file that exists, or the file is not UTF-8 text.
        """
        sailbox = self._require_sailbox()
        chunks: list[bytes] = []
        try:
            with _mapped("read a file"):
                async with sailbox.fs.read_stream(path) as stream:
                    async for chunk in stream:
                        chunks.append(chunk)  # noqa: PERF401
        except SandboxInfraError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            # The SDK error for a missing file is not known, so ask the box.
            probe = await self.exec(f"test -e {shlex.quote(path)}")
            if probe.exit_code == 1:
                return None
            raise
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            # The message names no bytes, so no file content reaches the logs.
            raise SandboxInfraError("Sail read a file that is not UTF-8 text.") from None

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a new egress policy to the running Sailbox.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The Sailbox is not running, or Sail refused the policy.
        """
        sailbox = self._require_sailbox()
        with _mapped("change the egress policy"):
            await sailbox.set_egress_policy.aio(
                _egress_policy(disabled=policy.disabled, allowed_hosts=policy.allowed_hosts)
            )

    async def stop(self) -> None:
        """Terminate the Sailbox once and settle its reservation. Safe to call again and before start.

        Raises:
            UsagePendingError: The settle failed, so the reservation stays pending.
            BudgetError: The ledger refused the settlement.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)
        # Only one stop caller gets a settle failure, so a second stop returns quietly.
        settle_error, self._settle_error = self._settle_error, None
        if settle_error is not None:
            raise settle_error

    async def _teardown(self) -> None:
        """End a running start, terminate the Sailbox, then settle its reservation."""
        starting = self._starting
        if starting is not None and not starting.done():
            # A stop during the start: end the create first, so its box handle
            # is known before this teardown looks for it. A create in its
            # grace already had its cancel; a second cancel would end the
            # grace before the handle arrives.
            if not self._settling:
                starting.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(starting), CREATE_GRACE + STOP_TIMEOUT)
        billing, self._billing_call = self._billing_call, None
        if billing is not None:
            # A reserve can wait longer than the start grace; a reservation
            # that arrives after the start ended must still be released.
            with contextlib.suppress(BaseException):
                await asyncio.shield(billing)
            if self._usage is None and not billing.cancelled() and billing.exception() is None:
                self._usage = billing.result()
        sailbox, self._sailbox = self._sailbox, None
        if sailbox is not None:
            boxes = _JOB_BOXES.get(self._job_id) if self._job_id is not None else None
            if boxes is not None:
                boxes.discard(self)
                if not boxes:
                    del _JOB_BOXES[self._job_id]
            self._terminated = await _bounded(sailbox.terminate.aio(), "terminate the Sailbox")
        try:
            await self._settle(sailbox)
        finally:
            self._box_gone.set()

    async def _settle(self, sailbox: Any) -> None:
        """Settle, release or keep pending the reservation of a protected box.

        A box that did not terminate can still run and spend, so its hold
        stays pending. The spend waits run on the event loop; only the ledger
        calls use threads. A failure keeps the reservation pending, and
        ``stop()`` raises it.

        Args:
            sailbox: The SDK handle of the box, or ``None`` when no box came back.
        """
        usage, self._usage = self._usage, None
        if usage is None:
            return
        try:
            if sailbox is None and self._create_failed:
                sailbox = await self._find_failed_create(usage)
            if sailbox is None:
                await asyncio.to_thread(usage.release, refused=self._create_failed and self._create_refused)
                return
            if usage.sailbox_id is None:
                # A create in its grace returns the handle without a confirm.
                await asyncio.to_thread(usage.bind, sailbox.sailbox_id)
            if not self._terminated:
                await asyncio.to_thread(usage.pending)
                raise UsagePendingError("The Sailbox did not terminate, so its usage stays pending.")
            self._box_gone.set()
            try:
                spend = await usage.final_spend()
            except BaseException:
                await asyncio.to_thread(usage.pending)
                raise
            await asyncio.to_thread(usage.settle_spend, spend)
        except Exception as error:
            logger.warning("Sail sandbox %s usage stays pending: %s", self._name, type(error).__name__)
            if isinstance(error, UsagePendingError | BudgetError):
                self._settle_error = error
            else:
                pending = UsagePendingError("Sandbox usage is not yet settled; the reservation remains active.")
                pending.__cause__ = error
                self._settle_error = pending

    async def _find_failed_create(self, usage: Any) -> Any:
        """Look up the box of a failed create by its name, and terminate it.

        Args:
            usage: The usage reservation of the box.

        Returns:
            The SDK handle of the box, or ``None`` when Sail lists no box with the name.

        Raises:
            UsagePendingError: The lookup failed, or it found more than one box.
        """
        try:
            found = await asyncio.wait_for(Sailbox.list.aio(name=self._name), STOP_TIMEOUT)
        except Exception as error:
            await asyncio.to_thread(usage.pending)
            raise UsagePendingError("Sail could not list boxes after a failed create, so the hold stays.") from error
        if not found:
            return None
        results = [await _bounded(box.terminate.aio(), "terminate the Sailbox") for box in found]
        self._terminated = all(results)
        if len(found) > 1:
            await asyncio.to_thread(usage.pending)
            raise UsagePendingError("Sail lists more than one box with the name, so the hold stays.")
        return found[0]

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every Sailbox that this process started for the job, all at the same time.

        Each stop terminates its box and then waits minutes for Sail's spend,
        so the stops run together and the waits overlap.

        Args:
            job_id: The job tag value.

        Returns:
            The number of Sailboxes that were terminated. A box that fails to
            terminate or settle is logged and the sweep goes on.

        Raises:
            Exception: The first stop error that is not a usage or budget error.
        """
        envs = list(_JOB_BOXES.get(job_id, ()))
        results = await asyncio.gather(*(env.stop() for env in envs), return_exceptions=True)
        unexpected: BaseException | None = None
        for env, result in zip(envs, results, strict=True):
            if isinstance(result, UsagePendingError | BudgetError):
                # The hold stays pending for reconciliation; the sweep goes on.
                logger.warning("Sail sandbox %s usage stays pending: %s", env._name, type(result).__name__)
            elif isinstance(result, BaseException) and unexpected is None:
                unexpected = result
        if unexpected is not None:
            raise unexpected
        return sum(1 for env in envs if env._terminated)

"""The async Novita sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/novita.py: the
hosted single-image create, the background command run, the file write and
read, the egress policy update and the kill come from its direct mode.
Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- A box with a budget is protected (SKY-935). It reserves its whole funded
  lifetime before the create, sets the Novita ``timeout`` to that lifetime
  (at most 24 hours) with ``on_timeout="kill"``, checks the created box
  against its funding, and settles its measured runtime after the kill,
  also after a failed or cancelled start. Harbor sets a fixed one-hour
  timeout.
- The ``long_running`` metadata key, which makes Novita ignore the timeout,
  is never sent.
- Each box carries a box tag. A stop kills the known box and every other
  box with the same tag, so a create that timed out does not leak a box.
  A hold is released only when the create was never sent, or when Novita
  refused it and the sweep found no box. Any other unproven outcome keeps
  the hold pending.
- The provider adds no retry. Harbor retries the network update and the
  kill once.
- ``stop_job_sandboxes`` finds the boxes of a job by their metadata.
- The spec image is the Novita template id. Harbor's template builds from a
  Dockerfile or an image, the compose and DinD mode, directory transfer,
  user mapping and attach are left out.
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
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from core.billing.budgets import BudgetError
from core.billing.novita_usage import MAX_MEMORY_MB, MAX_VCPUS, USAGE
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..pacing import SYSTEM_CLOCK, Clock
from ..resources import SandboxSpec
from ..telemetry import current_output_sink

try:
    from novita_sandbox.core import AsyncSandbox, SandboxQuery
    from novita_sandbox.core.compat import should_use_legacy
except ImportError:
    AsyncSandbox = None
    SandboxQuery = None
    should_use_legacy = None

logger = logging.getLogger(__name__)

API_KEY_ENV = "NOVITA_API_KEY"
BOX_TAG = "skynet_box"
# Novita ignores the timeout of a box whose metadata has this key set to "true".
LONG_RUNNING_KEY = "long_running"
ALL_TRAFFIC = "0.0.0.0/0"
CREATE_TIMEOUT = 120.0
STOP_TIMEOUT = 60.0
# The SDK create docs cap a box timeout at 24 hours on the Pro plan.
NOVITA_MAX_LIFETIME = 24 * 3600
# The runtime counts from the create call, and Novita starts the timeout
# when the create arrives, up to CREATE_TIMEOUT later. The hold covers both,
# and the quote prices at most 24 hours.
MAX_PROTECTED_LIFETIME = NOVITA_MAX_LIFETIME - CREATE_TIMEOUT
# The SDK maps a 401 to AuthenticationException and a 429 to
# RateLimitException, and puts the status at the start of other API error
# messages. These answers open no box: the SDK sends the create once.
REFUSAL_STATUSES = frozenset({400, 401, 403, 404, 422, 429})
_REFUSAL_ERRORS = frozenset({"AuthenticationException", "RateLimitException"})
_STATUS_PREFIX = re.compile(r"^(\d{3}):")
TIMEOUT_EXIT_CODE = 124
LEGACY_DOMAIN_MESSAGE = (
    "The Novita domain in NOVITA_DOMAIN is a legacy domain, which cannot size a sandbox. Use a current domain."
)

NOVITA_CAPABILITIES = EnvironmentCapabilities(
    name="novita",
    billable=True,
    max_vcpus=MAX_VCPUS,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
)


def _error_names(error: BaseException) -> set[str]:
    """Return the class names of an error and its bases.

    The provider matches SDK errors by name, so it loads without the SDK.

    Args:
        error: The error.

    Returns:
        The names in the error's method resolution order.
    """
    return {cls.__name__ for cls in type(error).__mro__}


def _is_refusal(error: BaseException) -> bool:
    """Tell whether Novita refused a create in a way that proves it opened no box.

    Args:
        error: The error of the create call.

    Returns:
        True for an authentication or rate-limit error, or an API error whose
        status is in ``REFUSAL_STATUSES``.
    """
    if _error_names(error) & _REFUSAL_ERRORS:
        return True
    if "SandboxException" not in _error_names(error):
        return False
    match = _STATUS_PREFIX.match(str(error))
    return match is not None and int(match.group(1)) in REFUSAL_STATUSES


def _legacy_domain() -> bool:
    """Tell whether the SDK talks to a legacy Novita domain.

    SDK 2.1.4 resolves the domain from ``NOVITA_DOMAIN`` or its default. On a
    legacy domain its create raises ``NotImplementedError`` for ``resources``
    and ignores ``lifecycle``, so this provider cannot size or bound a box there.

    Returns:
        True when the SDK resolves a legacy domain.
    """
    return should_use_legacy is not None and should_use_legacy(None)


def _network_options(*, disabled: bool, allowed_hosts: tuple[str, ...]) -> dict[str, Any]:
    """Build the ``set_network`` arguments of a policy, in Harbor's shapes.

    Args:
        disabled: True when the box has no egress.
        allowed_hosts: The hosts that egress may reach; empty means any host.

    Returns:
        The ``allow_out`` and ``deny_out`` arguments.
    """
    if disabled:
        return {"allow_out": [], "deny_out": [ALL_TRAFFIC]}
    if allowed_hosts:
        return {"allow_out": list(allowed_hosts), "deny_out": [ALL_TRAFFIC]}
    return {"allow_out": None, "deny_out": None}


class NovitaEnvironment(SandboxEnvironment):
    """Run commands in one Novita sandbox through the async SDK.

    The factory refuses the provider in production. With a budget, each box
    reserves its whole lifetime and settles its usage against that budget.
    """

    capabilities: ClassVar[EnvironmentCapabilities] = NOVITA_CAPABILITIES
    # The SDK classes; tests replace them with fakes.
    sandbox_cls: ClassVar[Any] = AsyncSandbox
    query_cls: ClassVar[Any] = SandboxQuery

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        clock: Clock | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = USAGE.reserve,
    ) -> None:
        """Check the spec and prepare the box without a network call.

        Args:
            spec: The sandbox to open.
            clock: The clock that measures the runtime.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: A protected box has no operation key, or its
                lifetime or memory is more than Novita allows, or Novita
                cannot size a box to the requested CPU count or memory.
        """
        # Novita takes 1 or an even number of vCPUs and whole GiB of memory
        # when it resizes a template at create.
        if spec.resources.cpus != 1 and spec.resources.cpus % 2:
            raise SandboxSpecError("A Novita sandbox needs 1 or an even number of vCPUs.")
        if (spec.resources.memory_mb or 0) % 1024:
            raise SandboxSpecError("A Novita sandbox needs its memory in whole GiB.")
        if budget is not None and spec.operation_key is None:
            raise SandboxSpecError("A protected Novita sandbox needs an operation key.")
        if budget is not None and spec.lifetime_seconds > MAX_PROTECTED_LIFETIME:
            raise SandboxSpecError(f"A protected Novita sandbox can live at most {MAX_PROTECTED_LIFETIME:.0f} seconds.")
        if budget is not None and (spec.resources.memory_mb or 0) > MAX_MEMORY_MB:
            raise SandboxSpecError(f"A protected Novita sandbox can have at most {MAX_MEMORY_MB} MiB of memory.")
        super().__init__(spec)
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._billing_call: asyncio.Future[Any] | None = None
        self._settle_error: Exception | None = None
        self._clock = clock or SYSTEM_CLOCK
        self.name = spec.name or f"skynet-{uuid.uuid4().hex[:12]}"
        self._box: Any | None = None
        self._create_sent = False
        # True while a create may have opened a box that this environment
        # never saw, so an empty sweep does not prove that no box runs.
        self._create_unknown = False
        self._starting: asyncio.Future[None] | None = None
        self._stop_task: asyncio.Future[None] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and the API key exist.

        Raises:
            SandboxFatalError: The SDK or the ``NOVITA_API_KEY`` variable is missing.
        """
        if cls.sandbox_cls is None:
            raise SandboxFatalError("The novita-sandbox package is not installed: install the `novita` extra.")
        if not os.environ.get(API_KEY_ENV):
            raise SandboxFatalError(f"The Novita sandbox needs the {API_KEY_ENV} variable.")
        if _legacy_domain():
            raise SandboxFatalError(LEGACY_DOMAIN_MESSAGE)

    def _timeout(self) -> int:
        """Return the Novita timeout of the box, in whole seconds.

        Returns:
            The spec lifetime, rounded up and cut to Novita's 24-hour cap.
        """
        return max(1, min(NOVITA_MAX_LIFETIME, math.ceil(self.spec.lifetime_seconds)))

    async def start(self) -> None:
        """Reserve the box's funds, then create the box.

        Any failure or cancel stops the box before the error goes on. The work
        runs in its own task, so a ``stop()`` during the start can cancel it
        and then stop the box.

        Raises:
            SandboxFatalError: The SDK is not installed.
            StartTimeoutError: The create did not finish in time.
            SandboxInfraError: The create failed, or a ``stop()`` ended the start.
        """
        if self.sandbox_cls is None:
            raise SandboxFatalError("The novita-sandbox package is not installed: install the `novita` extra.")
        self._starting = asyncio.ensure_future(self._start())
        try:
            await self._starting
        except BaseException as error:
            if self._stop_task is None:
                self._stop_task = asyncio.ensure_future(self._teardown())
            await asyncio.shield(self._stop_task)
            current = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and current is not None and not current.cancelling():
                # Only stop() cancelled the start task; the caller was not
                # cancelled, so it must not see a CancelledError.
                raise SandboxInfraError("The Novita sandbox stopped during start.") from None
            raise

    async def _start(self) -> None:
        """Reserve the funds, create the box and confirm its funded size.

        Raises:
            StartTimeoutError: The create did not finish in time.
            SandboxInfraError: Novita refused the create or its outcome is unknown.
            SandboxSpecError: Skynet cannot fund the box, or the box is larger
                than its funding.
        """
        # Checked before the reserve: no hold exists, so nothing stays pending.
        if _legacy_domain():
            raise SandboxSpecError(LEGACY_DOMAIN_MESSAGE)
        usage = await self._reserve()
        self._create_sent = True
        self._create_unknown = True
        if usage is not None:
            usage.start(self._clock.monotonic(), self.name)
        try:
            async with asyncio.timeout(CREATE_TIMEOUT) as deadline:
                self._box = await self.sandbox_cls.create(**self._create_options())
            self._create_unknown = False
            if usage is not None:
                # Novita starts the timeout before the create returns, so the
                # box ends by this time plus the timeout.
                usage.created(self._clock.monotonic(), self._timeout())
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(CREATE_TIMEOUT) from None
        except Exception as error:
            if _is_refusal(error):
                self._create_unknown = False
                if usage is not None:
                    usage.refused()
                raise SandboxInfraError(f"Novita refused the create: {type(error).__name__}") from error
            raise SandboxInfraError(f"The Novita create failed: {type(error).__name__}") from error
        if usage is not None:
            info = await self._box.get_info()
            await self._billing(usage.confirm, info)

    async def _reserve(self) -> Any | None:
        """Reserve the whole funded lifetime of a protected box once.

        Returns:
            The usage reservation, or ``None`` for an unprotected box.

        Raises:
            SandboxSpecError: Skynet cannot price the size or lifetime of the box.
        """
        if self._budget is None:
            return None
        reserving = asyncio.ensure_future(
            asyncio.to_thread(
                self._usage_factory, self._budget, self._usage_request(), operation_key=self.spec.operation_key
            )
        )
        # The thread cannot be cancelled and may still reserve after a stop,
        # so the teardown waits for this call and settles what it returns.
        self._billing_call = reserving
        try:
            self._usage = await asyncio.shield(reserving)
        except UnpricedOperationError as error:
            raise SandboxSpecError(f"Skynet cannot fund this Novita sandbox: {error}") from error
        return self._usage

    def _usage_request(self) -> dict[str, Any]:
        """Build the request that the reservation prices.

        The create sizes the box to the spec's CPU count and memory. With no
        memory in the spec the template sets it, so the request funds Novita's
        largest memory. The reservation checks the created box against it.

        Returns:
            The funded lifetime, the CPU count, the memory and the template.
        """
        resources = self.spec.resources
        return {
            "lifetime_ms": math.ceil((self._timeout() + CREATE_TIMEOUT) * 1000),
            "vcpus": resources.cpus,
            "memory_mb": resources.memory_mb or MAX_MEMORY_MB,
            "template": self.spec.image,
        }

    async def _billing(self, call: Callable[..., Any], *args: Any) -> Any:
        """Run one reservation call in a thread that a cancel cannot cut short.

        The teardown awaits the same call before it settles, so a settle never
        races a confirm that is still running.

        Args:
            call: The reservation method.
            *args: Its positional arguments.

        Returns:
            The result of the call.
        """
        self._billing_call = asyncio.ensure_future(asyncio.to_thread(call, *args))
        return await asyncio.shield(self._billing_call)

    def _create_options(self) -> dict[str, Any]:
        """Build the keyword arguments of the create call.

        Returns:
            The template, the funded timeout, metadata, env and network options.
        """
        spec = self.spec
        metadata = {key: value for key, value in spec.tags.items() if key != LONG_RUNNING_KEY}
        options: dict[str, Any] = {
            "timeout": self._timeout(),
            # A paused box keeps billed storage, so the timeout must end the box.
            "lifecycle": {"on_timeout": "kill", "auto_resume": False},
            "metadata": {**metadata, BOX_TAG: self.name},
            "envs": dict(spec.env),
            "allow_internet_access": not spec.network_disabled,
            "request_timeout": CREATE_TIMEOUT,
        }
        if spec.image:
            options["template"] = spec.image
        # The box gets its funded size, not the template's size.
        sizes: dict[str, Any] = {"cpu": spec.resources.cpus}
        if spec.resources.memory_mb:
            sizes["memory_gib"] = spec.resources.memory_mb // 1024
        options["resources"] = sizes
        if not spec.network_disabled and spec.allowed_hosts:
            options["network"] = {"allow_out": list(spec.allowed_hosts), "deny_out": [ALL_TRAFFIC]}
        return options

    async def stop(self) -> None:
        """Stop the box once and settle its reservation. Safe to call again.

        Raises:
            UsagePendingError: The box may still run or the settle failed, so
                the reservation stays pending.
            BudgetError: The ledger refused the settlement.
        """
        if self._stop_task is None and self._starting is None and self._box is None:
            return
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)
        # Only one stop caller gets a settle failure, so a second stop returns quietly.
        settle_error, self._settle_error = self._settle_error, None
        if settle_error is not None:
            raise settle_error

    async def _teardown(self) -> None:
        """End the start, kill every box of this environment, then settle."""
        if self._starting is not None and not self._starting.done():
            # End the start first, so no box appears after this teardown has
            # looked for it.
            self._starting.cancel()
            # asyncio.wait does not raise the start's own error or cancel, but
            # a cancel of this teardown still reaches it.
            await asyncio.wait({self._starting})
        if self._billing_call is not None:
            await asyncio.wait({self._billing_call})
            call = self._billing_call
            # A reserve that finished after the start was cancelled, before any create.
            late = self._usage is None and not self._create_sent and call.done() and not call.cancelled()
            if late and call.exception() is None:
                self._usage = call.result()
        box, self._box = self._box, None
        known = None
        confirmed = True
        seen = box is not None
        if box is not None:
            known = getattr(box, "sandbox_id", None)
            try:
                await asyncio.wait_for(box.kill(), STOP_TIMEOUT)
            except Exception as error:
                confirmed = False
                logger.warning("Novita sandbox %s did not stop: %s", self.name, type(error).__name__)
        if self._create_sent:
            try:
                killed, failed = await asyncio.wait_for(
                    self._kill_tagged({BOX_TAG: self.name}, skip=known), STOP_TIMEOUT
                )
            except Exception as error:
                confirmed = False
                logger.warning("Novita sandbox %s sweep failed: %s", self.name, type(error).__name__)
            else:
                if failed:
                    confirmed = False
                if killed:
                    seen = True
                # The list can lag behind a create, so an empty sweep after a
                # create with an unknown outcome does not prove that no box runs.
                if self._create_unknown and box is None and not killed:
                    confirmed = False
        await self._settle(confirmed=confirmed, seen=seen)

    async def _settle(self, *, confirmed: bool, seen: bool) -> None:
        """Settle, release or keep pending the reservation of a protected box.

        The usage leaves this environment before the ledger call, so exactly
        one teardown settles it. A failure keeps the reservation pending, and
        ``stop()`` raises it.

        Args:
            confirmed: True when every kill call answered and no unseen box can run.
            seen: True when a box handle existed or the sweep killed a tagged box.
        """
        usage, self._usage = self._usage, None
        if usage is None:
            return
        usage.stopped(self._clock.monotonic(), confirmed=confirmed, box_seen=seen)
        try:
            await asyncio.to_thread(usage.settle)
        except Exception as error:
            logger.warning("Novita sandbox %s usage stays pending: %s", self.name, type(error).__name__)
            if isinstance(error, UsagePendingError | BudgetError):
                self._settle_error = error
            else:
                pending = UsagePendingError("Sandbox usage is not yet settled; the reservation remains active.")
                pending.__cause__ = error
                self._settle_error = pending

    @classmethod
    async def _kill_tagged(cls, metadata: Mapping[str, str], *, skip: str | None = None) -> tuple[int, int]:
        """Kill every listed box whose metadata matches.

        A failure on one box does not stop the sweep.

        Args:
            metadata: The metadata that a box must carry.
            skip: A box id that is already stopped.

        Returns:
            The number of boxes that Novita confirmed as killed, and the
            number of kill calls that failed.
        """
        paginator = cls.sandbox_cls.list(query=cls.query_cls(metadata=dict(metadata)))
        killed = 0
        failed = 0
        while paginator.has_next:
            for item in await paginator.next_items():
                if item.sandbox_id == skip:
                    continue
                try:
                    if await cls.sandbox_cls.kill(item.sandbox_id):
                        killed += 1
                except Exception as error:
                    failed += 1
                    logger.warning("Novita sandbox %s did not stop: %s", item.sandbox_id, type(error).__name__)
        return killed, failed

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Kill every Novita box that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of boxes that the sweep stopped.
        """
        if cls.sandbox_cls is None:
            return 0
        killed, _ = await cls._kill_tagged({JOB_TAG: job_id})
        return killed

    def _require_box(self) -> Any:
        """Return the running box.

        Returns:
            The SDK sandbox.

        Raises:
            SandboxInfraError: The box is not running.
        """
        if self._box is None or self._stop_task is not None:
            raise SandboxInfraError("The Novita sandbox is not running.")
        return self._box

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
        """Run one shell command in the box.

        Args:
            command: The shell command.
            cwd: The working directory, or ``None`` for the box default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit, or ``None`` for no limit.
            on_output: Receives the stdout and stderr text as it arrives.
            sudo: Run the command as root.

        Returns:
            The exit code and the output. A timeout sets ``timed_out``.

        Raises:
            SandboxInfraError: The box is not running, or the command did not
                start or its connection failed.
        """
        box = self._require_box()
        sink = on_output or current_output_sink()
        stdout: list[str] = []
        stderr: list[str] = []

        def on_stdout(text: str) -> None:
            """Keep and forward stdout text.

            Args:
                text: The new output.
            """
            stdout.append(text)
            if sink is not None:
                sink("stdout", text)

        def on_stderr(text: str) -> None:
            """Keep and forward stderr text.

            Args:
                text: The new output.
            """
            stderr.append(text)
            if sink is not None:
                sink("stderr", text)

        # Harbor puts the cd in the command: the SDK cwd option fails with a
        # misleading error when the directory does not exist.
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        try:
            # timeout=0 keeps the connection open with no limit; exec applies
            # its own limit and kills the command.
            handle = await box.commands.run(
                command,
                background=True,
                envs={**self.spec.env, **(env or {})},
                user="root" if sudo else None,
                timeout=0,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        except Exception as error:
            raise SandboxInfraError(f"The Novita command did not start: {type(error).__name__}") from error
        try:
            async with asyncio.timeout(timeout_sec) as deadline:
                result = await handle.wait()
        except TimeoutError:
            if not deadline.expired():
                raise
            with contextlib.suppress(Exception):
                await asyncio.wait_for(box.commands.kill(handle.pid), STOP_TIMEOUT)
            return ExecResult(TIMEOUT_EXIT_CODE, "".join(stdout), "".join(stderr), timed_out=True)
        except asyncio.CancelledError:
            # A background command outlives the connection, so a cancelled
            # caller kills it. The kill is best effort and the cancel goes on.
            killing = asyncio.ensure_future(asyncio.wait_for(box.commands.kill(handle.pid), STOP_TIMEOUT))
            with contextlib.suppress(Exception):
                await asyncio.shield(killing)
            raise
        except Exception as error:
            if "CommandExitException" in _error_names(error):
                return ExecResult(error.exit_code, "".join(stdout), "".join(stderr))
            raise SandboxInfraError(f"The Novita command connection failed: {type(error).__name__}") from error
        return ExecResult(result.exit_code, "".join(stdout), "".join(stderr))

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the box. Novita creates the parent directories.

        Args:
            files: Maps each sandbox path to its text content.

        Raises:
            SandboxInfraError: The box is not running, or a write failed.
        """
        box = self._require_box()
        for path, text in files.items():
            try:
                await box.files.write(path, text.encode("utf-8"))
            except Exception as error:
                raise SandboxInfraError(f"Novita could not write a file: {type(error).__name__}") from error

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the box.

        Args:
            path: The sandbox path.

        Returns:
            The content, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The box is not running, the read failed, or the
                file is not UTF-8 text.
        """
        box = self._require_box()
        try:
            data = await box.files.read(path, format="bytes")
        except Exception as error:
            # SandboxNotFoundException is also a NotFoundException; it means the
            # box is gone, not the file.
            if "FileNotFoundException" in _error_names(error):
                return None
            raise SandboxInfraError(f"Novita could not read a file: {type(error).__name__}") from error
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            # The message names no bytes, so no file content reaches the logs.
            raise SandboxInfraError("Novita read a file that is not UTF-8 text.") from None

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a new egress policy to the running box.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The box is not running.
        """
        box = self._require_box()
        await box.set_network(**_network_options(disabled=policy.disabled, allowed_hosts=policy.allowed_hosts))

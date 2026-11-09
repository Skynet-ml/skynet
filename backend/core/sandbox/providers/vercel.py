"""The async Vercel Sandbox provider, funded in lifetime slices.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the create,
exec and stop flow comes from ``harbor/environments/vercel.py``. Changes:

- A protected box reserves its budget after the capacity wait and before
  the create call, and funds each lifetime slice before it asks Vercel for
  the extension. An unconfirmed extension stops the box.
- ``exec`` never polls the process status. It waits on the long-poll
  ``wait()`` call, survives transport cuts, and reads a sentinel exit file
  when the cuts do not stop.
- The box name comes from the operation key, so a replayed create finds the
  same box instead of a second one.
- A network policy change is read back and fails closed.
- Each box records its phase timings.
- A setup box can become one run-scoped snapshot (see ``..snapshots``), and
  a trial box can start from it. A box that holds env values or injected
  headers is refused, and the snapshot storage is metered as sandbox cost.
- Compose services (see ``..services``) run as processes in the box. The
  box grows by their size, so the reservation prices them. They start after
  the create or the snapshot restore, and stop before a snapshot.
- ``exec`` streams new command output to the output sink while the command
  runs, so a model mailbox frame reaches the parent before the exit.
- Harbor's directory transfer feature is left out.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import dataclasses
import logging
import math
import shlex
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import httpx

from core.billing.budgets import BudgetError
from core.billing.operation_pricing import json_fingerprint
from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached
from core.billing.vercel_usage import SANDBOX_NETWORK_BYTES_CAP, VercelSnapshotReservation, VercelUsageReservation
from core.config import settings

from ..capabilities import VERCEL_CAPABILITIES, SandboxSpecError, resolve_resources, validate_spec
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..pacing import SYSTEM_CLOCK, Clock, ProviderAtCapacityError, ProviderPacing, create_with_capacity
from ..resources import ResourceMode, SandboxSpec
from ..services import ComposeService, box_resources, start_services, stop_services
from ..snapshots import (
    SNAPSHOT_TIMEOUT,
    BuiltSnapshot,
    SnapshotRefusedError,
    refuse_secrets,
    snapshot_expiration,
    snapshot_hold_retention,
)
from ..telemetry import BoxTimings, current_output_sink

try:
    import httpx2
    from vercel.sandbox import (
        NetworkPolicy as SdkNetworkPolicy,
    )
    from vercel.sandbox import (
        NetworkPolicyRule,
        NetworkPolicyTransform,
        SandboxApiError,
        SandboxClient,
        SandboxCredentials,
        SandboxQueryByCreatedAt,
        SandboxResources,
        SandboxResponseError,
        SandboxServiceOptions,
        SandboxStreamError,
        SnapshotSource,
        TagFilter,
    )
except ImportError:
    httpx2 = None
    SdkNetworkPolicy = None
    NetworkPolicyRule = None
    NetworkPolicyTransform = None
    SandboxApiError = None
    SandboxClient = None
    SandboxCredentials = None
    SandboxQueryByCreatedAt = None
    SandboxResources = None
    SandboxResponseError = None
    SandboxServiceOptions = None
    SandboxStreamError = None
    SnapshotSource = None
    TagFilter = None

logger = logging.getLogger(__name__)

CREATE_TIMEOUT = 120.0
KILL_GRACE = 30.0
KILL_AFTER_MAX = 18_000.0
CANCEL_GRACE = 10.0
WAIT_ERROR_LIMIT = 20
WAIT_BACKOFF = 0.5
WAIT_BACKOFF_MAX = 5.0
STOP_TIMEOUT = 60.0
EXEC_ATTEMPTS = 3
SLICE_MS = 600_000
RENEW_LEAD = 180.0
FUND_MARGIN = 30.0
POLL_SLACK = 60.0
IMAGE_RETRY_DELAY = 2.0
EXIT_PREFIX = "/tmp/.app-exit-"
OUTPUT_PREFIX = "/tmp/.app-output-"
TIMEOUT_CODES = frozenset({124, 137})
SHELL_FLAGS = ("--noprofile", "--norc", "-c")
OUTPUT_POLL = 0.5
OUTPUT_POLL_WARN_AFTER = 3
# The SDK streams a run with no read timeout, so a stalled tail read would stop
# every later poll and starve a model mailbox until the guest proxy gives up.
OUTPUT_POLL_TIMEOUT = 10.0
# One poll reads at most this many bytes of each file. Without a cap, a large
# backlog can outlast OUTPUT_POLL_TIMEOUT on every poll, and the poller never
# catches up. With it, the offset moves forward in bounded steps.
OUTPUT_POLL_MAX_BYTES = 1 << 20
# Prints the bytes after each 1-based offset as one base64 line per file. The
# SDK has no ranged read, and a full re-read on every poll is O(n^2).
TAIL_SCRIPT = (
    'while [ $# -gt 0 ]; do tail -c +"$1" "$2" 2>/dev/null'
    f' | head -c {OUTPUT_POLL_MAX_BYTES} | base64 | tr -d "\\n"; echo; shift 2; done'
)
_LIVE_STATUSES = frozenset({"pending", "running"})

ResponseCapture = Callable[[httpx.Response], Awaitable[None]]
ClientFactory = Callable[[ResponseCapture | None], Any]
UsageFactory = Callable[..., Any]


def _error_types(*names: str) -> tuple[type[BaseException], ...]:
    """Collect transport error classes from both httpx packages.

    Args:
        names: Error class names that exist in httpx and httpx2.

    Returns:
        The classes that the installed packages define.
    """
    found: list[type[BaseException]] = []
    for module in (httpx, httpx2):
        for name in names:
            error = getattr(module, name, None) if module is not None else None
            if isinstance(error, type):
                found.append(error)
    return tuple(found)


# Only errors where the request never reached the box are safe to resend:
# a resent create_process after a read error could start the command twice.
_CONNECT_ERRORS = _error_types("ConnectError", "ConnectTimeout", "PoolTimeout")
_TRANSPORT_ERRORS = _error_types("TransportError")
_WAIT_ERRORS = _TRANSPORT_ERRORS + tuple(error for error in (SandboxStreamError, SandboxResponseError) if error)


def _is_retryable_wait_error(error: BaseException) -> bool:
    """Tell whether a failed ``wait()`` call can be repeated.

    Args:
        error: The error of the wait call.

    Returns:
        True for transport, stream and missing-code errors and 5xx answers.
    """
    if isinstance(error, _WAIT_ERRORS):
        return True
    return SandboxApiError is not None and isinstance(error, SandboxApiError) and error.status_code >= 500


def _is_session_stopped(error: BaseException) -> bool:
    """Tell whether an SDK error says that the runtime session ended.

    Args:
        error: The SDK error.

    Returns:
        True for a 410 answer or a ``sandbox_stopped`` error code.
    """
    if SandboxApiError is None or not isinstance(error, SandboxApiError):
        return False
    return error.status_code == 410 or error.code == "sandbox_stopped"


def _is_not_found(error: BaseException) -> bool:
    """Tell whether an SDK error says that the resource does not exist.

    Args:
        error: The SDK error.

    Returns:
        True for a 404 answer.
    """
    return SandboxApiError is not None and isinstance(error, SandboxApiError) and error.status_code == 404


def _settle_failure(error: Exception) -> Exception:
    """Return the usage error that a failed settle raises from ``stop()``.

    Args:
        error: The settle failure.

    Returns:
        A usage error unchanged. Any other failure becomes a
        ``UsagePendingError``, because the reservation stays pending and its
        own text can hold database details.
    """
    if isinstance(error, UsagePendingError | BudgetError):
        return error
    pending = UsagePendingError("Sandbox usage is not yet settled; the reservation remains active.")
    pending.__cause__ = error
    return pending


async def _close_client(client: Any) -> None:
    """Close a snapshot SDK client, bounded so a hung close cannot hold the caller.

    Args:
        client: The SDK client.
    """
    try:
        await asyncio.wait_for(client.aclose(), STOP_TIMEOUT)
    except Exception as error:
        logger.warning("Sandbox snapshot client did not close: %s", type(error).__name__)


def box_name(operation_key: str | None, budget: Any | None = None) -> str:
    """Make the provider name of one box.

    Args:
        operation_key: The stable key of the logical create, or ``None``.
        budget: The budget runtime of a protected box, or ``None``.

    Returns:
        A name that is the same for the same key and budget, or a random name
        when there is no key.
    """
    if operation_key is None:
        return f"skynet-{uuid.uuid4().hex}"
    parts = [budget.budget_id, budget.generation, operation_key] if budget is not None else [operation_key]
    return "skynet-" + json_fingerprint(parts)[:48]


def policy_confirmed(policy: NetworkPolicy, readback: Any | None) -> bool:
    """Check a policy readback against the policy that was sent.

    Any readback that does not prove the policy is rejected.

    Args:
        policy: The policy that the provider must apply.
        readback: The ``NetworkPolicyReadback`` of the session, or ``None``.

    Returns:
        True only when the readback proves the policy.
    """
    if readback is None:
        return False
    domains = tuple(readback.allowed_domains or ())
    subnets = readback.subnets
    subnet_allow = subnets is not None and bool(subnets.allow)
    if policy.disabled:
        return readback.mode == "deny-all" or (readback.mode == "custom" and not domains and not subnet_allow)
    if policy.allowed_hosts:
        return (
            readback.mode == "custom"
            and "*" not in domains
            and set(domains) <= set(policy.allowed_hosts)
            and not subnet_allow
        )
    return readback.mode == "allow-all" or (readback.mode == "custom" and "*" in domains)


@dataclasses.dataclass
class _OutputTail:
    """The read state of one command output file."""

    stream: str
    path: str
    offset: int = 0
    emitted: int = 0
    decoder: codecs.IncrementalDecoder = dataclasses.field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace")
    )


async def _stop_poller(poller: asyncio.Future[None] | None) -> BaseException | None:
    """Cancel the output poller and wait until it ends.

    Args:
        poller: The poller task, or ``None`` when no sink exists.

    Returns:
        The sink error that ended the poller, or ``None``.
    """
    if poller is None:
        return None
    poller.cancel()
    await asyncio.wait({poller})
    return None if poller.cancelled() else poller.exception()


class VercelEnvironment(SandboxEnvironment):
    """Run commands in one Vercel sandbox.

    With a budget the box is protected: it reserves the first lifetime slice
    before the create call and funds each later slice before the extension.
    """

    capabilities = VERCEL_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        budget: Any | None = None,
        client_factory: ClientFactory | None = None,
        usage_factory: UsageFactory = VercelUsageReservation,
        pacing: ProviderPacing | None = None,
        at_capacity: Callable[[], Awaitable[bool]] | None = None,
        clock: Clock | None = None,
        snapshot_id: str | None = None,
        snapshot_usage_factory: UsageFactory = VercelSnapshotReservation,
        services: Sequence[ComposeService] = (),
    ) -> None:
        """Check the spec and prepare the box without a network call.

        Args:
            spec: The sandbox to open.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            client_factory: Makes the async SDK client from a response
                capture hook. The default reads the Vercel settings.
            usage_factory: Makes the usage reservation of a protected box.
            pacing: The read and create buckets of the provider.
            at_capacity: Returns True while the provider has no capacity.
            clock: The clock for waits and timings.
            snapshot_id: Start the box from this run snapshot instead of the image.
            snapshot_usage_factory: Makes the storage reservation of a snapshot.
            services: The compose services in start order. The box grows by
                their size.

        Raises:
            SandboxSpecError: The spec is invalid, a protected box has no
                operation key, or the services do not fit the largest box.
        """
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Vercel sandbox needs an operation key.")
            # A protected box injects no headers: credentials stay in the
            # control plane, outside the guest.
            self.capabilities = dataclasses.replace(VERCEL_CAPABILITIES, protected=True, injects_headers=False)
        if services:
            # Check the workload size alone first, so an invalid request is not
            # hidden by the larger size that the services add.
            validate_spec(spec, self.capabilities)
            spec = dataclasses.replace(spec, resources=box_resources(spec.resources, services, self.capabilities))
        super().__init__(spec)
        self._services = tuple(services)
        self._services_up: list[ComposeService] = []
        self._budget = budget
        self._client_factory = client_factory or self._make_client
        self._usage_factory = usage_factory
        self._pacing = pacing
        self._at_capacity = at_capacity
        self._clock = clock or SYSTEM_CLOCK
        self.name = spec.name or box_name(spec.operation_key, budget)
        self.timings = BoxTimings(self.name, clock=self._clock.monotonic)
        self._client: Any | None = None
        self._usage: Any | None = None
        self._box: Any | None = None
        self._session: Any | None = None
        self._create_dispatched = False
        self._renewer: asyncio.Task[None] | None = None
        self._billing_call: asyncio.Future[Any] | None = None
        self._starting: asyncio.Future[None] | None = None
        self._stop_task: asyncio.Future[None] | None = None
        self._settle_error: Exception | None = None
        self._halted = False
        self._funded_until: float | None = None
        self.snapshot_id = snapshot_id
        self._snapshot_usage_factory = snapshot_usage_factory
        self._secret_exposed = False
        self._snapshotted = False

    @property
    def usage(self) -> Any | None:
        """Return the usage reservation of a protected box, once it exists."""
        return self._usage

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and the credentials exist.

        Raises:
            SandboxFatalError: The SDK or a credential setting is missing.
        """
        if SandboxClient is None:
            raise SandboxFatalError("The vercel-sandbox package is not installed.")
        if not (settings.vercel_token and settings.vercel_team_id and settings.vercel_project_id):
            raise SandboxFatalError("The Vercel sandbox credentials are not configured.")

    @staticmethod
    def _make_client(capture: ResponseCapture | None) -> Any:
        """Make the async SDK client that reads the Vercel settings.

        Args:
            capture: Receives every response after its body is read, or ``None``.

        Returns:
            A ``SandboxClient`` that opens its transport on first use.
        """
        VercelEnvironment.preflight()
        token = settings.vercel_token.get_secret_value()
        team_id = settings.vercel_team_id
        project_id = settings.vercel_project_id

        async def credentials() -> Any:
            """Return the credentials of the Vercel project.

            Returns:
                The SDK credentials.
            """
            return SandboxCredentials(token=token, team_id=team_id, project_id=project_id)

        async def on_response(response: httpx.Response) -> None:
            """Hand a response to the usage capture.

            Args:
                response: The SDK response.
            """
            if capture is None:
                return
            # The SDK drops provider fields that billing needs, so read the raw body first.
            await response.aread()
            await capture(response)

        read_timeout = max(settings.vercel_sandbox_max_lifetime_seconds, 60.0)
        return SandboxClient.create(
            options=SandboxServiceOptions(credentials_factory=credentials),
            httpx_client_factory=lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, read=read_timeout), event_hooks={"response": [on_response]}
            ),
        )

    async def _capture(self, response: httpx.Response) -> None:
        """Pass one provider response to the usage reservation.

        Args:
            response: A response whose body is already read.
        """
        if self._usage is not None:
            await asyncio.to_thread(self._usage.capture_response, response)

    def _first_ms(self) -> int:
        """Return the lifetime of the first create, in milliseconds.

        Returns:
            One slice for a protected box, or the whole lifetime.
        """
        whole = math.ceil(self.spec.lifetime_seconds * 1000)
        return min(whole, SLICE_MS) if self._budget is not None else whole

    def _usage_request(self) -> dict[str, Any]:
        """Build the usage request that the reservation prices.

        Returns:
            The immutable resource and network parameters of the create.
        """
        hosts = sorted(set(self.spec.allowed_hosts))
        return {
            "name": self.name,
            "image": self._image(),
            "lifetime_ms": self._first_ms(),
            "vcpus": self.spec.resources.cpus,
            "network_disabled": self.spec.network_disabled,
            **({"allowed_hosts": hosts, "network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP} if hosts else {}),
            "ports": [],
            "persistent": False,
            "environment_fingerprint": json_fingerprint(dict(self.spec.env)),
            **({"snapshot_id": self.snapshot_id} if self.snapshot_id else {}),
        }

    def _resources(self) -> Any | None:
        """Build the SDK size of the box.

        Returns:
            The vCPU and memory size, or ``None`` when the spec sends no size.
        """
        request = resolve_resources(self.spec.resources, self.capabilities)
        if request.mode is ResourceMode.IGNORE:
            return None
        return SandboxResources(vcpus=request.cpus, memory=request.memory_mb)

    def _image(self) -> str:
        """Return the image of the box.

        Returns:
            The spec image, or the configured default.
        """
        return self.spec.image or settings.vercel_sandbox_image

    def _sdk_policy(self, policy: NetworkPolicy, *, update: bool) -> Any | None:
        """Build the SDK network policy of one Skynet policy.

        Args:
            policy: The Skynet policy.
            update: True for a policy change, where open egress must be explicit.

        Returns:
            The SDK policy, or ``None`` for the create default of open egress.
        """
        rules = {
            host: (NetworkPolicyRule(transform=(NetworkPolicyTransform(headers=dict(headers)),)),)
            for host, headers in self.spec.inject_headers.items()
        }
        if policy.disabled:
            return SdkNetworkPolicy.deny_all()
        if policy.allowed_hosts:
            return SdkNetworkPolicy.custom(allow={host: rules.get(host, ()) for host in policy.allowed_hosts})
        if rules:
            return SdkNetworkPolicy.custom(allow={"*": (), **rules})
        return SdkNetworkPolicy.allow_all() if update else None

    @contextlib.asynccontextmanager
    async def _reserve(self) -> AsyncIterator[Any | None]:
        """Reserve the first slice of a protected box.

        Yields:
            The usage reservation, or ``None`` for an unprotected box.
        """
        if self._budget is not None and self._usage is None:
            reserving = asyncio.ensure_future(
                asyncio.to_thread(
                    self._usage_factory, self._budget, self._usage_request(), operation_key=self.spec.operation_key
                )
            )
            try:
                self._usage = await asyncio.shield(reserving)
            except asyncio.CancelledError:
                # The thread cannot be cancelled and may still reserve; keep
                # its result so the teardown releases it instead of leaking it.
                while not reserving.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(reserving)
                if not reserving.cancelled() and reserving.exception() is None:
                    self._usage = reserving.result()
                raise
        yield self._usage

    async def start(self) -> None:
        """Wait for capacity, reserve the budget, then create the box.

        A failed create is not retried, because the create may have reached
        Vercel. Any failure or cancel stops the box and keeps or releases the
        reservation before the error goes on. The work runs in its own task,
        so a ``stop()`` during the start can cancel it and then stop the box.

        Raises:
            StartTimeoutError: The create did not finish in time.
            SandboxInfraError: The box came back without a usable session, or
                a ``stop()`` ended the start.
        """
        if self._stop_task is not None:
            raise SandboxInfraError("The Vercel sandbox is stopped; it cannot start again.")
        self._starting = asyncio.ensure_future(self._start())
        try:
            await self._starting
        except BaseException as error:
            if self._stop_task is None:
                self._stop_task = asyncio.ensure_future(self._teardown(error))
            await asyncio.shield(self._stop_task)
            current = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and current is not None and not current.cancelling():
                # Only stop() cancelled the start task; the caller was not
                # cancelled, so it must not see a CancelledError.
                raise SandboxInfraError("The Vercel sandbox stopped during start.") from None
            raise

    async def _start(self) -> None:
        """Make the client, then wait for capacity, reserve and create the box."""
        with self.timings.phase("create"):
            if self._client is None:
                self._client = self._client_factory(self._capture)
            await create_with_capacity(
                self._create,
                self._reserve,
                at_capacity=self._at_capacity,
                bucket=self._pacing.creates if self._pacing else None,
                clock=self._clock,
            )
        if self._services:
            with self.timings.phase("services"):
                await start_services(self, self._services, self._services_up)

    async def _create(self, usage: Any | None) -> None:
        """Send the create call and confirm the session.

        Args:
            usage: The usage reservation, or ``None``.

        Raises:
            ProviderAtCapacityError: Vercel answered 429 for an unprotected box.
        """
        initial = NetworkPolicy(disabled=self.spec.network_disabled, allowed_hosts=tuple(self.spec.allowed_hosts))
        first_ms = self._first_ms()
        deadline = self._clock.monotonic() + CREATE_TIMEOUT
        try:
            async with asyncio.timeout(CREATE_TIMEOUT):
                while True:
                    operation = self._client.create_sandbox(
                        name=self.name,
                        image=None if self.snapshot_id else self._image(),
                        source=SnapshotSource(snapshot_id=self.snapshot_id) if self.snapshot_id else None,
                        ports=[],
                        execution_time_limit=timedelta(milliseconds=first_ms),
                        resources=self._resources(),
                        persistent=False,
                        network_policy=self._sdk_policy(initial, update=False),
                        env=dict(self.spec.env),
                        tags=dict(self.spec.tags),
                    )
                    self._create_dispatched = True
                    try:
                        self._box = await operation
                        break
                    except Exception as error:
                        if SandboxApiError is None or not isinstance(error, SandboxApiError):
                            raise
                        if error.status_code == 429 and usage is None:
                            raise ProviderAtCapacityError("Vercel has no sandbox capacity.") from error
                        # The image or snapshot is still being prepared; Vercel
                        # made no box, so this is a provider state, not a failed attempt.
                        if error.code != "image_not_ready" or self._clock.monotonic() >= deadline:
                            raise
                    await self._clock.sleep(IMAGE_RETRY_DELAY)
        except TimeoutError as error:
            raise StartTimeoutError(CREATE_TIMEOUT) from error
        session = self._box.current_session
        if session is None:
            raise SandboxInfraError("The Vercel sandbox has no running session.")
        self._session = session
        if usage is not None:
            self._funded_until = self._clock.monotonic() + first_ms / 1000
            await self._billing(usage.confirm_created, session)
        if (initial.disabled or initial.allowed_hosts) and not policy_confirmed(initial, session.network_policy):
            raise SandboxInfraError("Vercel did not confirm the sandbox network policy.")
        whole_ms = math.ceil(self.spec.lifetime_seconds * 1000)
        if usage is not None and first_ms < whole_ms:
            self._renewer = asyncio.ensure_future(self._renew(usage, session, first_ms, whole_ms))

    async def _renew(self, usage: Any, session: Any, funded_ms: int, ceiling_ms: int) -> None:
        """Fund and extend the session one slice at a time.

        Args:
            usage: The usage reservation.
            session: The runtime session to extend.
            funded_ms: The lifetime that is already funded and granted.
            ceiling_ms: The lifetime that the spec asks for.
        """
        started = self._clock.monotonic()
        while funded_ms < ceiling_ms:
            ends = started + funded_ms / 1000
            await self._clock.sleep(max(0.0, ends - RENEW_LEAD - self._clock.monotonic()))
            target = min(funded_ms + SLICE_MS, ceiling_ms)
            deadline = time.monotonic() + max(0.0, ends - FUND_MARGIN - self._clock.monotonic())
            try:
                await self._billing(usage.extend, target, deadline=deadline)
            except (Exception, BudgetReached) as error:
                # Without funding the box keeps its funded limit and Vercel ends
                # it there; _funded_until stays put, so later work is refused.
                logger.warning("Sandbox %s slice was not funded: %s", self.name, type(error).__name__)
                return
            try:
                updated = await session.extend_execution_time_limit(timedelta(milliseconds=target - funded_ms))
                await self._billing(usage.confirm_extended, updated)
            except Exception as error:
                logger.warning("Sandbox %s extension was not confirmed: %s", self.name, type(error).__name__)
                self._halted = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(session.stop(), STOP_TIMEOUT)
                return
            funded_ms = target
            self._funded_until = started + funded_ms / 1000

    async def _billing(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one reservation call in a thread that a cancel cannot cut short.

        The teardown awaits the same call before it settles, so a settle never
        races an extend that is still running.

        Args:
            call: The reservation method.
            *args: Its positional arguments.
            **kwargs: Its keyword arguments.

        Returns:
            The result of the call.
        """
        self._billing_call = asyncio.ensure_future(asyncio.to_thread(call, *args, **kwargs))
        return await asyncio.shield(self._billing_call)

    async def stop(self) -> None:
        """Stop the box once and settle its reservation. Safe to call again.

        Raises:
            UsagePendingError: The settle failed, so the reservation stays pending.
            BudgetError: Trusted usage violates the reserved bounds.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._timed_teardown())
        await asyncio.shield(self._stop_task)
        # Like the legacy close, only one stop caller gets a settle failure,
        # so a second close returns quietly. A stop that timed out before this
        # line leaves the error for the close retry.
        settle_error, self._settle_error = self._settle_error, None
        if settle_error is not None:
            raise settle_error

    async def _timed_teardown(self) -> None:
        """Run the teardown inside the stop phase."""
        with self.timings.phase("stop"):
            await self._teardown(None)

    async def _teardown(self, error: BaseException | None) -> None:
        """Stop the renewer and the box, settle the reservation and close the client.

        Args:
            error: The start failure, or ``None`` for a normal stop.
        """
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the start first, so no box or
            # reservation appears after this teardown has looked for it.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        if self._renewer is not None:
            self._renewer.cancel()
            with contextlib.suppress(BaseException):
                await self._renewer
        if self._billing_call is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(self._billing_call)
        box = self._box
        if box is None and self._create_dispatched and self._client is not None:
            # The create may have reached Vercel before the error or cancel.
            try:
                box = await asyncio.wait_for(self._client.get_sandbox(name=self.name), STOP_TIMEOUT)
            except Exception as lookup_error:
                logger.warning("Sandbox %s lookup failed: %s", self.name, type(lookup_error).__name__)
        # The snapshot already stopped the session, so do not stop or destroy
        # the builder here. Other pods look the snapshot up by its source
        # sandbox name. Vercel docs do not say if the builder record stays
        # after a snapshot of a persistent=False box; that is unverified.
        if box is not None and not self._snapshotted:
            try:
                await asyncio.wait_for(box.stop(), STOP_TIMEOUT)
            except Exception as stop_error:
                logger.warning("Sandbox %s did not stop: %s", self.name, type(stop_error).__name__)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(box.destroy(), STOP_TIMEOUT)
        usage = self._usage
        if usage is not None:
            try:
                if box is not None:
                    await asyncio.to_thread(usage.settle)
                else:
                    await asyncio.to_thread(usage.fail, error or SandboxInfraError("The sandbox was never created."))
            except Exception as billing_error:
                logger.warning("Sandbox %s usage stays pending: %s", self.name, type(billing_error).__name__)
                if box is not None:
                    self._settle_error = _settle_failure(billing_error)
        if self._client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._client.aclose(), STOP_TIMEOUT)

    def _require_session(self) -> Any:
        """Return the funded runtime session of the running box.

        Work goes to the session handle, never to the SDK ``Sandbox``: the
        sandbox resumes a stopped session on its own, and Skynet never funds
        or captures that new session.

        Returns:
            The SDK runtime session.

        Raises:
            SandboxInfraError: The box is not running, its session stopped, or
                its funded lifetime is over.
        """
        if self._box is None or self._session is None or self._stop_task is not None or self._halted:
            raise SandboxInfraError("The Vercel sandbox is not running.")
        if self._funded_until is not None and self._clock.monotonic() >= self._funded_until:
            raise SandboxInfraError("The Vercel sandbox has no funded lifetime left.")
        return self._session

    def _session_stopped(self, error: BaseException) -> SandboxInfraError:
        """Mark the box halted after its session ended under a call.

        Args:
            error: The SDK error that reported the stop.

        Returns:
            The infra error to raise.
        """
        self._halted = True
        return SandboxInfraError(f"The Vercel sandbox session stopped: {type(error).__name__}")

    @contextlib.contextmanager
    def _stop_guard(self) -> Iterator[None]:
        """Turn a session-stopped SDK error under a file or policy call into an infra error.

        Yields:
            Control to the guarded session calls.

        Raises:
            SandboxInfraError: The session stopped.
        """
        try:
            yield
        except Exception as error:
            if _is_session_stopped(error):
                raise self._session_stopped(error) from error
            raise

    def _cwd(self, session: Any) -> str | None:
        """Return the default working directory of the box.

        Args:
            session: The SDK runtime session.

        Returns:
            The session directory, or the box directory when the session has none.
        """
        return session.cwd or self._box.cwd

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
        """Run one shell command and wait for its exit code without polling its status.

        Args:
            command: The shell command.
            cwd: The working directory, or the box default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` means no limit.
            on_output: Receives each new ``(stream, text)`` chunk while the
                command runs, and the rest after it ends. The chunks of one
                stream join to its full text.
            sudo: Run the command as root.

        Returns:
            The exit code and the captured output.

        Raises:
            SandboxInfraError: The exit code could not be read, or the
                command outlived its kill deadline.
            Exception: The output sink failed; this error wins over the deadline.
        """
        session = self._require_session()
        if env:
            self._secret_exposed = True
        with self.timings.phase("run"):
            token = uuid.uuid4().hex
            sentinel = f"{EXIT_PREFIX}{token}"
            out_path = f"{OUTPUT_PREFIX}{token}.out"
            err_path = f"{OUTPUT_PREFIX}{token}.err"
            inner = f"bash {' '.join(SHELL_FLAGS)} {shlex.quote(command)}"
            kill_after = None
            if timeout_sec is not None:
                inner = f"timeout --signal=KILL {math.ceil(timeout_sec)}s {inner}"
                kill_after = min(timeout_sec + KILL_GRACE, KILL_AFTER_MAX)
            script = f"{inner} > {out_path} 2> {err_path}; echo $? > {sentinel}"
            process = await self._create_process(session, script, cwd=cwd, env=env, sudo=sudo, kill_after=kill_after)
            sink = on_output or current_output_sink()
            tails = (_OutputTail("stdout", out_path), _OutputTail("stderr", err_path))
            # A model mailbox answers frames only while the command runs, so
            # output that arrives after the exit would hang the guest (SKY-529).
            poller = asyncio.ensure_future(self._stream_output(session, tails, sink)) if sink is not None else None
            try:
                try:
                    if kill_after is None:
                        code = await self._wait_unless_sink_fails(session, process, sentinel, poller)
                    else:
                        async with asyncio.timeout(kill_after + POLL_SLACK):
                            code = await self._wait_unless_sink_fails(session, process, sentinel, poller)
                except TimeoutError as error:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(process.kill(), CANCEL_GRACE)
                    # A sink error can start an abort that outlives the deadline;
                    # the sink error is the cause, so the caller must see it.
                    if poller is not None and poller.done() and not poller.cancelled() and poller.exception():
                        raise poller.exception() from error
                    raise SandboxInfraError("The sandbox command outlived its kill deadline.") from error
                except asyncio.CancelledError:
                    await asyncio.shield(asyncio.ensure_future(self._abort(process)))
                    raise
            finally:
                sink_error = await _stop_poller(poller)
            if sink_error is not None:
                raise sink_error
            # Decode like the streaming decoder, so ``tail.emitted`` counts the same text.
            stdout = await self._read_optional(session, out_path, errors="replace") or ""
            stderr = await self._read_optional(session, err_path, errors="replace") or ""
            if sink is not None:
                for tail, text in zip(tails, (stdout, stderr), strict=True):
                    if text[tail.emitted :]:
                        sink(tail.stream, text[tail.emitted :])
            timed_out = timeout_sec is not None and code in TIMEOUT_CODES
            return ExecResult(exit_code=code, stdout=stdout, stderr=stderr, timed_out=timed_out)

    async def _create_process(
        self,
        session: Any,
        script: str,
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        sudo: bool,
        kill_after: float | None,
    ) -> Any:
        """Start the wrapped command, resending only when the request never arrived.

        Args:
            session: The SDK runtime session.
            script: The wrapped shell script.
            cwd: The working directory.
            env: Extra environment variables.
            sudo: Run as root.
            kill_after: The server-side kill deadline, in seconds.

        Returns:
            The SDK process handle.

        Raises:
            SandboxInfraError: The session stopped.
        """
        for attempt in range(1, EXEC_ATTEMPTS + 1):
            try:
                return await session.create_process(
                    "bash",
                    [*SHELL_FLAGS, script],
                    cwd=cwd,
                    env=dict(env) if env else None,
                    sudo=sudo,
                    kill_after=kill_after,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except _CONNECT_ERRORS:
                if attempt == EXEC_ATTEMPTS:
                    raise
                if self._pacing is not None:
                    await self._pacing.reads.acquire()
            except Exception as error:
                if _is_session_stopped(error):
                    raise self._session_stopped(error) from error
                raise
        raise AssertionError("unreachable")

    async def _stream_output(self, session: Any, tails: Sequence[_OutputTail], sink: OutputSink) -> None:
        """Send new command output to the sink until the caller cancels this task.

        Args:
            session: The SDK runtime session.
            tails: The read state of each output file.
            sink: Receives each new ``(stream, text)`` chunk.
        """
        failures = 0
        while True:
            await self._clock.sleep(OUTPUT_POLL)
            if self._pacing is not None:
                await self._pacing.reads.acquire()
            pairs = [value for tail in tails for value in (str(tail.offset + 1), tail.path)]
            try:
                # Not ``wait_for``: on 3.11 it swallows a cancel that lands as the
                # read ends, and then ``_stop_poller`` waits forever.
                async with asyncio.timeout(OUTPUT_POLL_TIMEOUT):
                    completed = await session.run_process(
                        "bash",
                        [*SHELL_FLAGS, TAIL_SCRIPT, "tail", *pairs],
                        capture_output=True,
                        kill_after=OUTPUT_POLL_TIMEOUT,
                    )
                if completed.returncode != 0:
                    raise RuntimeError(f"The tail script exited with {completed.returncode}: {completed.stderr}")
                chunks = [base64.b64decode(line) for line in completed.stdout.split("\n")[: len(tails)]]
            except Exception:
                failures += 1
                # A missed poll only delays output: the next poll or the final read sends it.
                # A poll that keeps failing starves a model mailbox, so it must be visible.
                level = logging.WARNING if failures == OUTPUT_POLL_WARN_AFTER else logging.DEBUG
                logger.log(level, "A sandbox output poll failed (%d in a row).", failures, exc_info=True)
                continue
            failures = 0
            for tail, chunk in zip(tails, chunks, strict=False):
                # A stale, shorter read gives no bytes, so the offset never moves back.
                tail.offset += len(chunk)
                text = tail.decoder.decode(chunk)
                if text:
                    tail.emitted += len(text)
                    sink(tail.stream, text)

    async def _wait_unless_sink_fails(
        self, session: Any, process: Any, sentinel: str, poller: asyncio.Future[None] | None
    ) -> int:
        """Wait for the command, but abort it as soon as the output sink fails.

        Args:
            session: The SDK runtime session.
            process: The SDK process handle.
            sentinel: The path of the exit code file.
            poller: The output poller task, or ``None`` when no sink exists.

        Returns:
            The exit code.

        Raises:
            Exception: The sink error that ended the poller. A command whose
                output nobody reads would only burn paid sandbox time.
        """
        if poller is None:
            return await self._wait(session, process, sentinel)
        waiter = asyncio.ensure_future(self._wait(session, process, sentinel))
        try:
            await asyncio.wait({waiter, poller}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            waiter.cancel()
            raise
        if waiter.done():
            return waiter.result()
        waiter.cancel()
        await asyncio.wait({waiter})
        await asyncio.shield(asyncio.ensure_future(self._abort(process)))
        raise poller.exception()

    async def _wait(self, session: Any, process: Any, sentinel: str) -> int:
        """Wait on the long poll; fall back to the sentinel file after many cuts.

        Args:
            session: The SDK runtime session.
            process: The SDK process handle.
            sentinel: The path of the exit code file.

        Returns:
            The exit code.

        Raises:
            SandboxInfraError: The session stopped, or every wait failed and
                the sentinel has no code.
        """
        errors = 0
        while True:
            try:
                return await process.wait()
            except Exception as error:
                if _is_session_stopped(error):
                    raise self._session_stopped(error) from error
                if not _is_retryable_wait_error(error):
                    raise
                errors += 1
                if errors >= WAIT_ERROR_LIMIT:
                    code = await self._read_exit(session, sentinel)
                    if code is None:
                        # The command may still run and use the funded lifetime.
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(process.kill(), CANCEL_GRACE)
                        raise SandboxInfraError("The sandbox command exit code could not be read.") from error
                    return code
                if self._pacing is not None:
                    await self._pacing.reads.acquire()
                await self._clock.sleep(min(errors * WAIT_BACKOFF, WAIT_BACKOFF_MAX))

    async def _read_exit(self, session: Any, sentinel: str) -> int | None:
        """Read the exit code that the wrapper wrote.

        Args:
            session: The SDK runtime session.
            sentinel: The path of the exit code file.

        Returns:
            The code, or ``None`` when the file is missing or unreadable.
        """
        try:
            text = await self._read_optional(session, sentinel)
        except Exception:
            return None
        try:
            return int(text.strip()) if text else None
        except ValueError:
            return None

    async def _abort(self, process: Any) -> None:
        """Stop a cancelled command: terminate, wait a short grace, then kill.

        Args:
            process: The SDK process handle.
        """
        # The caller shields this abort, so every call needs its own limit;
        # otherwise a stalled HTTP call keeps the box alive past the cancel.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.terminate(), CANCEL_GRACE)
        try:
            await asyncio.wait_for(process.wait(), CANCEL_GRACE)
        except Exception:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.kill(), CANCEL_GRACE)

    async def _read_optional(self, session: Any, path: str, *, errors: str = "strict") -> str | None:
        """Read one text file when it exists.

        Args:
            session: The SDK runtime session.
            path: The sandbox path.
            errors: The UTF-8 decoding error policy.

        Returns:
            The content, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The session stopped.
        """
        cwd = self._cwd(session)
        with self._stop_guard():
            if not await session.fs.exists(path, cwd=cwd):
                return None
            return await session.fs.read_text(path, cwd=cwd, errors=errors)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the box.

        Args:
            files: Maps each sandbox path to its text content.

        Raises:
            SandboxInfraError: The session stopped.
        """
        session = self._require_session()
        cwd = self._cwd(session)
        with self._stop_guard():
            for path, text in files.items():
                parent = path.rsplit("/", 1)[0] if "/" in path else ""
                if parent:
                    await session.fs.mkdir(parent, cwd=cwd, recursive=True)
                await session.fs.write_text(path, text, cwd=cwd)

    async def download_file(self, path: str) -> str | None:
        """Read one text file from the box.

        Args:
            path: The sandbox path.

        Returns:
            The content, or ``None`` when the file does not exist.
        """
        return await self._read_optional(self._require_session(), path)

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a policy to the session and read it back.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The session stopped, or the readback does not
                prove the policy.
        """
        session = self._require_session()
        with self._stop_guard():
            updated = await session.update_network_policy(self._sdk_policy(policy, update=True))
        if not policy_confirmed(policy, updated.network_policy):
            raise SandboxInfraError("Vercel did not confirm the sandbox network policy.")

    async def snapshot(self, *, run_deadline: datetime, now: datetime | None = None) -> BuiltSnapshot:
        """Turn the set-up box into the run snapshot. The box stops here.

        Call it after setup and before any secret enters the box. A failed
        snapshot is not retried: the call may have reached Vercel.

        Args:
            run_deadline: The latest time the run can end.
            now: The current time, or ``None`` for the wall clock.

        Returns:
            The snapshot that trial boxes start from.

        Raises:
            SnapshotRefusedError: The box holds env values or injected headers.
            SandboxInfraError: The box is not running or the snapshot did not finish.
        """
        refuse_secrets(self.spec)
        if self._secret_exposed:
            raise SnapshotRefusedError("A box that ran a command with env values cannot become a snapshot.")
        session = self._require_session()
        now = now or datetime.now(run_deadline.tzinfo)
        expiration = snapshot_expiration(run_deadline, now)
        # D5: a snapshot never holds a running service; each box from the
        # snapshot starts its own services. The stop runs before the hold, so
        # a cancel here leaves no reserved hold without its pending call.
        await stop_services(self, self._services_up)
        reservation = None
        if self._budget is not None:
            # The expiry slides from the last use, so the hold covers a last use
            # at the deadline plus the expiry, not only the expiry.
            retention = snapshot_hold_retention(run_deadline, now)
            request = {"session_id": session.id, "retention_ms": math.ceil(retention.total_seconds() * 1000)}
            reservation = await asyncio.to_thread(
                self._snapshot_usage_factory,
                self._budget,
                request,
                operation_key=f"{self.spec.operation_key}:snapshot",
            )
        self._halted = True
        # The snapshot stops the session, so a renewer would fund time nobody uses.
        if self._renewer is not None:
            self._renewer.cancel()
            with contextlib.suppress(BaseException):
                await self._renewer
        try:
            with self.timings.phase("snapshot"), self._stop_guard():
                async with asyncio.timeout(SNAPSHOT_TIMEOUT):
                    created = await session.snapshot(expiration=expiration)
            if created.status != "created":
                raise SandboxInfraError(f"The Vercel snapshot ended as {created.status}.")
            if reservation is not None:
                try:
                    await asyncio.to_thread(reservation.bind, created.id)
                except BaseException:
                    # No pod owns an unbound snapshot, so delete it now. The
                    # delete settles the hold; teardown still removes the builder.
                    orphan = BuiltSnapshot(created.id, self.name, created.expires_at, reservation)
                    with contextlib.suppress(Exception):
                        await self.delete_snapshot(orphan, client=self._client)
                    raise
            self._snapshotted = True
        except BaseException as error:
            if reservation is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(reservation.pending)
            if isinstance(error, TimeoutError):
                raise SandboxInfraError("The Vercel snapshot did not finish in time.") from error
            raise
        return BuiltSnapshot(
            snapshot_id=created.id,
            builder_name=self.name,
            expires_at_ms=created.expires_at,
            reservation=reservation,
        )

    @classmethod
    async def find_snapshot(cls, builder_name: str) -> BuiltSnapshot | None:
        """Find a live snapshot that another pod built from a builder box.

        Args:
            builder_name: The stable name of the builder box.

        Returns:
            The newest live snapshot, or ``None``. Its hold belongs to the pod
            that built it.
        """
        client = cls._make_client(None)
        try:
            now_ms = int(time.time() * 1000)
            async for found in client.query_snapshots(name=builder_name, sort_order="desc"):
                if found.status == "created" and (found.expires_at is None or found.expires_at > now_ms):
                    return BuiltSnapshot(
                        snapshot_id=found.id, builder_name=builder_name, expires_at_ms=found.expires_at
                    )
            return None
        finally:
            await _close_client(client)

    @classmethod
    async def delete_snapshot(cls, built: BuiltSnapshot, *, client: Any | None = None) -> None:
        """Delete a run snapshot, settle its storage, then remove the builder box.

        Args:
            built: The snapshot to delete.
            client: The SDK client, or ``None`` for one from the settings.

        Raises:
            Exception: The provider delete failed; the hold stays pending.
        """
        owned = client is None
        client = client or cls._make_client(None)
        try:
            try:
                handle = await asyncio.wait_for(client.get_snapshot(snapshot_id=built.snapshot_id), STOP_TIMEOUT)
                if handle.status != "deleted":
                    await asyncio.wait_for(handle.delete(), STOP_TIMEOUT)
            except BaseException:
                if built.reservation is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(built.reservation.pending)
                raise
            if built.reservation is not None:
                state = {
                    name: getattr(handle, name, None)
                    for name in ("id", "status", "size_bytes", "created_at", "updated_at", "expires_at")
                }
                await asyncio.to_thread(built.reservation.settle, state)
            try:
                box = await asyncio.wait_for(client.get_sandbox(name=built.builder_name), STOP_TIMEOUT)
                await asyncio.wait_for(box.destroy(), STOP_TIMEOUT)
            except Exception as error:
                # The snapshot is already deleted and settled, and a
                # persistent=False builder may have no record after the snapshot.
                if _is_not_found(error):
                    logger.info("Snapshot builder %s has no record to remove.", built.builder_name)
                else:
                    logger.warning("Snapshot builder %s was not removed: %s", built.builder_name, type(error).__name__)
        finally:
            if owned:
                await _close_client(client)

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every live Vercel sandbox that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandboxes that the sweep stopped.
        """
        client = cls._make_client(None)
        stopped = 0
        try:
            query = SandboxQueryByCreatedAt(tag=TagFilter(key=JOB_TAG, value=job_id))
            async for box in client.query_sandboxes(query=query):
                if str(box.status) not in _LIVE_STATUSES:
                    continue
                try:
                    await asyncio.wait_for(box.stop(), STOP_TIMEOUT)
                    stopped += 1
                except Exception as error:
                    logger.warning("Job sandbox %s did not stop: %s", box.name, type(error).__name__)
        finally:
            try:
                await asyncio.wait_for(client.aclose(), STOP_TIMEOUT)
            except Exception as error:
                logger.warning("Job sandbox client for %s did not close: %s", job_id, type(error).__name__)
        return stopped

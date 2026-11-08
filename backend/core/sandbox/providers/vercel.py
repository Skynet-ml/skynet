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
- Harbor's snapshot, directory transfer and log streaming features are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import shlex
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

import httpx

from core.billing.operation_pricing import json_fingerprint
from core.billing.signals import BudgetReached
from core.billing.vercel_usage import SANDBOX_NETWORK_BYTES_CAP, VercelUsageReservation
from core.config import settings

from ..capabilities import VERCEL_CAPABILITIES, SandboxSpecError, resolve_resources
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..pacing import SYSTEM_CLOCK, Clock, ProviderAtCapacityError, ProviderPacing, create_with_capacity
from ..resources import ResourceMode, SandboxSpec
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

        Raises:
            SandboxSpecError: The spec is invalid, or a protected box has no
                operation key.
        """
        if budget is not None:
            if spec.operation_key is None:
                raise SandboxSpecError("A protected Vercel sandbox needs an operation key.")
            # A protected box injects no headers: credentials stay in the
            # control plane, outside the guest.
            self.capabilities = dataclasses.replace(VERCEL_CAPABILITIES, protected=True, injects_headers=False)
        super().__init__(spec)
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
        self._stop_task: asyncio.Future[None] | None = None
        self._halted = False
        self._funded_until: float | None = None

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
        reservation before the error goes on.

        Raises:
            StartTimeoutError: The create did not finish in time.
            SandboxInfraError: The box came back without a usable session.
        """
        try:
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
        except BaseException as error:
            if self._stop_task is None:
                self._stop_task = asyncio.ensure_future(self._teardown(error))
            await asyncio.shield(self._stop_task)
            raise

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
                        image=self._image(),
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
                        # The image build is still running; Vercel made no box.
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
            await asyncio.to_thread(usage.confirm_created, session)
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
        """Stop the box once and settle its reservation. Safe to call again."""
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._timed_teardown())
        await asyncio.shield(self._stop_task)

    async def _timed_teardown(self) -> None:
        """Run the teardown inside the stop phase."""
        with self.timings.phase("stop"):
            await self._teardown(None)

    async def _teardown(self, error: BaseException | None) -> None:
        """Stop the renewer and the box, settle the reservation and close the client.

        Args:
            error: The start failure, or ``None`` for a normal stop.
        """
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
        if box is not None:
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
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()

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
        """Run one shell command and wait for its exit code without polling.

        Args:
            command: The shell command.
            cwd: The working directory, or the box default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` means no limit.
            on_output: Receives ``(stream, text)`` after the command ends.
            sudo: Run the command as root.

        Returns:
            The exit code and the captured output.

        Raises:
            SandboxInfraError: The exit code could not be read, or the
                command outlived its kill deadline.
        """
        session = self._require_session()
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
            try:
                if kill_after is None:
                    code = await self._wait(session, process, sentinel)
                else:
                    async with asyncio.timeout(kill_after + POLL_SLACK):
                        code = await self._wait(session, process, sentinel)
            except TimeoutError as error:
                with contextlib.suppress(Exception):
                    await process.kill()
                raise SandboxInfraError("The sandbox command outlived its kill deadline.") from error
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.ensure_future(self._abort(process)))
                raise
            stdout = await self._read_optional(session, out_path) or ""
            stderr = await self._read_optional(session, err_path) or ""
            sink = on_output or current_output_sink()
            if sink is not None:
                if stdout:
                    sink("stdout", stdout)
                if stderr:
                    sink("stderr", stderr)
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
        with contextlib.suppress(Exception):
            await process.terminate()
        try:
            await asyncio.wait_for(process.wait(), CANCEL_GRACE)
        except Exception:
            with contextlib.suppress(Exception):
                await process.kill()

    async def _read_optional(self, session: Any, path: str) -> str | None:
        """Read one text file when it exists.

        Args:
            session: The SDK runtime session.
            path: The sandbox path.

        Returns:
            The content, or ``None`` when the file does not exist.
        """
        cwd = self._cwd(session)
        if not await session.fs.exists(path, cwd=cwd):
            return None
        return await session.fs.read_text(path, cwd=cwd)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the box.

        Args:
            files: Maps each sandbox path to its text content.
        """
        session = self._require_session()
        cwd = self._cwd(session)
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
            SandboxInfraError: The readback does not prove the policy.
        """
        updated = await self._require_session().update_network_policy(self._sdk_policy(policy, update=True))
        if not policy_confirmed(policy, updated.network_policy):
            raise SandboxInfraError("Vercel did not confirm the sandbox network policy.")

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
            with contextlib.suppress(Exception):
                await client.aclose()
        return stopped

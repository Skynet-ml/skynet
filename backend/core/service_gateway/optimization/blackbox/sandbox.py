"""Sandbox runtimes for optimizers, scorers, and agent targets.

Protected jobs enter one parent-owned Vercel sandbox before the
optimizer starts. Scorer and agent commands then use private workspaces inside
that boundary so they do not create duplicate managed-sandbox charges.
:class:`SandboxRuntime` / :class:`SandboxSession` are the seams tests fake;
:class:`VercelSandboxRuntime` is the managed provider implementation, and
:class:`LocalSubprocessRuntime` remains an explicit test adapter.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import math
import posixpath
import shlex
import sys
import threading
import time
import types
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from ....billing.operation_pricing import json_fingerprint
from ....billing.runtime import BudgetRuntime
from ....billing.signals import BudgetReached
from ....billing.vercel_usage import SANDBOX_NETWORK_BYTES_CAP, VercelUsageReservation
from ....config import Settings
from ....exceptions import ServiceError
from . import docker_sandbox
from . import sandbox_runtime as _sandbox_runtime
from .sandbox_contract import (
    _TIMEOUT_EXIT_CODES,
    JOB_TAG,
    KILL_GRACE_SECONDS,
    SANDBOX_TAG,
    CommandResult,
    OutputSink,
    SandboxRuntime,
    SandboxSession,
    SandboxSpec,
    _brief,
)

# Re-exported: the scorer, agent and native runtimes import the name from here.
from .sandbox_contract import (
    unique_sandbox_name as unique_sandbox_name,
)
from .sandbox_log import logged_command

# Re-exported (SKY-486): callers and tests still import these names from here until SKY-433.
from .sandbox_runtime import (
    _ASYNC_CLOSE_BUDGET_SECONDS as _ASYNC_CLOSE_BUDGET_SECONDS,
)
from .sandbox_runtime import (
    _BUDGET_RELAY_ENV as _BUDGET_RELAY_ENV,
)
from .sandbox_runtime import (
    _CODEX_REQUIREMENTS_CHECK as _CODEX_REQUIREMENTS_CHECK,
)
from .sandbox_runtime import (
    _CODEX_REQUIREMENTS_COMMAND as _CODEX_REQUIREMENTS_COMMAND,
)
from .sandbox_runtime import (
    _CODEX_REQUIREMENTS_READBACK as _CODEX_REQUIREMENTS_READBACK,
)
from .sandbox_runtime import (
    _LOCAL_ENV_PASSTHROUGH as _LOCAL_ENV_PASSTHROUGH,
)
from .sandbox_runtime import (
    _LOCAL_KILLED_EXIT_CODE as _LOCAL_KILLED_EXIT_CODE,
)
from .sandbox_runtime import (
    _PRIVATE_ROOT_MODE as _PRIVATE_ROOT_MODE,
)
from .sandbox_runtime import (
    _RELAY_ENDPOINT_ENVS as _RELAY_ENDPOINT_ENVS,
)
from .sandbox_runtime import (
    _RUNTIME_OVERRIDE as _RUNTIME_OVERRIDE,
)
from .sandbox_runtime import (
    _SHELL_FLAGS,
    COVERAGE_SLICE_SECONDS,
    AsyncBridgeRuntime,
    current_sandbox_runtime,
)
from .sandbox_runtime import (
    _TOOL_RELAY_ENV as _TOOL_RELAY_ENV,
)
from .sandbox_runtime import (
    CODEX_REQUIREMENTS as CODEX_REQUIREMENTS,
)
from .sandbox_runtime import (
    CODEX_REQUIREMENTS_PATH as CODEX_REQUIREMENTS_PATH,
)
from .sandbox_runtime import (
    AsyncBridgeSession as AsyncBridgeSession,
)
from .sandbox_runtime import (
    ContainedSubprocessRuntime as ContainedSubprocessRuntime,
)
from .sandbox_runtime import (
    LocalSubprocessRuntime as LocalSubprocessRuntime,
)
from .sandbox_runtime import (
    LocalSubprocessSession as LocalSubprocessSession,
)
from .sandbox_runtime import (
    _async_spec as _async_spec,
)
from .sandbox_runtime import (
    _bridge_errors as _bridge_errors,
)
from .sandbox_runtime import (
    _contained_environment as _contained_environment,
)
from .sandbox_runtime import (
    _kill_process_group as _kill_process_group,
)
from .sandbox_runtime import (
    _private_root as _private_root,
)
from .sandbox_runtime import (
    _private_root_lock as _private_root_lock,
)
from .sandbox_runtime import (
    _private_root_path as _private_root_path,
)
from .sandbox_runtime import (
    _require_codex_requirements as _require_codex_requirements,
)
from .sandbox_runtime import (
    check_codex_requirements as check_codex_requirements,
)
from .sandbox_runtime import (
    load_provider as load_provider,
)
from .sandbox_runtime import (
    sandbox_runtime_context as sandbox_runtime_context,
)
from .sandbox_runtime import (
    write_codex_requirements as write_codex_requirements,
)

# The SDK is a declared dependency, but an image built from an older
# requirements file must degrade to "agent targets unavailable", not
# fail to import the worker.
try:
    from vercel import api as vercel_api
    from vercel.sandbox import NetworkPolicy, NetworkPolicyRule, NetworkPolicyTransform
    from vercel.sandbox import sync as vercel_sync
except ImportError:  # pragma: no cover
    vercel_api = None
    vercel_sync = None
    NetworkPolicy = NetworkPolicyRule = NetworkPolicyTransform = None

logger = logging.getLogger(__name__)

# The next slice is funded and the box extended this long before the current one ends.
_RENEWAL_LEAD_SECONDS = 180.0
# Waiting for in-flight work to free room for the next slice stops this long before the box would end.
_RENEWAL_MARGIN_SECONDS = 30.0
_RENEWAL_JOIN_SECONDS = 30.0
# Detached processes are watched with short status polls; the delay doubles from
# the floor to the cap while the command is silent. A quiet command is often
# waiting on a mailbox answer that only the next poll can deliver, so the cap
# stays low and the delay drops back to the floor whenever output moved.
_POLL_FLOOR_SECONDS = 0.5
_POLL_CAP_SECONDS = 2.0
# Vercel caps a process kill timer at 18,000,000 ms even though a box itself
# may live 24 hours, so a longer timer fails the whole command with HTTP 400.
_VERCEL_KILL_TIMER_CEILING_SECONDS = 18_000

# Client-side last resort past ``kill_after`` before declaring the process lost.
_POLL_DEADLINE_SLACK_SECONDS = 60.0
# Where a command's wrapper records its exit code for the poll to find.
_EXIT_FILE_PREFIX = "/tmp/.app-exit-"
# A detached command's output is redirected here and read back while it runs.
_OUTPUT_FILE_PREFIX = "/tmp/.app-output-"
_STOPPABLE_STATUSES = frozenset({"pending", "running"})
# Users see these reasons (engine catalog, run errors), so they never name the
# vendor; the operator log at the point of detection names the package and env vars.
_PACKAGE_MISSING = "The cloud sandbox is not available on this server."
_CREDENTIALS_MISSING = "The cloud sandbox is not configured on this server."
# A custom network policy denies every host it does not list; this entry keeps the
# network open, so only the header injection distinguishes it from ``allow-all``.
_ANY_HOST = "*"


@dataclass(frozen=True)
class VercelCredentials:
    """Explicit Vercel credentials, so the worker never depends on the SDK's env lookup."""

    token: str
    team_id: str
    project_id: str


@dataclass
class _OutputFile:
    """One output stream of a detached command: the file it goes to and the text read so far."""

    path: str
    text: str = ""


class VercelSandboxSession:
    """Session over one Vercel sandbox, bound to the SDK session that created it."""

    def __init__(
        self,
        box: Any,
        api_session: Any,
        context: contextvars.Context,
        usage: VercelUsageReservation | None = None,
        *,
        lifetime_ms: int | None = None,
        env: Mapping[str, str] | None = None,
        owner: str | None = None,
    ) -> None:
        """Wrap an open sandbox.

        Args:
            box: The SDK's managed sandbox handle.
            api_session: The entered ``vercel.api.session`` context that owns it.
            context: The ``contextvars`` context ``api_session`` was entered in.
            usage: Optional pre-dispatch reservation and trusted provider usage collector.
            lifetime_ms: The most the box may live once every slice is funded;
                only a protected box opened for a shorter first slice passes it.
            env: The box's environment, kept so its values are redacted from logged output.
            owner: Job the box belongs to, which its logged output is routed to.
        """
        self._env = dict(env or {})
        self.log_owner = owner
        self._box = box
        # The named-sandbox handle silently resumes stopped VMs. An exact session
        # cannot create another billable lifetime behind the admission boundary.
        self._session = box.current_session
        if self._session is None:
            raise ServiceError("The cloud sandbox did not return its exact execution session.")
        self._api_session = api_session
        self._context = context
        self._cwd = box.cwd
        self._usage = usage
        self._closed = False
        self._started = time.monotonic()
        self._ceiling_ms = lifetime_ms
        self._stopping = threading.Event()
        self._renewer: threading.Thread | None = None
        if usage is not None and lifetime_ms is not None and lifetime_ms > usage.lifetime_ms:
            self._renewer = threading.Thread(target=self._renew, name=f"renew-{box.name}", daemon=True)
            self._renewer.start()

    def _renew(self) -> None:
        """Fund and extend the box one slice at a time until it closes or reaches its ceiling.

        Runs only in the trusted parent: nothing in the box can ask for more
        time. Each slice is admitted by the ledger before Vercel is asked to
        extend, so Vercel's own timer never runs past funded coverage. When
        the budget cannot pay for the next slice, the box is left to end when
        its funded time runs out.
        """
        usage = self._usage
        assert usage is not None
        assert self._ceiling_ms is not None
        while usage.lifetime_ms < self._ceiling_ms:
            ends = self._started + usage.lifetime_ms / 1000
            if self._stopping.wait(max(0.0, ends - _RENEWAL_LEAD_SECONDS - time.monotonic())):
                return
            funded = usage.lifetime_ms
            target = min(funded + COVERAGE_SLICE_SECONDS * 1000, self._ceiling_ms)
            try:
                usage.extend(target, deadline=ends - _RENEWAL_MARGIN_SECONDS)
            except (Exception, BudgetReached) as exc:
                logger.warning("sandbox %s not extended past %ss: %s", self._box.name, funded // 1000, exc)
                return
            if self._stopping.is_set():
                return
            try:
                updated = self._session.extend_execution_time_limit(timedelta(milliseconds=target - funded))
                usage.confirm_extended(updated)
            except Exception:
                # The coverage is already held, so an unconfirmed extension
                # leaves the box over-funded, never under. A limit beyond the
                # funded lifetime ends the box at once instead.
                logger.exception("sandbox %s extension unconfirmed; ending it", self._box.name)
                with contextlib.suppress(Exception):
                    self._session.stop()
                return

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write text files at paths relative to the working directory, creating parents.

        Args:
            files: Relative path → content.
        """
        for path, text in files.items():
            parent = posixpath.dirname(path)
            if parent:
                self._session.fs.mkdir(parent, cwd=self._cwd, recursive=True)
            self._session.fs.write_text(path, text, cwd=self._cwd)

    @logged_command
    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Run ``command`` through ``bash --noprofile --norc -c`` in the working directory and wait for it.

        The process runs detached and is watched with short polls instead of
        one streamed response: Vercel's edge drops a response that stays open
        and silent for minutes, which killed long agent runs midway with
        "missing final metadata". A plain status read never carries a detached
        process's exit code either (the runtime only settles it on a blocking
        wait, which the edge cuts off the same way), so the wrapper records the
        code in a file and the poll watches for that file. Output goes to files
        in the box for the same reason: each poll reads what has landed since
        the last one and hands the new part to ``on_output``, and the files are
        the command's captured output once it exits.

        Args:
            command: Shell command line.
            env: Extra environment for this command.
            timeout_seconds: Kill the command after this long.
            on_output: Receives each new piece of output at every poll.
            sudo: Run as root through the provider's own sudo option.

        Returns:
            Exit code, captured output and whether the timeout fired.

        Raises:
            ServiceError: If the process outlives its kill deadline.
        """
        inner = f"bash {' '.join(_SHELL_FLAGS)} {shlex.quote(command)}"
        kill_after = None
        deadline = None
        seconds = None
        if timeout_seconds is not None:
            seconds = max(1, math.ceil(timeout_seconds))
            inner = f"timeout --signal=KILL {seconds}s {inner}"
            # No command outlives Vercel's five-hour kill-timer maximum, however
            # long its box may live.
            kill_after = min(seconds + KILL_GRACE_SECONDS, _VERCEL_KILL_TIMER_CEILING_SECONDS)
            deadline = time.monotonic() + kill_after + _POLL_DEADLINE_SLACK_SECONDS
        token = uuid.uuid4().hex
        sentinel = f"{_EXIT_FILE_PREFIX}{token}"
        streams = {name: _OutputFile(f"{_OUTPUT_FILE_PREFIX}{token}.{name}") for name in ("stdout", "stderr")}
        started = time.monotonic()
        process = self._session.create_process(
            "bash",
            [*_SHELL_FLAGS, f"{inner} > {streams['stdout'].path} 2> {streams['stderr'].path}; echo $? > {sentinel}"],
            cwd=self._cwd,
            env=dict(env) if env else None,
            kill_after=kill_after,
            sudo=sudo,
        )
        delay = _POLL_FLOOR_SECONDS
        exit_code = None
        while exit_code is None:
            if deadline is not None and time.monotonic() > deadline:
                with contextlib.suppress(Exception):
                    process.kill()
                overrun = time.monotonic() - started - seconds
                raise ServiceError(
                    f"command still running {overrun:.0f}s past its {seconds}s timeout: {_brief(command)}"
                )
            time.sleep(delay)
            delay = min(delay * 2, _POLL_CAP_SECONDS)
            exit_code = self._recorded_exit_code(sentinel)
            if self._forward_output(streams, on_output):
                delay = _POLL_FLOOR_SECONDS
        self._forward_output(streams, on_output)
        return CommandResult(
            exit_code=exit_code,
            stdout=streams["stdout"].text,
            stderr=streams["stderr"].text,
            timed_out=timeout_seconds is not None and exit_code in _TIMEOUT_EXIT_CODES,
        )

    def _forward_output(self, streams: Mapping[str, _OutputFile], on_output: OutputSink | None) -> bool:
        """Re-read the command's output files and hand anything new to ``on_output``.

        Args:
            streams: The output files, by stream name.
            on_output: The sink, or ``None`` to only refresh the captured text.

        Returns:
            Whether either stream had new output.
        """
        moved = False
        for name, stream in streams.items():
            text = stream.text
            with contextlib.suppress(Exception):
                if self._session.fs.exists(stream.path):
                    text = self._session.fs.read_text(stream.path) or ""
            # A shorter read is a stale replica or a race with the shell; keep what was seen.
            if len(text) <= len(stream.text):
                continue
            new = text[len(stream.text) :]
            stream.text = text
            moved = True
            if on_output is not None:
                on_output(name, new)
        return moved

    def _recorded_exit_code(self, sentinel: str) -> int | None:
        """Return the exit code the wrapper wrote to ``sentinel``, or ``None`` while the command runs.

        Args:
            sentinel: Absolute path of the wrapper's exit-code file.

        Returns:
            The exit code once it has landed, else ``None``.
        """
        if not self._session.fs.exists(sentinel):
            return None
        # The shell creates the file before it writes the code, so an empty read is "not yet".
        text = self._session.fs.read_text(sentinel).strip()
        return int(text) if text.isdigit() else None

    def read_file(self, path: str) -> str | None:
        """Return the text of ``path`` (relative to the working directory), or ``None`` when absent.

        Args:
            path: Relative file path.

        Returns:
            The file's text, or ``None``.
        """
        if not self._session.fs.exists(path, cwd=self._cwd):
            return None
        return self._session.fs.read_text(path, cwd=self._cwd)

    def disable_network(self) -> None:
        """Switch the running box to deny-all networking and confirm Vercel applied it.

        Raises:
            ServiceError: When Vercel does not report the box as deny-all afterwards.
        """
        updated = self._session.update_network_policy(NetworkPolicy.deny_all())
        policy = getattr(updated, "network_policy", None)
        mode = getattr(policy, "mode", None)
        # Vercel may echo an empty custom allowlist instead of the deny-all
        # mode; both admit no host, and anything else fails closed.
        if not (mode == "deny-all" or (mode == "custom" and not getattr(policy, "allow", None))):
            raise ServiceError("The cloud sandbox did not confirm that the scoring box's network is off.")
        self._session = updated

    def close(self) -> None:
        """Stop exactly one session, destroy its sandbox, and settle final usage.

        Raises:
            UsagePendingError: When paid usage cannot yet be reconciled.
            BudgetError: When trusted usage violates the reserved bounds.
        """
        if self._closed:
            return
        self._closed = True
        self._stopping.set()
        if self._renewer is not None:
            self._renewer.join(timeout=_RENEWAL_JOIN_SECONDS)
        try:
            if self._usage is None:
                try:
                    self._box.__exit__(None, None, None)
                except Exception:
                    logger.exception("failed to destroy sandbox %s", getattr(self._box, "name", "?"))
            else:
                try:
                    self._session.stop()
                except Exception:
                    logger.exception("failed to confirm stopped sandbox %s", self._box.name)
                try:
                    self._box.destroy()
                except Exception:
                    logger.exception("failed to destroy sandbox %s", self._box.name)
                self._usage.settle()
        finally:
            try:
                self._context.run(self._api_session.__exit__, None, None, None)
            except Exception:
                logger.exception("failed to close the Vercel API session")


def _network_policy(inject_headers: Mapping[str, Mapping[str, str]]) -> Any:
    """Build the policy that adds ``inject_headers`` at the network edge, or ``None`` for an open network.

    Args:
        inject_headers: Host → headers to add to the box's requests to that host.

    Returns:
        A custom ``NetworkPolicy`` that still allows every host, or ``None`` when nothing is injected.
    """
    if not inject_headers:
        return None
    rules = {
        host: (NetworkPolicyRule(transform=(NetworkPolicyTransform(headers=dict(headers)),)),)
        for host, headers in inject_headers.items()
    }
    return NetworkPolicy.custom(allow={_ANY_HOST: (), **rules})


def _allowlist_policy(hosts: tuple[str, ...], inject_headers: Mapping[str, Mapping[str, str]]) -> Any:
    """Build the policy that reaches only ``hosts``, adding ``inject_headers`` at the edge.

    Args:
        hosts: The only destinations the box may reach.
        inject_headers: Host → headers to add to the box's requests to that host.

    Returns:
        A custom ``NetworkPolicy`` without the any-host entry, so it denies everything else.
    """
    return NetworkPolicy.custom(
        allow={
            host: (
                (NetworkPolicyRule(transform=(NetworkPolicyTransform(headers=dict(inject_headers[host])),)),)
                if inject_headers.get(host)
                else ()
            )
            for host in hosts
        }
    )


class VercelSandboxRuntime:
    """Sandbox runtime over the Vercel Sandbox SDK."""

    injects_headers = True

    def __init__(self, credentials: VercelCredentials, *, image: str, budget: BudgetRuntime | None = None) -> None:
        """Bind the runtime to explicit credentials.

        Args:
            credentials: Token, team and project the sandboxes are created under.
            image: Image the boxes boot from unless their spec names one.
            budget: Shared authoritative budget for protected offline runs.

        Raises:
            ServiceError: When the SDK is not installed.
        """
        if vercel_sync is None or vercel_api is None:
            raise ServiceError(_PACKAGE_MISSING)
        self._credentials = credentials
        self._image = image
        self._budget = budget
        self.protected = budget is not None
        self.injects_headers = budget is None

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Create a sandbox per ``spec``.

        Args:
            spec: Lifetime, environment, image, header injection and labels for the sandbox.

        Returns:
            The session over the new sandbox.
        """
        if not math.isfinite(spec.lifetime_seconds) or spec.lifetime_seconds <= 0:
            raise ServiceError("The sandbox lifetime must be finite and positive.")
        if isinstance(spec.vcpus, bool) or spec.vcpus not in {1, *range(2, 33, 2)}:
            raise ServiceError("The sandbox must use 1 or an even number of vCPUs between 2 and 32.")
        if spec.network_disabled and (spec.inject_headers or spec.allowed_hosts):
            raise ServiceError("An offline sandbox cannot inject network credentials.")
        if spec.allowed_hosts and not set(spec.inject_headers) <= set(spec.allowed_hosts):
            raise ServiceError("A sandbox can only inject credentials for hosts it may reach.")
        image = spec.image or self._image
        name = spec.name
        usage = None
        lifetime_ms = math.ceil(spec.lifetime_seconds * 1000)
        first_ms = lifetime_ms
        if self._budget is not None:
            first_ms = min(lifetime_ms, COVERAGE_SLICE_SECONDS * 1000)
            if not spec.operation_key:
                raise ServiceError("A protected sandbox requires a stable operation identity.")
            # The name is the box's idempotency key in Vercel and in its usage
            # reservation, so it keeps the pre-rename prefix.
            name = (
                name
                or "skynet-"
                + json_fingerprint([self._budget.budget_id, self._budget.generation, spec.operation_key])[:48]
            )
            usage = VercelUsageReservation(
                self._budget,
                {
                    "name": name,
                    "image": image,
                    "lifetime_ms": first_ms,
                    "vcpus": spec.vcpus,
                    "network_disabled": spec.network_disabled,
                    **(
                        {
                            "allowed_hosts": sorted(set(spec.allowed_hosts)),
                            "network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP,
                        }
                        if spec.allowed_hosts
                        else {}
                    ),
                    "ports": [],
                    "persistent": False,
                    "environment_fingerprint": json_fingerprint(dict(spec.env)),
                },
                operation_key=spec.operation_key,
            )
        creds = self._credentials
        options = vercel_sync.SandboxServiceOptions(
            credentials_factory=lambda: vercel_sync.SandboxCredentials(
                token=creds.token, team_id=creds.team_id, project_id=creds.project_id
            )
        )
        # The SDK finds its credentials through a context variable bound by
        # ``session()``. The bind and its reset must happen in one ``Context``,
        # yet a box is routinely opened on a holdout worker thread and closed
        # from the job's main thread, so each box gets a private copy of the
        # caller's context that ``close()`` re-enters. The box itself keeps a
        # reference to its service, so nothing after creation needs the
        # variable, and the copy leaves the caller's own context untouched.
        context = contextvars.copy_context()
        # The SDK's default HTTP client gives any read 60 seconds. Commands are
        # watched with short polls rather than one long stream, but the
        # post-exit log replay of a chatty command can still take longer, so
        # reads get the box lifetime — nothing in a box outlives the box.
        read_ceiling = max(spec.lifetime_seconds, 60.0)
        api_session = vercel_api.session(
            service_options=[options],
            httpx_client_factory=lambda: httpx.Client(
                timeout=httpx.Timeout(60.0, read=read_ceiling),
                event_hooks={"response": [usage.capture_response]} if usage is not None else None,
            ),
        )
        box = None
        try:
            context.run(api_session.__enter__)
            box = context.run(
                vercel_sync.create_sandbox,
                name=name,
                image=image,
                execution_time_limit=first_ms / 1000,
                resources=vercel_sync.SandboxResources(vcpus=spec.vcpus, memory=spec.vcpus * 2048),
                persistent=False,
                ports=[],
                env=dict(spec.env) or None,
                network_policy=(
                    NetworkPolicy.deny_all()
                    if spec.network_disabled
                    else _allowlist_policy(spec.allowed_hosts, spec.inject_headers)
                    if spec.allowed_hosts
                    else _network_policy(spec.inject_headers)
                ),
                tags={**SANDBOX_TAG, **spec.tags},
            )
            if usage is not None:
                usage.confirm_created(box.current_session)
            return VercelSandboxSession(
                box,
                api_session,
                context,
                usage,
                lifetime_ms=lifetime_ms if usage is not None else None,
                env=spec.env,
                owner=spec.tags.get(JOB_TAG),
            )
        except BaseException as error:
            if box is not None:
                with contextlib.suppress(Exception):
                    box.__exit__(None, None, None)
            if usage is not None:
                usage.fail(error)
            context.run(api_session.__exit__, None, None, None)
            raise

    def stop_job_sandboxes(self, job_id: str) -> int:
        """Stop every box still up under ``job_id``'s tag.

        A job's child process closes its own boxes on the way out, but a
        cancel or a stall kills that process before its ``finally`` blocks
        run, so the worker sweeps the job's boxes by tag once the child is
        gone. Boxes already stopping or stopped are left alone.

        Args:
            job_id: The job whose boxes to stop.

        Returns:
            How many boxes were stopped.
        """
        creds = self._credentials
        options = vercel_sync.SandboxServiceOptions(
            credentials_factory=lambda: vercel_sync.SandboxCredentials(
                token=creds.token, team_id=creds.team_id, project_id=creds.project_id
            )
        )
        query = vercel_sync.SandboxQueryByCreatedAt(tag=vercel_sync.TagFilter(key=JOB_TAG, value=job_id))
        stopped = 0
        with vercel_api.session(service_options=[options]):
            for box in vercel_sync.query_sandboxes(query=query, project_id=creds.project_id):
                if box.status not in _STOPPABLE_STATUSES:
                    continue
                try:
                    box.stop()
                except Exception as exc:  # isolation boundary: one box's failure must not end the sweep
                    logger.warning("could not stop sandbox %s of job %s: %s", box.name, job_id, exc)
                    continue
                logger.info("stopped sandbox %s of job %s", box.name, job_id)
                stopped += 1
        return stopped


def sandbox_unavailable_reason(settings: Settings) -> str | None:
    """Explain why this deployment cannot create agent sandboxes, or return ``None``.

    Args:
        settings: The backend settings.

    Returns:
        A user-facing reason, or ``None`` when sandboxes can be created.
    """
    if current_sandbox_runtime() is not None:
        return None
    if settings.local_mode:
        return docker_sandbox.docker_sandbox_unavailable_reason(settings)
    if vercel_sync is None or vercel_api is None:
        logger.info("Agent sandboxes unavailable: the vercel-sandbox package is not installed.")
        return _PACKAGE_MISSING
    if not (settings.vercel_token and settings.vercel_team_id and settings.vercel_project_id):
        logger.info("Agent sandboxes unavailable: set VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID.")
        return _CREDENTIALS_MISSING
    return None


def sandbox_runtime_from_settings(settings: Settings) -> SandboxRuntime | None:
    """Build the deployment's runtime from settings, or return ``None`` when unconfigured.

    Local mode builds the local Docker runtime; every other deployment builds Vercel.
    With ``sandbox_backend = "async"`` the same provider comes from
    ``core.sandbox`` through the sync bridge instead.

    Args:
        settings: The backend settings.

    Returns:
        A runtime, or ``None`` when :func:`sandbox_unavailable_reason` is set.

    Raises:
        SandboxFatalError: The async backend cannot load its provider.
    """
    if current_sandbox_runtime() is not None:
        return current_sandbox_runtime()
    if sandbox_unavailable_reason(settings) is not None:
        return None
    if settings.sandbox_backend == "async":
        if settings.local_mode:
            return AsyncBridgeRuntime(
                "docker", image=settings.local_sandbox_image or None, docker_bin=settings.docker_bin
            )
        return AsyncBridgeRuntime("vercel")
    if settings.local_mode:
        return docker_sandbox.docker_runtime_from_settings(settings)
    return VercelSandboxRuntime(
        VercelCredentials(
            token=settings.vercel_token.get_secret_value(),
            team_id=str(settings.vercel_team_id),
            project_id=str(settings.vercel_project_id),
        ),
        image=settings.vercel_sandbox_image,
    )


def scorer_runtime_from_settings(settings: Settings) -> SandboxRuntime:
    """Return the current contained runtime, or the deployment's sandbox (Docker in local mode).

    Args:
        settings: The backend settings.

    Returns:
        The managed runtime scorers open their workspaces with.

    Raises:
        ServiceError: When the managed sandbox is not configured.
    """
    if current_sandbox_runtime() is not None:
        return current_sandbox_runtime()
    runtime = sandbox_runtime_from_settings(settings)
    if runtime is not None:
        return runtime
    raise ServiceError(sandbox_unavailable_reason(settings) or _CREDENTIALS_MISSING)


# SKY-486 moved the local and bridge runtimes to ``sandbox_runtime``. Tests still
# patch names such as ``load_provider`` on this module, and that patch must reach
# the moved code, as it did when both lived here. Remove this when SKY-433 moves
# those tests to ``sandbox_runtime``.
class _ForwardingModule(types.ModuleType):
    """Module type that copies each attribute write to ``sandbox_runtime`` when that module owns the name."""

    def __setattr__(self, name: str, value: Any) -> None:
        """Set ``name`` here, and also on ``sandbox_runtime`` when it defines ``name``.

        Args:
            name: The attribute name.
            value: The new value.
        """
        super().__setattr__(name, value)
        if name in vars(_sandbox_runtime):
            setattr(_sandbox_runtime, name, value)


sys.modules[__name__].__class__ = _ForwardingModule

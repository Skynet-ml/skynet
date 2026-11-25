"""The async Hugging Face Sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), hf_sandbox: the ``Sandbox.create``
call with a flavor and an idle timeout, the ``bash -c`` command form, the
exit code of -1 for a command that reports none, and the file write and
read calls come from ``harbor/environments/hf_sandbox.py``. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- A box with a budget is protected (SKY-943). It reserves its whole possible
  lifetime before the create and settles its billed minutes once HF
  confirms that the job is in a terminal stage, also after a failed or
  cancelled start. ``Sandbox.create`` fixes the job timeout at 24 hours with
  no option to change it, so the hold funds those 24 hours plus a start
  margin, and the provider enforces its own cap: a watchdog cancels the job
  at the spec lifetime. A spec lifetime above 24 hours is refused.
- Only the flavors with a pinned rate in ``core.billing.hf_sandbox_usage``
  can start, and the spec size must fit the flavor. The in-box server port
  is always exposed, so the box pays HF's exposed-ports rate.
- Every job gets a unique ``skynet-box`` label. After a failed create the
  provider looks the job up by that label: a found job is cancelled and
  settled, and the hold is released only when HF refused the create and the
  lookup finds no job. A failed lookup keeps the hold pending.
- A create that a cancel or a stop interrupts goes on in one tracked
  background task, which cancels and settles the job when the handle
  arrives, or runs the failed-create lookup.
- The stop cancels the job through ``HfApi.cancel_job`` and waits until
  ``inspect_job`` reports a terminal stage, because ``Sandbox.kill`` hides
  its errors. A job whose end HF does not confirm stays in the job registry,
  so a later stop or ``stop_job_sandboxes`` can retry, and its hold stays
  pending.
- The idle timeout is the spec lifetime, not Harbor's 600 seconds: HF does
  not count a foreground command as activity, so a shorter idle timeout
  could end the box during one long command. The HF token is never sent
  into the box (``forward_hf_token=False``).
- Commands stream their output to the output sink. HF Sandbox has no user
  switch, so ``sudo`` runs the command as the image user, as in Harbor.
- Harbor's Dockerfile and compose builds, the DinD strategy, directory
  transfer and attach are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
import time
import traceback
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any, NamedTuple

from core.billing.budgets import BudgetError
from core.billing.hf_sandbox_usage import FLAVOR_HOURLY_USD, LIFETIME_CEILING_MS, USAGE
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import UsagePendingError

from ..capabilities import EnvironmentCapabilities, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import SandboxError, SandboxFatalError, SandboxInfraError
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    from huggingface_hub import HfApi, Sandbox, get_token
except ImportError:
    HfApi = None
    Sandbox = None
    get_token = None

try:
    from huggingface_hub.errors import HfHubHTTPError
except ImportError:
    HfHubHTTPError = None

try:
    from huggingface_hub.errors import SandboxError as HfSandboxError
except ImportError:
    HfSandboxError = None

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "python:3.12"
DEFAULT_FLAVOR = "cpu-basic"
# The SDK default; the start phase timeout replaces it when one is set.
DEFAULT_START_TIMEOUT = 120.0
STOP_TIMEOUT = 60.0
# How often the stop asks HF whether the cancelled job ended.
POLL_INTERVAL = 2.0
# The SDK timeout should end a command first; this margin only catches a
# hung SDK call.
EXEC_GRACE = 30.0
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124
BOX_LABEL = "skynet-box"
# The JobStage values of huggingface_hub 1.33.0 in which a job no longer bills.
_TERMINAL_STAGES = frozenset({"COMPLETED", "CANCELED", "ERROR", "DELETED"})
# HTTP answers to the run_job request that make no job. 429 and 5xx are left
# out: they do not prove that HF did not start the job. A refused create
# still releases its hold only when the label lookup finds no job.
_REFUSED_CREATE_STATUSES = frozenset({400, 401, 402, 403, 404, 422})
# The steps of huggingface_hub 1.33.0 ``Sandbox.create`` before and after its
# ``run_job`` request; ``_create_outcome`` reads them from the error frames.
_BEFORE_RUN_JOB = frozenset({"_effective_token", "_derive_sandbox_token", "_bootstrap_job_spec"})
_AFTER_RUN_JOB = frozenset({"from_job", "wait_ready"})


class _Flavor(NamedTuple):
    """The size of one HF Jobs flavor."""

    vcpus: int
    memory_mb: int
    gpus: int


# Read from https://huggingface.co/docs/hub/jobs-pricing on 2026-11-24.
FLAVOR_SIZES: Mapping[str, _Flavor] = {
    "cpu-basic": _Flavor(vcpus=2, memory_mb=16 * 1024, gpus=0),
    "cpu-upgrade": _Flavor(vcpus=8, memory_mb=32 * 1024, gpus=0),
    "a10g-small": _Flavor(vcpus=4, memory_mb=15 * 1024, gpus=1),
}

HF_SANDBOX_CAPABILITIES = EnvironmentCapabilities(
    name="hf_sandbox",
    billable=True,
    gpus=True,
    max_vcpus=max(size.vcpus for size in FLAVOR_SIZES.values()),
)

# Process-local, keyed by the job tag (``None`` for untagged boxes): every
# box whose job HF has not confirmed as ended, so a later stop can retry.
_JOB_BOXES: dict[str | None, set[HfSandboxEnvironment]] = {}
# Clock seams, so tests drive the stop wait and the lifetime cap without real time.
_monotonic = time.monotonic
_sleep = time.sleep
_async_sleep = asyncio.sleep
# Strong references to the background tasks, so the event loop does not drop them.
_TASKS: set[asyncio.Future[Any]] = set()


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
        raise SandboxInfraError(f"HF Sandbox could not {action}: {type(error).__name__}.") from error


def _keep_task(task: asyncio.Future[Any], action: str) -> None:
    """Keep a background task alive until it ends, and log its failure then.

    Args:
        task: The running task.
        action: What the task does, for the log.
    """
    _TASKS.add(task)
    task.add_done_callback(functools.partial(_on_task_done, action))


def _on_task_done(action: str, task: asyncio.Future[Any]) -> None:
    """Forget a finished background task and log its failure.

    Args:
        action: What the task did, for the log.
        task: The finished task.
    """
    _TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("HF Sandbox could not %s: %s", action, type(task.exception()).__name__)


def _create_outcome(error: BaseException) -> str:
    """Tell from a create error whether HF can have made the job.

    ``Sandbox.create`` checks the labels, reads the token and builds the job
    spec, then sends the one ``run_job`` request, then waits for the box. The
    frames of the error show which step raised it; the names are those of
    huggingface_hub 1.33.0. An error in no known step counts as unknown.

    Args:
        error: The error of ``Sandbox.create``.

    Returns:
        ``"unsent"`` when the error came before the ``run_job`` request,
        ``"refused"`` when HF refused that request, otherwise ``"unknown"``.
    """
    frames = [frame.f_code.co_name for frame, _ in traceback.walk_tb(error.__traceback__)]
    if "run_job" in frames:
        refused = HfHubHTTPError is not None and isinstance(error, HfHubHTTPError)
        status = getattr(getattr(error, "response", None), "status_code", None)
        return "refused" if refused and status in _REFUSED_CREATE_STATUSES else "unknown"
    if _AFTER_RUN_JOB.intersection(frames):
        return "unknown"
    if _BEFORE_RUN_JOB.intersection(frames):
        return "unsent"
    if type(error) is ValueError and frames and frames[-1] == "create":
        return "unsent"
    return "unknown"


def _stage(job: Any) -> str | None:
    """Return the stage name of an HF job.

    Args:
        job: The ``JobInfo``.

    Returns:
        The stage name, or ``None`` when HF reported none.
    """
    stage = getattr(getattr(job, "status", None), "stage", None)
    return getattr(stage, "value", stage)


def _find_jobs(label: str, namespace: str | None) -> list[Any]:
    """List the HF jobs that carry one box label.

    Args:
        label: The box label value.
        namespace: The HF namespace of the jobs.

    Returns:
        The jobs with the label, in any stage.
    """
    return list(HfApi().list_jobs(labels={BOX_LABEL: label}, namespace=namespace))


def _cancel_and_wait(job_id: str, namespace: str | None) -> Any | None:
    """Cancel an HF job and wait until HF reports a terminal stage.

    Args:
        job_id: The job id.
        namespace: The HF namespace of the job.

    Returns:
        The terminal ``JobInfo``, or ``None`` when HF did not confirm the end
        within ``STOP_TIMEOUT``.
    """
    api = HfApi()
    deadline = _monotonic() + STOP_TIMEOUT
    try:
        api.cancel_job(job_id=job_id, namespace=namespace)
    except Exception as error:
        # A job that already ended can refuse the cancel; its stage decides.
        logger.warning("HF Sandbox could not cancel job %s: %s", job_id, type(error).__name__)
    while True:
        try:
            job = api.inspect_job(job_id=job_id, namespace=namespace)
            if _stage(job) in _TERMINAL_STAGES:
                return job
        except Exception as error:
            logger.warning("HF Sandbox could not read job %s: %s", job_id, type(error).__name__)
        if _monotonic() >= deadline:
            return None
        _sleep(POLL_INTERVAL)


class HfSandboxEnvironment(SandboxEnvironment):
    """Run commands in one dedicated HF Sandbox, which is one HF Job."""

    capabilities = HF_SANDBOX_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        flavor: str = DEFAULT_FLAVOR,
        namespace: str | None = None,
        timeouts: PhaseTimeouts | None = None,
        budget: Any | None = None,
        usage_factory: Callable[..., Any] = USAGE.reserve,
    ) -> None:
        """Check the spec and the flavor before any HF call.

        Args:
            spec: The sandbox to open.
            flavor: The HF Jobs hardware flavor; only flavors with a pinned rate start.
            namespace: The HF user or org that runs the job; ``None`` is the token's user.
            timeouts: The phase timeouts; the start phase bounds the wait for
                the box, and the run phase is the default ``exec`` limit.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: The flavor has no pinned rate, the spec does not
                fit the flavor, the lifetime is above 24 hours, or a protected
                box has no operation key.
        """
        super().__init__(spec)
        if flavor not in FLAVOR_HOURLY_USD or flavor not in FLAVOR_SIZES:
            raise SandboxSpecError(f"The hf_sandbox flavor {flavor!r} has no pinned rate.")
        if spec.lifetime_seconds * 1000 > LIFETIME_CEILING_MS:
            raise SandboxSpecError("The hf_sandbox job lives at most 24 hours.")
        size = FLAVOR_SIZES[flavor]
        resources = spec.resources
        if resources.gpu > size.gpus:
            raise SandboxSpecError(f"The hf_sandbox flavor {flavor} has {size.gpus} GPUs.")
        if resources.mode is not ResourceMode.IGNORE:
            if resources.cpus > size.vcpus:
                raise SandboxSpecError(f"The hf_sandbox flavor {flavor} has {size.vcpus} vCPUs.")
            if resources.memory_mb is not None and resources.memory_mb > size.memory_mb:
                raise SandboxSpecError(f"The hf_sandbox flavor {flavor} has {size.memory_mb} MB of memory.")
            if resources.disk_mb is not None:
                raise SandboxSpecError("The hf_sandbox sandbox cannot set a disk size.")
        if budget is not None and spec.operation_key is None:
            raise SandboxSpecError("A protected HF Sandbox needs an operation key.")
        self._flavor = flavor
        self._namespace = namespace
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: Any | None = None
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        # HF lists old jobs too, so the lookup after a failed create needs a
        # label that no earlier box has.
        self._label = f"skynet-{uuid.uuid4().hex}"
        self._job_tag = spec.tags.get(JOB_TAG)
        self._sandbox: Any = None
        self._job_id: str | None = None
        self._sent_at: float | None = None
        self._started = False
        self._ended = False
        self._starting: asyncio.Future[None] | None = None
        self._late: asyncio.Future[None] | None = None
        self._watchdog: asyncio.Future[None] | None = None
        self._stop_task: asyncio.Future[None] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and an HF token exist.

        Raises:
            SandboxFatalError: The SDK has no ``Sandbox``, or no HF token is set.
        """
        if Sandbox is None or HfApi is None:
            raise SandboxFatalError(
                "The hf_sandbox needs huggingface-hub with Sandbox: install the `hf_sandbox` extra."
            )
        if not get_token():
            raise SandboxFatalError("The hf_sandbox found no HF token: set HF_TOKEN or run `hf auth login`.")

    async def start(self) -> None:
        """Reserve the budget, create the HF Sandbox and arm the lifetime cap.

        Raises:
            SandboxInfraError: The box already started once, a stop ended the
                start, or HF could not create the box.
            SandboxSpecError: Skynet cannot price the box.
            SandboxFatalError: The SDK or the token is missing.
            UsagePendingError: The box could not end after a failed start, so
                its hold stays pending.
            BudgetError: The reserve failed, or the ledger refused the settlement
                of a box that could not finish its start.
        """
        if self._started:
            raise SandboxInfraError("The HF Sandbox already started once; it cannot start again.")
        self._started = True
        # A stop before the start leaves a finished teardown that must not stand for this box.
        self._stop_task = None
        self.preflight()
        self._starting = asyncio.ensure_future(self._start())
        try:
            await self._starting
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and not task.cancelling():
                raise SandboxInfraError("The HF Sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _start(self) -> None:
        """Run the reserve, the create and the adoption of the job.

        Raises:
            SandboxInfraError: HF could not create the box.
            UsagePendingError: A box that failed its start could not end.
            BudgetError: The reserve failed, or the ledger refused a settlement.
        """
        await self._reserve()
        usage = self._usage
        if usage is not None:
            usage.sent(self._label)
        self._sent_at = asyncio.get_running_loop().time()
        create = asyncio.ensure_future(asyncio.to_thread(self._create_sync))
        try:
            sandbox = await asyncio.shield(create)
        except asyncio.CancelledError:
            # The thread cannot be cancelled and may still make the job, so
            # one background task owns the box and its hold from here on.
            self._late = asyncio.ensure_future(self._finish_late_create(create))
            _keep_task(self._late, "end a sandbox whose start was cancelled")
            raise
        except Exception as error:
            # The cleanup owns the hold, so a stop that arrives meanwhile leaves it alone.
            self._late = asyncio.ensure_future(self._fail_create(error))
            try:
                await asyncio.shield(self._late)
            except asyncio.CancelledError:
                _keep_task(self._late, "clean up a failed create")
                raise
            raise SandboxInfraError(f"HF Sandbox could not create the sandbox: {type(error).__name__}.") from error
        await self._adopt(sandbox)
        remaining = self.spec.lifetime_seconds - (asyncio.get_running_loop().time() - self._sent_at)
        self._watchdog = asyncio.ensure_future(self._expire(max(0.0, remaining)))
        _keep_task(self._watchdog, "end a sandbox at its lifetime cap")

    def _create_sync(self) -> Any:
        """Create the HF Sandbox and block until it is ready.

        Returns:
            The SDK ``Sandbox`` handle.
        """
        spec = self.spec
        return Sandbox.create(
            spec.image or DEFAULT_IMAGE,
            flavor=self._flavor,
            idle_timeout=math.ceil(spec.lifetime_seconds),
            env=dict(spec.env) or None,
            namespace=self._namespace,
            forward_hf_token=False,
            labels={**{str(k): str(v) for k, v in spec.tags.items()}, BOX_LABEL: self._label},
            start_timeout=self._start_limit if self._start_limit is not None else DEFAULT_START_TIMEOUT,
        )

    async def _reserve(self) -> None:
        """Reserve the whole possible lifetime of a protected box before the create.

        Raises:
            SandboxSpecError: Skynet cannot price the box.
        """
        if self._budget is None or self._usage is not None:
            return
        reserving = asyncio.ensure_future(
            asyncio.to_thread(
                self._usage_factory, self._budget, self._usage_request(), operation_key=self.spec.operation_key
            )
        )
        try:
            self._usage = await asyncio.shield(reserving)
        except asyncio.CancelledError:
            _keep_task(asyncio.ensure_future(_release_late_reserve(reserving)), "release a late hold")
            raise
        except UnpricedOperationError as error:
            raise SandboxSpecError(f"Skynet cannot fund this HF Sandbox: {error}") from error

    def _usage_request(self) -> dict[str, Any]:
        """Build the request that the reservation prices.

        Returns:
            The flavor, the lifetime cap, the exposed port count and the image.
        """
        return {
            "flavor": self._flavor,
            "lifetime_ms": math.ceil(self.spec.lifetime_seconds) * 1000,
            # Sandbox.create always exposes the in-box server port.
            "exposed_ports": 1,
            "image": self.spec.image or DEFAULT_IMAGE,
        }

    def _keep(self, job_id: str, sandbox: Any = None) -> None:
        """Record the job and list the box in the job registry until HF confirms its end.

        Args:
            job_id: The HF job id.
            sandbox: The SDK handle, or ``None`` for a job that only the lookup found.
        """
        self._job_id = job_id
        self._sandbox = sandbox
        _JOB_BOXES.setdefault(self._job_tag, set()).add(self)

    def _forget(self) -> None:
        """Remove the box from the job registry once HF confirmed its end."""
        boxes = _JOB_BOXES.get(self._job_tag)
        if boxes is not None:
            boxes.discard(self)
            if not boxes:
                del _JOB_BOXES[self._job_tag]

    async def _adopt(self, sandbox: Any) -> None:
        """Keep a created box and bind its job to the hold.

        Args:
            sandbox: The SDK ``Sandbox`` handle.

        Raises:
            UsagePendingError: The hold could not bind the job; the box is stopped first.
        """
        self._keep(sandbox.id, sandbox)
        if self._usage is None:
            return
        confirming = asyncio.ensure_future(asyncio.to_thread(self._usage.confirm_created, sandbox))
        cancelled: asyncio.CancelledError | None = None
        failure: BaseException | None = None
        # The ledger call cannot be cancelled, so the teardown waits for it, as in _reserve.
        while not confirming.done():
            try:
                await asyncio.shield(confirming)
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
            except Exception:
                break
        if not confirming.cancelled() and confirming.exception() is not None:
            failure = confirming.exception()
        if cancelled is None and failure is None:
            return
        try:
            await self._end()
        except Exception as error:
            logger.warning("HF Sandbox could not stop an unconfirmed box: %s", type(error).__name__)
        if cancelled is not None:
            raise cancelled
        raise failure

    async def _finish_late_create(self, create: asyncio.Future[Any]) -> None:
        """End and settle the box of a create whose start was cancelled.

        Args:
            create: The create thread call; it can still run.
        """
        try:
            sandbox = await create
        except Exception as error:
            await self._fail_create(error)
            return
        self._keep(sandbox.id, sandbox)
        if self._usage is not None:
            try:
                await asyncio.to_thread(self._usage.confirm_created, sandbox)
            except Exception as error:
                logger.warning("HF Sandbox could not bind a late job to its hold: %s", type(error).__name__)
        await self._end()

    async def _fail_create(self, error: BaseException) -> None:
        """Look up the job of a failed create, then end it or release or keep the hold.

        Args:
            error: The create error.
        """
        usage = self._usage
        outcome = _create_outcome(error)
        if outcome == "unsent":
            # The create failed before its job request, so no job can exist.
            if usage is not None:
                self._usage = None
                await asyncio.to_thread(usage.fail, True)
            return
        try:
            found = await asyncio.to_thread(_find_jobs, self._label, self._namespace)
        except Exception as lookup_error:
            logger.warning("HF Sandbox could not look up a failed create: %s", type(lookup_error).__name__)
            if usage is not None:
                self._usage = None
                await asyncio.to_thread(usage.pending)
            return
        if found:
            if len(found) > 1:
                logger.warning("HF Sandbox found %d jobs with the label %s.", len(found), self._label)
            job = found[0]
            self._keep(job.id)
            if usage is not None:
                try:
                    await asyncio.to_thread(usage.confirm_created, job)
                except Exception as error:
                    logger.warning("HF Sandbox could not bind a found job to its hold: %s", type(error).__name__)
            await self._end()
            return
        if usage is not None:
            self._usage = None
            await asyncio.to_thread(usage.fail, outcome == "refused")

    async def _expire(self, delay: float) -> None:
        """Cancel the job at the spec lifetime, because HF's own timeout is 24 hours.

        Args:
            delay: Seconds until the lifetime cap.
        """
        await _async_sleep(delay)
        self._watchdog = None
        logger.warning("HF Sandbox job %s reached its lifetime cap; it is cancelled.", self._job_id)
        await self.stop()

    def _require_sandbox(self) -> Any:
        """Return the SDK handle of the running box.

        Returns:
            The SDK ``Sandbox`` handle.

        Raises:
            SandboxInfraError: The box is not running.
        """
        if self._sandbox is None or self._ended:
            raise SandboxInfraError("The HF Sandbox is not running.")
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
        """Run one shell command in the HF Sandbox and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory; the image default by default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` pairs as the command writes them.
            sudo: Ignored: HF Sandbox has no user switch, so the image user runs the command.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, the result has exit code 124 and ``timed_out`` set.

        Raises:
            SandboxInfraError: The box is not running, or the SDK call failed.
        """
        sandbox = self._require_sandbox()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        sink = on_output or current_output_sink()
        loop = asyncio.get_running_loop()
        # The SDK thread can stream on after exec returns; the flag drops those chunks.
        returned = False

        def deliver(stream: str, chunk: str) -> None:
            """Hand one chunk to the sink on the loop, unless exec already returned.

            Args:
                stream: ``stdout`` or ``stderr``.
                chunk: The text.
            """
            if not returned:
                sink(stream, chunk)

        def forward(stream: str) -> Callable[[str], None] | None:
            """Build the SDK callback that hands one stream's chunks to the sink on the loop.

            Args:
                stream: ``stdout`` or ``stderr``.

            Returns:
                The callback, or ``None`` when no sink listens.
            """
            if sink is None:
                return None

            def send(chunk: str) -> None:
                """Queue one chunk for the loop, unless exec already returned.

                Args:
                    chunk: The text.
                """
                if not returned:
                    loop.call_soon_threadsafe(deliver, stream, chunk)

            return send

        try:
            async with asyncio.timeout(limit + EXEC_GRACE if limit is not None else None):
                with _mapped("run the command"):
                    result = await asyncio.to_thread(
                        sandbox.run,
                        ["/bin/bash", "-c", command],
                        shell=False,
                        cwd=cwd,
                        env=dict(env) if env else None,
                        timeout=limit,
                        on_stdout=forward("stdout"),
                        on_stderr=forward("stderr"),
                        check=False,
                    )
        except TimeoutError:
            # The SDK call hung past its own timeout. The thread cannot be
            # cancelled and nothing kills the command; the lifetime cap or
            # stop() ends it with the job.
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        finally:
            returned = True
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if result.timed_out:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, stdout=stdout, stderr=stderr, timed_out=True)
        exit_code = result.exit_code if result.exit_code is not None else -1
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the HF Sandbox; HF makes the parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The box is not running, or a write failed.
        """
        sandbox = self._require_sandbox()
        for path, text in files.items():
            with _mapped("write a file"):
                await asyncio.to_thread(sandbox.files.write, path, text.encode("utf-8"))

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the HF Sandbox.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The box is not running, the read failed, or
                the file is not UTF-8 text.
        """
        sandbox = self._require_sandbox()
        try:
            with _mapped("read a file"):
                data = await asyncio.to_thread(sandbox.files.read, path)
        except SandboxInfraError as error:
            cause = error.__cause__
            if HfSandboxError is not None and isinstance(cause, HfSandboxError) and cause.status_code == 404:
                return None
            raise
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            # The message names no bytes, so no file content reaches the logs.
            raise SandboxInfraError("HF Sandbox read a file that is not UTF-8 text.") from None

    async def stop(self) -> None:
        """Cancel the HF job, wait for its terminal stage, then settle its hold once.

        Safe to call again and before start. A stop during the start ends
        the start first; a create that was in flight then belongs to its
        background task.

        Raises:
            UsagePendingError: HF did not confirm the end of the job, or the
                ledger call failed, so the hold stays pending.
            BudgetError: The ledger refused the settlement.
        """
        starting = self._starting
        if starting is not None and not starting.done() and starting is not asyncio.current_task():
            starting.cancel()
            await asyncio.wait({starting})
        late = self._late
        if late is not None and not late.done() and late is not asyncio.current_task():
            return
        await self._end()

    async def _end(self) -> None:
        """Run the one shared teardown, which a cancel of the caller does not stop.

        Raises:
            UsagePendingError: HF did not confirm the end of the job.
            BudgetError: The ledger refused the settlement.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)

    async def _teardown(self) -> None:
        """End the job and settle, release or keep its hold.

        Raises:
            UsagePendingError: HF did not confirm the end of the job; a later stop retries.
        """
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        job_id = self._job_id
        if job_id is None:
            usage, self._usage = self._usage, None
            if usage is not None:
                # No job came back: an unsent create releases, a sent one stays pending.
                await asyncio.to_thread(usage.fail)
            return
        try:
            job = await asyncio.wait_for(
                asyncio.to_thread(_cancel_and_wait, job_id, self._namespace), STOP_TIMEOUT + EXEC_GRACE
            )
        except Exception as error:
            logger.warning("HF Sandbox could not end job %s: %s", job_id, type(error).__name__)
            job = None
        if job is None:
            # The job can still run and bill, so the box stays listed and a
            # later stop() or the job sweep tries again.
            self._stop_task = None
            if self._usage is not None:
                await asyncio.to_thread(self._usage.pending)
            raise UsagePendingError(f"HF did not confirm the end of sandbox job {job_id}; its hold stays pending.")
        self._ended = True
        self._forget()
        sandbox, self._sandbox = self._sandbox, None
        if sandbox is not None:
            try:
                await asyncio.to_thread(sandbox.close)
            except Exception as error:
                logger.warning("HF Sandbox could not close its client: %s", type(error).__name__)
        usage, self._usage = self._usage, None
        if usage is not None:
            usage.stopped(
                getattr(job, "created_at", None),
                getattr(job, "finished_at", None),
                started_at=getattr(job, "started_at", None),
            )
            # The usage is already detached, so no later stop can settle it again.
            await asyncio.to_thread(usage.settle)

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every HF Sandbox that this process started for the job, all at the same time.

        Args:
            job_id: The job tag value.

        Returns:
            The number of boxes whose end HF confirmed. A box that fails to
            end or settle is logged and the sweep goes on.

        Raises:
            Exception: The first stop error that is not a usage or budget error.
        """
        envs = list(_JOB_BOXES.get(job_id, ()))
        results = await asyncio.gather(*(env.stop() for env in envs), return_exceptions=True)
        unexpected: BaseException | None = None
        for env, result in zip(envs, results, strict=True):
            if isinstance(result, UsagePendingError | BudgetError):
                logger.warning("HF Sandbox job %s usage stays pending: %s", env._job_id, type(result).__name__)
            elif isinstance(result, BaseException) and unexpected is None:
                unexpected = result
        if unexpected is not None:
            raise unexpected
        return sum(1 for env in envs if env._ended)


async def _release_late_reserve(reserving: asyncio.Future[Any]) -> None:
    """Release the hold of a reserve that finished after its start was cancelled.

    The create starts only after the reserve returns, so no job exists for this hold.

    Args:
        reserving: The reserve thread call; it can still run.
    """
    try:
        usage = await reserving
    except Exception:
        return
    await asyncio.to_thread(usage.fail)

"""Agent-target scoring: run the harness in a sandbox, then score the run record.

Every evaluation of an agent target is one sandbox. The wrapper writes the
harness under test (the version) and the case into a fresh box, installs
and runs the agent, collects what it produced, destroys the box, and hands
a run record to the user's scorer in place of the case. The scorer judges
what the agent did — its answer, transcript, exit code, an optional
in-sandbox check — rather than the version text itself.
"""

from __future__ import annotations

import itertools
import json
import logging
import posixpath
import threading
import time
import uuid
from typing import Any

from tenacity import Retrying, retry_if_exception, stop_after_attempt

from ....billing.budgets import BudgetError
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....config import Settings, settings
from ....exceptions import InfrastructureInterruptionError, ServiceError
from ....models.blackbox import BlackboxTarget
from ....sandbox.environment import HealthcheckConfig
from ....sandbox.errors import GROUP_RULES, ErrorGroup, classify
from ....sandbox.retry import DEFAULT_RETRY_POLICY, RetryPolicy, attempt_key
from .agent_runs import AgentRun, AgentRunRecorder
from .harness import ANSWER_FILE, PROMPT_FILE, GatewayConfig, HarnessLaunch, build_launch, pinned_harness_check
from .heartbeat import heartbeat
from .protocol import Candidate, ScorerAbortError, ScorerFn, SideInfo
from .sandbox import (
    JOB_TAG,
    KILL_GRACE_SECONDS,
    CommandResult,
    SandboxRuntime,
    SandboxSession,
    SandboxSpec,
    sandbox_unavailable_reason,
    unique_sandbox_name,
)

logger = logging.getLogger(__name__)

_GATEWAY_MISSING = (
    "Agent targets need a model gateway the sandboxes can reach: set BLACKBOX_AGENT_GATEWAY_URL and "
    "BLACKBOX_AGENT_GATEWAY_API_KEY (or LITELLM_PROXY_URL and LITELLM_PROXY_API_KEY)."
)

# Case keys that carry the task text for the agent, in priority order.
PROMPT_KEYS = ("prompt", "task", "input", "question", "instruction")
# Case keys with meaning to the runner rather than the agent.
FILES_KEY = "files"
CHECK_KEY = "check_command"
# Time allowed for installing the harness, the case's setup and its check, on top of the run timeout.
_SETUP_ALLOWANCE_SECONDS = 600.0
# What a box needs beyond its commands: booting, the file writes and the answer read.
_BOX_OVERHEAD_SECONDS = 60.0
# The readiness probe's box only boots and checks the harness install.
READINESS_LIFETIME_SECONDS = 180.0
_OUTPUT_CHARS = 20_000
_TRANSCRIPT_CHARS = 4_000
_STEP_CHARS = 6_000
_FEEDBACK_OUTPUT_CHARS = 2_000
_FEEDBACK_TRANSCRIPT_CHARS = 1_500
_CHECK_OUTPUT_CHARS = 2_000
# The readiness box must answer a trivial command before the real checks run,
# so a slow boot is a few failed probes instead of a failed setup (SKY-428).
READINESS_HEALTHCHECK = HealthcheckConfig(command="true", interval_sec=2.0, timeout_sec=30.0, retries=3)
# Skynet errors that ``core.sandbox.classify`` does not know, in Harbor's groups.
# Order matters: the first matching class wins (SKY-428).
_ERROR_GROUPS: tuple[tuple[type[BaseException], ErrorGroup], ...] = (
    (ServiceError, ErrorGroup.REJECTED),
    (InfrastructureInterruptionError, ErrorGroup.INFRA),
    (ConnectionError, ErrorGroup.TRANSIENT),
    (TimeoutError, ErrorGroup.TRANSIENT),
)
COST_OWNER_SKYNET = "skynet"
COST_OWNER_USER = "user"


def error_group(exc: BaseException) -> ErrorGroup | None:
    """Return the Harbor error group of an agent attempt's error.

    Args:
        exc: The error that the attempt raised.

    Returns:
        The group from ``core.sandbox.classify``, else from the Skynet table,
        else ``None`` for an unknown error.
    """
    group = classify(exc)
    if group is not None:
        return group
    for cls, mapped in _ERROR_GROUPS:
        if isinstance(exc, cls):
            return mapped
    return None


def should_retry(policy: RetryPolicy, exc: BaseException) -> bool:
    """Say whether an agent attempt that raised ``exc`` runs again in a fresh box.

    It extends :meth:`RetryPolicy.should_retry` with the Skynet table, with the
    same rules: no-retry groups never retry, ``include`` narrows, ``exclude`` wins.

    Args:
        policy: The scorer's retry policy.
        exc: The error that the attempt raised.

    Returns:
        True when the attempt runs again.
    """
    if not isinstance(exc, Exception) or classify(exc) is not None:
        return policy.should_retry(exc)
    group = error_group(exc)
    if group is None:
        return policy.should_retry(exc)
    if policy.max_retries == 0 or not GROUP_RULES[group].retryable:
        return False
    if policy.exclude and isinstance(exc, policy.exclude):
        return False
    return isinstance(exc, policy.include) if policy.include else True


def cost_owner(group: ErrorGroup | None) -> str:
    """Return who carries the cost of an attempt that ended in ``group``.

    Args:
        group: The attempt's error group; ``None`` for a success or an unknown error.

    Returns:
        ``skynet`` for an infrastructure failure, else ``user``.
    """
    return COST_OWNER_SKYNET if group is ErrorGroup.INFRA else COST_OWNER_USER


def gateway_from_settings(settings: Settings) -> GatewayConfig | None:
    """Resolve where sandboxed harnesses send their model calls, or ``None`` when unset.

    The dedicated agent-gateway settings win; the LiteLLM proxy settings are
    the fallback, since that proxy already fronts managed inference.

    Args:
        settings: The backend settings.

    Returns:
        The gateway, or ``None`` when no URL/key pair is configured.
    """
    url = settings.blackbox_agent_gateway_url or settings.litellm_proxy_url
    key = settings.blackbox_agent_gateway_api_key or settings.litellm_proxy_api_key
    if not url or key is None:
        return None
    return GatewayConfig(url=url.rstrip("/"), api_key=key.get_secret_value())


def agent_target_unavailable_reason(settings: Settings) -> str | None:
    """Explain why this deployment cannot run agent targets, or return ``None``.

    Args:
        settings: The backend settings.

    Returns:
        A user-facing reason (no sandboxes, or no gateway), or ``None``.
    """
    reason = sandbox_unavailable_reason(settings)
    if reason is not None:
        return reason
    # Local mode routes model calls through the run's own parent gateway
    # (the user's CLI subscriptions), never through a deployment proxy.
    if not settings.local_mode and gateway_from_settings(settings) is None:
        return _GATEWAY_MISSING
    return None


def safe_relative_path(path: Any) -> str:
    """Normalize a workspace-relative path, refusing anything that could escape it.

    Args:
        path: The path as the case or version gave it.

    Returns:
        The normalized relative path.

    Raises:
        ServiceError: When the path is empty, absolute, or climbs out of the workspace.
    """
    if not isinstance(path, str) or not path.strip():
        raise ServiceError("file paths must be non-empty strings")
    normalized = posixpath.normpath(path.strip())
    if normalized.startswith(("/", "../")) or normalized in (".", ".."):
        raise ServiceError(f"file path '{path}' must stay inside the workspace")
    return normalized


def candidate_files(candidate: Candidate, instructions_file: str) -> dict[str, str]:
    """Map a version onto the files it becomes in the sandbox.

    Args:
        candidate: Text (the harness's instruction file) or named parts (one file each).
        instructions_file: Where the harness reads its instructions.

    Returns:
        Relative path → content.
    """
    if isinstance(candidate, str):
        return {instructions_file: candidate}
    return {safe_relative_path(path): text for path, text in candidate.items()}


def case_files(case: Any) -> dict[str, str]:
    """Return the files a case ships into the sandbox.

    Args:
        case: The case; only a dict with a ``files`` mapping contributes.

    Returns:
        Relative path → content.

    Raises:
        ServiceError: When ``files`` is not a mapping or a path is unsafe.
    """
    if not isinstance(case, dict):
        return {}
    files = case.get(FILES_KEY) or {}
    if not isinstance(files, dict):
        raise ServiceError(f"case '{FILES_KEY}' must map relative paths to text")
    return {safe_relative_path(path): str(content) for path, content in files.items()}


def case_prompt(case: Any) -> str:
    """Render the task text the agent reads, with the answer-file instruction appended.

    Args:
        case: The case; the first of :data:`PROMPT_KEYS` present is the task
            text, otherwise the case is shown as JSON.

    Returns:
        The prompt file's content.
    """
    text: str | None = None
    if isinstance(case, dict):
        for key in PROMPT_KEYS:
            value = case.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        if text is None:
            visible = {key: value for key, value in case.items() if key not in (FILES_KEY, CHECK_KEY)}
            text = json.dumps(visible, indent=2, default=str)
    elif case is not None:
        text = str(case)
    else:
        text = "Follow the instructions you were given."
    return (
        f"{text.rstrip()}\n\n---\n"
        f"When you are done, write your final answer to `{ANSWER_FILE}` (create the directory if needed). "
        "Only that file is scored."
    )


def case_name(case: Any) -> str | None:
    """Return the case's own id or name, when it carries one.

    Args:
        case: The task.

    Returns:
        The trimmed id or name, or ``None``.
    """
    name = (case.get("id") or case.get("name")) if isinstance(case, dict) else None
    if isinstance(name, str | int) and str(name).strip():
        return str(name).strip()
    return None


def _clip(text: str, limit: int) -> str:
    """Keep the head of ``text``.

    Args:
        text: Any text.
        limit: Maximum characters.

    Returns:
        ``text`` or its first ``limit`` characters with an ellipsis.
    """
    return text if len(text) <= limit else text[:limit] + "…"


def _tail(text: str, limit: int) -> str:
    """Keep the tail of ``text``.

    Args:
        text: Any text.
        limit: Maximum characters.

    Returns:
        ``text`` or its last ``limit`` characters with an ellipsis.
    """
    return text if len(text) <= limit else "…" + text[-limit:]


def _format_step(label: str, result: CommandResult) -> str:
    """Render one command's outcome for the transcript.

    Args:
        label: ``install``, ``setup``, ``run`` or ``check``.
        result: The command's outcome.

    Returns:
        A bounded transcript block.
    """
    status = f"exit={result.exit_code}" + (" (timed out)" if result.timed_out else "")
    body = "\n".join(part for part in (result.stdout.rstrip(), result.stderr.rstrip()) if part)
    return f"[{label}] {status}\n{_tail(body, _STEP_CHARS)}".rstrip()


def _stream_step(
    session: SandboxSession, run: AgentRun, step: str, command: str, *, timeout_seconds: float
) -> CommandResult:
    """Run one command in the box, feeding its output to the run record as it arrives.

    Args:
        session: The open sandbox.
        run: The run's record.
        step: ``install``, ``setup``, ``run`` or ``check``.
        command: The shell command line.
        timeout_seconds: Kill the command after this long.

    Returns:
        The command's outcome.
    """
    run.write(f"[{step}] started\n")
    streamed = {"chars": 0, "open_line": False}

    def forward(stream: str, chunk: str) -> None:
        """Pass one piece of the box's output to the record."""
        streamed["chars"] += len(chunk)
        streamed["open_line"] = not chunk.endswith("\n")
        run.write(chunk)

    result = session.run(command, timeout_seconds=timeout_seconds, on_output=forward)
    if streamed["chars"] == 0:
        # A session that cannot stream hands the output over with the result.
        body = "\n".join(part for part in (result.stdout.rstrip(), result.stderr.rstrip()) if part)
        if body:
            run.write(body + "\n")
    elif streamed["open_line"]:
        run.write("\n")
    status = f"exit={result.exit_code}" + (" (timed out)" if result.timed_out else "")
    run.write(f"[{step}] {status}\n")
    return result


# Three dead runs cover both sandboxes in flight plus one more, so a single
# flaky box never aborts a run.
def case_lifetime_seconds(target_timeout_seconds: float, setup_steps: int) -> float:
    """Return the box lifetime one case needs before any ceiling applies.

    Each step gets its timeout plus the KILL wrapper's grace, on top of the
    box's own overhead, so a slow install can never eat into the agent's
    run time.

    Args:
        target_timeout_seconds: The agent's own run allowance.
        setup_steps: How many install, setup and check commands run around it.

    Returns:
        The lifetime, in seconds.
    """
    steps = [target_timeout_seconds] + [_SETUP_ALLOWANCE_SECONDS] * setup_steps
    return _BOX_OVERHEAD_SECONDS + sum(step + KILL_GRACE_SECONDS for step in steps)


def _check_command(case: Any) -> str | None:
    """Return the case's in-sandbox check command, when it ships a usable one.

    Args:
        case: The task.

    Returns:
        The command, or ``None``.
    """
    check = case.get(CHECK_KEY) if isinstance(case, dict) else None
    return check if isinstance(check, str) and check.strip() else None


def _usage_summary(usage: dict[str, Any]) -> str:
    """Render a run's token usage for a log line.

    Args:
        usage: ``input_tokens``/``output_tokens`` counts, possibly empty.

    Returns:
        The counts, or a note that the harness reported none.
    """
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens")]
    if not any(isinstance(count, int | float) and count > 0 for count in counts):
        return "no usage reported"
    tokens_in, tokens_out = (int(count or 0) for count in counts)
    return f"{tokens_in:,} in / {tokens_out:,} out tokens"


_DEAD_RUNS_BEFORE_ABORT = 3


class AgentHarnessError(ScorerAbortError, ServiceError):
    """The harness died on every one of the first runs before the model did any work.

    A run whose agent cannot even reach its model scores 0 everywhere and
    burns the whole budget for no signal, so the scorer stops it instead.
    """


class SandboxAgentScorer:
    """Scorer wrapper for agent targets: one sandbox per ``(version, case)``.

    Callable like any scorer. The wrapped scorer receives the run record
    where it would receive the case; the record keeps the case under
    ``"case"``.
    """

    def __init__(
        self,
        scorer: ScorerFn,
        *,
        runtime: SandboxRuntime,
        target: BlackboxTarget,
        gateway: GatewayConfig,
        job_id: str | None = None,
        max_lifetime_seconds: float | None = None,
        recorder: AgentRunRecorder | None = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        healthcheck: HealthcheckConfig = READINESS_HEALTHCHECK,
    ) -> None:
        """Bind the wrapper to a sandbox runtime and the job's target.

        Args:
            scorer: The user's scorer, called with ``(version, run_record)``.
            runtime: Creates the sandboxes.
            target: Harness, model, timeouts and commands.
            gateway: Where the harness sends its model calls.
            job_id: Tags the sandboxes with the job, when known.
            max_lifetime_seconds: Ceiling on a box's lifetime; the configured one when unset.
            recorder: Receives every run's record and live transcript; inert when unset.
            retry_policy: Which attempt errors run again in a fresh box; the
                default never retries. Advanced mode sets the count and classes.
            healthcheck: The readiness poll that :meth:`check_ready` runs first.
        """
        self._scorer = scorer
        self._runtime = runtime
        self._target = target
        self._launch: HarnessLaunch = build_launch(
            target, gateway, protected=bool(getattr(runtime, "protected", False))
        )
        self._job_id = job_id
        self._recorder = recorder or AgentRunRecorder()
        self._max_lifetime_seconds = (
            settings.vercel_sandbox_max_lifetime_seconds if max_lifetime_seconds is None else max_lifetime_seconds
        )
        # Sandboxes run in parallel, but the user's scorer (a metric-sandbox
        # process or a remote call) is not promised to be thread-safe.
        self._scorer_lock = threading.Lock()
        self._pulse_lock = threading.Lock()
        self._dead_runs = 0
        self._alive = False
        self._run_ids = itertools.count(1)
        self._retry_policy = retry_policy
        self._healthcheck = healthcheck

    def check_ready(self) -> dict[str, str]:
        """Verify a seedless agent's offline CLI, dependencies, and command syntax.

        Returns:
            Explicit readiness evidence, with candidate execution deferred until a real candidate exists.

        Raises:
            ServiceError: When protected runtime requirements, authored
                dependencies, or command syntax are invalid, or the box
                never passes its healthcheck.
        """
        if not getattr(self._runtime, "protected", False):
            raise ServiceError("Agent readiness requires a protected sandbox runtime.")
        lifetime = min(READINESS_LIFETIME_SECONDS, self._max_lifetime_seconds)
        spec = SandboxSpec(
            lifetime_seconds=lifetime,
            env=self._launch.env,
            name=self._sandbox_name(),
            tags={JOB_TAG: self._job_id} if self._job_id else {},
            network_disabled=True,
        )
        session = self._runtime.open(spec)
        failed = False
        try:
            self._wait_healthy(session, lifetime)
            files = {
                f".app/readiness-{index}.sh": command
                for index, command in enumerate(
                    (self._launch.run_command, self._target.setup_command, self._target.install_command)
                )
                if command
            }
            session.write_files({**self._launch.files, **files})
            checks = [f"bash -n {name}" for name in files]
            cli = pinned_harness_check(self._target.harness)
            if cli:
                checks.append(cli)
            result = session.run(" && ".join(checks), timeout_seconds=min(120.0, lifetime))
            if not result.ok:
                raise ServiceError("The selected agent harness failed its offline version or command syntax check.")
            if self._target.install_command and self._launch.install_command:
                result = session.run(
                    self._launch.install_command,
                    timeout_seconds=min(_SETUP_ALLOWANCE_SECONDS, lifetime),
                )
                if not result.ok:
                    detail = _tail(result.stderr or result.stdout, 800)
                    suffix = f": {detail}" if detail else "."
                    raise ServiceError("The agent dependency command failed inside the offline cloud sandbox" + suffix)
            return {
                "harness": self._target.harness,
                "readiness": (
                    "offline_dependencies_and_syntax_verified"
                    if self._target.install_command
                    else "pinned_cli_and_syntax_verified"
                    if cli
                    else "command_syntax_verified"
                ),
                "candidate_execution": "awaiting_first_generated_candidate",
            }
        except BaseException:
            failed = True
            raise
        finally:
            try:
                session.close()
            except UsagePendingError:
                if not failed:
                    raise

    def _wait_healthy(self, session: SandboxSession, lifetime: float) -> None:
        """Poll the healthcheck command until the box answers, with Docker HEALTHCHECK semantics.

        Args:
            session: The open readiness box.
            lifetime: The box lifetime, which caps each probe's timeout.

        Raises:
            ServiceError: ``retries`` consecutive probes failed after the start period.
        """
        config = self._healthcheck
        started = time.monotonic()
        failures = 0
        while True:
            probe_started = time.monotonic()
            try:
                result = session.run(config.command, timeout_seconds=min(config.timeout_sec, lifetime))
            except (BudgetError, UsagePendingError, UnpricedOperationError):
                raise
            except Exception as error:
                # A transport error is one failed probe, not the end of the poll.
                last_failure = f"{type(error).__name__}: {error}"
            else:
                if result.ok:
                    return
                last_failure = f"exit code {result.exit_code}"
            if probe_started - started < config.start_period_sec:
                time.sleep(config.start_interval_sec)
                continue
            failures += 1
            if failures >= config.retries:
                raise ServiceError(
                    f"The agent sandbox failed its readiness healthcheck {failures} times in a row "
                    f"({last_failure[:500]})."
                )
            time.sleep(config.interval_sec)

    def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float, SideInfo]:
        """Run the agent on ``case`` with ``candidate`` as its harness, then score the record.

        Args:
            candidate: The harness version.
            case: The task the agent works on.

        Returns:
            The score and the scorer's side information plus the run's feedback.

        Raises:
            AgentHarnessError: When the first runs all died before the model did any work.
        """
        run_id, label = self._next_label(case)
        record = self._run(candidate, case, run_id, label)
        self._check_pulse(record)
        scoring_started = time.perf_counter()
        with self._scorer_lock:
            score, side_info = self._scorer(candidate, record)
        logger.info("%s: scored %.3f (scorer took %.1fs)", label, score, time.perf_counter() - scoring_started)
        return score, {**side_info, **self.feedback(record)}

    def _check_pulse(self, record: dict[str, Any]) -> None:
        """Abort the run once the first runs have all died without any model usage.

        One live run — a usage count above zero, or an answer — clears the
        check for the rest of the run: a harness that works sometimes is a
        flaky one, and flakiness is the run's business, not this guard's.

        Args:
            record: The run record just produced.

        Raises:
            AgentHarnessError: When ``_DEAD_RUNS_BEFORE_ABORT`` runs died before any run lived.
        """
        used = any(isinstance(count, int | float) and count > 0 for count in record["usage"].values())
        with self._pulse_lock:
            if used or record["error"] is None:
                self._alive = True
                return
            if self._alive:
                return
            self._dead_runs += 1
            if self._dead_runs < _DEAD_RUNS_BEFORE_ABORT:
                return
            dead = self._dead_runs
        raise AgentHarnessError(
            f"the agent harness failed on the first {dead} runs before the model did any work "
            f"(last error: {record['error']}); check the harness, model and gateway settings"
        )

    @staticmethod
    def feedback(record: dict[str, Any]) -> SideInfo:
        """Distill a run record into what a proposer needs to see.

        Args:
            record: A run record from :meth:`run`.

        Returns:
            Bounded output, transcript tail, exit code, usage and any error/check.
        """
        feedback: SideInfo = {
            "agent_output": _clip(record["output"] or "", _FEEDBACK_OUTPUT_CHARS),
            "transcript_tail": _tail(record["transcript"], _FEEDBACK_TRANSCRIPT_CHARS),
            "exit_code": record["exit_code"],
            "elapsed_seconds": record["elapsed_seconds"],
            "usage": record["usage"],
        }
        if record["error"] is not None:
            feedback["error"] = record["error"]
        if record["check"] is not None:
            feedback["check"] = record["check"]
        return feedback

    def run(self, candidate: Candidate, case: Any = None) -> dict[str, Any]:
        """Run the agent once in a fresh sandbox and return the run record.

        Args:
            candidate: The harness version.
            case: The task the agent works on.

        Returns:
            ``case``, ``output`` (the answer file, else the parsed final
            message), ``transcript``, ``exit_code``, ``timed_out``,
            ``elapsed_seconds``, ``check``, ``usage``, ``error`` and
            ``attempts`` (one entry per box: its operation key, error group
            and cost owner).

        Raises:
            ServiceError: When the version or case ships an unsafe path, or the
                sandbox could not be created or driven on its last allowed
                attempt; the eval server turns the latter into a floor score.
        """
        return self._run(candidate, case, *self._next_label(case))

    def _next_label(self, case: Any) -> tuple[int, str]:
        """Return the next run's ordinal and log label, which names the case when the case names itself.

        Args:
            case: The task the run works on.

        Returns:
            The run id and the label.
        """
        run_id = next(self._run_ids)
        label = f"agent run {run_id}"
        name = case_name(case)
        if name is not None:
            label = f"{label} (case {name[:40]})"
        return run_id, label

    def _run(self, candidate: Candidate, case: Any, run_id: int, label: str) -> dict[str, Any]:
        """Run the agent, one fresh box per attempt, as the retry policy allows; see :meth:`run`.

        Each attempt gets its own ``:attempt-N`` operation key, so a protected
        runtime opens a new reservation and box for it and never charges one
        attempt twice.

        Args:
            candidate: The harness version.
            case: The task the agent works on.
            run_id: The run's ordinal within the job.
            label: Prefix of this run's log lines.

        Returns:
            The run record.
        """
        launch = self._launch
        files = {
            **launch.files,
            **candidate_files(candidate, launch.instructions_file),
            **case_files(case),
            PROMPT_FILE: case_prompt(case),
        }
        run = self._recorder.begin(run_id=run_id, label=label, case_id=case_name(case), model=self._target.model)
        policy = self._retry_policy
        base_key = f"agent-run:{uuid.uuid4().hex}"
        attempts: list[dict[str, Any]] = []

        def note_failure(attempt: int, key: str, exc: BaseException) -> None:
            """Record a failed attempt and, when it runs again, say so in the transcript.

            Args:
                attempt: The attempt number, from 1.
                key: The attempt's operation key.
                exc: The attempt's error.
            """
            group = error_group(exc)
            owner = cost_owner(group)
            attempts.append(
                {
                    "attempt": attempt,
                    "operation_key": key,
                    "error_group": group.value if group else None,
                    "cost_owner": owner,
                }
            )
            if attempt < policy.max_attempts and should_retry(policy, exc):
                logger.warning(
                    "%s: attempt %d failed (%s, cost owner %s); retrying in a fresh box",
                    label,
                    attempt,
                    group or "unknown",
                    owner,
                    exc_info=True,
                )
                run.write(f"\n[sandbox] attempt {attempt} failed ({group or 'unknown'}); retrying in a fresh box\n")

        retrying = Retrying(
            stop=stop_after_attempt(policy.max_attempts),
            retry=retry_if_exception(lambda exc: should_retry(policy, exc)),
            wait=policy.wait_strategy(),
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    number = attempt.retry_state.attempt_number
                    key = attempt_key(base_key, number)
                    try:
                        record = self._attempt(files, case, label, run, operation_key=key)
                    except BaseException as exc:
                        note_failure(number, key, exc)
                        raise
                    attempts.append(
                        {"attempt": number, "operation_key": key, "error_group": None, "cost_owner": COST_OWNER_USER}
                    )
        except (BudgetError, UsagePendingError, UnpricedOperationError):
            raise
        except ServiceError as exc:
            logger.warning("%s: %s", label, exc)
            run.abort(str(exc))
            raise
        except Exception as exc:
            if getattr(self._runtime, "protected", False):
                raise
            count = len(attempts)
            logger.warning("%s: sandbox failed on attempt %d; giving up", label, count, exc_info=True)
            tries = "1 attempt" if count == 1 else f"{count} attempts"
            error = ServiceError(f"agent sandbox failed after {tries} ({type(exc).__name__}): {exc}")
            run.abort(str(error))
            raise error from exc
        record["attempts"] = attempts
        run.finish(record)
        return record

    def _lifetime_seconds(self, case: Any) -> float:
        """Return a box lifetime that covers every step's own allowance.

        Only the lifetime ceiling can cut into the agent's run time, and
        then :meth:`_execute` shortens the run to what is left.

        Args:
            case: The task, for its optional ``check_command``.

        Returns:
            The lifetime, in seconds.
        """
        setup_steps = sum(1 for command in (self._launch.install_command, self._target.setup_command) if command)
        if _check_command(case) is not None:
            setup_steps += 1
        return min(case_lifetime_seconds(self._target.timeout_seconds, setup_steps), self._max_lifetime_seconds)

    def _attempt(
        self, files: dict[str, str], case: Any, label: str, run: AgentRun, *, operation_key: str
    ) -> dict[str, Any]:
        """Open one box under a fresh name, drive it and close it.

        Args:
            files: Everything to write before the first command.
            case: The task the agent works on.
            label: Prefix of this run's log lines.
            run: The run's record, fed the box's output as it arrives.
            operation_key: The attempt's own key for its reservation and box.

        Returns:
            The run record.
        """
        spec = SandboxSpec(
            lifetime_seconds=self._lifetime_seconds(case),
            env=self._launch.env,
            name=self._sandbox_name(),
            tags={JOB_TAG: self._job_id} if self._job_id else {},
            operation_key=operation_key,
        )
        record: dict[str, Any] = {
            "case": case,
            "output": None,
            "transcript": "",
            "exit_code": None,
            "timed_out": False,
            "elapsed_seconds": 0.0,
            "check": None,
            "usage": {},
            "error": None,
        }
        transcript: list[str] = []
        logger.info("%s: opening sandbox %s (lifetime %.0fs)", label, spec.name, spec.lifetime_seconds)
        started = time.perf_counter()
        session = self._runtime.open(spec)
        logger.info("%s: sandbox ready in %.1fs", label, time.perf_counter() - started)
        try:
            self._execute(
                session, files, case, record, transcript, run, label=label, deadline=started + spec.lifetime_seconds
            )
        finally:
            try:
                session.close()
            except UsagePendingError:
                # The parent publishes the durable pending hold separately from
                # the completed agent record and the optimizer's original error.
                record["runtime_usage_pending"] = True
            record["transcript"] = _tail("\n".join(transcript), _TRANSCRIPT_CHARS)
            record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            logger.debug("%s: sandbox closed after %.1fs", label, record["elapsed_seconds"])
        return record

    def _execute(
        self,
        session: SandboxSession,
        files: dict[str, str],
        case: Any,
        record: dict[str, Any],
        transcript: list[str],
        run: AgentRun,
        *,
        label: str,
        deadline: float,
    ) -> None:
        """Drive one open sandbox through install → setup → run → check.

        Args:
            session: The open sandbox.
            files: Everything to write before the first command.
            case: The task, for its optional ``check_command``.
            record: Filled in place.
            transcript: Appended with one block per command.
            run: The run's record, fed every command's output as it arrives.
            label: Prefix of this run's log lines.
            deadline: When the box's lifetime ends, on the monotonic clock.
        """
        session.write_files(files)
        for step, command in (("install", self._launch.install_command), ("setup", self._target.setup_command)):
            if not command:
                continue
            logger.info("%s: %s running (allowance %.0fs)", label, step, _SETUP_ALLOWANCE_SECONDS)
            step_started = time.perf_counter()
            with heartbeat(logger, label, step, _SETUP_ALLOWANCE_SECONDS):
                result = _stream_step(session, run, step, command, timeout_seconds=_SETUP_ALLOWANCE_SECONDS)
            transcript.append(_format_step(step, result))
            if not result.ok:
                logger.warning(
                    "%s: %s failed (exit %s) after %.1fs",
                    label,
                    step,
                    result.exit_code,
                    time.perf_counter() - step_started,
                )
                record["exit_code"], record["timed_out"] = result.exit_code, result.timed_out
                record["error"] = f"{step} command failed (exit {result.exit_code})"
                return
            logger.info("%s: %s done in %.1fs", label, step, time.perf_counter() - step_started)
        # The lifetime was sized for every step, so only the ceiling can leave
        # the run short; a run cut to fit still ends in a clean timeout rather
        # than the box dying under it.
        run_timeout = self._target.timeout_seconds
        remaining = deadline - time.perf_counter() - KILL_GRACE_SECONDS
        if remaining < run_timeout:
            run_timeout = max(remaining, 1.0)
            logger.warning(
                "%s: the box lifetime leaves %.0fs of the %gs run timeout",
                label,
                run_timeout,
                self._target.timeout_seconds,
            )
        logger.info("%s: agent running (timeout %.0fs)", label, run_timeout)
        run_started = time.perf_counter()
        with heartbeat(logger, label, "agent", run_timeout):
            result = _stream_step(session, run, "run", self._launch.run_command, timeout_seconds=run_timeout)
        transcript.append(_format_step("run", result))
        record["exit_code"], record["timed_out"] = result.exit_code, result.timed_out
        answer = session.read_file(ANSWER_FILE)
        parsed, usage = self._launch.parse_output(result.stdout)
        record["usage"] = usage
        output = answer if answer is not None and answer.strip() else parsed
        run.output = output
        record["output"] = None if output is None else _clip(output, _OUTPUT_CHARS)
        if result.timed_out:
            record["error"] = f"agent timed out after {run_timeout:g}s"
        elif not result.ok:
            record["error"] = f"agent exited with code {result.exit_code}"
        elif record["output"] is None:
            record["error"] = "agent produced no answer"
        run_elapsed = time.perf_counter() - run_started
        if record["error"] is None:
            logger.info(
                "%s: agent finished in %.1fs (exit %s, %s)", label, run_elapsed, result.exit_code, _usage_summary(usage)
            )
        else:
            logger.warning("%s: %s after %.1fs (%s)", label, record["error"], run_elapsed, _usage_summary(usage))
        check = _check_command(case)
        if check is not None:
            check_started = time.perf_counter()
            with heartbeat(logger, label, "check", _SETUP_ALLOWANCE_SECONDS):
                checked = _stream_step(session, run, "check", check, timeout_seconds=_SETUP_ALLOWANCE_SECONDS)
            transcript.append(_format_step("check", checked))
            record["check"] = {
                "passed": checked.ok,
                "exit_code": checked.exit_code,
                "output": _tail(
                    "\n".join(part for part in (checked.stdout, checked.stderr) if part), _CHECK_OUTPUT_CHARS
                ),
            }
            verdict = "passed" if checked.ok else f"failed (exit {checked.exit_code})"
            logger.info("%s: check %s in %.1fs", label, verdict, time.perf_counter() - check_started)

    def _sandbox_name(self) -> str:
        """Return a fresh sandbox name that carries the job id."""
        return unique_sandbox_name(f"app-{self._job_id or 'blackbox'}")

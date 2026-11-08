"""Tests for agent-target scoring: path safety, case rendering, the sandbox run and gateway resolution."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pytest

from core.billing.runtime import UsagePendingError
from core.config import Settings
from core.exceptions import InfrastructureInterruptionError, ServiceError
from core.models.blackbox import BLACKBOX_HARNESS_CUSTOM, BLACKBOX_TARGET_AGENT, BlackboxTarget
from core.sandbox.environment import HealthcheckConfig
from core.sandbox.errors import (
    ErrorGroup,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxRunOutcomeError,
    SandboxTransientError,
)
from core.sandbox.retry import DEFAULT_RETRY_POLICY, RetryPolicy

from .. import agent_eval as agent_eval_mod
from ..agent_eval import (
    _GATEWAY_MISSING,
    AgentHarnessError,
    SandboxAgentScorer,
    agent_target_unavailable_reason,
    candidate_files,
    case_files,
    case_prompt,
    error_group,
    gateway_from_settings,
    safe_relative_path,
    should_retry,
)
from ..agent_runs import PHASE_BASELINE, AgentRunRecorder, run_scope
from ..harness import ANSWER_FILE, PROMPT_FILE, GatewayConfig
from ..sandbox import CommandResult, OutputSink
from .mocks import FakeSandboxRuntime, FakeSandboxSession

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")


def _target(**overrides: Any) -> BlackboxTarget:
    """Build a custom agent target that runs ``run-agent``.

    Args:
        **overrides: Field overrides.

    Returns:
        The target.
    """
    fields: dict[str, Any] = {
        "kind": BLACKBOX_TARGET_AGENT,
        "harness": BLACKBOX_HARNESS_CUSTOM,
        "model": "m",
        "run_command": "run-agent",
    }
    fields.update(overrides)
    return BlackboxTarget(**fields)


def _scorer_of(
    runtime: FakeSandboxRuntime, target: BlackboxTarget, scorer: Any = None, *, job_id: str | None = "job-1"
):
    """Wrap ``scorer`` in a :class:`SandboxAgentScorer` over ``runtime``.

    Args:
        runtime: The fake sandbox runtime.
        target: The agent target.
        scorer: The user's scorer; a constant one when unset.
        job_id: Job id for tagging.

    Returns:
        The wrapped scorer.
    """
    scorer = scorer or (lambda candidate, case: (0.0, {}))
    return SandboxAgentScorer(scorer, runtime=runtime, target=target, gateway=_GATEWAY, job_id=job_id)


def _runtime_of(session: FakeSandboxSession) -> FakeSandboxRuntime:
    """Return a runtime that always opens ``session``.

    Args:
        session: The session to hand out.

    Returns:
        The runtime.
    """
    return FakeSandboxRuntime(session_factory=lambda: session)


def test_safe_relative_path_normalizes_and_rejects_escapes() -> None:
    """Relative paths are normalized; anything that could escape the workspace is refused."""
    assert safe_relative_path("./a/b") == "a/b"
    assert safe_relative_path("a/./b/") == "a/b"
    assert safe_relative_path("a/c/../d") == "a/d"

    for bad in ("/etc/passwd", "..", "../x", "a/../../x", ".", "", "   ", 123, None):
        with pytest.raises(ServiceError):
            safe_relative_path(bad)


@pytest.mark.parametrize("harness", ["custom", "pi", "codex", "opencode"])
def test_seedless_agent_readiness_uses_real_runtime_without_candidate(harness: str) -> None:
    """Check shell syntax and pinned binaries while never launching a model or inventing input."""
    runtime = FakeSandboxRuntime()
    runtime.protected = True
    scorer = _scorer_of(runtime, _target(harness=harness, setup_command="echo prepare"))
    result = scorer.check_ready()
    session = runtime.sessions[0]
    assert runtime.specs[0].network_disabled is True
    assert session.closed
    assert session.commands[0] == "true"
    assert len(session.commands) == 2
    assert "bash -n .app/readiness-0.sh" in session.commands[1]
    assert "--version" in session.commands[1] or harness == "custom"
    assert "npm install" not in session.commands[1]
    assert not any(name in session.files for name in (PROMPT_FILE, "AGENTS.md"))
    assert result["candidate_execution"] == "awaiting_first_generated_candidate"
    assert result["readiness"] == (
        "command_syntax_verified" if harness == "custom" else "pinned_cli_and_syntax_verified"
    )


def test_agent_readiness_rejects_invalid_bootstrap_and_closes() -> None:
    """Retain an explicit setup failure when a real sandbox version or syntax check fails."""
    session = FakeSandboxSession(script=lambda command: CommandResult(exit_code=0 if command == "true" else 2))
    runtime = _runtime_of(session)
    runtime.protected = True
    with pytest.raises(ServiceError, match="offline version or command syntax"):
        _scorer_of(runtime, _target()).check_ready()
    assert session.closed


_FAST_HEALTHCHECK = HealthcheckConfig(
    command="probe", interval_sec=0.0, timeout_sec=5.0, start_interval_sec=0.0, retries=3
)


def _ready_scorer(session: FakeSandboxSession) -> tuple[SandboxAgentScorer, FakeSandboxRuntime]:
    """Return a protected scorer whose readiness box is ``session``, with a zero-wait healthcheck.

    Args:
        session: The readiness box.

    Returns:
        The scorer and its runtime.
    """
    runtime = _runtime_of(session)
    runtime.protected = True
    scorer = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}),
        runtime=runtime,
        target=_target(),
        gateway=_GATEWAY,
        healthcheck=_FAST_HEALTHCHECK,
    )
    return scorer, runtime


class _SlowStartSession(FakeSandboxSession):
    """Box whose first probes raise or fail before the healthcheck passes."""

    def __init__(self, outcomes: list[BaseException | int]) -> None:
        """Create the session.

        Args:
            outcomes: For each early probe, an error to raise or an exit code; later probes pass.
        """
        super().__init__()
        self._outcomes = list(outcomes)

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Play the next probe outcome, then defer to the fake for other commands.

        Args:
            command: The command line.
            env: Passed through.
            timeout_seconds: Passed through.
            on_output: Passed through.

        Returns:
            The probe result.

        Raises:
            BaseException: When the next outcome is an error.
        """
        if command == "probe" and self._outcomes:
            self.commands.append(command)
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return CommandResult(exit_code=outcome)
        return super().run(command, env=env, timeout_seconds=timeout_seconds, on_output=on_output)


def test_readiness_healthcheck_waits_for_a_slow_box_within_the_retry_limit() -> None:
    """Probes that fail or raise below the limit do not fail readiness; the first passing probe ends the poll."""
    session = _SlowStartSession([TimeoutError("not up"), 1])
    scorer, _runtime = _ready_scorer(session)

    result = scorer.check_ready()

    assert session.commands[:3] == ["probe", "probe", "probe"]
    assert "probe" not in session.commands[3:]
    assert result["readiness"] == "command_syntax_verified"
    assert session.closed


def test_readiness_healthcheck_gives_up_after_the_retry_limit_and_closes() -> None:
    """A box that never answers fails readiness after exactly ``retries`` probes, and the box is closed."""
    session = _SlowStartSession([1, ConnectionError("refused"), 1, 1, 1])
    scorer, _runtime = _ready_scorer(session)

    with pytest.raises(ServiceError, match=r"readiness healthcheck 3 times in a row \(exit code 1\)"):
        scorer.check_ready()

    assert session.commands == ["probe", "probe", "probe"]
    assert session.closed


def test_readiness_healthcheck_does_not_count_failures_in_the_start_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures inside the start period are free, so a slow first boot does not use up the retries."""
    clock = iter([0.0, 0.0, 1.0, 2.0, 100.0, 101.0, 102.0, 103.0])
    monkeypatch.setattr(agent_eval_mod.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(agent_eval_mod.time, "sleep", lambda _seconds: None)
    session = _SlowStartSession([1, 1, 1, 1])
    runtime = _runtime_of(session)
    runtime.protected = True
    scorer = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}),
        runtime=runtime,
        target=_target(),
        gateway=_GATEWAY,
        healthcheck=HealthcheckConfig(command="probe", interval_sec=0.0, start_period_sec=10.0, retries=2),
    )

    scorer.check_ready()

    assert session.commands[:5] == ["probe"] * 5


def test_readiness_healthcheck_lets_a_billing_error_through() -> None:
    """A budget error during a probe is not a failed probe; it stops readiness at once."""
    session = _SlowStartSession([UsagePendingError("usage pending")])
    scorer, _runtime = _ready_scorer(session)

    with pytest.raises(UsagePendingError):
        scorer.check_ready()

    assert session.commands == ["probe"]
    assert session.closed


def test_agent_readiness_executes_authored_dependencies_offline() -> None:
    """Run an authored install command during Continue before accepting the setup."""
    runtime = FakeSandboxRuntime()
    runtime.protected = True
    scorer = _scorer_of(
        runtime,
        _target(install_command="pip install --no-index ./wheels/tool.whl"),
    )

    result = scorer.check_ready()

    session = runtime.sessions[0]
    assert runtime.specs[0].network_disabled is True
    assert session.commands[-1] == "pip install --no-index ./wheels/tool.whl"
    assert result["readiness"] == "offline_dependencies_and_syntax_verified"


def test_agent_readiness_reports_missing_offline_dependency() -> None:
    """Fail Continue when the pinned image cannot satisfy an install command."""

    def script(command: str) -> CommandResult:
        """Fail only the dependency installation after syntax validation."""
        if command.startswith("pip install"):
            return CommandResult(exit_code=1, stderr="No matching distribution found")
        return CommandResult(exit_code=0)

    runtime = FakeSandboxRuntime(session_factory=lambda: FakeSandboxSession(script=script))
    runtime.protected = True

    with pytest.raises(ServiceError, match="offline cloud sandbox: No matching distribution found"):
        _scorer_of(runtime, _target(install_command="pip install unavailable-package")).check_ready()

    assert runtime.sessions[0].closed is True


def test_candidate_files_maps_text_and_named_parts() -> None:
    """A text version becomes the instructions file; a dict version becomes its safe parts."""
    assert candidate_files("hello", "AGENTS.md") == {"AGENTS.md": "hello"}
    assert candidate_files({"./AGENTS.md": "x", "docs/y.md": "y"}, "AGENTS.md") == {"AGENTS.md": "x", "docs/y.md": "y"}

    with pytest.raises(ServiceError):
        candidate_files({"../escape": "x"}, "AGENTS.md")


def test_case_files_only_from_a_files_mapping() -> None:
    """Only a dict case with a ``files`` mapping contributes files, stringifying contents."""
    assert case_files("a string") == {}
    assert case_files({"prompt": "hi"}) == {}
    assert case_files({"files": {"a.txt": 5}}) == {"a.txt": "5"}

    with pytest.raises(ServiceError, match="must map relative paths"):
        case_files({"files": ["not", "a", "map"]})


def test_case_prompt_prefers_task_keys_then_json_then_default() -> None:
    """The task text comes from the first prompt key, else the case as JSON, else a default."""
    assert case_prompt({"task": "T", "prompt": "P"}).startswith("P")
    assert case_prompt({"prompt": "   ", "task": "real"}).startswith("real")

    rendered = case_prompt({"foo": "bar", "files": {"a": "b"}, "check_command": "c"})
    assert '"foo": "bar"' in rendered
    assert "check_command" not in rendered
    assert "files" not in rendered.split("---")[0]

    assert case_prompt(None).startswith("Follow the instructions you were given.")
    assert case_prompt("just do it").startswith("just do it")


def test_case_prompt_appends_the_answer_file_instruction() -> None:
    """Every rendered prompt tells the agent where to write its scored answer."""
    rendered = case_prompt({"prompt": "go"})
    assert ANSWER_FILE in rendered
    assert rendered.rstrip().endswith("Only that file is scored.")


def test_run_writes_the_version_and_case_then_records_a_clean_run() -> None:
    """A clean run writes files, runs the agent, prefers the answer file and closes the box."""
    runtime = FakeSandboxRuntime()
    scorer = _scorer_of(runtime, _target())

    record = scorer.run("AGENTS instructions", {"prompt": "do the thing"})

    session = runtime.sessions[0]
    assert session.files["AGENTS.md"] == "AGENTS instructions"
    assert session.files[PROMPT_FILE].startswith("do the thing")
    assert session.commands == ["run-agent"]
    assert session.closed is True
    assert record["output"] == "done"
    assert record["exit_code"] == 0
    assert record["timed_out"] is False
    assert record["error"] is None


def test_run_builds_a_tagged_named_spec_within_the_lifetime_ceiling() -> None:
    """The sandbox spec carries the lifetime, harness env, job name and job tag."""
    runtime = FakeSandboxRuntime()
    scorer = _scorer_of(runtime, _target(timeout_seconds=600), job_id="job-1")

    scorer.run("v", {"prompt": "p"})

    spec = runtime.specs[0]
    assert spec.lifetime_seconds == 60 + 600 + 15
    assert spec.env["APP_MODEL"] == "m"
    assert spec.name.startswith("app-job-1-")
    assert len(spec.name) <= 60
    assert spec.tags == {"skynet_job": "job-1"}


def test_each_run_opens_a_box_under_its_own_name() -> None:
    """Holdout cases run in parallel, so two runs of one job never ask Vercel for the same sandbox name."""
    runtime = FakeSandboxRuntime()
    scorer = _scorer_of(runtime, _target(), job_id="job-1")

    scorer.run("v", {"prompt": "p"})
    scorer.run("v", {"prompt": "q"})

    names = [spec.name for spec in runtime.specs]
    assert len(set(names)) == 2
    assert all(name.startswith("app-job-1-") for name in names)


class _DyingSession(FakeSandboxSession):
    """Box whose transport fails on the first command, the way a stalled SDK call does."""

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Raise a transport error instead of running anything.

        Args:
            command: The command line.
            env: Ignored.
            timeout_seconds: Ignored.

        Raises:
            TimeoutError: Always.
        """
        self.commands.append(command)
        raise TimeoutError("The read operation timed out")


_RETRY_ONCE = RetryPolicy(max_retries=1, wait_min=0.0, wait_max=0.0)


def _flaky_runtime(error: BaseException, failures: int = 1) -> FakeSandboxRuntime:
    """Return a runtime whose first ``failures`` boxes raise ``error`` on their first command.

    Args:
        error: What the failing boxes raise.
        failures: How many boxes fail before healthy ones.

    Returns:
        The runtime.
    """
    boxes: list[FakeSandboxSession] = []

    def factory() -> FakeSandboxSession:
        """Hand out failing boxes first, then healthy ones."""
        if len(boxes) < failures:
            session: FakeSandboxSession = _RaisingSession(error)
        else:
            session = FakeSandboxSession(produces={"run-agent": {"output/answer.txt": "done"}})
        boxes.append(session)
        return session

    return FakeSandboxRuntime(session_factory=factory)


class _RaisingSession(FakeSandboxSession):
    """Box whose first command raises a given error."""

    def __init__(self, error: BaseException) -> None:
        """Create the session.

        Args:
            error: What :meth:`run` raises.
        """
        super().__init__()
        self._error = error

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Raise the configured error.

        Args:
            command: The command line.
            env: Ignored.
            timeout_seconds: Ignored.
            on_output: Ignored.

        Raises:
            BaseException: Always.
        """
        self.commands.append(command)
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        SandboxInfraError("host recycled"),
        SandboxTransientError("rate limited"),
        SandboxRejectedError("bad spec"),
        SandboxFatalError("cancelled"),
        SandboxRunOutcomeError("agent timed out"),
        InfrastructureInterruptionError("transport broke"),
        TimeoutError("The read operation timed out"),
        RuntimeError("unknown"),
    ],
)
def test_default_policy_never_retries_any_error_class(error: BaseException) -> None:
    """D3/D10: with the default policy, every error class ends the run after one box."""
    runtime = _flaky_runtime(error)

    with pytest.raises(ServiceError):
        _scorer_of(runtime, _target()).run("v", {"prompt": "p"})

    assert len(runtime.sessions) == 1
    assert runtime.sessions[0].closed is True


@pytest.mark.parametrize(
    ("error", "group", "retries"),
    [
        (SandboxInfraError("host recycled"), ErrorGroup.INFRA, True),
        (InfrastructureInterruptionError("transport broke"), ErrorGroup.INFRA, True),
        (SandboxTransientError("rate limited"), ErrorGroup.TRANSIENT, True),
        (TimeoutError("The read operation timed out"), ErrorGroup.TRANSIENT, True),
        (ConnectionResetError("reset"), ErrorGroup.TRANSIENT, True),
        (SandboxRejectedError("bad spec"), ErrorGroup.REJECTED, False),
        (ServiceError("bad case"), ErrorGroup.REJECTED, False),
        (SandboxFatalError("cancelled"), ErrorGroup.FATAL, False),
        (SandboxRunOutcomeError("agent timed out"), ErrorGroup.RUN_OUTCOME, False),
        (RuntimeError("unknown"), None, False),
    ],
)
def test_error_class_table_retries_only_infra_and_transient(
    error: BaseException, group: ErrorGroup | None, retries: bool
) -> None:
    """The table follows Harbor's groups: only infra and transient errors open a retry."""
    assert error_group(error) is group
    assert should_retry(_RETRY_ONCE, error) is retries
    assert should_retry(DEFAULT_RETRY_POLICY, error) is False


def test_policy_include_opens_an_unknown_class_but_never_a_no_retry_group() -> None:
    """Advanced mode names classes: an unknown one can retry, a rejected one never does."""
    policy = RetryPolicy(max_retries=1, include=(RuntimeError, ServiceError), wait_min=0.0, wait_max=0.0)
    assert should_retry(policy, RuntimeError("unknown")) is True
    assert should_retry(policy, ServiceError("bad case")) is False
    assert should_retry(policy, TimeoutError("slow")) is False
    narrowed = RetryPolicy(max_retries=1, exclude=(TimeoutError,), wait_min=0.0, wait_max=0.0)
    assert should_retry(narrowed, TimeoutError("slow")) is False
    assert should_retry(narrowed, SandboxInfraError("host")) is True


def test_billing_errors_never_retry_even_when_included() -> None:
    """A budget error stops the run at once, whatever the policy says."""
    policy = RetryPolicy(max_retries=3, include=(Exception,), wait_min=0.0, wait_max=0.0)
    runtime = _flaky_runtime(UsagePendingError("usage pending"))

    with pytest.raises(UsagePendingError):
        SandboxAgentScorer(
            lambda candidate, case: (0.0, {}),
            runtime=runtime,
            target=_target(),
            gateway=_GATEWAY,
            retry_policy=policy,
        ).run("v", {"prompt": "p"})

    assert should_retry(policy, UsagePendingError("usage pending")) is False
    assert len(runtime.sessions) == 1


def test_advanced_policy_retries_in_a_fresh_box_with_its_own_attempt_key() -> None:
    """Each attempt gets its own ``:attempt-N`` key and box name, so no attempt is charged twice."""
    runtime = _flaky_runtime(SandboxInfraError("host recycled"), failures=2)
    scorer = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}),
        runtime=runtime,
        target=_target(),
        gateway=_GATEWAY,
        job_id="job-1",
        retry_policy=RetryPolicy(max_retries=2, wait_min=0.0, wait_max=0.0),
    )

    record = scorer.run("v", {"prompt": "p"})

    assert record["output"] == "done"
    assert record["error"] is None
    assert len(runtime.sessions) == 3
    assert all(session.closed for session in runtime.sessions)
    keys = [spec.operation_key for spec in runtime.specs]
    assert len(set(keys)) == 3
    base = keys[0].removesuffix(":attempt-1")
    assert keys == [f"{base}:attempt-{n}" for n in (1, 2, 3)]
    assert len({spec.name for spec in runtime.specs}) == 3
    assert record["attempts"] == [
        {"attempt": 1, "operation_key": keys[0], "error_group": "infra", "cost_owner": "skynet"},
        {"attempt": 2, "operation_key": keys[1], "error_group": "infra", "cost_owner": "skynet"},
        {"attempt": 3, "operation_key": keys[2], "error_group": None, "cost_owner": "user"},
    ]


def test_retry_count_bounds_the_attempts_and_reports_a_sandbox_failure() -> None:
    """When every allowed attempt dies, the run ends as a sandbox error after ``max_retries + 1`` boxes."""
    runtime = FakeSandboxRuntime(session_factory=_DyingSession)
    scorer = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}),
        runtime=runtime,
        target=_target(),
        gateway=_GATEWAY,
        retry_policy=_RETRY_ONCE,
    )

    with pytest.raises(
        ServiceError, match=r"agent sandbox failed after 2 attempts \(TimeoutError\): The read operation timed out"
    ):
        scorer.run("v", {"prompt": "p"})

    assert len(runtime.sessions) == 2
    assert all(session.closed for session in runtime.sessions)
    assert len({spec.operation_key for spec in runtime.specs}) == 2


def test_run_reports_a_dead_box_as_a_sandbox_failure_without_a_retry() -> None:
    """A dead box surfaces as a sandbox error, not as the raw transport exception, after one attempt."""
    runtime = FakeSandboxRuntime(session_factory=_DyingSession)
    scorer = _scorer_of(runtime, _target())

    with pytest.raises(
        ServiceError, match=r"agent sandbox failed after 1 attempt \(TimeoutError\): The read operation timed out"
    ):
        scorer.run("v", {"prompt": "p"})

    assert len(runtime.sessions) == 1
    assert runtime.specs[0].operation_key.endswith(":attempt-1")


def test_protected_runtime_raises_the_raw_error_of_its_last_attempt() -> None:
    """A protected runtime keeps the original error so the parent can handle the interruption."""
    runtime = _flaky_runtime(InfrastructureInterruptionError("transport broke"), failures=5)
    runtime.protected = True
    scorer = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}),
        runtime=runtime,
        target=_target(),
        gateway=_GATEWAY,
        retry_policy=_RETRY_ONCE,
    )

    with pytest.raises(InfrastructureInterruptionError):
        scorer.run("v", {"prompt": "p"})

    assert len(runtime.sessions) == 2


class _StuckSession(FakeSandboxSession):
    """Box whose runtime gives up on a command that outlives its timeout, the way the real one does."""

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Raise the runtime's own error instead of running anything.

        Args:
            command: The command line.
            env: Ignored.
            timeout_seconds: Ignored.

        Raises:
            ServiceError: Always.
        """
        raise ServiceError(f"command still running 60s past its {timeout_seconds:g}s timeout: {command}")


def test_run_logs_a_runtime_failure_before_raising_it(caplog: pytest.LogCaptureFixture) -> None:
    """A runtime error ends the run at once, and the job log says so under the run's label."""
    runtime = FakeSandboxRuntime(session_factory=_StuckSession)
    scorer = _scorer_of(runtime, _target())

    with (
        caplog.at_level(logging.WARNING, logger=agent_eval_mod.logger.name),
        pytest.raises(ServiceError, match="still running 60s past its 600s timeout"),
    ):
        scorer.run("v", {"prompt": "p"})

    assert len(runtime.sessions) == 1
    assert [record.getMessage() for record in caplog.records] == [
        "agent run 1: command still running 60s past its 600s timeout: run-agent"
    ]


def test_run_caps_the_lifetime_at_the_configured_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """``VERCEL_SANDBOX_MAX_LIFETIME_SECONDS`` bounds the box unless an explicit ceiling is given."""
    monkeypatch.setattr(agent_eval_mod.settings, "vercel_sandbox_max_lifetime_seconds", 1000.0)
    runtime = FakeSandboxRuntime()
    target = _target(timeout_seconds=2000)

    _scorer_of(runtime, target).run("v", {"prompt": "p"})
    explicit = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}), runtime=runtime, target=target, gateway=_GATEWAY, max_lifetime_seconds=700
    )
    explicit.run("v", {"prompt": "p"})

    assert [spec.lifetime_seconds for spec in runtime.specs] == [1000, 700]


def test_lifetime_covers_each_step_with_its_own_allowance() -> None:
    """Setup and check each add their allowance plus the kill grace on top of the run's."""
    runtime = FakeSandboxRuntime()
    scorer = _scorer_of(runtime, _target(timeout_seconds=600, setup_command="make setup"))

    scorer.run("v", {"prompt": "p", "check_command": "test -f out"})

    assert runtime.specs[0].lifetime_seconds == 60 + 3 * (600 + 15)
    assert runtime.sessions[0].commands == ["make setup", "run-agent", "test -f out"]


class _TimeoutRecorder(FakeSandboxSession):
    """Fake session that also remembers the timeout each command ran under."""

    def __init__(self) -> None:
        """Start with a clean-exit script that answers ``done`` and no recorded timeouts."""
        super().__init__(produces={"run-agent": {ANSWER_FILE: "done"}})
        self.timeouts: list[float | None] = []

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Record the timeout, then behave like the base fake."""
        self.timeouts.append(timeout_seconds)
        return super().run(command, env=env, timeout_seconds=timeout_seconds, on_output=on_output)


def test_run_timeout_is_shortened_only_when_the_ceiling_bites(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ceiling below the run's own allowance cuts the run to what is left and says so."""
    monkeypatch.setattr(agent_eval_mod.settings, "vercel_sandbox_max_lifetime_seconds", 1000.0)
    caplog.set_level(logging.WARNING, logger="core.service_gateway.optimization")
    session = _TimeoutRecorder()
    scorer = _scorer_of(_runtime_of(session), _target(timeout_seconds=2000))

    record = scorer.run("v", {"prompt": "p"})

    assert record["error"] is None
    assert 900 <= (session.timeouts[-1] or 0) <= 1000 - 15
    assert "leaves" in caplog.text


def test_run_falls_back_to_parsed_stdout_when_no_answer_file() -> None:
    """With no answer file the parsed stdout is the output."""
    session = FakeSandboxSession(script=lambda command: CommandResult(exit_code=0, stdout="parsed answer"))
    scorer = _scorer_of(_runtime_of(session), _target())

    record = scorer.run("v", {"prompt": "p"})

    assert record["output"] == "parsed answer"
    assert record["error"] is None


def test_run_reports_when_the_agent_produces_no_answer() -> None:
    """No answer file and empty stdout is a no-answer error."""
    session = FakeSandboxSession(script=lambda command: CommandResult(exit_code=0, stdout=""))
    scorer = _scorer_of(_runtime_of(session), _target())

    record = scorer.run("v", {"prompt": "p"})

    assert record["output"] is None
    assert record["error"] == "agent produced no answer"


def test_run_reports_a_timeout() -> None:
    """A timed-out run is flagged with the run timeout in the error."""
    session = FakeSandboxSession(script=lambda command: CommandResult(exit_code=124, timed_out=True))
    scorer = _scorer_of(_runtime_of(session), _target(timeout_seconds=600))

    record = scorer.run("v", {"prompt": "p"})

    assert record["timed_out"] is True
    assert record["error"] == "agent timed out after 600s"


def test_run_reports_a_nonzero_exit() -> None:
    """A non-zero exit that did not time out is reported with the code."""
    session = FakeSandboxSession(script=lambda command: CommandResult(exit_code=3, stdout=""))
    scorer = _scorer_of(_runtime_of(session), _target())

    record = scorer.run("v", {"prompt": "p"})

    assert record["error"] == "agent exited with code 3"


def test_install_failure_short_circuits_the_run() -> None:
    """A failed install stops before the agent runs."""

    def script(command: str) -> CommandResult:
        """Fail only the authored installation command."""
        return CommandResult(exit_code=1, stdout="boom") if command == "install-me" else CommandResult(exit_code=0)

    session = FakeSandboxSession(script=script)
    scorer = _scorer_of(_runtime_of(session), _target(install_command="install-me"))

    record = scorer.run("v", {"prompt": "p"})

    assert session.commands == ["install-me"]
    assert record["error"] == "install command failed (exit 1)"
    assert record["output"] is None


def test_setup_failure_short_circuits_the_run() -> None:
    """A failed case setup stops before the agent runs."""

    def script(command: str) -> CommandResult:
        """Fail only the authored case setup command."""
        return CommandResult(exit_code=2, stdout="nope") if command == "setup-me" else CommandResult(exit_code=0)

    session = FakeSandboxSession(script=script)
    scorer = _scorer_of(_runtime_of(session), _target(setup_command="setup-me"))

    record = scorer.run("v", {"prompt": "p"})

    assert session.commands == ["setup-me"]
    assert record["error"] == "setup command failed (exit 2)"


def test_check_command_is_recorded() -> None:
    """A case check runs after the agent and its outcome lands in the record."""

    def script(command: str) -> CommandResult:
        """Return visible success output for the case check."""
        return CommandResult(exit_code=0, stdout="checked ok") if command == "verify" else CommandResult(exit_code=0)

    session = FakeSandboxSession(script=script, produces={"run-agent": {ANSWER_FILE: "answer"}})
    scorer = _scorer_of(_runtime_of(session), _target())

    record = scorer.run("v", {"prompt": "p", "check_command": "verify"})

    assert "verify" in session.commands
    assert record["check"] == {"passed": True, "exit_code": 0, "output": "checked ok"}


def test_unsafe_candidate_path_raises_before_opening_a_sandbox() -> None:
    """An unsafe version path fails before any box is created."""
    runtime = FakeSandboxRuntime()
    scorer = _scorer_of(runtime, _target())

    with pytest.raises(ServiceError):
        scorer.run({"../escape": "x"}, {"prompt": "p"})
    assert runtime.sessions == []


class _RecordedRuns:
    """Everything a recorder sent: run rows and deltas on the sink, summaries on progress."""

    def __init__(self) -> None:
        """Start empty and build the recorder that feeds this collector."""
        self.rows: list[dict[str, Any]] = []
        self.progress: list[tuple[str, dict[str, Any]]] = []
        self.recorder = AgentRunRecorder(
            progress_callback=lambda event, metrics: self.progress.append((event, metrics)),
            run_sink=self.rows.append,
            secrets=("sk-secret",),
        )


class _StreamingSession(FakeSandboxSession):
    """Box that streams two lines, one carrying the gateway key, before exiting cleanly."""

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Stream the lines to ``on_output`` and return a clean exit.

        Args:
            command: The command line.
            env: Ignored.
            timeout_seconds: Ignored.
            on_output: Receives the streamed lines.

        Returns:
            A clean exit with no captured output.
        """
        self.commands.append(command)
        if on_output is not None:
            on_output("stdout", "thinking with sk-secret\n")
            on_output("stderr", "warn: slow\n")
        self.files[ANSWER_FILE] = "the answer"
        return CommandResult(exit_code=0)


def test_run_records_the_answer_and_transcript_under_the_current_scope() -> None:
    """The run view gets a running row at start and, at the end, the answer, transcript and outcome."""
    runs = _RecordedRuns()
    runtime = FakeSandboxRuntime()
    scorer = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}),
        runtime=runtime,
        target=_target(),
        gateway=_GATEWAY,
        job_id="job-1",
        recorder=runs.recorder,
    )

    with run_scope(PHASE_BASELINE, "2"):
        record = scorer.run("v", {"prompt": "p", "id": "case-a"})

    assert record["output"] == "done"
    assert [event for event, _ in runs.progress] == ["agent_run", "agent_run"]
    running, finished = (metrics for _, metrics in runs.progress)
    assert running["status"] == "running"
    assert running["phase"] == PHASE_BASELINE
    assert running["example_id"] == "2"
    assert running["trial"] is None
    assert running["case_id"] == "case-a"
    assert running["run_id"] == 1
    assert finished["status"] == "finished"
    assert finished["exit_code"] == 0
    assert runs.rows[0]["status"] == "running"
    assert runs.rows[0]["transcript"] == ""
    last = runs.rows[-1]
    assert last["status"] == "finished"
    assert last["output"] == "done"
    assert "[run] started\n" in last["transcript"]
    assert "ok\n" in last["transcript"]
    assert "[run] exit=0\n" in last["transcript"]
    assert last["finished_at"] is not None


def test_run_streams_box_output_into_the_transcript_and_redacts_the_key() -> None:
    """Streamed lines land in the transcript in order, with the gateway key masked."""
    runs = _RecordedRuns()
    session = _StreamingSession()
    scorer = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}),
        runtime=_runtime_of(session),
        target=_target(),
        gateway=_GATEWAY,
        job_id="job-1",
        recorder=runs.recorder,
    )

    record = scorer.run("v", {"prompt": "p"})

    assert record["output"] == "the answer"
    transcript = runs.rows[-1]["transcript"]
    assert "thinking with ***\n" in transcript
    assert "warn: slow\n" in transcript
    assert "sk-secret" not in transcript
    assert runs.rows[-1]["output"] == "the answer"


def test_run_marks_the_record_failed_when_every_attempt_dies() -> None:
    """A run whose boxes all die is closed as failed with the sandbox error, not left running."""
    runs = _RecordedRuns()
    scorer = SandboxAgentScorer(
        lambda candidate, case: (0.0, {}),
        runtime=FakeSandboxRuntime(session_factory=_DyingSession),
        target=_target(),
        gateway=_GATEWAY,
        job_id="job-1",
        recorder=runs.recorder,
        retry_policy=_RETRY_ONCE,
    )

    with pytest.raises(ServiceError):
        scorer.run("v", {"prompt": "p"})

    last = runs.rows[-1]
    assert last["status"] == "failed"
    assert last["error"].startswith("agent sandbox failed after 2 attempts")
    assert "[sandbox] attempt 1 failed (transient); retrying in a fresh box" in last["transcript"]
    assert "attempt 2 failed" not in last["transcript"]
    assert runs.progress[-1][1]["status"] == "failed"


def test_call_scores_the_record_and_merges_feedback() -> None:
    """The wrapped scorer sees the run record and the feedback is merged into its side info."""
    seen: dict[str, Any] = {}

    def scorer_fn(candidate: Any, record: Any) -> tuple[float, dict[str, Any]]:
        """Capture the run record and return deterministic feedback."""
        seen["record"] = record
        return 0.5, {"note": "x"}

    runtime = FakeSandboxRuntime()
    scorer = _scorer_of(runtime, _target(), scorer_fn)

    score, side_info = scorer("version-text", {"prompt": "p"})

    assert score == 0.5
    assert side_info["note"] == "x"
    assert side_info["agent_output"] == "done"
    assert set(side_info) >= {"note", "agent_output", "transcript_tail", "exit_code", "elapsed_seconds", "usage"}
    assert seen["record"]["case"] == {"prompt": "p"}
    assert seen["record"]["output"] == "done"


def test_call_aborts_once_the_first_runs_all_died_without_model_usage() -> None:
    """Three answerless runs in a row with no model usage stop the run with a typed error."""
    session = FakeSandboxSession(script=lambda command: CommandResult(exit_code=0, stdout=""))
    scorer = _scorer_of(_runtime_of(session), _target())

    for _ in range(2):
        score, side_info = scorer("v", {"prompt": "p"})
        assert (score, side_info["error"]) == (0.0, "agent produced no answer")
    with pytest.raises(AgentHarnessError, match=r"failed on the first 3 runs .*agent produced no answer"):
        scorer("v", {"prompt": "p"})


def test_one_live_run_disarms_the_dead_run_guard() -> None:
    """After a run produced an answer, later dead runs score 0 instead of aborting the run."""
    outputs = ["done", "", "", ""]
    session = FakeSandboxSession(script=lambda command: CommandResult(exit_code=0, stdout=outputs.pop(0)))
    scorer = _scorer_of(_runtime_of(session), _target())

    scores = [scorer("v", {"prompt": "p"})[0] for _ in range(4)]

    assert scores == [0.0, 0.0, 0.0, 0.0]
    assert scorer._dead_runs == 0


def test_feedback_clips_output_and_tails_transcript() -> None:
    """Feedback bounds the output and transcript and carries any error and check."""
    record = {
        "output": "a" * 3000,
        "transcript": "b" * 2000,
        "exit_code": 0,
        "elapsed_seconds": 1.0,
        "usage": {"input_tokens": 1},
        "error": "boom",
        "check": {"passed": False},
    }

    feedback = SandboxAgentScorer.feedback(record)

    assert feedback["agent_output"] == "a" * 2000 + "…"
    assert feedback["transcript_tail"] == "…" + "b" * 1500
    assert feedback["error"] == "boom"
    assert feedback["check"] == {"passed": False}


def _gateway_settings(**values: Any) -> Settings:
    """Build settings with every gateway field cleared, then the given overrides.

    Init kwargs outrank the dotenv file, so this ignores any ambient LiteLLM config.

    Args:
        **values: Field overrides.

    Returns:
        The settings.
    """
    fields: dict[str, Any] = {
        "blackbox_agent_gateway_url": None,
        "blackbox_agent_gateway_api_key": None,
        "litellm_proxy_url": None,
        "litellm_proxy_api_key": None,
    }
    fields.update(values)
    return Settings(**fields)


def test_gateway_from_settings_prefers_the_dedicated_gateway() -> None:
    """The dedicated agent-gateway settings win over the LiteLLM fallback and lose the trailing slash."""
    settings = _gateway_settings(
        blackbox_agent_gateway_url="https://dedicated/",
        blackbox_agent_gateway_api_key="dk",
        litellm_proxy_url="https://litellm",
        litellm_proxy_api_key="lk",
    )

    gateway = gateway_from_settings(settings)

    assert gateway == GatewayConfig(url="https://dedicated", api_key="dk")


def test_gateway_from_settings_falls_back_to_litellm() -> None:
    """When the dedicated gateway is unset, the LiteLLM proxy is used."""
    settings = _gateway_settings(litellm_proxy_url="https://litellm/", litellm_proxy_api_key="lk")

    assert gateway_from_settings(settings) == GatewayConfig(url="https://litellm", api_key="lk")


def test_gateway_from_settings_is_none_without_a_url_or_key() -> None:
    """A missing URL or key yields no gateway."""
    assert gateway_from_settings(_gateway_settings()) is None
    assert gateway_from_settings(_gateway_settings(blackbox_agent_gateway_url="https://x")) is None
    assert gateway_from_settings(_gateway_settings(blackbox_agent_gateway_api_key="k")) is None


def test_agent_target_unavailable_reason_orders_sandbox_before_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing sandbox is reported first; then a missing gateway; then nothing."""
    monkeypatch.setattr(agent_eval_mod, "sandbox_unavailable_reason", lambda settings: "no sandbox")
    assert agent_target_unavailable_reason(_gateway_settings()) == "no sandbox"

    monkeypatch.setattr(agent_eval_mod, "sandbox_unavailable_reason", lambda settings: None)
    assert agent_target_unavailable_reason(_gateway_settings()) == _GATEWAY_MISSING

    configured = _gateway_settings(blackbox_agent_gateway_url="https://x", blackbox_agent_gateway_api_key="k")
    assert agent_target_unavailable_reason(configured) is None

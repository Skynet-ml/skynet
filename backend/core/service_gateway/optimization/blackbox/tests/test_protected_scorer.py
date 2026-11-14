"""Verify protected scorer routing, seedless readiness, and irreversible usage signals."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.model_gateway import ROUTE_KEY
from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached
from core.config import Settings
from core.exceptions import ServiceError
from core.models.common import ModelConfig

from .. import runner
from .. import service as service_module
from ..sandbox import (
    CommandResult,
    current_sandbox_runtime,
    sandbox_runtime_context,
    sandbox_runtime_from_settings,
    sandbox_unavailable_reason,
    scorer_runtime_from_settings,
)
from ..sandbox_scorer import SandboxPythonScorer, ScorerGateway, scorer_gateway
from .mocks import FakeSandboxRuntime, FakeSandboxSession
from .test_agent_eval import _scorer_of, _target
from .test_sandbox_scorer import _InstallSession, _RunnerSession
from .test_service import _payload


def test_protected_scorer_route_precedes_provider_credentials() -> None:
    """Use only the trusted parent's opaque route even when stale provider fields exist."""
    config = ModelConfig(
        name="openrouter/provider/model",
        token_source="byok",
        base_url="https://provider.invalid/v1",
        extra={
            "api_key": "must-not-leave-parent",
            ROUTE_KEY: {"url": "http://127.0.0.1:12345/v1", "model": "bound-model", "token": "opaque-role"},
        },
    )
    gateway = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=30))
    assert (gateway.url, gateway.model, gateway.api_key) == ("http://127.0.0.1:12345/v1", "bound-model", "opaque-role")
    assert gateway.protected is True
    assert gateway.runner_payload()["protected"] is True
    assert "must-not-leave-parent" not in json.dumps(gateway.runner_payload())


def test_readiness_loads_real_scorer_without_a_candidate() -> None:
    """Verify scorer loading and dependencies without invoking the scorer's body."""
    runtime = FakeSandboxRuntime(lambda: _RunnerSession(runner.run_call))
    runtime.protected = True
    scorer = SandboxPythonScorer(
        "import math\ndef metric(candidate, case):\n    raise AssertionError('must not score readiness')\n",
        runtime=runtime,
        gateway=None,
        timeout_seconds=10,
    )
    try:
        assert scorer.check_ready() == {
            "ready": True,
            "entrypoint": "metric",
            "accepts_case": True,
            "model_configured": False,
        }
    finally:
        scorer.close()
    [session] = runtime.sessions
    assert "candidate" not in session.payloads[0]
    assert "case" not in session.payloads[0]
    assert session.closed is True
    assert runtime.specs[0].network_disabled is True
    assert runtime.specs[0].operation_key.startswith("scorer:")


def test_protected_runtime_refuses_unregistered_model_credentials() -> None:
    """Reject stale provider configuration before any key can reach a protected guest."""
    runtime = FakeSandboxRuntime()
    runtime.protected = True
    gateway = ScorerGateway("https://provider.invalid/v1", "model", "private-provider-key", "model")
    with pytest.raises(ServiceError, match="opaque model route"):
        SandboxPythonScorer("def score(candidate): return 1", runtime=runtime, gateway=gateway, timeout_seconds=10)
    with sandbox_runtime_context(runtime), pytest.raises(ServiceError, match="opaque model route"):
        service_module._agent_scorer(
            lambda candidate, case: (1, {}), _target(), job_id="job", progress_callback=None, agent_run_sink=None
        )
    assert runtime.specs == []


def test_readiness_reports_real_import_failures() -> None:
    """Fail setup when the runtime cannot import a scorer dependency."""
    runtime = FakeSandboxRuntime(lambda: _RunnerSession(runner.run_call))
    scorer = SandboxPythonScorer(
        "import missing_scorer_dependency_789\ndef score(candidate): return 1\n",
        runtime=runtime,
        gateway=None,
        timeout_seconds=10,
    )
    try:
        with pytest.raises(ServiceError, match="missing_scorer_dependency_789"):
            scorer.check_ready()
    finally:
        scorer.close()


@pytest.mark.parametrize("method", ["run", "check_ready"])
@pytest.mark.parametrize(
    ("code", "exception"), [("budget_reached", BudgetReached), ("usage_pending", UsagePendingError)]
)
def test_protected_controls_escape_without_scorer_zero_or_reopen(
    method: str, code: str, exception: type[BaseException]
) -> None:
    """Propagate admission and usage controls without inventing a score or a second sandbox.

    Args:
        method: Candidate scoring or seedless readiness path.
        code: Parent control signal.
        exception: Expected boundary exception.
    """
    runtime = FakeSandboxRuntime(lambda: _RunnerSession(lambda payload: {"control": {"code": code, "message": "halt"}}))
    runtime.protected = True
    scorer = SandboxPythonScorer("def score(candidate): return 1", runtime=runtime, gateway=None, timeout_seconds=10)
    try:
        with pytest.raises(exception, match="halt"):
            getattr(scorer, method)(*(["seed"] if method == "run" else []))
    finally:
        scorer.close()
    assert len(runtime.sessions) == 1


def test_protected_scorer_keeps_close_pending_and_never_retries_uncertain_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep both command transport failures and close reconciliation failures observable.

    Args:
        monkeypatch: Pytest patch fixture.
    """

    def uncertain(payload: dict[str, Any]) -> dict[str, Any]:
        """Simulate a command transport losing its reply after execution.

        Args:
            payload: Unused runner request.

        Raises:
            RuntimeError: Always, after the sandbox has already opened.
        """
        raise RuntimeError("lost command reply")

    def pending() -> None:
        """Simulate unresolved stopped-sandbox metrics.

        Raises:
            UsagePendingError: Always, after attempting cleanup.
        """
        raise UsagePendingError("missing final receipt")

    runtime = FakeSandboxRuntime(lambda: _RunnerSession(uncertain))
    runtime.protected = True
    scorer = SandboxPythonScorer("def score(candidate): return 1", runtime=runtime, gateway=None, timeout_seconds=10)
    with pytest.raises(RuntimeError, match="lost command reply"):
        scorer.run("seed")
    assert len(runtime.sessions) == 1
    monkeypatch.setattr(runtime.sessions[0], "close", pending)
    with pytest.raises(UsagePendingError, match="missing final receipt"):
        scorer.close()
    scorer.close()


def test_protected_install_command_runs_inside_the_managed_boundary() -> None:
    """Run offline dependency preparation inside the same charged sandbox as scoring."""
    install = "pip install --no-index ./wheels/scorer_dependency.whl"
    runtime = FakeSandboxRuntime(lambda: _InstallSession(runner.run_call, CommandResult(exit_code=0)))
    runtime.protected = True
    scorer = SandboxPythonScorer(
        "def score(candidate): return 1",
        runtime=runtime,
        gateway=None,
        timeout_seconds=10,
        install_command=install,
    )
    try:
        assert scorer.check_ready()["ready"] is True
    finally:
        scorer.close()
    [session] = runtime.sessions
    assert session.commands[0] == install
    assert runtime.specs[0].network_disabled is True


@pytest.mark.parametrize("code", ["budget_reached", "usage_pending"])
def test_runner_uses_guest_relay_and_preserves_caught_control(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    """Prevent user exception handling from hiding a stop or retrying uncertain usage.

    Args:
        monkeypatch: Pytest patch fixture.
        code: Parent budget or usage control.
    """
    requests: list[urllib.request.Request] = []

    def refuse(request: urllib.request.Request, **kwargs: Any) -> None:
        """Return an authentic parent control envelope through urllib's HTTP error path.

        Args:
            request: Captured outgoing request.
            **kwargs: Unused urllib timeout and TLS options.

        Raises:
            urllib.error.HTTPError: Always, carrying the stable parent signal.
        """
        requests.append(request)
        body = json.dumps({"error": {"code": code, "message": "stop now"}}).encode()
        raise urllib.error.HTTPError(request.full_url, 402, "budget", {}, io.BytesIO(body))

    monkeypatch.setenv(runner.ENV_BUDGET_RELAY_URL, "http://127.0.0.1:45678/v1")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    result = runner.run_call(
        {
            "code": "def score(candidate):\n    for attempt in range(2):\n        try:\n            llm(candidate)\n        except BaseException:\n            pass\n    return 999\n",
            "candidate": "seed",
            "gateway": {"url": "http://parent.invalid/v1", "model": "bound", "api_key": "opaque"},
        }
    )
    assert result["control"] == {"code": code, "message": "stop now"}
    assert result["score"] is None
    assert len(requests) == 1
    assert requests[0].full_url == "http://127.0.0.1:45678/v1/chat/completions"
    assert requests[0].get_header("Authorization") == "Bearer opaque"


def test_runtime_context_overrides_settings_and_restores_nested_bindings() -> None:
    """Bind request-owned runtimes without leaking them into later requests."""
    settings = Settings(_env_file=None)
    first, second = FakeSandboxRuntime(), FakeSandboxRuntime()
    assert current_sandbox_runtime() is None
    with sandbox_runtime_context(first):
        assert sandbox_runtime_from_settings(settings) is first
        assert scorer_runtime_from_settings(settings) is first
        assert sandbox_unavailable_reason(settings) is None
        with sandbox_runtime_context(second):
            assert current_sandbox_runtime() is second
        assert current_sandbox_runtime() is first
    assert current_sandbox_runtime() is None


@pytest.mark.parametrize("primary_error", [None, BudgetReached("already stopped")])
def test_job_finalization_preserves_outcome_while_usage_is_pending(
    monkeypatch: pytest.MonkeyPatch, primary_error: BaseException | None
) -> None:
    """Keep evaluated results and original stop signals when only final billing is pending.

    Args:
        monkeypatch: Pytest patch fixture.
        primary_error: Optional original optimizer stop to preserve.
    """
    outcome = SimpleNamespace(details={})

    def close() -> None:
        """Report a retained provider hold after sandbox cleanup.

        Raises:
            UsagePendingError: Always, with usage retained in the parent's ledger.
        """
        raise UsagePendingError("final billing pending")

    def run(*args: Any, **kwargs: Any) -> Any:
        """Return the real outcome or original optimizer stop before cleanup.

        Args:
            *args: Unused service arguments.
            **kwargs: Unused service options.

        Returns:
            Test outcome marker.

        Raises:
            BaseException: The parametrized original stop, when present.
        """
        if primary_error is not None:
            raise primary_error
        return outcome

    monkeypatch.setattr(service_module, "build_scorer", lambda *args, **kwargs: SimpleNamespace(close=close))
    monkeypatch.setattr(service_module, "_run_job", run)
    if primary_error is None:
        assert service_module.run_blackbox_optimization(_payload(), artifact_id="completed") is outcome
        assert service_module.CASE_SPLIT_KEY in outcome.details
    else:
        with pytest.raises(type(primary_error)) as caught:
            service_module.run_blackbox_optimization(_payload(), artifact_id="stopped")
        assert caught.value is primary_error


def test_completed_agent_record_survives_pending_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve the actual agent answer separately from its pending runtime receipt.

    Args:
        monkeypatch: Pytest patch fixture.
    """
    session = FakeSandboxSession(produces={"run-agent": {"output/answer.txt": "actual answer"}})
    runtime = FakeSandboxRuntime(lambda: session)
    runtime.protected = True

    def close() -> None:
        """Report pending runtime usage after cleanup.

        Raises:
            UsagePendingError: Always, without invalidating the completed answer.
        """
        raise UsagePendingError("runtime pending")

    monkeypatch.setattr(session, "close", close)
    result = _scorer_of(runtime, _target()).run("instructions", {"input": "question"})
    assert result["output"] == "actual answer"
    assert result["runtime_usage_pending"] is True
    assert len(runtime.sessions) == 1


def test_protected_agent_transport_failure_does_not_reopen() -> None:
    """Retain uncertain execution without a hidden new sandbox attempt."""

    def fail(command: str) -> Any:
        """Simulate losing the reply after agent execution may have started.

        Args:
            command: Unused agent command.

        Raises:
            UsagePendingError: Always, to prevent hidden replay.
        """
        raise UsagePendingError("lost reply")

    runtime = FakeSandboxRuntime(lambda: FakeSandboxSession(script=fail))
    runtime.protected = True
    with pytest.raises(UsagePendingError, match="lost reply"):
        _scorer_of(runtime, _target()).run("instructions", {"input": "question"})
    assert len(runtime.sessions) == 1

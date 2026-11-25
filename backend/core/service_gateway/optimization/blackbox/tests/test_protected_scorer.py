"""Verify protected scorer routing, runner stop signals, runtime binding and pending usage at job end."""

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
from core.models.common import ModelConfig

from .. import runner
from .. import service as service_module
from ..sandbox import (
    current_sandbox_runtime,
    sandbox_runtime_context,
    sandbox_runtime_from_settings,
    sandbox_unavailable_reason,
)
from ..sandbox_scorer import scorer_gateway
from .mocks import FakeSandboxRuntime
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

    monkeypatch.setattr(service_module, "RemoteScorer", lambda *args, **kwargs: SimpleNamespace(close=close))
    monkeypatch.setattr(service_module, "validate_blackbox_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr(service_module, "_run_job", run)
    payload = _payload(target={"kind": "repo", "repo": {"repository": "o/r", "editable_paths": ["src"]}})
    route = {"evaluator_route": {"token": "route"}, "repo_snapshot": {"chunks": ["chunk"]}}
    if primary_error is None:
        assert service_module.run_blackbox_optimization(payload, artifact_id="completed", **route) is outcome
        assert service_module.CASE_SPLIT_KEY in outcome.details
    else:
        with pytest.raises(type(primary_error)) as caught:
            service_module.run_blackbox_optimization(payload, artifact_id="stopped", **route)
        assert caught.value is primary_error

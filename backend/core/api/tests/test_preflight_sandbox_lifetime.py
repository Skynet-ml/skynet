"""Bind the setup check's sandbox for the time its own checks need, not the run's ceiling."""

from types import SimpleNamespace
from typing import Any

import pytest

from core.api import preflight_execution
from core.api.preflight_execution import WizardPreflightRequest, _perform_preflight
from core.api.routers.execution_budgets import CHUNK_CENTS

SCORER = {"kind": "python", "metric_code": "def metric(candidate, case):\n    return 1.0\n"}
PASSED = {"checks": [{"key": "scorer.readiness", "status": "succeeded"}]}


class _Gateway:
    """Stand in for the model gateway without any paid transport."""

    def __init__(self, runtime: Any) -> None:
        """Keep the runtime the stub gateway wraps."""
        self.runtime = runtime

    def protect_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Hand the payload back unchanged."""
        return payload

    def close(self) -> None:
        """Nothing to settle."""


def _run(
    monkeypatch: pytest.MonkeyPatch, request: WizardPreflightRequest, *, setup_in_total: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Perform one attempt with every transport replaced.

    Args:
        monkeypatch: The pytest fixture that replaces the transports.
        request: The preflight request to perform.
        setup_in_total: Whether the stub budget counts setup in its limit.

    Returns:
        The arguments the preflight bound its sandbox with, and those it built its budget runtime with.
    """
    bound: dict[str, Any] = {}
    runtime_kwargs: dict[str, Any] = {}

    def runtime(*args: Any, **kwargs: Any) -> object:
        """Record how the preflight meters its own calls."""
        runtime_kwargs.update(kwargs)
        return object()

    def bind(gateway: Any, settings: Any, **kwargs: Any) -> dict[str, Any]:
        """Record what the preflight asks the broker for."""
        bound.update(kwargs)
        return {"image": "image", "lifetime_seconds": kwargs["lifetime_seconds"]}

    monkeypatch.setattr(preflight_execution, "ModelGateway", _Gateway)
    monkeypatch.setattr(preflight_execution, "BudgetRuntime", runtime)
    monkeypatch.setattr(preflight_execution, "bind_protected_sandbox", bind)
    monkeypatch.setattr(preflight_execution, "payload_uses_token_source", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        preflight_execution, "_verify_anything", lambda gateway, payload, *, scope, identity, unchecked: dict(PASSED)
    )
    status, _ = _perform_preflight(
        request,
        SimpleNamespace(username="alice"),
        object(),
        SimpleNamespace(id="budget", generation=1, setup_in_total=setup_in_total),
        {"id": "setup"},
        attempt=1,
        engine=object(),
    )
    assert status == "succeeded"
    return bound, runtime_kwargs


def test_anything_check_binds_a_request_sized_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound lifetime is the check's own bound, rounded up to whole seconds."""
    request = WizardPreflightRequest(
        scope="evaluation",
        workflow="anything",
        payload={"scorer": {**SCORER, "timeout_seconds": 120.25}},
        execution_budget_id="draft",
        execution_budget_revision=1,
    )

    bound, _ = _run(monkeypatch, request)

    assert bound["lifetime_seconds"] == 961
    assert bound["owner_id"] == "setup"


@pytest.mark.parametrize(("setup_in_total", "kept"), [(True, CHUNK_CENTS), (False, 0)])
def test_only_a_setup_in_total_check_keeps_one_lease(
    monkeypatch: pytest.MonkeyPatch, setup_in_total: bool, kept: int
) -> None:
    """Keep the run's first lease only when setup counts in the limit; else the wallet alone bounds setup (SKY-925)."""
    request = WizardPreflightRequest(
        scope="evaluation",
        workflow="anything",
        payload={"scorer": SCORER},
        execution_budget_id="draft",
        execution_budget_revision=1,
    )

    _, runtime_kwargs = _run(monkeypatch, request, setup_in_total=setup_in_total)

    assert runtime_kwargs["phase"] == "setup"
    assert runtime_kwargs["keep_cents"] == kept

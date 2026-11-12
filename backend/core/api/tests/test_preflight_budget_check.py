"""A setup runtime stopped by the budget fails the budget check, not the generic setup check."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.api import preflight_execution
from core.billing.budgets import AccountFundsError, BudgetInsufficientError
from core.worker.failure_events import failure_event

SPENT = "The next operation needs up to $1.35, but only $0.53 of the $0.53 limit remain."


@pytest.mark.parametrize(
    ("error", "key", "field"),
    [
        (BudgetInsufficientError(SPENT), "budget", None),
        (AccountFundsError("The account cannot fund the next operation."), "budget", "balance"),
        (RuntimeError(SPENT), "setup", None),
    ],
)
def test_preflight_reports_a_reservation_over_the_limit_as_a_budget_check(
    monkeypatch: pytest.MonkeyPatch, error: Exception, key: str, field: str | None
) -> None:
    """A runtime stopped by the budget fails the ``budget`` check; any other failure stays ``setup``.

    The sandbox supervisor reports every failure as an error event, so this
    drives the real ``_verify_anything`` through that event rather than
    raising past it. A stop the account balance caused names the balance, so
    the wizard does not offer to raise a limit that was never the problem.
    """

    class _Gateway:
        """Stand in for the model gateway without any paid transport."""

        budget_refusal = None

        def __init__(self, runtime: Any) -> None:
            """Ignore the budget runtime."""

        def protect_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            """Hand the payload back unchanged."""
            return payload

        def close(self) -> None:
            """Nothing to settle."""

    def dispatch(payload: Any, artifact_id: str, events: Any, start_method: str) -> None:
        """Report the failure the way ``run_vercel_dspy`` does: as an error event."""
        events.put(failure_event(error))

    monkeypatch.setattr(preflight_execution, "ModelGateway", _Gateway)
    monkeypatch.setattr(preflight_execution, "bind_protected_sandbox", lambda *args, **kwargs: {})
    monkeypatch.setattr(preflight_execution, "payload_uses_token_source", lambda *args, **kwargs: False)
    monkeypatch.setattr(preflight_execution, "run_vercel_dspy", dispatch)
    request = preflight_execution.WizardPreflightRequest(
        scope="evaluation",
        workflow="anything",
        payload={"scorer": {"kind": "python", "metric_code": "def metric(candidate, case):\n    return 1.0\n"}},
        execution_budget_id="draft",
        execution_budget_revision=1,
    )

    status, result = preflight_execution._perform_preflight(
        request,
        SimpleNamespace(username="alice"),
        object(),
        SimpleNamespace(id="budget", generation=1),
        {"id": "setup"},
        attempt=1,
        engine=object(),
    )

    assert status == "failed"
    expected = {"key": key, "status": "failed", "message": str(error)}
    assert result["checks"] == [{**expected, "field": field} if field else expected]

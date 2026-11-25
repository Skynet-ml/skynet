"""A setup runtime stopped by the budget fails the budget check, not the generic setup check."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from core.api import preflight_execution
from core.api.errors import DomainError
from core.api.routers import wizard_preflight
from core.billing.budgets import CHUNK_CENTS, AccountFundsError, BudgetInsufficientError
from core.worker.failure_events import failure_event

SPENT = "The next operation needs up to $1.35, but only $0.53 of the $0.53 limit remain."


@pytest.mark.parametrize(
    ("error", "setup_in_total", "key", "field"),
    [
        (BudgetInsufficientError(SPENT), False, "budget", "balance"),
        (AccountFundsError("The account cannot fund the next operation."), False, "budget", "balance"),
        (RuntimeError(SPENT), False, "setup", None),
        (BudgetInsufficientError(SPENT), True, "budget", None),
        (AccountFundsError("The account cannot fund the next operation."), True, "budget", "balance"),
    ],
)
def test_preflight_reports_a_reservation_over_the_limit_as_a_budget_check(
    monkeypatch: pytest.MonkeyPatch, error: Exception, setup_in_total: bool, key: str, field: str | None
) -> None:
    """A runtime stopped by the budget fails the ``budget`` check; any other failure stays ``setup``.

    The sandbox supervisor reports every failure as an error event, so this
    drives the real ``_verify_anything`` through that event rather than
    raising past it. Setup bills the balance only (SKY-925), so every budget
    stop names the balance, even a plain one the guest relays. The wizard then
    does not offer to raise a limit that was never the problem. A
    continuous-plan limit still bounds setup, so its plain stop names the limit.
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
        SimpleNamespace(id="budget", generation=1, setup_in_total=setup_in_total),
        {"id": "setup"},
        attempt=1,
        engine=object(),
    )

    assert status == "failed"
    expected = {"key": key, "status": "failed", "message": str(error)}
    assert result["checks"] == [{**expected, "field": field} if field else expected]


@pytest.mark.parametrize(("setup_in_total", "refused"), [(False, False), (True, True)])
def test_the_wizard_check_counts_setup_spend_in_a_setup_in_total_limit_as_submit_does(
    monkeypatch: pytest.MonkeyPatch, setup_in_total: bool, refused: bool
) -> None:
    """Refuse a one-lease limit that setup spend reduced only when the limit counts setup (SKY-925).

    Args:
        monkeypatch: Replaces the budget service and the balance gate.
        setup_in_total: Whether the budget counts setup in its limit.
        refused: Whether the check must refuse the budget.
    """
    spent = Decimal(1)
    budget = SimpleNamespace(
        uncapped=False,
        total_cents=CHUNK_CENTS,
        limit_spent_cents=spent if setup_in_total else Decimal(0),
    )
    monkeypatch.setattr(wizard_preflight, "BudgetService", lambda **kwargs: SimpleNamespace(get=lambda *a: budget))
    monkeypatch.setattr(wizard_preflight.settings, "local_mode", True)
    request = SimpleNamespace(execution_budget_id="budget")
    user = SimpleNamespace(username="alice")

    if refused:
        with pytest.raises(DomainError) as caught:
            wizard_preflight._require_budget_lease(request, user, SimpleNamespace(engine=None))
        assert caught.value.status_code == 409
    else:
        wizard_preflight._require_budget_lease(request, user, SimpleNamespace(engine=None))

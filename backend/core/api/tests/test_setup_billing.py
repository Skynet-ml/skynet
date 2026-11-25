"""Tests for the caller-scoped setup billing preference and the budgets it shapes."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...billing.budgets import BudgetService
from ...storage.models import Base, BillingCustomerModel, SetupBillingPreferenceModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers.execution_budgets import create_execution_budgets_router
from ..routers.setup_billing import create_setup_billing_router

_USER = "oauth-user@example.com"


def _harness() -> tuple[TestClient, object]:
    """Build an authenticated client over an in-memory database with a funded wallet.

    Returns:
        Client and shared SQLAlchemy engine.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username=_USER, stripe_customer_id="local-user", balance_cents=5_000, grant_remaining=0
            )
        )
        session.commit()
    job_store = SimpleNamespace(engine=engine)
    app = FastAPI()
    app.include_router(create_setup_billing_router(job_store=job_store))
    app.include_router(create_execution_budgets_router(job_store=job_store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    return TestClient(app), engine


def _new_budget_counts_setup(client: TestClient, engine: object, key: str) -> bool:
    """Create a wizard budget and read whether it counts setup against its limit.

    Args:
        client: Authenticated client.
        engine: Shared SQLAlchemy engine.
        key: Idempotency key for the create.

    Returns:
        The new budget's ``setup_in_total`` flag.
    """
    response = client.post("/execution-budgets", json={"total_cents": 500}, headers={"Idempotency-Key": key})
    assert response.status_code in (200, 201), response.text
    return BudgetService(engine=engine).get(response.json()["id"], _USER).setup_in_total


def test_new_identity_bills_setup_from_the_wallet_without_creating_data() -> None:
    """Default to wallet billing, and to unflagged budgets, before the caller chooses."""
    client, engine = _harness()
    assert client.get("/account/setup-billing").json() == {"from_wallet": True}
    assert not _new_budget_counts_setup(client, engine, "default")
    with Session(engine) as session:
        assert session.get(SetupBillingPreferenceModel, _USER) is None


def test_turning_wallet_billing_off_flags_only_new_budgets() -> None:
    """The setting off flags the next budget; an earlier budget and its replay keep their flag."""
    client, engine = _harness()
    assert not _new_budget_counts_setup(client, engine, "before")
    assert client.put("/account/setup-billing", json={"from_wallet": False}).json() == {"from_wallet": False}
    assert client.get("/account/setup-billing").json() == {"from_wallet": False}

    assert _new_budget_counts_setup(client, engine, "after")
    assert not _new_budget_counts_setup(client, engine, "before")
    assert client.put("/account/setup-billing", json={"from_wallet": "sometimes"}).status_code == 422

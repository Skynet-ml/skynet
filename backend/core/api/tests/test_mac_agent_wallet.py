"""Tests that the Mac app's agent reads the linked hosted wallet, never the local credit."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...config import settings
from ...local import hosted_link
from ...storage.models import Base, BillingCustomerModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers.billing import create_billing_router

_USER = "local@skynet.local"


def _app(*, local: bool, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build an app with only the billing router, signed in as the local user.

    Args:
        local: Whether the app runs in local mode.
        monkeypatch: Pytest fixture used to set local mode.

    Returns:
        The app.
    """
    monkeypatch.setattr(settings, "local_mode", local)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(create_billing_router(job_store=SimpleNamespace(engine=engine)))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(_USER, "user", ())
    return app


def _agent_wallet(monkeypatch: pytest.MonkeyPatch, *, linked: bool, hosted: Any) -> dict[str, Any]:
    """Call the Mac agent's wallet tool with a faked link and hosted answer.

    Args:
        monkeypatch: Pytest fixture used to fake the hosted link.
        linked: Whether the Mac holds a link.
        hosted: The hosted balance body, or an exception the hosted call raises.

    Returns:
        The tool's JSON result.
    """
    app = _app(local=True, monkeypatch=monkeypatch)
    monkeypatch.setattr(hosted_link, "link_status", lambda engine, username: object() if linked else None)

    def hosted_get(engine: Any, username: str, path: str) -> dict[str, Any]:
        """Answer the hosted balance call, or raise the given error."""
        assert path == "/desktop/v1/balance"
        if isinstance(hosted, Exception):
            raise hosted
        return hosted

    monkeypatch.setattr(hosted_link, "hosted_get", hosted_get)
    response = TestClient(app).get("/billing/wallet/agent")
    assert response.status_code == 200
    assert str(settings.local_grant_cents) not in response.text
    return response.json()


def test_linked_mac_agent_reads_the_hosted_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A linked Mac returns the hosted spendable balance and its low flag."""
    body = _agent_wallet(monkeypatch, linked=True, hosted={"available_cents": 1234, "low_balance": True})
    assert body["linked"] is True
    assert body["hosted_balance_cents"] == 1234
    assert body["low_balance"] is True


@pytest.mark.parametrize(
    ("linked", "hosted"),
    [
        (False, {"available_cents": 1234, "low_balance": False}),
        (True, DomainError("hosted_link.not_linked", status=409)),
        (True, DomainError("hosted_link.failed", status=401)),
    ],
)
def test_unlinked_mac_agent_gets_no_number(monkeypatch: pytest.MonkeyPatch, linked: bool, hosted: Any) -> None:
    """No link, or a revoked one, returns a sign-in note and no amount."""
    body = _agent_wallet(monkeypatch, linked=linked, hosted=hosted)
    assert body["linked"] is False
    assert "sign in" in body["message"]
    assert not any(isinstance(value, int) and not isinstance(value, bool) for value in body.values())


def test_offline_hosted_gives_no_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted that does not answer says the balance is unavailable, with no amount."""
    body = _agent_wallet(monkeypatch, linked=True, hosted=DomainError("hosted_link.unreachable", status=503))
    assert body == {"linked": True, "balance_available": False, "message": body["message"]}


def test_mac_agent_tool_moves_off_the_local_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """In the Mac app the agent tool is the hosted read; the wizard keeps the local wallet."""
    paths = _app(local=True, monkeypatch=monkeypatch).openapi()["paths"]
    assert paths["/billing/wallet/agent"]["get"]["operationId"] == "get_wallet_for_agent"
    assert paths["/billing/wallet/agent"]["get"]["tags"] == ["agent"]
    assert "agent" not in paths["/billing/wallet"]["get"].get("tags", [])
    assert paths["/billing/wallet"]["get"]["operationId"] != "get_wallet_for_agent"


def test_hosted_wallet_tool_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The web keeps get_wallet_for_agent on /billing/wallet and has no Mac route."""
    paths = _app(local=False, monkeypatch=monkeypatch).openapi()["paths"]
    assert paths["/billing/wallet"]["get"]["operationId"] == "get_wallet_for_agent"
    assert paths["/billing/wallet"]["get"]["tags"] == ["agent"]
    assert "/billing/wallet/agent" not in paths


def test_wallet_shows_one_balance_of_grant_and_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wallet sends one balance, the grant plus the paid balance, and no separate grant."""
    monkeypatch.setattr(settings, "local_mode", False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username=_USER, stripe_customer_id="cus_x", balance_cents=700, grant_remaining=300)
        )
        session.commit()
    app = FastAPI()
    app.include_router(create_billing_router(job_store=SimpleNamespace(engine=engine)))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(_USER, "user", ())
    response = TestClient(app).get("/billing/wallet")
    assert response.status_code == 200
    body = response.json()
    assert body["balance_cents"] == 1000
    assert "free" not in response.text.lower()
    assert set(body) <= {"balance_cents", "usage", "pricing"}

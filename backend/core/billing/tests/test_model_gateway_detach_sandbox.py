"""Verify that a detached sandbox broker leaves the gateway, so closing the gateway does not stop its boxes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.model_gateway import ModelGateway
from core.billing.runtime import BudgetRuntime
from core.storage.models import Base, BillingCustomerModel

IMAGE = "fixture@sha256:" + "a" * 64


def _gateway(tmp_path: Path) -> ModelGateway:
    """Start a gateway against a private funded wallet.

    Args:
        tmp_path: Directory for the SQLite ledger.

    Returns:
        The started gateway.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'gateway.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=50, grant_remaining=0)
        )
        session.commit()
    ledger = BudgetService(engine=engine)
    budget = ledger.create("alice", 20, idempotency_key="fixture")
    return ModelGateway(BudgetRuntime(ledger, username="alice", budget_id=budget.id, generation=0, phase="run"))


def test_a_detached_broker_is_not_closed_with_the_gateway(tmp_path: Path) -> None:
    """Return the bound broker once and leave its boxes running when the gateway closes."""
    gateway = _gateway(tmp_path)
    broker = MagicMock()
    gateway.bind_sandbox(broker, image=IMAGE, lifetime_seconds=20)

    assert gateway.detach_sandbox() is broker
    assert gateway.detach_sandbox() is None
    gateway.close()

    broker.close.assert_not_called()


def test_a_bound_broker_still_closes_with_the_gateway(tmp_path: Path) -> None:
    """Keep the old close behavior when no caller detaches the broker."""
    gateway = _gateway(tmp_path)
    broker = MagicMock()
    gateway.bind_sandbox(broker, image=IMAGE, lifetime_seconds=20)

    gateway.close()

    broker.close.assert_called_once_with()

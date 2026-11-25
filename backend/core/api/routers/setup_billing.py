"""Authenticated setup billing preference endpoints. [INTERNAL]"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ...storage.models import SetupBillingPreferenceModel
from ..auth import AuthenticatedUser, get_authenticated_user

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


class SetupBillingPreference(BaseModel):
    from_wallet: bool = Field(
        description=(
            "True (the default) bills each new run's setup checks to your balance, outside the run's spending "
            "limit. False counts setup against the run's spending limit. Continuous plans always count setup "
            "in the cycle budget. Runs that already exist keep their setting."
        )
    )


def bills_setup_from_wallet(engine: Engine, username: str) -> bool:
    """Return whether ``username``'s new runs bill setup to the wallet only.

    Args:
        engine: ORM engine that stores the preference.
        username: Account whose setting is read.

    Returns:
        The stored choice, or ``True`` when the account never chose.
    """
    with Session(engine) as session:
        row = session.get(SetupBillingPreferenceModel, username)
    return True if row is None else bool(row.from_wallet)


def create_setup_billing_router(*, job_store) -> APIRouter:
    """Build the caller-scoped setup billing preference router.

    Args:
        job_store: Job-store instance whose ORM engine persists preferences.

    Returns:
        A router exposing get and put operations for the authenticated caller.
    """
    router = APIRouter()

    @router.get(
        "/account/setup-billing",
        response_model=SetupBillingPreference,
        summary="Read whether new runs bill setup checks to the balance only",
    )
    def get_setup_billing(user: AuthenticatedUserDep) -> SetupBillingPreference:
        """Return the caller's setting, ``from_wallet`` true when never set.

        Args:
            user: Authenticated caller whose setting is requested.

        Returns:
            The effective setup billing setting.
        """
        return SetupBillingPreference(from_wallet=bills_setup_from_wallet(job_store.engine, user.username))

    @router.put(
        "/account/setup-billing",
        response_model=SetupBillingPreference,
        summary="Set whether new runs bill setup checks to the balance only",
    )
    def put_setup_billing(body: SetupBillingPreference, user: AuthenticatedUserDep) -> SetupBillingPreference:
        """Persist the caller's setting; it applies to runs created afterwards.

        Args:
            body: The new setting.
            user: Authenticated caller whose setting is updated.

        Returns:
            The stored setting.
        """
        with Session(job_store.engine) as session:
            row = session.get(SetupBillingPreferenceModel, user.username)
            if row is None:
                row = SetupBillingPreferenceModel(username=user.username)
                session.add(row)
            row.from_wallet = body.from_wallet
            row.updated_at = datetime.now(UTC)
            session.commit()
        return SetupBillingPreference(from_wallet=body.from_wallet)

    return router

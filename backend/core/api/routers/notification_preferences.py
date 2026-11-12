"""Authenticated notification-routing preference endpoints. [INTERNAL]"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...notifications.catalog import CHANNELS, DELIVERY_CHANNELS, LOCAL_CHANNELS, MAX_HOLD_MINUTES, MIN_HOLD_MINUTES
from ...notifications.preferences import clean_held_channels, lock_preferences, merge_grid, settings_from_row
from ...storage.models import NotificationPreferenceModel
from ..auth import AuthenticatedUser, get_authenticated_user

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


EventTypeValue = Literal["budget_near", "failed", "finished", "new_best", "stuck", "shared", "product_news"]
ChannelValue = Literal["inbox", "email", "chrome", "slack"]
HeldChannelValue = Literal["email", "chrome", "slack"]


class NotificationPreferencesResponse(BaseModel):
    grid: dict[EventTypeValue, dict[ChannelValue, bool]] = Field(
        description="Which channels each event type is routed to (event type -> channel -> on)."
    )
    hold_minutes: int = Field(description="Minutes non-urgent events wait on the held channels.")
    held_channels: list[HeldChannelValue] = Field(
        description="Channels whose non-urgent sends wait the hold and are dropped if the inbox item was read."
    )
    stuck_fraction: float = Field(
        default=0.25,
        description="Notify once when the best score stalls for this fraction of the run's evaluation budget.",
    )
    budget_alert_fraction: float = Field(
        default=0.8,
        description="Notify once when spend reaches this fraction of the run's spending limit.",
    )


class NotificationPreferencesUpdate(BaseModel):
    grid: dict[EventTypeValue, dict[ChannelValue, bool]] | None = Field(
        default=None, description="Grid cells to change; cells left out keep their current value."
    )
    hold_minutes: int | None = Field(default=None, ge=MIN_HOLD_MINUTES, le=MAX_HOLD_MINUTES)
    held_channels: list[HeldChannelValue] | None = Field(default=None)
    stuck_fraction: float | None = Field(default=None, ge=0.05, le=1.0)
    budget_alert_fraction: float | None = Field(default=None, ge=0.1, le=1.0)


def _response(row: NotificationPreferenceModel | None) -> NotificationPreferencesResponse:
    """Render a stored row, or the defaults for an identity without one.

    Args:
        row: Stored preferences, or ``None``.

    Returns:
        The effective preferences.
    """
    settings = settings_from_row(row)
    # Local-only channels (macOS cells and hold) have their own route, so this contract is the hosted one.
    grid = {event: {ch: on for ch, on in cells.items() if ch in CHANNELS} for event, cells in settings.grid.items()}
    return NotificationPreferencesResponse(
        grid=grid,
        hold_minutes=settings.hold_minutes,
        held_channels=[ch for ch in settings.held_channels if ch in DELIVERY_CHANNELS],
        stuck_fraction=settings.stuck_fraction,
        budget_alert_fraction=settings.budget_alert_fraction,
    )


def create_notification_preferences_router(*, job_store) -> APIRouter:
    """Build the caller-scoped notification preference router.

    Args:
        job_store: Job-store instance whose ORM engine persists preferences.

    Returns:
        A router exposing get and patch operations for the authenticated caller.
    """
    router = APIRouter()

    @router.get(
        "/account/notification-preferences",
        response_model=NotificationPreferencesResponse,
        summary="Read the caller's notification routing",
    )
    def get_notification_preferences(
        user: AuthenticatedUserDep,
    ) -> NotificationPreferencesResponse:
        """Return the caller's stored routing or the defaults.

        Args:
            user: Authenticated caller whose preferences are requested.

        Returns:
            Effective notification preferences.
        """
        with Session(job_store.engine) as session:
            return _response(session.get(NotificationPreferenceModel, user.username))

    @router.patch(
        "/account/notification-preferences",
        response_model=NotificationPreferencesResponse,
        summary="Update the caller's notification routing",
    )
    def update_notification_preferences(
        body: NotificationPreferencesUpdate,
        user: AuthenticatedUserDep,
    ) -> NotificationPreferencesResponse:
        """Merge the supplied grid cells and hold settings into the caller's routing.

        Args:
            body: Partial set of preference changes.
            user: Authenticated caller whose preferences are updated.

        Returns:
            Effective preferences after the update.
        """
        with Session(job_store.engine) as session:
            row = lock_preferences(session, user.username)
            if body.grid is not None:
                grid = merge_grid(row.channel_grid)
                for event, cells in body.grid.items():
                    grid[event].update(cells)
                row.channel_grid = merge_grid(grid)
            if body.hold_minutes is not None:
                row.hold_minutes = body.hold_minutes
            if body.held_channels is not None:
                # This contract names only the hosted channels; keep the Mac app's own holds as they are.
                kept = [ch for ch in clean_held_channels(row.held_channels) if ch in LOCAL_CHANNELS]
                row.held_channels = list(clean_held_channels([*body.held_channels, *kept]))
            if body.stuck_fraction is not None:
                row.stuck_fraction = body.stuck_fraction
            if body.budget_alert_fraction is not None:
                row.budget_alert_fraction = body.budget_alert_fraction
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return _response(row)

    return router

"""Verify desktop account-data routing stays inside the existing token scope."""

import pytest

from core.api.desktop_auth import desktop_sync_route_allowed


@pytest.mark.parametrize(
    "path",
    [
        "/account/notification-preferences",
        "/notifications",
        "/notifications/unread-count",
        "/notifications/{notification_id}/read",
        "/notifications/read-all",
        "/notifications/{notification_id}",
        "/notifications/push/vapid-key",
        "/notifications/push/subscriptions",
        "/notifications/slack",
        "/notifications/slack/channels",
        "/billing/profile",
        "/billing/transactions",
        "/billing/usage",
        "/billing/wallet",
    ],
)
def test_account_data_routes_accept_desktop_sync_scope(path: str) -> None:
    """Allow account data without enabling financial or credential routes."""
    assert desktop_sync_route_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "/billing/checkout",
        "/billing/portal",
        "/billing/payment-methods/{method_id}",
        "/billing/subscription/checkout",
        "/notifications/slack/oauth/start",
    ],
)
def test_financial_and_oauth_routes_remain_closed(path: str) -> None:
    """Keep financial and credential actions on the hosted web session."""
    assert not desktop_sync_route_allowed(path)

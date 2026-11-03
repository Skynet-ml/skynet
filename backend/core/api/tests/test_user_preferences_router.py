"""Tests for the agent-facing browser-preference patch route."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers.user_preferences import create_user_preferences_router


def _client() -> TestClient:
    """Build a client over the preferences router with a signed-in caller.

    Returns:
        A test client whose requests run as an ordinary user.
    """
    app = FastAPI()
    app.include_router(create_user_preferences_router())
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username="prefs@example.com", role="user", groups=()
    )
    return TestClient(app)


def test_supported_preferences_come_back_as_the_patch() -> None:
    """The route echoes only the fields the caller set."""
    response = _client().post(
        "/settings/user-preferences",
        json={"wizard_code_assist": "manual", "tagger_assist": False},
    )
    assert response.status_code == 200
    assert response.json() == {
        "updates": {"wizard_code_assist": "manual", "tagger_assist": False},
        "changed": ["wizard_code_assist", "tagger_assist"],
    }


def test_removed_dictation_preference_is_ignored() -> None:
    """An old agent call with ``dictation_enabled`` still succeeds and drops the key (SKY-300 follow-up)."""
    response = _client().post(
        "/settings/user-preferences",
        json={"dictation_enabled": False, "tagger_assist": True},
    )
    assert response.status_code == 200
    assert response.json() == {"updates": {"tagger_assist": True}, "changed": ["tagger_assist"]}

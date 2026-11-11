"""Tests for the sandbox runtime that the settings build (SKY-529, SKY-433)."""

# mypy: disable-error-code="call-arg"

from __future__ import annotations

import pytest

from core.config import Settings
from core.sandbox import factory as sandbox_factory
from core.sandbox.factory import ProviderEntry
from core.service_gateway.optimization.blackbox import sandbox as sandbox_mod
from core.service_gateway.optimization.blackbox.sandbox import (
    AsyncBridgeRuntime,
    sandbox_runtime_from_settings,
)

_ENV_VARS = (
    "APP_LOCAL_MODE",
    "SKYNET_LOCAL_MODE",
    "VERCEL_TOKEN",
    "VERCEL_TEAM_ID",
    "VERCEL_PROJECT_ID",
)
_VERCEL_CREDS = {"vercel_token": "t", "vercel_team_id": "team", "vercel_project_id": "proj"}
_needs_sdk = pytest.mark.skipif(
    sandbox_mod.vercel_sandbox is None,
    reason="the Vercel Sandbox SDK is not installed",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient values that would change what ``Settings()`` reads.

    Args:
        monkeypatch: Pytest fixture that restores the environment afterwards.
    """
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_a_stale_backend_variable_does_not_stop_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a deployment that still sets the removed backend switch loads its settings.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    monkeypatch.setenv("SANDBOX_BACKEND", "legacy")

    Settings(_env_file=None)


@_needs_sdk
def test_the_default_settings_build_the_async_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a hosted deployment opens Vercel through the async bridge.

    A fake provider stands in for Vercel, so the test makes no network call.

    Args:
        monkeypatch: Pytest fixture used to swap the Vercel provider for a fake.
    """
    monkeypatch.setitem(
        sandbox_factory.PROVIDERS,
        "vercel",
        ProviderEntry("core.sandbox.tests.fake_provider", "InMemorySandbox", None, True),
    )

    runtime = sandbox_runtime_from_settings(Settings(_env_file=None, **_VERCEL_CREDS))

    assert isinstance(runtime, AsyncBridgeRuntime)
    assert runtime.provider == "vercel"

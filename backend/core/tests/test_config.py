"""Tests for the ``sandbox_backend`` default (SKY-529)."""

# mypy: disable-error-code="call-arg"

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import Settings
from core.sandbox import factory as sandbox_factory
from core.sandbox.factory import ProviderEntry
from core.service_gateway.optimization.blackbox import sandbox as sandbox_mod
from core.service_gateway.optimization.blackbox.sandbox import (
    AsyncBridgeRuntime,
    VercelSandboxRuntime,
    sandbox_runtime_from_settings,
)

_ENV_VARS = (
    "SANDBOX_BACKEND",
    "APP_LOCAL_MODE",
    "SKYNET_LOCAL_MODE",
    "VERCEL_TOKEN",
    "VERCEL_TEAM_ID",
    "VERCEL_PROJECT_ID",
)
_VERCEL_CREDS = {"vercel_token": "t", "vercel_team_id": "team", "vercel_project_id": "proj"}
_needs_sdk = pytest.mark.skipif(
    sandbox_mod.vercel_sync is None or sandbox_mod.vercel_api is None,
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


def test_the_default_sandbox_backend_is_async() -> None:
    """Check that the field default and an empty environment both give ``async``."""
    assert Settings.model_fields["sandbox_backend"].default == "async"
    assert Settings(_env_file=None).sandbox_backend == "async"


def test_the_legacy_backend_stays_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that ``SANDBOX_BACKEND=legacy`` still selects the sync runtimes.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    monkeypatch.setenv("SANDBOX_BACKEND", "legacy")

    assert Settings(_env_file=None).sandbox_backend == "legacy"


def test_an_unknown_backend_still_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a value other than ``legacy`` or ``async`` stops the settings from loading.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    monkeypatch.setenv("SANDBOX_BACKEND", "turbo")

    with pytest.raises(ValidationError, match=r"sandbox_backend|SANDBOX_BACKEND"):
        Settings(_env_file=None)


@_needs_sdk
def test_the_default_settings_build_the_async_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a hosted deployment with no backend set opens Vercel through the async bridge.

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


@_needs_sdk
def test_the_explicit_legacy_backend_builds_the_sync_runtime() -> None:
    """Check that ``sandbox_backend = legacy`` still builds the sync Vercel runtime."""
    runtime = sandbox_runtime_from_settings(Settings(_env_file=None, sandbox_backend="legacy", **_VERCEL_CREDS))

    assert type(runtime) is VercelSandboxRuntime

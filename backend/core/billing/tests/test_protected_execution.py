"""Tests for protected execution on the async sandbox bridge (hosted Vercel). Fakes only: no network, no money."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from core.billing import protected_execution
from core.billing.protected_execution import bind_protected_sandbox, protected_vercel_unavailable_reason
from core.config import Settings
from core.config import settings as global_settings
from core.sandbox.bridge import ProtectedBridgeRuntime
from core.sandbox.providers.vercel import VercelEnvironment
from core.sandbox.snapshots import SnapshotCache
from core.sandbox.tests.test_provider_vercel import FakeClient, FakeClock, FakeUsage, readback
from core.service_gateway.optimization.blackbox import sandbox_runtime
from core.service_gateway.optimization.blackbox.tests.test_sandbox_broker import FakeRuntime

_DIGEST = "registry.example/skynet@sha256:" + "a" * 64
_ENV = ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID", "APP_LOCAL_MODE", "SKYNET_LOCAL_MODE")


class _Gateway:
    """Record what ``bind_protected_sandbox`` hands the model gateway."""

    def __init__(self) -> None:
        """Start with a setup-phase budget and no bound broker."""
        # Setup work builds no run-call recorder, so the bind reads only this phase.
        self.runtime = SimpleNamespace(budget_id="budget-1", generation=3, phase="setup")
        self.broker: Any = None
        self.bound: dict[str, Any] = {}

    def dispatch_guest(self, *_args: Any) -> None:
        """Accept guest model calls; none happen in these tests."""

    def dispatch_route(self, *_args: Any) -> None:
        """Accept parent box model calls; none happen in these tests."""

    def bind_sandbox(self, broker: Any, **values: Any) -> None:
        """Keep the broker and its profile.

        Args:
            broker: The parent-owned sandbox broker.
            **values: Image and lifetime.
        """
        self.broker = broker
        self.bound = values

    def fund_parent_sandbox(self, hosts: tuple[str, ...]) -> None:
        """Accept the parent box's hosts.

        Args:
            hosts: Package registries.
        """


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Build hosted settings with fake Vercel credentials and a pinned digest image.

    Args:
        monkeypatch: Pytest fixture.

    Returns:
        A fresh ``Settings`` instance.
    """
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return Settings(
        _env_file=None,
        vercel_token=SecretStr("token"),
        vercel_team_id="team",
        vercel_project_id="project",
        vercel_sandbox_image=_DIGEST,
        dspy_sandbox_image=_DIGEST,
    )


@pytest.fixture
def provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the async provider preflight fake credentials; it reads the global settings.

    Args:
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setattr(global_settings, "vercel_token", SecretStr("token"))
    monkeypatch.setattr(global_settings, "vercel_team_id", "team")
    monkeypatch.setattr(global_settings, "vercel_project_id", "project")


def _fake_vercel(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[FakeUsage]]:
    """Route the async Vercel provider to a fake SDK client and fake reservations.

    Args:
        monkeypatch: Pytest fixture.

    Returns:
        The shared call log and every reservation made.
    """
    log: list[str] = []
    client = FakeClient(log, readback("deny-all"))
    usages: list[FakeUsage] = []

    def usage(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make one fake reservation and keep it.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Returns:
            The fake reservation.
        """
        made = FakeUsage(log, budget, request, operation_key=operation_key)
        made.budget = budget  # type: ignore[attr-defined]
        usages.append(made)
        return made

    def client_factory(capture: Any) -> FakeClient:
        """Return the fake client.

        Args:
            capture: The response capture hook.

        Returns:
            The fake client.
        """
        client.capture = capture
        return client

    def create(name: str, spec: Any, **options: Any) -> VercelEnvironment:
        """Build the real async provider over the fakes.

        Args:
            name: The provider name.
            spec: The async spec.
            **options: The constructor options from the bridge runtime.

        Returns:
            The provider.
        """
        assert name == "vercel"
        return VercelEnvironment(
            spec, budget=options["budget"], client_factory=client_factory, usage_factory=usage, clock=FakeClock()
        )

    monkeypatch.setattr(sandbox_runtime, "create_environment", create)
    return log, usages


def test_protected_execution_imports_no_sync_runtime() -> None:
    """Check that the billing module builds its sandbox only through the bridge."""
    source = Path(protected_execution.__file__).read_text()
    assert "docker_sandbox" not in source
    assert "blackbox.sandbox import" not in source
    assert "VercelSandboxRuntime" not in source
    assert "DockerSandboxRuntime" not in source


@pytest.mark.usefixtures("provider_credentials")
def test_async_backend_reports_no_unavailable_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a configured async deployment can open protected boxes."""
    assert protected_vercel_unavailable_reason(_settings(monkeypatch), "dspy") is None


def test_async_backend_reports_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the protected check reports the provider's credential problem."""
    monkeypatch.setattr(global_settings, "vercel_token", None)
    reason = protected_vercel_unavailable_reason(_settings(monkeypatch), "dspy")
    assert reason == "The Vercel sandbox credentials are not configured."


@pytest.mark.usefixtures("provider_credentials")
def test_async_bind_reserves_and_settles_each_box_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an async protected box reserves once against the run budget and settles once."""
    log, usages = _fake_vercel(monkeypatch)
    gateway = _Gateway()

    identity = bind_protected_sandbox(gateway, _settings(monkeypatch), workflow="dspy", owner_id="job-9")

    assert identity["image"] == _DIGEST
    runtime = gateway.broker._runtime
    assert isinstance(runtime, ProtectedBridgeRuntime)
    assert runtime.protected
    assert runtime.billable
    assert not runtime.injects_headers
    assert runtime._options == {"budget": gateway.runtime}

    opened = gateway.broker.handle("open", {"request_id": "r1", "spec": {"lifetime_seconds": 60}})
    gateway.broker.handle("close", {"sandbox_id": opened["sandbox_id"]})
    gateway.broker.close()

    (usage,) = usages
    assert usage.budget is gateway.runtime
    assert usage.request["image"] == _DIGEST
    assert log.count("reserve") == 1
    assert usage.calls.count("confirm_created") == 1
    assert usage.calls.count("settle") == 1
    assert "fail" not in usage.calls


@pytest.mark.usefixtures("provider_credentials")
def test_async_bind_gives_the_broker_one_snapshot_cache_for_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a hosted run gets one setup snapshot cache, owned by the run and closed with the broker."""
    _fake_vercel(monkeypatch)
    gateway = _Gateway()

    bind_protected_sandbox(gateway, _settings(monkeypatch), workflow="dspy", owner_id="job-9")

    cache = gateway.broker._snapshots
    assert isinstance(cache, SnapshotCache)
    assert cache.run_id == "job-9"
    gateway.broker.close()
    assert cache._closed


@pytest.mark.usefixtures("provider_credentials")
def test_async_bind_skips_the_cache_without_snapshot_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a runtime without snapshots gets no cache, so boxes set up normally."""
    _fake_vercel(monkeypatch)
    plain = FakeRuntime()

    def runtime(*_args: Any, **_kwargs: Any) -> FakeRuntime:
        """Return a runtime that cannot snapshot.

        Args:
            *_args: Unused.
            **_kwargs: Unused.

        Returns:
            The fake runtime.
        """
        return plain

    monkeypatch.setattr(protected_execution, "protected_runtime", runtime)
    gateway = _Gateway()

    bind_protected_sandbox(gateway, _settings(monkeypatch), workflow="dspy", owner_id="job-9")

    assert gateway.broker._runtime is plain
    assert gateway.broker._snapshots is None

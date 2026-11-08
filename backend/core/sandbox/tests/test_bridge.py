"""Tests for the protected runtimes that ``core.sandbox.bridge`` picks. Fakes only: no network, no money."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from core.config import Settings
from core.config import settings as global_settings
from core.sandbox import bridge
from core.sandbox.providers.vercel import VercelEnvironment
from core.sandbox.tests.test_provider_vercel import FakeClient, FakeClock, FakeUsage, readback
from core.service_gateway.optimization.blackbox import sandbox as legacy_sandbox
from core.service_gateway.optimization.blackbox import sandbox_runtime
from core.service_gateway.optimization.blackbox.docker_sandbox import DockerSandboxRuntime
from core.service_gateway.optimization.blackbox.sandbox import VercelSandboxRuntime
from core.service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec

_DIGEST = "registry.example/skynet@sha256:" + "a" * 64
_LOCAL_IMAGE = "sha256:" + "c" * 64
_BUDGET = SimpleNamespace(budget_id="budget-1", generation=3)


class _StopBeforeNetworkError(Exception):
    """Stop the legacy open right after it builds its usage request."""


def _settings(**values: Any) -> Settings:
    """Build settings without the developer's ``.env`` file.

    Args:
        **values: Settings fields to set.

    Returns:
        A fresh ``Settings`` instance.
    """
    if values.get("local_mode"):
        values.setdefault("api_host", "127.0.0.1")
    return Settings(_env_file=None, **values)


def _hosted(backend: str) -> Settings:
    """Build hosted settings with fake Vercel credentials.

    Args:
        backend: The ``sandbox_backend`` value.

    Returns:
        The settings.
    """
    return _settings(
        sandbox_backend=backend,
        vercel_token=SecretStr("token"),
        vercel_team_id="team",
        vercel_project_id="project",
        vercel_sandbox_image=_DIGEST,
    )


class _Wiring:
    """Route the async Vercel provider to the fake SDK client and fake reservations."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, policy: Any = None) -> None:
        """Patch the bridge's environment factory.

        Args:
            monkeypatch: Pytest fixture.
            policy: The network policy readback of each new session; a
                deny-all readback when ``None``.
        """
        self.log: list[str] = []
        self.client = FakeClient(self.log, readback("deny-all") if policy is None else policy)
        self.usages: list[FakeUsage] = []
        self.options: list[dict[str, Any]] = []
        monkeypatch.setattr(sandbox_runtime, "create_environment", self.create)

    def _usage(self, budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make one fake reservation and keep it.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Returns:
            The fake reservation.
        """
        usage = FakeUsage(self.log, budget, request, operation_key=operation_key)
        self.usages.append(usage)
        return usage

    def _client(self, capture: Any) -> FakeClient:
        """Return the fake client.

        Args:
            capture: The response capture hook.

        Returns:
            The fake client.
        """
        self.client.capture = capture
        return self.client

    def create(self, name: str, spec: Any, **options: Any) -> VercelEnvironment:
        """Build the real async provider over the fakes.

        Args:
            name: The provider name.
            spec: The async spec.
            **options: The constructor options from the bridge runtime.

        Returns:
            The provider.
        """
        assert name == "vercel"
        self.options.append(options)
        return VercelEnvironment(
            spec,
            budget=options.get("budget"),
            client_factory=self._client,
            usage_factory=self._usage,
            clock=FakeClock(),
        )


def _spec(**values: Any) -> SandboxSpec:
    """Build the spec the broker sends for one guest box.

    Args:
        **values: Spec fields to change.

    Returns:
        The blackbox spec.
    """
    fields: dict[str, Any] = {
        "lifetime_seconds": 1800,
        "env": {"A": "1"},
        "image": _DIGEST,
        "network_disabled": True,
        "operation_key": "sandbox:r1",
    }
    return SandboxSpec(**{**fields, **values})


def _legacy_request(spec: SandboxSpec, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the legacy Vercel open until it builds its usage request.

    Args:
        spec: The blackbox spec.
        monkeypatch: Pytest fixture.

    Returns:
        The request the legacy runtime reserves.
    """
    captured: dict[str, Any] = {}

    def reserve(budget: Any, request: dict[str, Any], *, operation_key: str) -> None:
        """Keep the request and stop before any network call.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Raises:
            _StopBeforeNetworkError: Always.
        """
        captured.update(request, operation_key=operation_key)
        raise _StopBeforeNetworkError

    monkeypatch.setattr(legacy_sandbox, "VercelUsageReservation", reserve)
    runtime = bridge.protected_runtime(_hosted("legacy"), image=_DIGEST, budget=_BUDGET)
    with pytest.raises(_StopBeforeNetworkError):
        runtime.open(spec)
    return captured


def test_legacy_backend_keeps_the_legacy_runtimes() -> None:
    """Check that ``sandbox_backend = legacy`` builds the same runtimes as before."""
    vercel = bridge.protected_runtime(_hosted("legacy"), image=_DIGEST, budget=_BUDGET)
    assert isinstance(vercel, VercelSandboxRuntime)
    assert vercel.protected
    assert not vercel.injects_headers
    local = _settings(local_mode=True, local_sandbox_image=_LOCAL_IMAGE, docker_bin="docker")
    docker = bridge.protected_runtime(local, image=_LOCAL_IMAGE, budget=_BUDGET, recorder="recorder")
    assert isinstance(docker, DockerSandboxRuntime)
    assert docker.protected


def test_async_vercel_runtime_is_protected_and_funded() -> None:
    """Check that the async Vercel runtime funds each box and injects no headers."""
    runtime = bridge.protected_runtime(_hosted("async"), image=_DIGEST, budget=_BUDGET)
    assert isinstance(runtime, bridge.ProtectedBridgeRuntime)
    assert runtime.provider == "vercel"
    assert runtime.protected
    assert runtime.billable
    assert not runtime.injects_headers
    assert runtime._options == {"budget": _BUDGET}


def test_async_docker_runtime_is_free_and_unfunded() -> None:
    """Check that the async Docker runtime never gets the budget."""
    local = _settings(sandbox_backend="async", local_mode=True, local_sandbox_image=_LOCAL_IMAGE, docker_bin="dk")
    runtime = bridge.protected_runtime(local, image=_LOCAL_IMAGE, budget=_BUDGET)
    assert isinstance(runtime, bridge.ProtectedBridgeRuntime)
    assert runtime.provider == "docker"
    assert runtime.protected
    assert not runtime.billable
    assert not runtime.injects_headers
    assert runtime._options == {"docker_bin": "dk", "image": _LOCAL_IMAGE}


@pytest.mark.parametrize(
    ("values", "policy"),
    [
        ({}, readback("deny-all")),
        (
            {"network_disabled": False, "allowed_hosts": ("pypi.org", "files.pythonhosted.org", "pypi.org")},
            readback("custom", ("files.pythonhosted.org", "pypi.org")),
        ),
    ],
)
def test_async_reservation_matches_the_legacy_reservation(
    values: dict[str, Any], policy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that the async box reserves the same request as the legacy box.

    Args:
        values: Spec fields to change.
        policy: The network policy readback the fake Vercel returns.
        monkeypatch: Pytest fixture.
    """
    spec = _spec(**values)
    legacy = _legacy_request(spec, monkeypatch)
    wiring = _Wiring(monkeypatch, policy)
    runtime = bridge.protected_runtime(_hosted("async"), image=_DIGEST, budget=_BUDGET)

    session = runtime.open(spec)
    session.close()

    (usage,) = wiring.usages
    assert {**usage.request, "operation_key": usage.operation_key} == legacy
    assert usage.request["lifetime_ms"] == 600_000


def test_async_box_reserves_once_and_settles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check one reservation and one settlement for one box, even on a second close."""
    wiring = _Wiring(monkeypatch)
    runtime = bridge.protected_runtime(_hosted("async"), image=_DIGEST, budget=_BUDGET)

    session = runtime.open(_spec(lifetime_seconds=60))
    session.close()
    session.close()

    (usage,) = wiring.usages
    assert wiring.log.count("reserve") == 1
    assert usage.calls.count("confirm_created") == 1
    assert usage.calls.count("settle") == 1
    assert "fail" not in usage.calls
    assert wiring.options == [{"budget": _BUDGET}]


def test_async_create_error_releases_the_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a failed create fails the reservation once and never settles it."""
    wiring = _Wiring(monkeypatch)
    wiring.client.create_errors.append(RuntimeError("create failed"))
    runtime = bridge.protected_runtime(_hosted("async"), image=_DIGEST, budget=_BUDGET)

    with pytest.raises(RuntimeError, match="create failed"):
        runtime.open(_spec(lifetime_seconds=60))

    (usage,) = wiring.usages
    assert usage.calls.count("fail") + usage.calls.count("settle") == 1
    assert "confirm_created" not in usage.calls


def test_async_cancel_during_create_releases_the_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a cancel during the create stops the box and ends the reservation once."""
    wiring = _Wiring(monkeypatch)
    wiring.client.hang = True
    runtime = bridge.protected_runtime(_hosted("async"), image=_DIGEST, budget=_BUDGET)
    environment = wiring.create("vercel", sandbox_runtime._async_spec(_spec(lifetime_seconds=60)), **runtime._options)

    async def cancel_create() -> None:
        """Start the box, then cancel it while the create hangs."""
        task = asyncio.create_task(environment.start())
        await wiring.client.created.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_create())

    (usage,) = wiring.usages
    assert usage.calls.count("fail") + usage.calls.count("settle") == 1


def test_legacy_unavailable_reason_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the legacy backend asks the legacy checks."""
    monkeypatch.setattr(bridge, "sandbox_unavailable_reason", lambda settings: "legacy vercel")
    monkeypatch.setattr(bridge, "docker_sandbox_unavailable_reason", lambda settings: "legacy docker")
    assert bridge.protected_unavailable_reason(_hosted("legacy")) == "legacy vercel"
    assert bridge.protected_unavailable_reason(_settings(local_mode=True)) == "legacy docker"


def test_async_unavailable_reason_asks_the_vercel_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the async backend reports missing Vercel credentials from the provider preflight."""
    monkeypatch.setattr(global_settings, "vercel_token", None)
    assert bridge.protected_unavailable_reason(_hosted("async")) == "The Vercel sandbox credentials are not configured."
    monkeypatch.setattr(global_settings, "vercel_token", SecretStr("token"))
    monkeypatch.setattr(global_settings, "vercel_team_id", "team")
    monkeypatch.setattr(global_settings, "vercel_project_id", "project")
    assert bridge.protected_unavailable_reason(_hosted("async")) is None


def test_async_unavailable_reason_checks_local_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the async local backend still probes the Docker CLI and image."""
    monkeypatch.setattr(bridge, "docker_sandbox_unavailable_reason", lambda settings: "no docker")
    local = _settings(sandbox_backend="async", local_mode=True)
    assert bridge.protected_unavailable_reason(local) == "no docker"


def test_async_unavailable_reason_trusts_a_bound_parent_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a child with a bound parent runtime needs no provider of its own."""
    monkeypatch.setattr(global_settings, "vercel_token", None)
    with sandbox_runtime.sandbox_runtime_context(object()):  # type: ignore[arg-type]
        assert bridge.protected_unavailable_reason(_hosted("async")) is None

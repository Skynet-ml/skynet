"""Tests for the protected runtimes that ``core.sandbox.bridge`` picks. Fakes only: no network, no money."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from core.api import preflight_execution
from core.billing.budgets import BudgetError
from core.billing.operation_pricing import ChargePolicy, image_copy_usd, json_fingerprint
from core.billing.runtime import UsagePendingError
from core.billing.vercel_usage import SANDBOX_NETWORK_BYTES_CAP
from core.config import Settings
from core.config import settings as global_settings
from core.exceptions import ServiceError
from core.sandbox import bridge
from core.sandbox.errors import SandboxFatalError
from core.sandbox.factory import PROVIDERS, ProviderEntry
from core.sandbox.image_registry import RegistryCredentials
from core.sandbox.metering import USAGE_ADAPTERS
from core.sandbox.providers import vercel as vercel_provider
from core.sandbox.providers.vercel import VercelEnvironment
from core.sandbox.tests.test_provider_vercel import FakeClient, FakeClock, FakeCopier, FakeUsage, make_env, readback
from core.sandbox.vcr_copy import VcrCopier
from core.service_gateway.optimization.blackbox import sandbox_runtime
from core.service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec, UserImage
from core.worker import repo_staging

_DIGEST = "registry.example/skynet@sha256:" + "a" * 64
_LOCAL_IMAGE = "sha256:" + "c" * 64
_BUDGET = SimpleNamespace(budget_id="budget-1", generation=3)


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


def _hosted() -> Settings:
    """Build hosted settings with fake Vercel credentials.

    Returns:
        The settings.
    """
    return _settings(
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


def _expected_request(spec: SandboxSpec) -> dict[str, Any]:
    """Build the usage request that the removed sync Vercel box reserved for a spec.

    The sync runtime is gone, so this pins its request shape by hand: the
    async box must keep the same reservation, or a live budget changes.

    Args:
        spec: The blackbox spec.

    Returns:
        The request with its operation key.
    """
    request: dict[str, Any] = {
        "name": "skynet-" + json_fingerprint([_BUDGET.budget_id, _BUDGET.generation, spec.operation_key])[:48],
        "image": spec.image,
        "lifetime_ms": min(int(spec.lifetime_seconds * 1000), 600_000),
        "vcpus": spec.vcpus,
        "network_disabled": spec.network_disabled,
        "ports": [],
        "persistent": False,
        "environment_fingerprint": json_fingerprint(dict(spec.env)),
        "operation_key": spec.operation_key,
    }
    if spec.allowed_hosts:
        request["allowed_hosts"] = sorted(set(spec.allowed_hosts))
        request["network_bytes_cap"] = SANDBOX_NETWORK_BYTES_CAP
    return request


def test_async_vercel_runtime_is_protected_and_funded() -> None:
    """Check that the async Vercel runtime funds each box and injects no headers."""
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)
    assert isinstance(runtime, bridge.ProtectedBridgeRuntime)
    assert runtime.provider == "vercel"
    assert runtime.protected
    assert runtime.billable
    assert not runtime.injects_headers
    assert runtime._options == {"budget": _BUDGET}


def test_hosted_vercel_runtime_copies_user_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the hosted Vercel runtime meters the VCR copier, so it accepts user images (SKY-898)."""
    metered: list[Any] = []
    monkeypatch.setattr(bridge, "metered_copier", lambda copy_image, budget: metered.append(copy_image) or copy_image)
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)
    assert runtime.accepts_user_images
    assert len(metered) == 1
    assert isinstance(metered[0], VcrCopier)
    assert metered[0]._token == "token"


def test_vercel_runtime_without_credentials_has_no_copier() -> None:
    """Check that a Vercel runtime with no Vercel credentials refuses user images."""
    runtime = bridge.protected_runtime(_settings(), image=_DIGEST, budget=_BUDGET, provider=None)
    assert runtime.provider == "vercel"
    assert not runtime.accepts_user_images


def test_async_docker_runtime_is_free_and_unfunded() -> None:
    """Check that the async Docker runtime never gets the budget."""
    local = _settings(local_mode=True, local_sandbox_image=_LOCAL_IMAGE, docker_bin="dk")
    runtime = bridge.protected_runtime(local, image=_LOCAL_IMAGE, budget=_BUDGET)
    assert isinstance(runtime, bridge.ProtectedBridgeRuntime)
    assert runtime.provider == "docker"
    assert runtime.protected
    assert not runtime.billable
    assert not runtime.injects_headers
    assert runtime._options == {"docker_bin": "dk", "image": _LOCAL_IMAGE}


def test_async_docker_runtime_passes_the_run_recorder_to_its_boxes() -> None:
    """Give each Docker box the run's recorder, so it writes its egress requests (SKY-634)."""
    local = _settings(local_mode=True, local_sandbox_image=_LOCAL_IMAGE, docker_bin="dk")
    recorder = object()
    runtime = bridge.protected_runtime(local, image=_LOCAL_IMAGE, budget=_BUDGET, recorder=recorder)
    assert runtime._options == {"docker_bin": "dk", "image": _LOCAL_IMAGE, "recorder": recorder}


def test_async_vercel_runtime_passes_the_run_recorder_to_its_boxes() -> None:
    """Give each Vercel box the run's recorder for its in-box egress proxy (SKY-634)."""
    recorder = object()
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET, recorder=recorder)
    assert runtime._options == {"budget": _BUDGET, "recorder": recorder}


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
def test_async_reservation_matches_the_pinned_reservation(
    values: dict[str, Any], policy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that the async box reserves the same request as the removed sync box.

    Args:
        values: Spec fields to change.
        policy: The network policy readback the fake Vercel returns.
        monkeypatch: Pytest fixture.
    """
    spec = _spec(**values)
    wiring = _Wiring(monkeypatch, policy)
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)

    session = runtime.open(spec)
    session.close()

    (usage,) = wiring.usages
    assert {**usage.request, "operation_key": usage.operation_key} == _expected_request(spec)
    assert usage.request["lifetime_ms"] == 600_000


def test_async_box_reserves_once_and_settles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check one reservation and one settlement for one box, even on a second close."""
    wiring = _Wiring(monkeypatch)
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)

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
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)

    with pytest.raises(RuntimeError, match="create failed"):
        runtime.open(_spec(lifetime_seconds=60))

    (usage,) = wiring.usages
    assert usage.calls.count("fail") + usage.calls.count("settle") == 1
    assert "confirm_created" not in usage.calls


def test_async_cancel_during_create_releases_the_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a cancel during the create stops the box and ends the reservation once."""
    wiring = _Wiring(monkeypatch)
    wiring.client.hang = True
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)
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


def _fail_settle(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Make every fake settle log its call and then raise ``error``.

    Args:
        monkeypatch: Pytest fixture.
        error: The settle failure.
    """

    def settle(self: FakeUsage) -> None:
        """Log the settlement, then fail it.

        Raises:
            Exception: Always ``error``.
        """
        self._record("settle")
        raise error

    monkeypatch.setattr(FakeUsage, "settle", settle)


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (UsagePendingError("pending"), UsagePendingError),
        (BudgetError("bad bounds"), BudgetError),
        (RuntimeError("db down"), UsagePendingError),
    ],
)
def test_async_close_raises_a_failed_settle_once(
    monkeypatch: pytest.MonkeyPatch, error: Exception, raised: type[Exception]
) -> None:
    """Check that close raises a settle failure once, and settles once."""
    wiring = _Wiring(monkeypatch)
    _fail_settle(monkeypatch, error)
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)
    session = runtime.open(_spec(lifetime_seconds=60))
    box = next(iter(wiring.client.boxes.values()))

    with pytest.raises(raised) as caught:
        session.close()
    session.close()

    if raised is not type(error):
        assert caught.value.__cause__ is error
    (usage,) = wiring.usages
    assert usage.calls.count("settle") == 1
    assert "fail" not in usage.calls
    assert box.stops == 1


def test_async_close_retry_after_a_bridge_timeout_still_raises_the_settle_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check that a close retried after a bridge timeout reports the settle failure and settles once."""
    wiring = _Wiring(monkeypatch)
    _fail_settle(monkeypatch, UsagePendingError("pending"))
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)
    session = runtime.open(_spec(lifetime_seconds=60))
    inner = session._session
    original = inner.close
    calls = {"n": 0}

    def timed_out_once() -> None:
        """Time out the first bridge call before the stop runs, then close for real.

        Raises:
            TimeoutError: On the first call.
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError
        original()

    monkeypatch.setattr(inner, "close", timed_out_once)
    with pytest.raises(UsagePendingError):
        session.close()

    (usage,) = wiring.usages
    assert calls["n"] == 2
    assert usage.calls.count("settle") == 1


def test_preflight_reports_pending_when_the_async_settle_stays_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a pending settle at close turns a passed async preflight into "pending"."""
    wiring = _Wiring(monkeypatch)
    _fail_settle(monkeypatch, UsagePendingError("pending"))
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET)

    class _Gateway:
        """Hold the bound box and close it the way the model gateway closes its sandbox."""

        def __init__(self, budget: Any) -> None:
            """Start with no box.

            Args:
                budget: Unused.
            """
            self.session: Any = None

        def protect_payload(self, payload: dict[str, Any], **options: Any) -> dict[str, Any]:
            """Return the payload unchanged.

            Args:
                payload: The setup payload.
                **options: Unused.

            Returns:
                The payload.
            """
            return payload

        def close(self) -> None:
            """Close the bound box."""
            self.session.close()

    def bind(gateway: _Gateway, settings: Any, **options: Any) -> None:
        """Open one async box for the gateway.

        Args:
            gateway: The fake gateway.
            settings: Unused.
            **options: Unused.
        """
        gateway.session = runtime.open(_spec(lifetime_seconds=60))

    monkeypatch.setattr(preflight_execution, "ModelGateway", _Gateway)
    monkeypatch.setattr(preflight_execution, "BudgetRuntime", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight_execution, "bind_protected_sandbox", bind)
    monkeypatch.setattr(preflight_execution, "payload_uses_token_source", lambda *args, **kwargs: False)
    monkeypatch.setattr(preflight_execution, "preflight_lifetime_seconds", lambda payload: 60)
    monkeypatch.setattr(preflight_execution, "report_preflight_phase", lambda phase: None)
    monkeypatch.setattr(
        preflight_execution,
        "_verify_anything",
        lambda gateway, payload, **options: {"checks": [preflight_execution._check("setup", "succeeded")]},
    )
    request = SimpleNamespace(payload={}, workflow="anything", scope="evaluation", execution_budget_id="budget-1")

    status, result = preflight_execution._perform_preflight(
        request,  # type: ignore[arg-type]
        SimpleNamespace(username="user"),
        None,  # type: ignore[arg-type]
        SimpleNamespace(id="budget-1", generation=3),
        {"id": "preflight-1"},
        attempt=1,
        engine=None,
    )

    assert status == "pending"
    assert result["pending_reason"]
    (usage,) = wiring.usages
    assert usage.calls.count("settle") == 1


def test_async_unavailable_reason_asks_the_vercel_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the async backend reports missing Vercel credentials from the provider preflight."""
    monkeypatch.setattr(global_settings, "vercel_token", None)
    assert bridge.protected_unavailable_reason(_hosted()) == "The Vercel sandbox credentials are not configured."
    monkeypatch.setattr(global_settings, "vercel_token", SecretStr("token"))
    monkeypatch.setattr(global_settings, "vercel_team_id", "team")
    monkeypatch.setattr(global_settings, "vercel_project_id", "project")
    assert bridge.protected_unavailable_reason(_hosted()) is None


def test_async_unavailable_reason_checks_local_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the async local backend still probes the Docker CLI and image."""
    monkeypatch.setattr(bridge, "docker_sandbox_unavailable_reason", lambda settings: "no docker")
    local = _settings(local_mode=True)
    assert bridge.protected_unavailable_reason(local) == "no docker"


def test_async_unavailable_reason_trusts_a_bound_parent_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a child with a bound parent runtime needs no provider of its own."""
    monkeypatch.setattr(global_settings, "vercel_token", None)
    with sandbox_runtime.sandbox_runtime_context(object()):  # type: ignore[arg-type]
        assert bridge.protected_unavailable_reason(_hosted()) is None


def _vercel_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the Vercel provider preflight fake credentials and SDK.

    Args:
        monkeypatch: Pytest fixture.
    """
    # The preflight checks only that the SDK imported; a dev venv may lack it.
    monkeypatch.setattr(vercel_provider, "SandboxClient", object)
    monkeypatch.setattr(global_settings, "vercel_token", SecretStr("token"))
    monkeypatch.setattr(global_settings, "vercel_team_id", "team")
    monkeypatch.setattr(global_settings, "vercel_project_id", "project")


def _offered_ids(settings: Settings) -> list[str]:
    """Return the ids of the providers a run may pick.

    Args:
        settings: The deployment settings.

    Returns:
        The offered provider ids, in order.
    """
    return [entry["id"] for entry in bridge.offered_providers(settings)]


def test_hosted_deployment_offers_only_the_configured_metered_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted Skynet offers Vercel when it is configured, and nothing without its credentials (SKY-606)."""
    _vercel_credentials(monkeypatch)
    assert bridge.offered_providers(_hosted()) == [{"id": "vercel", "label": "Vercel Sandbox"}]
    monkeypatch.setattr(global_settings, "vercel_token", None)
    assert bridge.offered_providers(_hosted()) == []


def test_local_mode_offers_only_docker() -> None:
    """Local mode offers its Docker box and no hosted provider."""
    assert _offered_ids(_settings(local_mode=True)) == ["docker"]


@pytest.mark.parametrize(
    ("production_allowed", "metered", "offered"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_a_provider_appears_once_it_is_metered_and_allowed_in_production(
    monkeypatch: pytest.MonkeyPatch, production_allowed: bool, metered: bool, offered: bool
) -> None:
    """A registry entry joins the list only with a usage adapter and production_allowed, with no other change."""
    _vercel_credentials(monkeypatch)
    monkeypatch.setitem(
        PROVIDERS,
        "fake",
        ProviderEntry("core.sandbox.tests.fake_provider", "InMemorySandbox", None, production_allowed),
    )
    if metered:
        monkeypatch.setitem(USAGE_ADAPTERS, "fake", USAGE_ADAPTERS["vercel"])

    assert ("fake" in _offered_ids(_hosted())) is offered
    assert bridge.provider_offered(_hosted(), "fake") is offered


def test_runtime_uses_the_chosen_offered_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that picks Vercel gets the funded Vercel runtime."""
    _vercel_credentials(monkeypatch)
    runtime = bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET, provider="vercel")
    assert isinstance(runtime, bridge.ProtectedBridgeRuntime)
    assert runtime.provider == "vercel"


@pytest.mark.parametrize("provider", ["docker", "daytona", "nope"])
def test_runtime_refuses_a_provider_the_deployment_does_not_offer(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Hosted Skynet refuses local Docker, an unmetered provider and an unknown name."""
    _vercel_credentials(monkeypatch)
    with pytest.raises(SandboxFatalError, match="not offered"):
        bridge.protected_runtime(_hosted(), image=_DIGEST, budget=_BUDGET, provider=provider)


def test_gate_passes_the_stored_default_provider_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replayed run that stores the default provider passes; the availability check reports a missing default."""
    monkeypatch.setattr(global_settings, "vercel_token", None)
    assert bridge.offered_providers(_hosted()) == []
    assert bridge.provider_offered(_hosted(), "vercel")
    assert not bridge.provider_offered(_hosted(), "docker")


_USER_IMAGE = "registry.example/users/u1@sha256:" + "d" * 64


class _StopError(Exception):
    """End a box start once the fake factory has its options."""


class _Options:
    """Record the provider options of each box start, then stop it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the bridge's environment factory.

        Args:
            monkeypatch: Pytest fixture.
        """
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        monkeypatch.setattr(sandbox_runtime, "create_environment", self.create)

    def create(self, name: str, spec: Any, **options: Any) -> Any:
        """Record one box start.

        Args:
            name: The provider name.
            spec: The async spec.
            **options: The constructor options from the bridge runtime.

        Raises:
            _StopError: Always, so no box starts.
        """
        self.calls.append((name, spec, options))
        raise _StopError


async def _copier(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
    """Copy nothing and return a pinned copy.

    Args:
        source_image: The user image.
        pull_credentials: The pull credentials.

    Returns:
        The copy's reference.
    """
    return "vercel.example/copy@sha256:" + "e" * 64


def _user_image(credentials: Any = lambda: ("AWS", "pull-token")) -> UserImage:
    """Build a user image in the owner's repository.

    Args:
        credentials: The pull credentials callable.

    Returns:
        The user image.
    """
    return UserImage(_USER_IMAGE, repository="registry.example/users/u1", credentials=credentials)


def test_vercel_box_gets_the_source_image_the_repository_and_a_metered_copier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass the user image, the owner's repository and fresh credentials to the Vercel copy step (SKY-891)."""
    options = _Options(monkeypatch)
    runtime = bridge.ProtectedBridgeRuntime("vercel", budget=_BUDGET, copy_image=_copier)

    assert runtime.accepts_user_images
    assert runtime.copies_user_images
    with pytest.raises(_StopError):
        runtime.open(_spec(image=_USER_IMAGE, user_image=_user_image()))

    ((name, spec, values),) = options.calls
    assert name == "vercel"
    assert spec.image == _USER_IMAGE
    assert values["budget"] is _BUDGET
    assert values["pull_credentials"] == ("AWS", "pull-token")
    assert values["source_image"] == _USER_IMAGE
    assert values["user_image_repository"] == "registry.example/users/u1"
    # The billable runtime meters the copy, so the raw copier never reaches the provider.
    assert values["copy_image"] is not _copier
    assert callable(values["copy_image"])


def test_vercel_without_a_copier_refuses_a_user_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a user image on Vercel with no copier, so no box runs Skynet's image in its place (SKY-891)."""
    options = _Options(monkeypatch)
    runtime = bridge.ProtectedBridgeRuntime("vercel", budget=_BUDGET)

    assert not runtime.accepts_user_images
    with pytest.raises(ServiceError, match="cannot start a box from a user image"):
        runtime.open(_spec(image=_USER_IMAGE, user_image=_user_image()))
    assert options.calls == []


def test_docker_box_pulls_the_user_image_with_its_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the Docker pull only the credentials, with no copy step (SKY-891)."""
    options = _Options(monkeypatch)
    runtime = bridge.ProtectedBridgeRuntime("docker", budget=_BUDGET, image=_LOCAL_IMAGE, docker_bin="dk")

    assert runtime.accepts_user_images
    assert not runtime.copies_user_images
    with pytest.raises(_StopError):
        runtime.open(_spec(image=_USER_IMAGE, user_image=_user_image()))

    ((name, spec, values),) = options.calls
    assert name == "docker"
    assert spec.image == _USER_IMAGE
    assert values == {"docker_bin": "dk", "image": _LOCAL_IMAGE, "pull_credentials": ("AWS", "pull-token")}


def test_docker_box_with_a_public_user_image_pulls_with_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pull an image outside Skynet's registry with no credentials (SKY-891)."""
    options = _Options(monkeypatch)
    runtime = bridge.ProtectedBridgeRuntime("docker", budget=_BUDGET, image=_LOCAL_IMAGE, docker_bin="dk")

    with pytest.raises(_StopError):
        runtime.open(_spec(image="python:3.12", user_image=UserImage("python:3.12")))

    ((_, _, values),) = options.calls
    assert values["pull_credentials"] is None


def test_box_with_no_user_image_starts_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give a box with no user image none of the user image options (SKY-891)."""
    options = _Options(monkeypatch)
    vercel = bridge.ProtectedBridgeRuntime("vercel", budget=_BUDGET, copy_image=_copier)
    docker = bridge.ProtectedBridgeRuntime("docker", budget=_BUDGET, image=_LOCAL_IMAGE, docker_bin="dk")

    for runtime in (vercel, docker):
        with pytest.raises(_StopError):
            runtime.open(_spec())

    assert [values for _, _, values in options.calls] == [
        {"budget": _BUDGET},
        {"docker_bin": "dk", "image": _LOCAL_IMAGE},
    ]


def test_a_failed_credentials_call_names_only_the_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a registry message out of the box start error, because it can echo a token (SKY-891)."""
    options = _Options(monkeypatch)
    runtime = bridge.ProtectedBridgeRuntime("docker", budget=_BUDGET, image=_LOCAL_IMAGE, docker_bin="dk")

    def leak() -> tuple[str, str]:
        """Fail with a secret in the message.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("token=secret-value")

    with pytest.raises(ServiceError) as caught:
        runtime.open(_spec(image=_USER_IMAGE, user_image=_user_image(leak)))

    assert "RuntimeError" in str(caught.value)
    assert "secret-value" not in str(caught.value)
    assert options.calls == []


class _CopyBudget:
    """Record each paid operation and settle it, like the budget runtime."""

    def __init__(self, *, refuse: bool = False) -> None:
        """Start with no operations.

        Args:
            refuse: Whether the budget refuses each reservation.
        """
        self.refuse = refuse
        self.reserved: list[Any] = []
        self.settled: list[Any] = []
        self.keys: list[str] = []

    def execute(self, quote: Any, policy: Any, dispatch: Any, *, operation_key: str, cost_kind: str) -> Any:
        """Reserve the quote, dispatch once, and settle the result.

        Args:
            quote: The operation quote.
            policy: The charge policy.
            dispatch: The paid call.
            operation_key: The operation key.
            cost_kind: The cost kind.

        Returns:
            The dispatch value.

        Raises:
            BudgetError: The budget refuses the reservation.
        """
        assert cost_kind == "sandbox"
        if self.refuse:
            raise BudgetError("no funds")
        self.reserved.append(quote)
        self.keys.append(operation_key)
        result = dispatch()
        self.settled.append(result)
        return result.value


def test_metered_copier_reserves_the_time_limit_and_settles_the_started_minutes() -> None:
    """Reserve the copy's time limit before the copy, and settle each copy once on its own key (SKY-891)."""
    budget = _CopyBudget()
    calls: list[tuple[str, Any]] = []

    async def copy(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
        """Record the copy.

        Args:
            source_image: The user image.
            pull_credentials: The pull credentials.

        Returns:
            The copy's reference.
        """
        calls.append((source_image, pull_credentials))
        return "vercel.example/copy@sha256:" + "e" * 64

    metered = bridge.metered_copier(copy, budget)

    async def run() -> list[str]:
        """Copy twice.

        Returns:
            The two copy references.
        """
        return [await metered(_USER_IMAGE, ("AWS", "t")), await metered(_USER_IMAGE, None)]

    assert asyncio.run(run()) == ["vercel.example/copy@sha256:" + "e" * 64] * 2
    assert calls == [(_USER_IMAGE, ("AWS", "t")), (_USER_IMAGE, None)]
    reserve = ChargePolicy("sandbox").convert(image_copy_usd(bridge.COPY_RESERVE_SECONDS))
    assert [quote.maximum for quote in budget.reserved] == [reserve] * 2
    assert [result.provider_usd for result in budget.settled] == [image_copy_usd(0.0001)] * 2
    assert len(set(budget.keys)) == 2


def test_metered_copier_settles_a_failed_copy_and_raises_its_error() -> None:
    """Charge a failed copy for its started minutes and give its error to the caller (SKY-891)."""
    budget = _CopyBudget()

    async def copy(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
        """Fail the copy.

        Args:
            source_image: The user image.
            pull_credentials: The pull credentials.

        Raises:
            OSError: Always.
        """
        raise OSError("copy failed")

    with pytest.raises(OSError, match="copy failed"):
        asyncio.run(bridge.metered_copier(copy, budget)(_USER_IMAGE, None))
    (result,) = budget.settled
    assert result.provider_usd == image_copy_usd(0.0001)


def test_metered_copier_settles_a_copy_past_its_time_limit_at_the_reserved_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settle a copy that runs past the reserved time limit at that limit, never above it (SKY-891)."""
    budget = _CopyBudget()
    clock = iter([0.0, bridge.COPY_RESERVE_SECONDS + 65])
    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def copy(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
        """Return the copy.

        Args:
            source_image: The user image.
            pull_credentials: The pull credentials.

        Returns:
            The copy's reference.
        """
        return source_image

    asyncio.run(bridge.metered_copier(copy, budget)(_USER_IMAGE, None))
    (quote,) = budget.reserved
    (result,) = budget.settled
    assert image_copy_usd(bridge.COPY_RESERVE_SECONDS + 65) > image_copy_usd(bridge.COPY_RESERVE_SECONDS)
    assert result.provider_usd == image_copy_usd(bridge.COPY_RESERVE_SECONDS)
    assert ChargePolicy("sandbox").convert(result.provider_usd) == quote.maximum


def test_metered_copier_starts_no_copy_when_the_budget_refuses() -> None:
    """Start no copy that the run cannot fund (SKY-891)."""
    budget = _CopyBudget(refuse=True)
    calls: list[str] = []

    async def copy(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
        """Record the copy.

        Args:
            source_image: The user image.
            pull_credentials: The pull credentials.

        Returns:
            The copy's reference.
        """
        calls.append(source_image)
        return source_image

    with pytest.raises(BudgetError):
        asyncio.run(bridge.metered_copier(copy, budget)(_USER_IMAGE, None))
    assert calls == []


def test_metered_copier_holds_no_default_pool_thread() -> None:
    """Keep the default thread pool free while a copy runs, so other settlements still run (SKY-891)."""
    budget = _CopyBudget()

    async def run() -> tuple[str, str]:
        """Run a copy with a one-thread default pool, and use that pool while the copy waits.

        Returns:
            The pool call's result and the copy's reference.
        """
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        release = asyncio.Event()

        async def copy(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
            """Wait until the test releases the copy.

            Args:
                source_image: The user image.
                pull_credentials: The pull credentials.

            Returns:
                The copy's reference.
            """
            await release.wait()
            return source_image

        copying = asyncio.create_task(bridge.metered_copier(copy, budget)(_USER_IMAGE, None))
        await asyncio.sleep(0)
        free = await asyncio.wait_for(asyncio.to_thread(lambda: "free"), timeout=5)
        release.set()
        return free, await copying

    assert asyncio.run(run()) == ("free", _USER_IMAGE)


class _HeldCopyBudget(_CopyBudget):
    """Hold each reservation until the test releases it, so a cancel can come before the copy starts."""

    def __init__(self) -> None:
        """Start with the reservation held."""
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, quote: Any, policy: Any, dispatch: Any, *, operation_key: str, cost_kind: str) -> Any:
        """Wait for the release, then reserve, dispatch and settle.

        Args:
            quote: The operation quote.
            policy: The charge policy.
            dispatch: The paid call.
            operation_key: The operation key.
            cost_kind: The cost kind.

        Returns:
            The dispatch value.
        """
        self.entered.set()
        assert self.release.wait(timeout=5)
        return super().execute(quote, policy, dispatch, operation_key=operation_key, cost_kind=cost_kind)


def test_metered_copier_settles_nothing_for_a_copy_cancelled_before_it_starts() -> None:
    """A copy that a cancel stops before it starts settles at $0, not at one minute (SKY-891)."""
    budget = _HeldCopyBudget()
    calls: list[str] = []

    async def copy(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
        """Record the copy.

        Args:
            source_image: The user image.
            pull_credentials: The pull credentials.

        Returns:
            The copy's reference.
        """
        calls.append(source_image)
        return source_image

    async def run() -> None:
        """Cancel the copy while its reservation is held, then release the reservation."""
        copying = asyncio.create_task(bridge.metered_copier(copy, budget)(_USER_IMAGE, None))
        assert await asyncio.to_thread(budget.entered.wait, 5)
        copying.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        budget.release.set()
        with pytest.raises(asyncio.CancelledError):
            await copying

    asyncio.run(run())
    assert calls == []
    (result,) = budget.settled
    assert result.provider_usd == image_copy_usd(0) == 0


def test_vercel_copy_step_lets_a_budget_stop_through() -> None:
    """Give a budget stop of the metered copier to the run as it is, not as a failed copy (SKY-891)."""

    async def copy(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
        """Refuse the copy for its budget.

        Args:
            source_image: The user image.
            pull_credentials: The pull credentials.

        Raises:
            BudgetError: Always.
        """
        raise BudgetError("no funds")

    with pytest.raises(BudgetError):
        asyncio.run(vercel_provider._copy_image_once(copy, _USER_IMAGE, None))


@pytest.mark.parametrize(
    ("seconds", "usd"), [(0, "0"), (-5, "0"), (1, "0.01"), (60, "0.01"), (61, "0.02"), (3600, "0.60")]
)
def test_image_copy_usd_charges_each_started_minute(seconds: float, usd: str) -> None:
    """Charge each started minute of a copy (SKY-891).

    Args:
        seconds: The copy time.
        usd: The expected charge.
    """
    assert image_copy_usd(seconds) == Decimal(usd)


class _TokenRegistry:
    """Hand out one-hour pull tokens and count them, like the user image registry (SKY-891)."""

    def __init__(self, left: timedelta = timedelta(hours=1)) -> None:
        """Start with no tokens.

        Args:
            left: The time each new token has before it ends.
        """
        self.left = left
        self.pulls = 0

    def repository_uri(self, owner: str) -> str:
        """Return the owner's repository.

        Args:
            owner: The registry repository's owner.

        Returns:
            The repository of :data:`_USER_IMAGE`.
        """
        return "registry.example/users/u1"

    def pull_credentials(self, owner: str) -> RegistryCredentials:
        """Return a new token each call, as AssumeRole and GetAuthorizationToken do.

        Args:
            owner: The registry repository's owner.

        Returns:
            A token with a new password.
        """
        self.pulls += 1
        return RegistryCredentials(
            server="registry.example",
            username="AWS",
            password=f"token-{self.pulls}",
            expires_at=datetime.now(UTC) + self.left,
        )


def test_two_vercel_boxes_of_one_user_image_get_one_token_and_one_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The box starts reuse one pull token, so the copy cache hits and the image is copied once (SKY-891).

    Args:
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setattr(vercel_provider, "_COPIED", {})
    monkeypatch.setattr(vercel_provider, "_COPYING", {})
    registry = _TokenRegistry()
    image = repo_staging._user_image(registry, "alice", "run", _USER_IMAGE)
    log: list[str] = []
    client, copier = FakeClient(log), FakeCopier(log)

    async def start_two_boxes() -> None:
        """Start and stop two boxes from the user image, as two box opens do."""
        for _ in range(2):
            env = make_env(
                client,
                log,
                image=None,
                options={
                    "source_image": image.reference,
                    "copy_image": copier,
                    "user_image_repository": image.repository,
                    "pull_credentials": image.credentials(),
                },
            )
            await env.start()
            await env.stop()

    asyncio.run(start_two_boxes())

    assert registry.pulls == 1
    assert copier.calls == [(_USER_IMAGE, ("AWS", "token-1"))]


def test_boxes_on_many_threads_share_one_pull_token() -> None:
    """Boxes that open on several threads at once ask the registry for one token (SKY-891)."""
    registry = _TokenRegistry()
    image = repo_staging._user_image(registry, "alice", "run", _USER_IMAGE)
    assert image.credentials is not None
    tokens: list[tuple[str, str]] = []
    threads = [threading.Thread(target=lambda: tokens.append(image.credentials())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert registry.pulls == 1
    assert set(tokens) == {("AWS", "token-1")}


def test_a_pull_token_near_its_end_is_renewed() -> None:
    """A token with 15 minutes or less left is replaced, so a box start never pulls with a dying token (SKY-891)."""
    registry = _TokenRegistry(left=timedelta(minutes=15))
    image = repo_staging._user_image(registry, "alice", "run", _USER_IMAGE)
    assert image.credentials is not None

    assert image.credentials() == ("AWS", "token-1")
    assert image.credentials() == ("AWS", "token-2")
    assert registry.pulls == 2

"""Tests for the sandbox provider factory: lookup, lazy load, production gate and preflight."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import ClassVar

import pytest

from core.sandbox import SandboxSpec, factory
from core.sandbox.errors import SandboxFatalError
from core.sandbox.factory import (
    PROVIDERS,
    ProductionNotAllowedError,
    ProviderEntry,
    ProviderUnavailableError,
    UnknownProviderError,
)
from core.sandbox.tests.fake_provider import InMemorySandbox

_FAKE_MODULE = "core.sandbox.tests.fake_provider"
_SPEC = SandboxSpec(lifetime_seconds=60)


class PreflightSandbox(InMemorySandbox):
    """Record each call of the provider preflight check."""

    preflight_calls: ClassVar[int] = 0

    @classmethod
    def preflight(cls) -> None:
        """Count one preflight call."""
        cls.preflight_calls += 1


class NotASandbox:
    """Stand in for a registry entry that names the wrong class."""


@pytest.fixture(autouse=True)
def _not_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test outside production unless the test changes it.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(factory.settings, "app_environment", "")


def _register(monkeypatch: pytest.MonkeyPatch, name: str, entry: ProviderEntry) -> None:
    """Add a provider entry to the registry for one test.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        name: The provider name.
        entry: The registry entry.
    """
    monkeypatch.setitem(PROVIDERS, name, entry)


def test_unknown_provider_names_the_known_providers() -> None:
    """Fail with a clear error for a name that is not in the registry."""
    with pytest.raises(UnknownProviderError, match=r"Unknown sandbox provider 'nope'.*vercel"):
        factory.create_environment("nope", _SPEC)


def test_unknown_provider_error_is_fatal() -> None:
    """Classify the factory errors in the no-retry fatal group."""
    assert issubclass(UnknownProviderError, SandboxFatalError)
    assert issubclass(ProviderUnavailableError, SandboxFatalError)
    assert issubclass(ProductionNotAllowedError, SandboxFatalError)


def test_create_environment_loads_the_provider_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the module by its import path and build the class with the spec."""
    _register(monkeypatch, "fake", ProviderEntry(_FAKE_MODULE, "InMemorySandbox", None, False))
    environment = factory.create_environment("fake", _SPEC)
    assert type(environment) is InMemorySandbox
    assert environment.spec is _SPEC


def test_missing_extra_names_the_extra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tell the user which extra to install when the provider SDK is missing."""
    (tmp_path / "sky411_needs_sdk.py").write_text(
        textwrap.dedent(
            '''
            """A provider module whose SDK is not installed."""

            import sky411_sdk_that_is_not_installed  # noqa: F401
            '''
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    _register(monkeypatch, "sdkless", ProviderEntry("sky411_needs_sdk", "Environment", "sdkless", False))
    with pytest.raises(ProviderUnavailableError, match="install the `sdkless` extra") as caught:
        factory.create_environment("sdkless", _SPEC)
    assert "sky411_sdk_that_is_not_installed" in str(caught.value)
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)


def test_missing_dependency_without_extra_names_the_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Name the missing package when the provider has no extra of its own."""
    (tmp_path / "sky411_core_dep.py").write_text("import sky411_core_dep_that_is_not_installed  # noqa: F401\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    _register(monkeypatch, "coredep", ProviderEntry("sky411_core_dep", "Environment", None, False))
    with pytest.raises(ProviderUnavailableError, match="sky411_core_dep_that_is_not_installed") as caught:
        factory.create_environment("coredep", _SPEC)
    assert "extra" not in str(caught.value)


def test_failed_name_import_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Wrap an ImportError that is not a missing module, such as an SDK of the wrong version."""
    (tmp_path / "sky411_wrong_sdk.py").write_text("from os import sky411_name_that_does_not_exist  # noqa: F401\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    _register(monkeypatch, "wrongsdk", ProviderEntry("sky411_wrong_sdk", "Environment", "wrongsdk", False))
    with pytest.raises(ProviderUnavailableError, match="could not be imported") as caught:
        factory.create_environment("wrongsdk", _SPEC)
    assert "sky411_name_that_does_not_exist" in str(caught.value)
    assert type(caught.value.__cause__) is ImportError


def test_registered_provider_without_module_is_not_in_this_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Say that a registered provider is not in this build until its module exists."""
    _register(monkeypatch, "ghost", ProviderEntry("sky453_missing_provider_module", "Environment", None, False))
    with pytest.raises(ProviderUnavailableError, match="not in this build"):
        factory.create_environment("ghost", _SPEC)


def test_missing_class_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail clearly when the module has no class with the registered name."""
    _register(monkeypatch, "noclass", ProviderEntry(_FAKE_MODULE, "MissingSandbox", None, False))
    with pytest.raises(ProviderUnavailableError, match="MissingSandbox"):
        factory.create_environment("noclass", _SPEC)


def test_class_must_be_a_sandbox_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a registry entry whose class does not implement the sandbox interface."""
    _register(monkeypatch, "wrong", ProviderEntry(__name__, "NotASandbox", None, False))
    with pytest.raises(ProviderUnavailableError, match="SandboxEnvironment"):
        factory.create_environment("wrong", _SPEC)


def test_only_vercel_is_allowed_in_production() -> None:
    """Keep Vercel the only production provider (SKY-406 D1)."""
    assert {name for name, entry in PROVIDERS.items() if entry.production_allowed} == {"vercel"}


def test_registry_lists_every_provider() -> None:
    """Keep the full provider list explicit, so an added or lost entry fails here."""
    assert sorted(PROVIDERS) == [
        "apple_container",
        "daytona",
        "docker",
        "e2b",
        "kata",
        "kernel",
        "langsmith",
        "local",
        "modal",
        "mosaic",
        "openshift",
        "podman",
        "prime",
        "runloop",
        "sail",
        "sbx",
        "singularity",
        "smol",
        "vercel",
    ]


def test_production_refuses_a_non_production_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse at create time a provider that is not allowed in production."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    _register(monkeypatch, "fake", ProviderEntry(_FAKE_MODULE, "InMemorySandbox", None, False))
    with pytest.raises(ProductionNotAllowedError, match=r"'fake'.*production"):
        factory.create_environment("fake", _SPEC)


def test_production_refusal_comes_before_the_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse in production even when the provider module cannot load."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        factory.create_environment("e2b", _SPEC)


def test_production_allows_a_production_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a provider that is allowed in production."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    _register(monkeypatch, "fake", ProviderEntry(_FAKE_MODULE, "InMemorySandbox", None, True))
    assert isinstance(factory.create_environment("fake", _SPEC), InMemorySandbox)


@pytest.mark.parametrize("environment", ["", "staging", "skynet-pr-12"])
def test_non_production_environments_allow_every_provider(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    """Allow dev-only providers outside production, previews and staging included."""
    monkeypatch.setattr(factory.settings, "app_environment", environment)
    _register(monkeypatch, "fake", ProviderEntry(_FAKE_MODULE, "InMemorySandbox", None, False))
    assert isinstance(factory.create_environment("fake", _SPEC), InMemorySandbox)


def test_preflight_calls_the_provider_classmethod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch preflight to the provider class without building a sandbox."""
    monkeypatch.setattr(PreflightSandbox, "preflight_calls", 0)
    _register(monkeypatch, "checked", ProviderEntry(__name__, "PreflightSandbox", None, False))
    factory.preflight("checked")
    assert PreflightSandbox.preflight_calls == 1


def test_preflight_passes_the_provider_error_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the provider's own preflight error reach the caller unchanged."""

    def _fail(cls: type[PreflightSandbox]) -> None:
        """Fail the check like a provider with no credentials.

        Args:
            cls: The provider class.

        Raises:
            SandboxFatalError: Always.
        """
        raise SandboxFatalError("set FAKE_TOKEN")

    monkeypatch.setattr(PreflightSandbox, "preflight", classmethod(_fail))
    _register(monkeypatch, "checked", ProviderEntry(__name__, "PreflightSandbox", None, False))
    with pytest.raises(SandboxFatalError, match="set FAKE_TOKEN"):
        factory.preflight("checked")


def test_preflight_without_a_provider_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat a provider with no preflight classmethod as ready."""
    _register(monkeypatch, "fake", ProviderEntry(_FAKE_MODULE, "InMemorySandbox", None, False))
    factory.preflight("fake")


def test_preflight_applies_the_same_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail preflight for an unknown name and for a refused production provider."""
    with pytest.raises(UnknownProviderError):
        factory.preflight("nope")
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    _register(monkeypatch, "checked", ProviderEntry(__name__, "PreflightSandbox", None, False))
    with pytest.raises(ProductionNotAllowedError):
        factory.preflight("checked")

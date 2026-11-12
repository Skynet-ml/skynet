"""Pick a sandbox provider by name, load it lazily and refuse dev-only providers in production.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the registry of
provider import paths and the lazy import come from
``harbor/environments/factory.py``. Changes: each entry names its optional
dependency extra and whether production may use it, the factory refuses a
dev-only provider in production before it imports the module (SKY-406 D1),
and every failure is a fatal sandbox error that never retries.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from core.config import settings

from .environment import SandboxEnvironment
from .errors import SandboxFatalError
from .resources import SandboxSpec

_PRODUCTION = "production"


@dataclass(frozen=True)
class ProviderEntry:
    """Hold where a provider class lives and where it may run."""

    module: str
    cls: str
    extra: str | None
    production_allowed: bool


# The provider modules land in later issues (SBX-6 local, SBX-7 docker,
# SBX-10 vercel, SBX-25+ the hosted providers); until then the factory reports
# them as "not in this build". A hosted extra joins pyproject with its module.
PROVIDERS: dict[str, ProviderEntry] = {
    "local": ProviderEntry("core.sandbox.providers.local", "LocalEnvironment", None, False),
    "docker": ProviderEntry("core.sandbox.providers.docker", "DockerEnvironment", None, False),
    "vercel": ProviderEntry("core.sandbox.providers.vercel", "VercelEnvironment", None, True),
    "e2b": ProviderEntry("core.sandbox.providers.e2b", "E2BEnvironment", "e2b", False),
    "daytona": ProviderEntry("core.sandbox.providers.daytona", "DaytonaEnvironment", "daytona", False),
    "modal": ProviderEntry("core.sandbox.providers.modal", "ModalEnvironment", "modal", False),
    "runloop": ProviderEntry("core.sandbox.providers.runloop", "RunloopEnvironment", "runloop", False),
    "mosaic": ProviderEntry("core.sandbox.providers.mosaic", "MosaicEnvironment", "mosaic", False),
    "prime": ProviderEntry("core.sandbox.providers.prime", "PrimeEnvironment", "prime", False),
    "sbx": ProviderEntry("core.sandbox.providers.sbx", "SbxEnvironment", "sbx", False),
    "langsmith": ProviderEntry("core.sandbox.providers.langsmith", "LangSmithEnvironment", "langsmith", False),
    "kernel": ProviderEntry("core.sandbox.providers.kernel", "KernelEnvironment", "kernel", False),
    "apple_container": ProviderEntry(
        "core.sandbox.providers.apple_container", "AppleContainerEnvironment", None, False
    ),
    "podman": ProviderEntry("core.sandbox.providers.podman", "PodmanEnvironment", None, False),
    "kata": ProviderEntry("core.sandbox.providers.kata", "KataEnvironment", None, False),
    "sail": ProviderEntry("core.sandbox.providers.sail", "SailEnvironment", "sail", False),
    "smol": ProviderEntry("core.sandbox.providers.smol", "SmolEnvironment", "smol", False),
    "ec2": ProviderEntry("core.sandbox.providers.ec2", "EC2Environment", "ec2", False),
    "gke": ProviderEntry("core.sandbox.providers.gke", "GKEEnvironment", "gke", False),
    "singularity": ProviderEntry("core.sandbox.providers.singularity", "SingularityEnvironment", None, False),
    "openshift": ProviderEntry("core.sandbox.providers.openshift", "OpenshiftEnvironment", None, False),
}


class UnknownProviderError(SandboxFatalError):
    """Report a provider name that is not in the registry."""


class ProviderUnavailableError(SandboxFatalError):
    """Report a provider that this install cannot load."""


class ProductionNotAllowedError(SandboxFatalError):
    """Report a dev-only provider that production asked for."""


def create_environment(name: str, spec: SandboxSpec, **kwargs: Any) -> SandboxEnvironment:
    """Build a stopped sandbox environment of the named provider.

    Args:
        name: The provider name, a key of ``PROVIDERS``.
        spec: The sandbox to open.
        **kwargs: Provider-specific constructor options.

    Returns:
        The environment. The caller starts it.

    Raises:
        UnknownProviderError: The name is not in the registry.
        ProductionNotAllowedError: Production asked for a dev-only provider.
        ProviderUnavailableError: The provider module, class or dependency is missing.
    """
    return load_provider(name)(spec, **kwargs)


def preflight(name: str) -> None:
    """Check that the named provider can run here, before any sandbox starts.

    The registry gates run first. Then the provider's own ``preflight``
    classmethod runs, when the provider defines one, to check credentials or
    a local daemon.

    Args:
        name: The provider name, a key of ``PROVIDERS``.

    Raises:
        UnknownProviderError: The name is not in the registry.
        ProductionNotAllowedError: Production asked for a dev-only provider.
        ProviderUnavailableError: The provider module, class or dependency is missing.
    """
    check = getattr(load_provider(name), "preflight", None)
    if callable(check):
        check()


def load_provider(name: str) -> type[SandboxEnvironment]:
    """Return the provider class after the registry and production gates.

    Args:
        name: The provider name, a key of ``PROVIDERS``.

    Returns:
        The provider class.

    Raises:
        UnknownProviderError: The name is not in the registry.
        ProductionNotAllowedError: Production asked for a dev-only provider.
        ProviderUnavailableError: The provider module, class or dependency is missing.
    """
    entry = PROVIDERS.get(name)
    if entry is None:
        known = ", ".join(sorted(PROVIDERS))
        raise UnknownProviderError(f"Unknown sandbox provider {name!r}. Known providers: {known}.")
    # Checked before the import, so production never loads dev-only code.
    if settings.app_environment == _PRODUCTION and not entry.production_allowed:
        raise ProductionNotAllowedError(f"Sandbox provider {name!r} is not allowed in production.")
    module = _import_provider_module(name, entry)
    cls = getattr(module, entry.cls, None)
    if cls is None:
        raise ProviderUnavailableError(f"Sandbox provider {name!r}: {entry.module} has no class {entry.cls}.")
    if not (isinstance(cls, type) and issubclass(cls, SandboxEnvironment)):
        raise ProviderUnavailableError(
            f"Sandbox provider {name!r}: {entry.module}.{entry.cls} is not a SandboxEnvironment."
        )
    return cls


def _import_provider_module(name: str, entry: ProviderEntry) -> Any:
    """Import the provider module and explain a missing module or dependency.

    Args:
        name: The provider name.
        entry: The registry entry.

    Returns:
        The imported module.

    Raises:
        ProviderUnavailableError: The module or one of its dependencies is
            missing, or an import in them fails.
    """
    try:
        return importlib.import_module(entry.module)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if entry.module == missing or entry.module.startswith(f"{missing}."):
            raise ProviderUnavailableError(f"Sandbox provider {name!r} is not in this build.") from exc
        if entry.extra is not None:
            raise ProviderUnavailableError(
                f"Sandbox provider {name!r} needs the package {missing!r}: install the `{entry.extra}` extra."
            ) from exc
        raise ProviderUnavailableError(f"Sandbox provider {name!r} needs the package {missing!r}.") from exc
    except ImportError as exc:
        # The module exists but a name in it or its dependencies does not, for
        # example an SDK of the wrong version.
        raise ProviderUnavailableError(f"Sandbox provider {name!r} could not be imported: {exc}") from exc

"""Protected sandbox runtimes for the trusted parent.

``core.billing.protected_execution`` builds its sandbox here, so it imports no
runtime itself. This module returns the run's chosen provider, or the
deployment default (Docker in local mode, Vercel everywhere else), through the
SBX-11 sync bridge. It also lists the providers a run may choose (SKY-606).
The Vercel provider reserves and settles each usage slice against the run's
budget.

``core.sandbox.__init__`` never imports this module, because this module
imports the blackbox runtimes, which import ``core.sandbox``.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..service_gateway.optimization.blackbox.sandbox import docker_sandbox_unavailable_reason
from ..service_gateway.optimization.blackbox.sandbox_contract import SandboxRuntime
from ..service_gateway.optimization.blackbox.sandbox_runtime import AsyncBridgeRuntime, current_sandbox_runtime
from .errors import SandboxFatalError
from .factory import PROVIDERS, preflight
from .metering import USAGE_ADAPTERS

# A fallback English label; the UI shows its own translated name when it has one.
_LABELS = {"vercel": "Vercel Sandbox", "docker": "Local Docker"}


class ProtectedBridgeRuntime(AsyncBridgeRuntime):
    """Open protected boxes of one async provider through the sync bridge."""

    def __init__(self, provider: str, *, budget: Any, image: str | None = None, docker_bin: str | None = None) -> None:
        """Bind the runtime to a provider and the run's budget.

        Args:
            provider: A provider name from ``core.sandbox.factory.PROVIDERS``.
            budget: The run's budget runtime. A billable provider reserves and
                settles each box against it.
            image: The default image of a provider that takes one (Docker).
            docker_bin: The Docker CLI path of the Docker provider.

        Raises:
            SandboxFatalError: The provider is unknown, missing from this
                build, or not allowed in production.
        """
        super().__init__(provider, image=image, docker_bin=docker_bin)
        self.protected = True
        # Credentials stay in the control plane, so no box gets injected headers.
        self.injects_headers = False
        # The broker prices a billable box before it opens one; a local box is free.
        self.billable = self._capabilities.billable
        if self.billable:
            self._options["budget"] = budget


def default_provider(settings: Settings) -> str:
    """Return the async provider a run uses when it names none.

    Args:
        settings: Trusted deployment configuration.

    Returns:
        ``"docker"`` in local mode, otherwise ``"vercel"``.
    """
    return "docker" if settings.local_mode else "vercel"


def offered_providers(settings: Settings) -> list[dict[str, str]]:
    """List the providers a user may pick for a run on this deployment.

    Local mode offers only its Docker box. A hosted deployment offers every
    provider that production allows, that protected execution meters through
    a usage adapter, and whose own preflight passes here. A new provider
    joins the list once it meets all three, with no other change.

    Args:
        settings: Trusted deployment configuration.

    Returns:
        One ``{"id", "label"}`` entry per offered provider, in registry order.
    """
    if settings.local_mode:
        names = ["docker"]
    else:
        names = []
        for name, entry in PROVIDERS.items():
            if not entry.production_allowed or name not in USAGE_ADAPTERS:
                continue
            try:
                preflight(name)
            except SandboxFatalError:
                continue
            names.append(name)
    return [{"id": name, "label": _LABELS.get(name, name)} for name in names]


def provider_offered(settings: Settings, provider: object) -> bool:
    """Check a run's requested provider against the offered list.

    Args:
        settings: Trusted deployment configuration.
        provider: The requested provider id, or ``None`` for the default.

    Returns:
        True when the run names no provider or an offered one.
    """
    return provider is None or provider in {entry["id"] for entry in offered_providers(settings)}


def protected_unavailable_reason(settings: Settings) -> str | None:
    """Explain why this deployment cannot open protected boxes, or return ``None``.

    Args:
        settings: Trusted deployment configuration.

    Returns:
        A user-facing reason, or ``None`` when protected boxes can open.
    """
    if current_sandbox_runtime() is not None:
        return None
    try:
        preflight(default_provider(settings))
    except SandboxFatalError as error:
        return str(error)
    # The async Docker provider has no preflight; the same probe checks the
    # Docker CLI, the daemon and the image.
    return docker_sandbox_unavailable_reason(settings) if settings.local_mode else None


def protected_runtime(
    settings: Settings, *, image: str, budget: Any, recorder: Any = None, provider: str | None = None
) -> SandboxRuntime:
    """Build the protected runtime of this deployment.

    Args:
        settings: Trusted deployment configuration.
        image: The immutable image of every box.
        budget: The run's budget runtime.
        recorder: Unused. The async providers record no egress proxy
            requests; callers still pass the run's call recorder.
        provider: The run's chosen provider; the deployment default when None.

    Returns:
        The chosen async provider through the sync bridge.

    Raises:
        SandboxFatalError: The provider is not offered here, or cannot load.
    """
    if not provider_offered(settings, provider):
        raise SandboxFatalError(f"Sandbox provider {provider!r} is not offered on this deployment.")
    name = provider or default_provider(settings)
    if name == "docker":
        return ProtectedBridgeRuntime("docker", budget=budget, image=image, docker_bin=settings.docker_bin)
    return ProtectedBridgeRuntime(name, budget=budget)

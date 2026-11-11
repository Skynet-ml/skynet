"""Protected sandbox runtimes for the trusted parent.

``core.billing.protected_execution`` builds its sandbox here, so it imports no
runtime itself. This module returns the async provider of the deployment
(Docker in local mode, Vercel everywhere else) through the SBX-11 sync bridge.
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
from .factory import preflight


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


def _provider(settings: Settings) -> str:
    """Return the async provider of this deployment.

    Args:
        settings: Trusted deployment configuration.

    Returns:
        ``"docker"`` in local mode, otherwise ``"vercel"``.
    """
    return "docker" if settings.local_mode else "vercel"


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
        preflight(_provider(settings))
    except SandboxFatalError as error:
        return str(error)
    # The async Docker provider has no preflight; the same probe checks the
    # Docker CLI, the daemon and the image.
    return docker_sandbox_unavailable_reason(settings) if settings.local_mode else None


def protected_runtime(settings: Settings, *, image: str, budget: Any, recorder: Any = None) -> SandboxRuntime:
    """Build the protected runtime of this deployment.

    Args:
        settings: Trusted deployment configuration.
        image: The immutable image of every box.
        budget: The run's budget runtime.
        recorder: Unused. The async providers record no egress proxy
            requests; callers still pass the run's call recorder.

    Returns:
        The async provider of this deployment through the sync bridge.

    Raises:
        SandboxFatalError: The provider cannot load.
    """
    if settings.local_mode:
        return ProtectedBridgeRuntime("docker", budget=budget, image=image, docker_bin=settings.docker_bin)
    return ProtectedBridgeRuntime("vercel", budget=budget)

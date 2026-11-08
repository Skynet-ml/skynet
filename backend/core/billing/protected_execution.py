"""Configure trusted spending and sandbox boundaries for setup and optimization."""

from __future__ import annotations

import math
import re
from typing import Any

from ..config import VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS, Settings
from ..service_gateway.optimization.blackbox.docker_sandbox import (
    DockerSandboxRuntime,
    docker_sandbox_unavailable_reason,
)
from ..service_gateway.optimization.blackbox.sandbox import (
    JOB_TAG,
    VercelCredentials,
    VercelSandboxRuntime,
    sandbox_unavailable_reason,
)
from ..service_gateway.optimization.blackbox.sandbox_broker import SandboxBroker
from ..storage.run_call_store import PostgresRunCallStore
from ..worker.scoped_relay import CallRecorder
from .model_gateway import ModelGateway
from .model_mailbox import ModelMailbox
from .operation_pricing import UnpricedOperationError
from .vercel_usage import vercel_sandbox_cost_range

_IMMUTABLE_IMAGE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
# A locally built image has no registry digest, only its content-addressed id.
_LOCAL_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _immutable(settings: Settings, image: str) -> bool:
    """Check that ``image`` is an immutable reference this deployment may boot.

    Args:
        settings: Trusted backend configuration.
        image: Image reference to check.

    Returns:
        Whether the image is a bare local image id in local mode, or a
        registry digest reference on any other deployment.
    """
    pattern = _LOCAL_IMAGE if settings.local_mode else _IMMUTABLE_IMAGE
    return bool(pattern.fullmatch(image))


def protected_image(settings: Settings, workflow: str) -> str | None:
    """Return the deployment-owned immutable profile for a workflow.

    Args:
        settings: Trusted backend configuration.
        workflow: DSPy or Anything execution family.

    Returns:
        The pinned image reference when configured, otherwise None. Local
        mode uses one local image for every workflow.
    """
    if settings.local_mode:
        image = settings.local_sandbox_image
    else:
        image = settings.dspy_sandbox_image if workflow == "dspy" else settings.vercel_sandbox_image
    return image if image and _immutable(settings, image) else None


def protected_vercel_unavailable_reason(settings: Settings, workflow: str) -> str | None:
    """Check required provider access and an immutable offline workload image.

    Args:
        settings: Trusted deployment configuration.
        workflow: Execution family selecting its dependency profile.

    Returns:
        A configuration reason, or None when real setup verification may proceed.
        Local mode reports the local Docker daemon and image instead of Vercel.
    """
    if settings.local_mode:
        return docker_sandbox_unavailable_reason(settings)
    reason = sandbox_unavailable_reason(settings)
    if reason:
        return reason
    if protected_image(settings, workflow) is None:
        return "This deployment needs a pinned sandbox image with the optimizer dependencies installed."
    return None


def runtime_cost_profile(settings: Settings, workflow: str, runtime: str) -> dict[str, Any]:
    """Describe the selected sandbox's incremental user-funded cost category.

    Args:
        settings: Trusted deployment configuration.
        workflow: DSPy or Anything execution family.
        runtime: Managed sandbox identity.

    Returns:
        Machine-readable session bounds, including the usage markup. A local
        Docker box costs nothing, so its bounds are zero.
    """
    if runtime == "docker":
        return {
            "billing_basis": "at_cost",
            "minimum_session_cents": "0",
            "maximum_session_cents": "0",
            "maximum_lifetime_seconds": None,
            "vcpus": 2,
        }
    image = protected_image(settings, workflow)
    if runtime != "vercel" or image is None:
        return {
            "billing_basis": "at_cost",
            "minimum_session_cents": None,
            "maximum_session_cents": None,
            "maximum_lifetime_seconds": None,
            "vcpus": 2,
        }
    lifetime = min(settings.vercel_sandbox_max_lifetime_seconds, VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS)
    request = {
        "image": image,
        "lifetime_ms": max(1, math.ceil(lifetime * 1000)),
        "vcpus": 2,
        "network_disabled": True,
        "ports": [],
        "persistent": False,
    }
    minimum, maximum = vercel_sandbox_cost_range(request)
    return {
        "billing_basis": "at_cost",
        "minimum_session_cents": str(minimum),
        "maximum_session_cents": str(maximum),
        "maximum_lifetime_seconds": lifetime,
        "vcpus": 2,
    }


def run_call_recorder(gateway: ModelGateway, owner_id: str) -> CallRecorder | None:
    """Return the recorder that stores each relay call of one optimization run.

    Args:
        gateway: Generation-fenced model and spend authority of the run.
        owner_id: The job id; only the run phase uses the job id as owner.

    Returns:
        A recorder for the run with no candidate or role yet, or None for
        setup and interaction phases, which have no run record.
    """
    if gateway.runtime.phase != "run":
        return None
    # The worker builds the gateway from the job store's engine, and engine.py
    # passes no store here, so the run-call store reuses the ledger's engine.
    return CallRecorder(PostgresRunCallStore(gateway.runtime.service.engine), owner_id, "", "")


def bind_protected_sandbox(
    gateway: ModelGateway,
    settings: Settings,
    *,
    workflow: str,
    owner_id: str,
    lifetime_seconds: int | None = None,
    parent_hosts: tuple[str, ...] = (),
    image: str | None = None,
) -> dict[str, Any]:
    """Keep provider credentials, fixed resource profiles, and metering in the parent.

    Args:
        gateway: Existing generation-fenced model and spend authority.
        settings: Trusted Vercel account and deployment image configuration,
            or the local Docker image in local mode.
        workflow: Execution family selecting its immutable prebuilt image.
        owner_id: Stable job or setup identity used for cleanup after interruption.
        lifetime_seconds: Optional shorter ceiling for one bounded interaction.
        parent_hosts: Package registries the parent's own repository scoring
            box may reach during setup; empty when the run opens no such box.
        image: The immutable image the run is pinned to; the deployment's
            current image when None.

    Returns:
        Non-secret deployment identity usable in setup evidence.

    Raises:
        UnpricedOperationError: When the protected runtime cannot be configured.
    """
    reason = protected_vercel_unavailable_reason(settings, workflow)
    if reason:
        raise UnpricedOperationError(reason)
    if image is None:
        image = protected_image(settings, workflow)
    elif not _immutable(settings, image):
        raise UnpricedOperationError("The run's pinned sandbox image is not an immutable reference.")
    assert image is not None
    configured_lifetime = settings.vercel_sandbox_max_lifetime_seconds
    lifetime = min(
        lifetime_seconds or configured_lifetime, configured_lifetime, VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS
    )
    runtime: DockerSandboxRuntime | VercelSandboxRuntime
    if settings.local_mode:
        runtime = DockerSandboxRuntime(
            image,
            docker_bin=settings.docker_bin,
            budget=gateway.runtime,
            recorder=run_call_recorder(gateway, owner_id),
        )
    else:
        assert settings.vercel_token is not None
        runtime = VercelSandboxRuntime(
            VercelCredentials(
                token=settings.vercel_token.get_secret_value(),
                team_id=str(settings.vercel_team_id),
                project_id=str(settings.vercel_project_id),
            ),
            image=image,
            budget=gateway.runtime,
        )
    mailbox = ModelMailbox(gateway.dispatch_guest)
    broker = SandboxBroker(
        runtime,
        image=image,
        max_lifetime_seconds=lifetime,
        tags={JOB_TAG: owner_id},
        command_runner=mailbox.run,
        parent_command_runner=ModelMailbox(gateway.dispatch_route).run,
    )
    gateway.bind_sandbox(broker, image=image, lifetime_seconds=lifetime)
    if parent_hosts:
        gateway.fund_parent_sandbox(parent_hosts)
    return {"image": image, "lifetime_seconds": lifetime}

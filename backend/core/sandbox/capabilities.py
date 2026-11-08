"""Provider capability flags and the spec checks that use them.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the flag set comes
from ``harbor/environments/capabilities.py`` and the checks from
``harbor/environments/resource_policies.py``. Changes: a frozen dataclass
replaces the Pydantic models, the flags add Skynet's header injection,
protection, billing and Vercel size limits, and ``validate_spec`` takes over
the inline checks in the blackbox ``VercelSandboxRuntime.open``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum

from .resources import ResourceMode, ResourceRequest, SandboxSpec


class NetworkMode(StrEnum):
    """Name an egress policy that a provider can apply."""

    OPEN = "open"
    DENY_ALL = "deny_all"
    ALLOWLIST = "allowlist"


class SandboxSpecError(ValueError):
    """Signal a sandbox spec that the provider cannot open."""


@dataclass(frozen=True)
class EnvironmentCapabilities:
    """Describe what one sandbox provider supports.

    ``None`` for a size limit means the provider sets no limit.
    ``even_vcpus`` allows 1 vCPU or an even count, as Vercel requires.
    """

    name: str
    injects_headers: bool = False
    protected: bool = False
    billable: bool = False
    network_modes: frozenset[NetworkMode] = frozenset({NetworkMode.OPEN})
    dynamic_network_policy: bool = False
    snapshots: bool = False
    gpus: bool = False
    max_vcpus: int | None = None
    even_vcpus: bool = False
    memory_mb_per_vcpu: int | None = None
    max_disk_mb: int | None = None


# Vercel Sandbox: 2048 MB of memory per vCPU, 32 vCPU and 32 GB of disk at most.
# A protected runtime (budget broker) copies this with protected=True and
# injects_headers=False.
VERCEL_CAPABILITIES = EnvironmentCapabilities(
    name="vercel",
    injects_headers=True,
    billable=True,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
    snapshots=True,
    max_vcpus=32,
    even_vcpus=True,
    memory_mb_per_vcpu=2048,
    max_disk_mb=32 * 1024,
)


def validate_spec(spec: SandboxSpec, caps: EnvironmentCapabilities) -> None:
    """Reject a spec that the provider with these capabilities cannot open.

    Args:
        spec: The sandbox spec to check.
        caps: The capabilities of the target provider.

    Raises:
        SandboxSpecError: The spec breaks a lifetime, size or network rule.
    """
    if not math.isfinite(spec.lifetime_seconds) or spec.lifetime_seconds <= 0:
        raise SandboxSpecError("The sandbox lifetime must be a finite, positive number of seconds.")
    _validate_resources(spec.resources, caps)
    _validate_network(spec, caps)


def resolve_resources(request: ResourceRequest, caps: EnvironmentCapabilities) -> ResourceRequest:
    """Fill the memory size that the provider derives from the vCPU count.

    Args:
        request: A request that passed ``validate_spec``.
        caps: The capabilities of the target provider.

    Returns:
        The request with ``memory_mb`` set when the provider ties memory to vCPUs.
    """
    if request.mode is ResourceMode.IGNORE or caps.memory_mb_per_vcpu is None or request.memory_mb is not None:
        return request
    return replace(request, memory_mb=request.cpus * caps.memory_mb_per_vcpu)


def _validate_resources(request: ResourceRequest, caps: EnvironmentCapabilities) -> None:
    """Check the requested sizes against the provider limits.

    Args:
        request: The requested sizes.
        caps: The capabilities of the target provider.

    Raises:
        SandboxSpecError: A size is invalid or above a provider limit.
    """
    if not _is_whole(request.gpu) or request.gpu < 0:
        raise SandboxSpecError("The sandbox must use a whole, non-negative number of GPUs.")
    if request.gpu and not caps.gpus:
        raise SandboxSpecError(f"The {caps.name} sandbox does not support GPUs.")
    if request.mode is ResourceMode.IGNORE:
        return
    cpus = request.cpus
    if not _is_whole(cpus) or cpus < 1:
        raise SandboxSpecError("The sandbox must use a whole, positive number of vCPUs.")
    if caps.max_vcpus is not None and cpus > caps.max_vcpus:
        raise SandboxSpecError(f"The {caps.name} sandbox allows at most {caps.max_vcpus} vCPUs.")
    if caps.even_vcpus and cpus > 1 and cpus % 2:
        raise SandboxSpecError("The sandbox must use 1 or an even number of vCPUs.")
    if request.memory_mb is not None:
        if not _is_whole(request.memory_mb) or request.memory_mb <= 0:
            raise SandboxSpecError("The sandbox memory must be a whole, positive number of MB.")
        if caps.memory_mb_per_vcpu is not None and request.memory_mb > cpus * caps.memory_mb_per_vcpu:
            raise SandboxSpecError(
                f"The {caps.name} sandbox allows at most {caps.memory_mb_per_vcpu} MB of memory per vCPU."
            )
    if request.disk_mb is not None:
        if not _is_whole(request.disk_mb) or request.disk_mb <= 0:
            raise SandboxSpecError("The sandbox disk must be a whole, positive number of MB.")
        if caps.max_disk_mb is not None and request.disk_mb > caps.max_disk_mb:
            raise SandboxSpecError(f"The {caps.name} sandbox allows at most {caps.max_disk_mb} MB of disk.")


def _is_whole(value: object) -> bool:
    """Return True when the value is an int and not a bool.

    Args:
        value: The requested size.

    Returns:
        True for a plain integer.
    """
    # bool is an int subclass; True must not pass as a size of 1.
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_network(spec: SandboxSpec, caps: EnvironmentCapabilities) -> None:
    """Check the egress policy and header injection against the provider.

    Args:
        spec: The sandbox spec to check.
        caps: The capabilities of the target provider.

    Raises:
        SandboxSpecError: The policy is contradictory or not supported.
    """
    if spec.network_disabled and (spec.inject_headers or spec.allowed_hosts):
        raise SandboxSpecError("A sandbox with no network cannot allow hosts or inject headers.")
    # An empty allowlist means open egress, which reaches every injection host.
    if spec.allowed_hosts and not set(spec.inject_headers) <= set(spec.allowed_hosts):
        raise SandboxSpecError("Header injection is allowed only for hosts in allowed_hosts.")
    if spec.inject_headers and not caps.injects_headers:
        raise SandboxSpecError(f"The {caps.name} sandbox does not inject egress headers.")
    mode = network_mode(disabled=spec.network_disabled, allowed_hosts=spec.allowed_hosts)
    if mode not in caps.network_modes:
        raise SandboxSpecError(f"The {caps.name} sandbox does not support the {mode.value} network mode.")


def network_mode(*, disabled: bool, allowed_hosts: tuple[str, ...]) -> NetworkMode:
    """Return the network mode that a policy needs.

    Args:
        disabled: True when the sandbox has no egress.
        allowed_hosts: The hosts that egress may reach; empty means any host.

    Returns:
        The matching network mode.
    """
    if disabled:
        return NetworkMode.DENY_ALL
    return NetworkMode.ALLOWLIST if allowed_hosts else NetworkMode.OPEN

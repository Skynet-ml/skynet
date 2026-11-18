"""Pick the sandbox provider of one box from its grants and the Vercel quota (SKY-732).

Vercel is the default provider. A ``gpu:<type>`` grant places the box on
Modal, because Vercel has no GPU. When the Vercel box count is at its cap, the
box spills to a second provider. A GPU grant is not a network grant:
:func:`network_grants` removes it before the grant list resolves to a network
policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .grants import Grant, GrantError

VERCEL = "vercel"
MODAL = "modal"
GPU_PREFIX = "gpu:"


@dataclass(frozen=True)
class Quota:
    """Hold the Vercel box cap, the open Vercel boxes and the provider that takes the spill."""

    vercel_cap: int
    vercel_in_use: int
    spill: str = MODAL


@dataclass(frozen=True)
class Placement:
    """Name the provider of one box and its GPU type, if any."""

    provider: str
    gpu: str | None = None


def gpu_type(grants: Sequence[Grant]) -> str | None:
    """Return the GPU type that the grants ask for.

    Args:
        grants: The box's grants.

    Returns:
        The type of the one ``gpu:<type>`` grant, or ``None`` when no grant asks for a GPU.

    Raises:
        GrantError: When a GPU grant has no type, carries a credential, or two grants ask for different types.
    """
    types = set()
    for grant in grants:
        if not _is_gpu(grant):
            continue
        kind = grant.host[len(GPU_PREFIX) :].strip().lower()
        if not kind or grant.credential:
            raise GrantError(f"Grant {grant.host!r} must be gpu:<type>, without a credential.")
        types.add(kind)
    if len(types) > 1:
        raise GrantError("A box can use only one GPU type.")
    return types.pop() if types else None


def network_grants(grants: Sequence[Grant]) -> tuple[Grant, ...]:
    """Return the grants without the GPU grants, for the network policy.

    Args:
        grants: The box's grants.

    Returns:
        The host grants, in their order.
    """
    return tuple(grant for grant in grants if not _is_gpu(grant))


def _is_gpu(grant: Grant) -> bool:
    """Return whether a grant asks for a GPU.

    Args:
        grant: One grant.

    Returns:
        True for a ``gpu:<type>`` grant. A host that is not text is a network
        grant here, so the grant resolution refuses it with GrantError.
    """
    return isinstance(grant.host, str) and grant.host.startswith(GPU_PREFIX)


def choose(grants: Sequence[Grant], quota: Quota) -> Placement:
    """Pick the provider of one box.

    Args:
        grants: The box's grants.
        quota: The Vercel cap, the open Vercel boxes and the spill provider.

    Returns:
        Modal with the GPU type for a GPU grant, the spill provider when Vercel
        is at its cap, and Vercel otherwise.

    Raises:
        GrantError: When the GPU grants are not valid.
    """
    gpu = gpu_type(grants)
    if gpu is not None:
        return Placement(MODAL, gpu)
    if quota.vercel_in_use >= quota.vercel_cap:
        return Placement(quota.spill)
    return Placement(VERCEL)

"""Deny-by-default sandbox grants, resolved against the workspace capability registry.

The registry maps a host to the headers that the provider edge injects into
egress requests for that host (SKY-714). A box gets only the hosts that its
grant list names: an empty list resolves to no network, and only the explicit
``OPEN_EGRESS`` grant resolves to open egress. A grant with ``credential=True``
takes the registry headers of its host; a plain grant opens the host and
injects nothing. Credential values stay in the control plane: the provider
edge adds them, and neither the box nor the grant-list id sees them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from core.billing.operation_pricing import json_fingerprint
from core.models.blackbox import REPO_SECRET_HOST_PATTERN

from .capabilities import NetworkMode, SandboxSpecError, network_mode
from .resources import SandboxSpec

# The one grant host that opens all egress. It cannot carry a credential.
OPEN_EGRESS = "*"


class GrantError(SandboxSpecError):
    """Signal a grant list that cannot resolve to a sandbox policy."""


class UnknownCapabilityError(GrantError):
    """Signal a credential grant for a host that the registry does not hold."""

    def __init__(self, host: str) -> None:
        """Keep the host that has no registry entry.

        Args:
            host: The normalized host of the grant.
        """
        super().__init__(f"No workspace capability holds a credential for host '{host}'.")
        self.host = host


@dataclass(frozen=True)
class Grant:
    """Allow a box to reach one host, with or without the registry credential."""

    host: str
    credential: bool = False


# A metric box scores a candidate and needs no network unless a grant says so.
METRIC_BOX_GRANTS: tuple[Grant, ...] = ()


@dataclass(frozen=True)
class ResolvedGrants:
    """Hold the egress policy that one grant list resolves to.

    ``inject_headers`` holds credential values, so it stays out of ``repr``.
    """

    grant_list_id: str
    mode: NetworkMode
    allowed_hosts: tuple[str, ...] = ()
    inject_headers: Mapping[str, Mapping[str, str]] = field(default_factory=dict, repr=False)

    def apply(self, spec: SandboxSpec) -> SandboxSpec:
        """Replace the egress fields of a spec with this policy.

        Args:
            spec: The sandbox spec to open.

        Returns:
            The spec with this policy's network mode, hosts and headers.
        """
        return replace(
            spec,
            network_disabled=self.mode is NetworkMode.DENY_ALL,
            allowed_hosts=self.allowed_hosts,
            inject_headers=self.inject_headers,
        )


def resolve_grants(grants: Iterable[Grant], registry: Mapping[str, Mapping[str, str]]) -> ResolvedGrants:
    """Resolve a grant list against the workspace registry.

    Args:
        grants: The grants of one box, in any order.
        registry: Maps each capability host to the headers that the edge
            injects for it.

    Returns:
        The network mode, allowed hosts, injected headers and grant-list id.

    Raises:
        GrantError: A host is not a valid host, or ``OPEN_EGRESS`` asks for
            a credential.
        UnknownCapabilityError: A credential grant names a host that the
            registry does not hold.
    """
    wanted = _merge(grants)
    grant_list_id = json_fingerprint(sorted([host, credential] for host, credential in wanted.items()))
    if not wanted:
        return ResolvedGrants(grant_list_id=grant_list_id, mode=NetworkMode.DENY_ALL)
    capabilities = _clean_registry(registry)
    headers: dict[str, Mapping[str, str]] = {}
    for host, credential in wanted.items():
        if not credential:
            continue
        if host not in capabilities:
            raise UnknownCapabilityError(host)
        headers[host] = capabilities[host]
    # An open grant leaves the allowlist empty, which is how a spec says open egress.
    hosts = () if OPEN_EGRESS in wanted else tuple(sorted(wanted))
    return ResolvedGrants(
        grant_list_id=grant_list_id,
        mode=network_mode(disabled=False, allowed_hosts=hosts),
        allowed_hosts=hosts,
        inject_headers=MappingProxyType(headers),
    )


def _merge(grants: Iterable[Grant]) -> dict[str, bool]:
    """Normalize the grant hosts and merge repeated hosts.

    Args:
        grants: The grants as given.

    Returns:
        Maps each normalized host to True when any of its grants asks for
        the credential.

    Raises:
        GrantError: A host is invalid, or ``OPEN_EGRESS`` asks for a credential.
    """
    wanted: dict[str, bool] = {}
    for grant in grants:
        if grant.host == OPEN_EGRESS:
            if grant.credential:
                raise GrantError("The open egress grant cannot carry a credential.")
            host = OPEN_EGRESS
        else:
            host = _clean_host(grant.host)
        wanted[host] = wanted.get(host, False) or grant.credential
    return wanted


def _clean_registry(registry: Mapping[str, Mapping[str, str]]) -> dict[str, Mapping[str, str]]:
    """Normalize the registry hosts so that they match the grant hosts.

    Args:
        registry: Maps each capability host to its injected headers.

    Returns:
        The same entries under normalized hosts, with read-only headers.

    Raises:
        GrantError: A registry host is invalid, or two hosts normalize to one.
    """
    cleaned: dict[str, Mapping[str, str]] = {}
    for host, headers in registry.items():
        key = _clean_host(host)
        if key in cleaned:
            raise GrantError(f"The workspace registry holds host '{key}' more than once.")
        cleaned[key] = MappingProxyType(dict(headers))
    return cleaned


def _clean_host(host: object) -> str:
    """Normalize one host as a run's ``allowed_hosts`` does, and refuse a bad one.

    Args:
        host: An exact host or a ``*.suffix`` pattern.

    Returns:
        The lower-case host without surrounding space or a trailing dot.

    Raises:
        GrantError: The value is not an exact host or ``*.`` and a suffix of
            two or more labels; ports, paths and schemes are refused.
    """
    cleaned = host.strip().rstrip(".").lower() if isinstance(host, str) else ""
    if not cleaned or len(cleaned) > 253 or not REPO_SECRET_HOST_PATTERN.match(cleaned):
        raise GrantError(f"Grant host {host!r} must be a host such as api.example.com, or *.example.com.")
    return cleaned

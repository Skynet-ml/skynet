"""Deny-by-default sandbox grants, resolved against the workspace capability registry.

The registry maps a host and a URL path prefix to the headers that the
provider edge injects into egress requests for that host under that prefix
(SKY-714, SKY-833). A box gets only the hosts that its grant list names: an
empty list resolves to no network, and only the explicit ``OPEN_EGRESS`` grant
resolves to open egress. A grant with ``credential=True`` takes the registry
headers of its host and path prefix; a plain grant opens the host and injects
nothing. Credential values stay in the control plane: the provider edge adds
them, and neither the box nor the grant-list id sees them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import Field, dataclass, field, replace
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, TypeVar

from core.billing.operation_pricing import json_fingerprint
from core.models.blackbox import REPO_SECRET_HOST_PATTERN

from .capabilities import NetworkMode, SandboxSpecError, network_mode

# The one grant host that opens all egress. It cannot carry a credential.
OPEN_EGRESS = "*"
# The path prefix that covers every path of a host.
ROOT_PATH = "/"
# RFC 3986 path segment characters only: no query, fragment, space or wildcard reaches an edge path matcher.
_PATH_PATTERN = re.compile(r"(?:/[A-Za-z0-9._~!$&'()+,;=:@%-]+)*/?")

# Host -> path prefix -> the headers that the edge adds to requests under that prefix.
HeaderRules = Mapping[str, Mapping[str, Mapping[str, str]]]


class _EgressSpec(Protocol):
    """Match a frozen spec dataclass with the egress fields that a grant policy replaces.

    Both ``core.sandbox.resources.SandboxSpec`` and the blackbox contract's
    ``SandboxSpec`` match, so :meth:`ResolvedGrants.apply` keeps the type it gets.
    """

    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    @property
    def network_disabled(self) -> bool:
        """Return whether the box has no network."""
        ...

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """Return the only hosts the box may reach; empty means any host."""
        ...

    @property
    def inject_headers(self) -> HeaderRules:
        """Return the headers the edge adds to requests for each host and path prefix."""
        ...


SpecT = TypeVar("SpecT", bound=_EgressSpec)


class GrantError(SandboxSpecError):
    """Signal a grant list that cannot resolve to a sandbox policy."""


class UnknownCapabilityError(GrantError):
    """Signal a credential grant for a host and path prefix that the registry does not hold."""

    def __init__(self, host: str, path: str = ROOT_PATH) -> None:
        """Keep the host and path prefix that have no registry entry.

        Args:
            host: The normalized host of the grant.
            path: The normalized path prefix of the grant.
        """
        where = f"host '{host}'" if path == ROOT_PATH else f"host '{host}' and path prefix '{path}'"
        super().__init__(f"No workspace capability holds a credential for {where}.")
        self.host = host
        self.path = path


@dataclass(frozen=True)
class Grant:
    """Allow a box to reach one host, with or without the registry credential.

    ``path`` is the URL path prefix of a credential grant: the edge adds the
    credential only to requests under it. A plain grant ignores it.
    """

    host: str
    credential: bool = False
    path: str = ROOT_PATH


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
    inject_headers: HeaderRules = field(default_factory=dict, repr=False)

    def apply(self, spec: SpecT) -> SpecT:
        """Replace the egress fields of a spec with this policy.

        Args:
            spec: The sandbox spec to open: this package's spec or the
                blackbox contract's spec.

        Returns:
            The spec, of the same type, with this policy's network mode, hosts and headers.
        """
        return replace(
            spec,
            network_disabled=self.mode is NetworkMode.DENY_ALL,
            allowed_hosts=self.allowed_hosts,
            inject_headers=self.inject_headers,
        )


def resolve_grants(grants: Iterable[Grant], registry: HeaderRules) -> ResolvedGrants:
    """Resolve a grant list against the workspace registry.

    Args:
        grants: The grants of one box, in any order.
        registry: Maps each capability host and path prefix to the headers
            that the edge injects for requests under that prefix.

    Returns:
        The network mode, allowed hosts, injected headers and grant-list id.

    Raises:
        GrantError: A host or path is not valid, ``OPEN_EGRESS`` asks for a
            credential, or one host has more than one credential path prefix.
        UnknownCapabilityError: A credential grant names a host and path
            prefix that the registry does not hold.
    """
    wanted = merge_grants(grants)
    # A plain host keeps its old [host, False] entry, so the ids of grant lists with no credential do not change.
    grant_list_id = json_fingerprint(sorted([host, sorted(paths) or False] for host, paths in wanted.items()))
    if not wanted:
        return ResolvedGrants(
            grant_list_id=grant_list_id, mode=NetworkMode.DENY_ALL, inject_headers=MappingProxyType({})
        )
    capabilities = _clean_registry(registry)
    headers: dict[str, Mapping[str, Mapping[str, str]]] = {}
    for host, paths in wanted.items():
        if not paths:
            continue
        # A box can send '/a/../b' or '/a/%2e%2e/b': the edge matches prefix '/a', and a server that
        # normalizes the path gives A's headers to server B. So one host takes one prefix (SKY-833).
        if len(paths) > 1:
            listed = ", ".join(f"'{path}'" for path in sorted(paths))
            raise GrantError(
                f"Host '{host}' has credential grants for more than one path prefix ({listed}); "
                "one host takes one credential prefix."
            )
        for path in paths:
            if path not in capabilities.get(host, {}):
                raise UnknownCapabilityError(host, path)
        headers[host] = MappingProxyType({path: capabilities[host][path] for path in sorted(paths)})
    # An open grant leaves the allowlist empty, which is how a spec says open egress.
    hosts = () if OPEN_EGRESS in wanted else tuple(sorted(wanted))
    return ResolvedGrants(
        grant_list_id=grant_list_id,
        mode=network_mode(disabled=False, allowed_hosts=hosts),
        allowed_hosts=hosts,
        inject_headers=MappingProxyType(headers),
    )


def merge_grants(grants: Iterable[Grant]) -> dict[str, frozenset[str]]:
    """Normalize the grant hosts and paths, and merge repeated hosts.

    Args:
        grants: The grants as given.

    Returns:
        Maps each normalized host to the path prefixes of its credential
        grants; an empty set means a plain host.

    Raises:
        GrantError: A host or path is invalid, or ``OPEN_EGRESS`` asks for a credential.
    """
    wanted: dict[str, frozenset[str]] = {}
    for grant in grants:
        if grant.host == OPEN_EGRESS:
            if grant.credential:
                raise GrantError("The open egress grant cannot carry a credential.")
            host = OPEN_EGRESS
        else:
            host = _clean_host(grant.host)
        paths = frozenset({_clean_path(grant.path)}) if grant.credential else frozenset()
        wanted[host] = wanted.get(host, frozenset()) | paths
    return wanted


def _clean_registry(registry: HeaderRules) -> dict[str, dict[str, Mapping[str, str]]]:
    """Normalize the registry hosts and paths so that they match the grants.

    Args:
        registry: Maps each capability host and path prefix to its injected headers.

    Returns:
        The same entries under normalized hosts and paths, with read-only headers.

    Raises:
        GrantError: A registry host or path is invalid, or two entries normalize to one.
    """
    cleaned: dict[str, dict[str, Mapping[str, str]]] = {}
    for host, prefixes in registry.items():
        key = _clean_host(host)
        if key in cleaned:
            raise GrantError(f"The workspace registry holds host '{key}' more than once.")
        cleaned[key] = {}
        for path, headers in prefixes.items():
            prefix = _clean_path(path)
            if prefix in cleaned[key]:
                raise GrantError(f"The workspace registry holds path prefix '{prefix}' of host '{key}' more than once.")
            cleaned[key][prefix] = MappingProxyType(dict(headers))
    return cleaned


def _clean_path(path: object) -> str:
    """Normalize one URL path prefix, and refuse a bad one.

    Args:
        path: The path of a server URL, such as ``/mcp`` or ``/mcp/``; empty means the root.

    Returns:
        The path without a trailing slash, or ``/`` for the root.

    Raises:
        GrantError: The value is not a plain URL path: a query, fragment,
            space, wildcard, dot segment or non-ASCII text is refused.
    """
    if not isinstance(path, str) or not _PATH_PATTERN.fullmatch(path or "/"):
        raise GrantError(f"Grant path {path!r} must be a URL path such as /mcp.")
    # A dot segment names another path once the server resolves it, so it cannot bound a credential.
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise GrantError(f"Grant path {path!r} must not hold a '.' or '..' segment.")
    return path.rstrip("/") or ROOT_PATH


def _clean_host(host: object) -> str:
    """Normalize one host as a run's ``allowed_hosts`` does, and refuse a bad one.

    Args:
        host: An exact host or a ``*.suffix`` pattern.

    Returns:
        The lower-case host without surrounding space or a trailing dot.

    Raises:
        GrantError: The value is not an exact host or ``*.`` and a suffix of
            two or more labels; ports, paths, schemes and non-ASCII text are
            refused.
    """
    # str.lower() and str.strip() turn some non-ASCII text into a valid ASCII
    # host (the Kelvin sign into "k", a no-break space into nothing).
    cleaned = host.strip().rstrip(".").lower() if isinstance(host, str) and host.isascii() else ""
    if not cleaned or len(cleaned) > 253 or not REPO_SECRET_HOST_PATTERN.fullmatch(cleaned):
        raise GrantError(f"Grant host {host!r} must be a host such as api.example.com, or *.example.com.")
    return cleaned

"""Egress policies for each sandbox phase, with credential rules.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the policy modes
come from ``harbor/environments/base.py``, the phase context manager from
``_phase_network_policy`` in ``harbor/trial.py``, and the allowlist merge
notices from ``merge_extra_allowlists``. Changes: the modes reuse Skynet's
``NetworkMode``, a credential rule matches only the method and a path prefix,
credential rules are allowed with open egress (blackbox parity), and every
policy change must come back confirmed by the provider or the phase fails.
The model holds no provider types; providers implement ``PolicyApplier``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from .capabilities import NetworkMode, SandboxSpecError


class Phase(StrEnum):
    """Name a sandbox phase that can have its own egress policy."""

    SETUP = "setup"
    RUN = "run"
    SCORE = "score"


class NetworkPolicyError(RuntimeError):
    """Signal a policy change that the provider did not confirm."""


@dataclass(frozen=True)
class RequestMatch:
    """Limit a credential rule to some requests to its host.

    ``None`` in a field matches every request.
    """

    method: str | None = None
    path_prefix: str | None = None

    def __post_init__(self) -> None:
        """Reject a path prefix that is not an absolute path.

        Raises:
            SandboxSpecError: The path prefix does not start with ``/``.
        """
        if self.path_prefix is not None and not self.path_prefix.startswith("/"):
            raise SandboxSpecError("A credential rule path prefix must start with '/'.")


@dataclass(frozen=True)
class CredentialRule:
    """Add headers at the network edge to egress requests for one host.

    The secret stays outside the sandbox; only the provider edge sees it.
    """

    host: str
    headers: Mapping[str, str]
    match: RequestMatch | None = None


@dataclass(frozen=True)
class EgressPolicy:
    """Describe the egress of a sandbox for one phase.

    ``OPEN`` reaches every host and takes no ``allow_hosts``. ``ALLOWLIST``
    reaches only ``allow_hosts``. ``DENY_ALL`` reaches nothing.
    """

    mode: NetworkMode
    allow_hosts: tuple[str, ...] = ()
    credential_rules: tuple[CredentialRule, ...] = ()

    def __post_init__(self) -> None:
        """Reject a contradictory policy.

        Raises:
            SandboxSpecError: The hosts or credential rules do not fit the mode.
        """
        if self.mode is NetworkMode.DENY_ALL and (self.allow_hosts or self.credential_rules):
            raise SandboxSpecError("A sandbox with no network cannot allow hosts or inject headers.")
        if self.mode is NetworkMode.OPEN and self.allow_hosts:
            raise SandboxSpecError("Open egress reaches every host, so it takes no allowlist.")
        if self.mode is NetworkMode.ALLOWLIST:
            if not self.allow_hosts:
                raise SandboxSpecError("An allowlist policy needs at least one host.")
            # Same rule as the blackbox VercelSandboxRuntime.open subset check.
            if not {rule.host for rule in self.credential_rules} <= set(self.allow_hosts):
                raise SandboxSpecError("Header injection is allowed only for hosts in allow_hosts.")

    @property
    def has_credentials(self) -> bool:
        """Return True when any credential rule is set (D5: never snapshot such a box)."""
        return bool(self.credential_rules)


class PolicyApplier(Protocol):
    """Apply an egress policy to one running sandbox."""

    async def apply_policy(self, policy: EgressPolicy) -> EgressPolicy | None:
        """Send the policy to the provider and read back what it applies.

        Args:
            policy: The policy to apply.

        Returns:
            The policy that the provider reports, or ``None`` when it cannot
            report one.
        """
        ...


@dataclass(frozen=True)
class NetworkPlan:
    """Hold the egress policy of each phase."""

    setup: EgressPolicy
    run: EgressPolicy
    score: EgressPolicy

    def for_phase(self, phase: Phase) -> EgressPolicy:
        """Return the policy of one phase.

        Args:
            phase: The phase.

        Returns:
            The policy for that phase.
        """
        return {Phase.SETUP: self.setup, Phase.RUN: self.run, Phase.SCORE: self.score}[phase]


class PhaseNetwork:
    """Switch a sandbox to the policy of each phase and back again."""

    def __init__(self, applier: PolicyApplier, plan: NetworkPlan, current: EgressPolicy) -> None:
        """Keep the applier, the plan and the policy that the sandbox has now.

        Args:
            applier: Applies policies to the sandbox.
            plan: The policy of each phase.
            current: The policy that the sandbox applies now.
        """
        self._applier = applier
        self._plan = plan
        self._current = current

    @property
    def current(self) -> EgressPolicy:
        """Return the last policy that the provider confirmed."""
        return self._current

    @asynccontextmanager
    async def phase_policy(self, phase: Phase) -> AsyncGenerator[EgressPolicy, None]:
        """Apply the policy of a phase, then restore the old policy.

        The old policy comes back on exit, also when the block raises.

        Args:
            phase: The phase to enter.

        Yields:
            The policy of the phase.

        Raises:
            NetworkPolicyError: The provider did not confirm a policy change.
        """
        previous = self._current
        policy = self._plan.for_phase(phase)
        await self._apply(policy)
        try:
            yield policy
        finally:
            await self._apply(previous)

    async def _apply(self, policy: EgressPolicy) -> None:
        """Apply a policy and fail closed unless the provider confirms it.

        Args:
            policy: The policy to apply.

        Raises:
            NetworkPolicyError: The provider reported no policy or another policy.
        """
        if policy == self._current:
            return
        confirmed = await self._applier.apply_policy(policy)
        if confirmed != policy:
            raise NetworkPolicyError(f"The sandbox did not confirm the {policy.mode.value} network policy.")
        self._current = policy


class NoticeKind(StrEnum):
    """Say what ``merge_allowlists`` did with one extra host."""

    ADDED = "added"
    DUPLICATE = "duplicate"
    OPEN_EGRESS = "open_egress"
    NO_NETWORK = "no_network"
    EMPTY = "empty"


@dataclass(frozen=True)
class AllowlistNotice:
    """Report one extra host that the merge added or dropped."""

    host: str
    kind: NoticeKind

    @property
    def dropped(self) -> bool:
        """Return True when the merge did not add the host."""
        return self.kind is not NoticeKind.ADDED


@dataclass(frozen=True)
class AllowlistMerge:
    """Hold the merged policy and one notice for each extra host."""

    policy: EgressPolicy
    notices: tuple[AllowlistNotice, ...] = ()

    @property
    def hosts(self) -> tuple[str, ...]:
        """Return the merged allowlist."""
        return self.policy.allow_hosts


def merge_allowlists(policy: EgressPolicy, *extra: Iterable[str]) -> AllowlistMerge:
    """Add extra hosts to an allowlist policy and report each host.

    Open egress already reaches every host and no network must stay closed,
    so both modes drop every extra host. Hosts are stripped and lowercased.

    Args:
        policy: The policy to extend.
        *extra: Groups of extra hosts, for example from a task and a tool.

    Returns:
        The merged policy and one notice for each extra host, in input order.
    """
    hosts = list(policy.allow_hosts)
    notices: list[AllowlistNotice] = []
    for group in extra:
        for raw in group:
            host = raw.strip().lower()
            if not host:
                kind = NoticeKind.EMPTY
            elif policy.mode is NetworkMode.OPEN:
                kind = NoticeKind.OPEN_EGRESS
            elif policy.mode is NetworkMode.DENY_ALL:
                kind = NoticeKind.NO_NETWORK
            elif host in hosts:
                kind = NoticeKind.DUPLICATE
            else:
                kind = NoticeKind.ADDED
                hosts.append(host)
            notices.append(AllowlistNotice(host=host or raw, kind=kind))
    merged = EgressPolicy(mode=policy.mode, allow_hosts=tuple(hosts), credential_rules=policy.credential_rules)
    return AllowlistMerge(policy=merged, notices=tuple(notices))


def inference_host(gateway_url: str) -> str:
    """Return the host that sandbox inference calls reach, from the gateway URL.

    Args:
        gateway_url: The model gateway base URL, for example ``https://gw.example.com/v1``.

    Returns:
        The lowercased host name, without the port.

    Raises:
        ValueError: The URL has no host.
    """
    host = urlsplit(gateway_url.strip()).hostname
    if not host:
        raise ValueError("The gateway URL has no host.")
    return host

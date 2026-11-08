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

import asyncio
import ipaddress
import re
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
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


# One DNS label in letter-digit-hyphen form, 1 to 63 characters, with no
# hyphen at either end. Every other character (comma, backslash, percent,
# underscore, a blank) is a parse risk at the provider, so it is refused.
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def _normalize_host(raw: str) -> str:
    """Strip and lowercase a host name, and drop one trailing dot.

    ``x.com.`` and ``x.com`` name the same host, so they must compare equal
    for the dedupe and the credential subset check, as in ``inference_host``.

    Args:
        raw: The host as the caller wrote it.

    Returns:
        The host without surrounding blanks or a trailing dot, in lowercase.
    """
    return raw.strip().lower().removesuffix(".")


def _is_valid_host(host: str) -> bool:
    """Return True when a normalized host is a bare host name or IP address.

    A leading ``*.`` label is a valid wildcard when two or more labels follow
    it, so ``*.com`` is refused. A bare ``*``, the unspecified addresses
    ``0.0.0.0``, ``::`` and ``::ffff:0.0.0.0``, an IPv6 scope ID, an empty label and any character
    outside letters, digits, hyphens and dots are not valid, because the
    provider could read them as "every host" or as a host that the caller did
    not mean. A name whose last label is all digits is refused, because some
    parsers read ``1.2.3`` as an IP address.

    Args:
        host: The normalized host.

    Returns:
        True when the host is valid.
    """
    if "%" in host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        mapped = getattr(address, "ipv4_mapped", None)
        return not (address.is_unspecified or (mapped is not None and mapped.is_unspecified))
    wildcard = host.startswith("*.")
    labels = host.removeprefix("*.").split(".")
    if len(host) > 253 or (wildcard and len(labels) < 2) or labels[-1].isdigit():
        return False
    return all(_LABEL.fullmatch(label) for label in labels)


@dataclass(frozen=True)
class RequestMatch:
    """Limit a credential rule to some requests to its host.

    ``None`` in a field matches every request.
    """

    method: str | None = None
    # A plain string prefix: the provider decides whether "/v1" also matches
    # "/v1beta", so segment-boundary matching belongs to the provider.
    path_prefix: str | None = None

    def __post_init__(self) -> None:
        """Uppercase the method and reject a path prefix that is not absolute.

        Raises:
            SandboxSpecError: The path prefix does not start with ``/``.
        """
        if self.method is not None:
            object.__setattr__(self, "method", self.method.upper())
        if self.path_prefix is not None and not self.path_prefix.startswith("/"):
            raise SandboxSpecError("A credential rule path prefix must start with '/'.")


@dataclass(frozen=True)
class CredentialRule:
    """Add headers at the network edge to egress requests for one host.

    The secret stays outside the sandbox; only the provider edge sees it.
    """

    host: str
    # Headers stay out of the hash because a mapping is not hashable; equal
    # rules still hash equal, so the hash stays consistent with ``==``. They
    # stay out of the repr so that a log line or traceback never holds a secret.
    headers: Mapping[str, str] = field(hash=False, repr=False)
    match: RequestMatch | None = None

    def __post_init__(self) -> None:
        """Normalize the host and freeze a copy of the headers.

        Raises:
            SandboxSpecError: The host is not a bare host name.
        """
        host = _normalize_host(self.host)
        if not _is_valid_host(host):
            raise SandboxSpecError(f"A credential rule needs a bare host name, not {self.host!r}.")
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


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
        """Normalize and dedupe the hosts, then reject a contradictory policy.

        Raises:
            SandboxSpecError: A host is not a bare host name, or the hosts or
                credential rules do not fit the mode.
        """
        hosts = tuple(dict.fromkeys(_normalize_host(host) for host in self.allow_hosts))
        for host in hosts:
            if not _is_valid_host(host):
                raise SandboxSpecError(f"An allowlist needs bare host names, not {host!r}.")
        object.__setattr__(self, "allow_hosts", hosts)
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
    """Switch a sandbox to the policy of each phase and back again.

    Phases nest in one task only, and exit in reverse order of entry. A
    phase that exits before a later phase sets the box to deny-all and
    raises. A phase from another task while a phase is
    active is refused, because the two restores would race and could leave
    the box with the wrong policy (for example open egress during scoring).
    """

    def __init__(self, applier: PolicyApplier, plan: NetworkPlan, current: EgressPolicy) -> None:
        """Keep the applier, the plan and the policy to restore between phases.

        Args:
            applier: Applies policies to the sandbox.
            plan: The policy of each phase.
            current: The policy that the sandbox must have outside every
                phase. It is not trusted as applied until the provider
                confirms it, so the first restore always sends it.
        """
        self._applier = applier
        self._plan = plan
        self._baseline = current
        self._current: EgressPolicy | None = None
        self._active: list[tuple[object, EgressPolicy]] = []
        self._owner: asyncio.Task[object] | None = None

    @property
    def current(self) -> EgressPolicy | None:
        """Return the policy that the provider confirmed, or None when it is unknown."""
        return self._current

    @asynccontextmanager
    async def phase_policy(self, phase: Phase) -> AsyncGenerator[EgressPolicy, None]:
        """Apply the policy of a phase, then restore the policy from before it.

        The restore target is the policy of the enclosing phase, or the
        baseline policy outside every phase. It comes back on exit, also when
        the block raises or the entry apply fails or is cancelled. When the
        block raised and the restore also fails, the block's exception
        reaches the caller with a note about the restore. A phase that exits
        while a later phase is still active applies deny-all instead.

        Args:
            phase: The phase to enter.

        Yields:
            The policy of the phase.

        Raises:
            NetworkPolicyError: The provider did not confirm a policy change,
                another task has a phase active, or the phase exited before
                a later phase.
        """
        task = asyncio.current_task()
        if self._active and self._owner is not task:
            raise NetworkPolicyError("Another task has a network phase active; phases must not overlap.")
        token = object()
        policy = self._plan.for_phase(phase)
        self._owner = task
        self._active.append((token, policy))
        try:
            try:
                await self._apply(policy)
                yield policy
            except BaseException as error:
                if self._active[-1][0] is not token:
                    await self._exit_out_of_order(phase, token, error)
                try:
                    await self._restore(self._previous())
                except Exception as restore_error:
                    error.add_note(f"Restoring the network policy failed too: {restore_error!r}")
                raise
            if self._active[-1][0] is not token:
                await self._exit_out_of_order(phase, token, None)
            await self._restore(self._previous())
        finally:
            self._active = [entry for entry in self._active if entry[0] is not token]
            if not self._active:
                self._owner = None

    def _previous(self) -> EgressPolicy:
        """Return the restore target of the top phase.

        It is the policy of the phase below the top one, or the baseline. It
        is read at exit, not at entry, so that after an out-of-order exit the
        remaining phases restore what is below them now, not a policy of a
        phase that already ended.

        Returns:
            The policy to restore when the top phase exits.
        """
        return self._active[-2][1] if len(self._active) > 1 else self._baseline

    async def _exit_out_of_order(self, phase: Phase, token: object, error: BaseException | None) -> None:
        """Fail closed when a phase exits while a later phase is still active.

        The restore target of this phase is now wrong for the box, and the
        later phase still expects its own policy. No restore is correct, so
        the box goes to deny-all until the remaining phases exit.

        Args:
            phase: The phase that exits.
            token: The stack entry of that phase.
            error: The exception that the phase block raised, if any.

        Raises:
            NetworkPolicyError: Always, unless a cancel or an interpreter exit
                is in flight; that exception keeps going with a note.
        """
        self._active = [entry for entry in self._active if entry[0] is not token]
        failure = NetworkPolicyError(
            f"The {phase.value} network phase exited before a later phase; "
            "phases must exit in reverse order. The sandbox is set to deny-all."
        )
        try:
            await self._restore(EgressPolicy(mode=NetworkMode.DENY_ALL))
        except Exception as restore_error:
            failure.add_note(f"Applying deny-all failed too: {restore_error!r}")
        # A cancel or an interpreter exit must not turn into a normal error,
        # or the caller would swallow it. GeneratorExit is replaced, because
        # aclose() would hide it and the out-of-order exit would go unseen.
        if error is not None and not isinstance(error, (Exception, GeneratorExit)):
            error.add_note(str(failure))
            raise error
        raise failure from error

    async def _restore(self, previous: EgressPolicy) -> None:
        """Apply the policy from before a phase, and finish it even when cancelled.

        A cancel must not stop the restore half-way and leave the phase policy
        on the box. It also must not return early while the restore still
        runs: a later phase could then apply its policy first, and the late
        restore would overwrite it. So a cancel waits for the restore to end,
        then reaches the caller.

        Args:
            previous: The policy from before the phase.

        Raises:
            NetworkPolicyError: The provider did not confirm the old policy.
            asyncio.CancelledError: The task was cancelled during the restore.
        """
        restore = asyncio.ensure_future(self._apply(previous))
        cancelled: asyncio.CancelledError | None = None
        while not restore.done():
            try:
                await asyncio.wait({restore})
            except asyncio.CancelledError as error:
                cancelled = error
        if cancelled is not None:
            if not restore.cancelled() and (restore_error := restore.exception()) is not None:
                cancelled.add_note(f"Restoring the network policy failed too: {restore_error!r}")
            raise cancelled
        restore.result()

    async def _apply(self, policy: EgressPolicy) -> None:
        """Apply a policy and fail closed unless the provider confirms it.

        Args:
            policy: The policy to apply.

        Raises:
            NetworkPolicyError: The provider reported no policy or another policy.
        """
        if self._current is not None and policy == self._current:
            return
        # Until the provider confirms, the box may hold either policy, so a
        # failed or cancelled call must leave the state unknown, never stale.
        self._current = None
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
    INVALID = "invalid"


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
    A host that is not a bare host name (``*``, a URL, a port) is dropped.

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
            host = _normalize_host(raw)
            if not host:
                kind = NoticeKind.EMPTY
            elif not _is_valid_host(host):
                kind = NoticeKind.INVALID
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
        The lowercased host name, without the port or a trailing dot.

    Raises:
        ValueError: The URL has no host.
    """
    host = urlsplit(gateway_url.strip()).hostname
    if host and host.endswith("."):
        host = host[:-1]
    if not host:
        raise ValueError("The gateway URL has no host.")
    return host

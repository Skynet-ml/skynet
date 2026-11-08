"""Tests for the phase egress policies, credential rules and allowlist merge."""

from __future__ import annotations

import pytest

from core.sandbox import NetworkMode, SandboxSpecError
from core.sandbox.network import (
    AllowlistNotice,
    CredentialRule,
    EgressPolicy,
    NetworkPlan,
    NetworkPolicyError,
    NoticeKind,
    Phase,
    PhaseNetwork,
    RequestMatch,
    inference_host,
    merge_allowlists,
)

GATEWAY = "gw.example.com"
RULE = CredentialRule(host=GATEWAY, headers={"Authorization": "Bearer test"})
OPEN = EgressPolicy(mode=NetworkMode.OPEN)
DENY = EgressPolicy(mode=NetworkMode.DENY_ALL)
SETUP = EgressPolicy(mode=NetworkMode.ALLOWLIST, allow_hosts=("pypi.org", GATEWAY))
RUN = EgressPolicy(mode=NetworkMode.ALLOWLIST, allow_hosts=(GATEWAY,), credential_rules=(RULE,))
PLAN = NetworkPlan(setup=SETUP, run=RUN, score=DENY)


class FakeApplier:
    """Record applied policies and confirm them, unless told not to."""

    def __init__(self, *, confirm: bool = True) -> None:
        """Set whether the fake confirms each policy.

        Args:
            confirm: False makes the fake report no policy.
        """
        self.confirm = confirm
        self.applied: list[EgressPolicy] = []

    async def apply_policy(self, policy: EgressPolicy) -> EgressPolicy | None:
        """Record the policy and echo it back when confirming.

        Args:
            policy: The policy to apply.

        Returns:
            The policy, or ``None`` when the fake does not confirm.
        """
        self.applied.append(policy)
        return policy if self.confirm else None


async def test_phase_applies_policy_and_restores_on_exit() -> None:
    """Apply the phase policy inside the block and the old policy after it."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, PLAN, current=OPEN)
    async with network.phase_policy(Phase.RUN) as policy:
        assert policy is RUN
        assert network.current is RUN
    assert network.current is OPEN
    assert applier.applied == [RUN, OPEN]


async def test_phase_restores_old_policy_on_exception() -> None:
    """Restore the old policy when the phase block raises."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, PLAN, current=OPEN)
    with pytest.raises(KeyError):
        async with network.phase_policy(Phase.SCORE):
            assert network.current is DENY
            raise KeyError("boom")
    assert network.current is OPEN
    assert applier.applied == [DENY, OPEN]


async def test_each_phase_uses_its_own_policy() -> None:
    """Give setup, run and score their own policies, nested or in order."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, PLAN, current=OPEN)
    async with network.phase_policy(Phase.SETUP):
        async with network.phase_policy(Phase.RUN):
            assert network.current is RUN
        assert network.current is SETUP
    async with network.phase_policy(Phase.SCORE):
        assert network.current is DENY
    assert applier.applied == [SETUP, RUN, SETUP, OPEN, DENY, OPEN]


async def test_same_policy_is_not_sent_again() -> None:
    """Skip the provider call when the phase policy is already applied."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, PLAN, current=DENY)
    async with network.phase_policy(Phase.SCORE):
        pass
    assert applier.applied == []


async def test_unconfirmed_policy_fails_the_phase() -> None:
    """Fail closed when the provider cannot prove the new policy."""
    network = PhaseNetwork(FakeApplier(confirm=False), PLAN, current=OPEN)
    with pytest.raises(NetworkPolicyError):
        async with network.phase_policy(Phase.SCORE):
            pytest.fail("The phase must not start.")
    assert network.current is OPEN


async def test_other_reported_policy_fails_the_phase() -> None:
    """Fail closed when the provider reports a policy that differs."""

    class WrongApplier(FakeApplier):
        """Report open egress for every policy."""

        async def apply_policy(self, policy: EgressPolicy) -> EgressPolicy | None:
            """Return open egress.

            Args:
                policy: The ignored policy.

            Returns:
                Open egress.
            """
            return OPEN

    network = PhaseNetwork(WrongApplier(), PLAN, current=SETUP)
    with pytest.raises(NetworkPolicyError):
        async with network.phase_policy(Phase.SCORE):
            pytest.fail("The phase must not start.")


async def test_unconfirmed_restore_raises() -> None:
    """Fail when the old policy does not come back after the phase."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, PLAN, current=OPEN)
    with pytest.raises(NetworkPolicyError):
        async with network.phase_policy(Phase.RUN):
            applier.confirm = False
    assert network.current is RUN


def test_credential_rule_for_host_outside_allowlist_fails() -> None:
    """Reject a credential rule whose host the allowlist does not reach."""
    with pytest.raises(SandboxSpecError, match="allow_hosts"):
        EgressPolicy(mode=NetworkMode.ALLOWLIST, allow_hosts=("pypi.org",), credential_rules=(RULE,))


def test_credential_rule_with_open_egress_is_allowed() -> None:
    """Allow credential rules with open egress, as the blackbox runtime does."""
    policy = EgressPolicy(mode=NetworkMode.OPEN, credential_rules=(RULE,))
    assert policy.has_credentials


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": NetworkMode.DENY_ALL, "allow_hosts": ("pypi.org",)},
        {"mode": NetworkMode.DENY_ALL, "credential_rules": (RULE,)},
        {"mode": NetworkMode.OPEN, "allow_hosts": ("pypi.org",)},
        {"mode": NetworkMode.ALLOWLIST},
    ],
)
def test_contradictory_policy_fails(kwargs: dict) -> None:
    """Reject hosts or rules that do not fit the mode.

    Args:
        kwargs: The policy fields.
    """
    with pytest.raises(SandboxSpecError):
        EgressPolicy(**kwargs)


def test_has_credentials() -> None:
    """Report credentials only when a credential rule is set."""
    assert RUN.has_credentials
    assert not SETUP.has_credentials
    assert not OPEN.has_credentials
    assert not DENY.has_credentials


def test_request_match_needs_absolute_path_prefix() -> None:
    """Accept a method and an absolute path prefix; reject a relative prefix."""
    rule = CredentialRule(host=GATEWAY, headers={"X": "1"}, match=RequestMatch(method="POST", path_prefix="/v1/"))
    assert rule.match == RequestMatch(method="POST", path_prefix="/v1/")
    with pytest.raises(SandboxSpecError):
        RequestMatch(path_prefix="v1/")


def test_merge_adds_new_host() -> None:
    """Add a new host and report it as added."""
    merged = merge_allowlists(SETUP, ["Files.Example.com "])
    assert merged.hosts == ("pypi.org", GATEWAY, "files.example.com")
    assert merged.notices == (AllowlistNotice("files.example.com", NoticeKind.ADDED),)
    assert not merged.notices[0].dropped


def test_merge_drops_duplicate_host() -> None:
    """Drop a host that the allowlist has, also across and within groups."""
    merged = merge_allowlists(SETUP, ["pypi.org"], ["a.example.com", "a.example.com"])
    assert merged.hosts == ("pypi.org", GATEWAY, "a.example.com")
    assert [notice.kind for notice in merged.notices] == [NoticeKind.DUPLICATE, NoticeKind.ADDED, NoticeKind.DUPLICATE]
    assert merged.notices[0].dropped


def test_merge_drops_hosts_with_open_egress() -> None:
    """Drop extra hosts for open egress, which reaches them already."""
    merged = merge_allowlists(OPEN, ["pypi.org"])
    assert merged.policy == OPEN
    assert merged.notices == (AllowlistNotice("pypi.org", NoticeKind.OPEN_EGRESS),)


def test_merge_drops_hosts_with_no_network() -> None:
    """Keep a no-network policy closed and drop every extra host."""
    merged = merge_allowlists(DENY, ["pypi.org"])
    assert merged.policy == DENY
    assert merged.notices == (AllowlistNotice("pypi.org", NoticeKind.NO_NETWORK),)


def test_merge_drops_empty_host() -> None:
    """Drop a blank host."""
    merged = merge_allowlists(SETUP, ["  "])
    assert merged.policy == SETUP
    assert [notice.kind for notice in merged.notices] == [NoticeKind.EMPTY]


def test_merge_keeps_credential_rules() -> None:
    """Carry the credential rules into the merged policy."""
    merged = merge_allowlists(RUN, ["pypi.org"])
    assert merged.policy.credential_rules == (RULE,)
    assert merged.policy.has_credentials


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://gw.example.com/v1", "gw.example.com"),
        ("http://LiteLLM.internal:4000", "litellm.internal"),
        (" https://gw.example.com ", "gw.example.com"),
    ],
)
def test_inference_host_reads_gateway_url(url: str, host: str) -> None:
    """Read the host from the gateway URL without the port or path.

    Args:
        url: The gateway URL.
        host: The expected host.
    """
    assert inference_host(url) == host


def test_inference_host_needs_a_host() -> None:
    """Reject a gateway URL without a host."""
    with pytest.raises(ValueError):
        inference_host("gw.example.com/v1")

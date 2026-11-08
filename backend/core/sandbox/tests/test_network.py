"""Tests for the phase egress policies, credential rules and allowlist merge."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

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
    """Skip the provider call when the phase policy is already confirmed."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, PLAN, current=DENY)
    async with network.phase_policy(Phase.SCORE):
        pass
    async with network.phase_policy(Phase.SCORE):
        pass
    assert applier.applied == [DENY]


async def test_unconfirmed_policy_fails_the_phase() -> None:
    """Fail closed when the provider cannot prove the new policy."""
    network = PhaseNetwork(FakeApplier(confirm=False), PLAN, current=OPEN)
    with pytest.raises(NetworkPolicyError):
        async with network.phase_policy(Phase.SCORE):
            pytest.fail("The phase must not start.")
    assert network.current is None


class ScriptedApplier(FakeApplier):
    """Confirm each policy except on the call numbers in ``unconfirmed``."""

    def __init__(self, *unconfirmed: int) -> None:
        """Keep the 1-based call numbers that report no policy.

        Args:
            *unconfirmed: The call numbers that do not confirm.
        """
        super().__init__()
        self.unconfirmed = set(unconfirmed)

    async def apply_policy(self, policy: EgressPolicy) -> EgressPolicy | None:
        """Record the policy and confirm it unless this call is unconfirmed.

        Args:
            policy: The policy to apply.

        Returns:
            The policy, or ``None`` for an unconfirmed call.
        """
        self.applied.append(policy)
        return None if len(self.applied) in self.unconfirmed else policy


async def test_unconfirmed_entry_restores_the_old_policy() -> None:
    """Restore the old policy when the entry apply is not confirmed."""
    applier = ScriptedApplier(1)
    network = PhaseNetwork(applier, PLAN, current=OPEN)
    with pytest.raises(NetworkPolicyError):
        async with network.phase_policy(Phase.SCORE):
            pytest.fail("The phase must not start.")
    assert network.current is OPEN
    assert applier.applied == [DENY, OPEN]


async def test_unknown_state_after_failed_restore_still_applies_next_policy() -> None:
    """Send the next policy to the provider after a restore fails (F1).

    The old code kept the old policy as current, so a later phase with that
    policy skipped the provider and the box kept open egress.
    """
    applier = ScriptedApplier(2)
    plan = NetworkPlan(setup=OPEN, run=OPEN, score=DENY)
    network = PhaseNetwork(applier, plan, current=DENY)
    with pytest.raises(NetworkPolicyError):
        async with network.phase_policy(Phase.SETUP):
            pass
    assert network.current is None
    async with network.phase_policy(Phase.SCORE) as policy:
        assert policy is DENY
        assert network.current is DENY
        assert applier.applied == [OPEN, DENY, DENY]
    assert network.current is DENY
    assert applier.applied == [OPEN, DENY, DENY]


async def test_unknown_state_after_failed_entry_and_restore_still_applies_next_policy() -> None:
    """Send the next policy to the provider after entry and restore both fail (F1)."""
    applier = ScriptedApplier(1, 2)
    plan = NetworkPlan(setup=DENY, run=OPEN, score=DENY)
    network = PhaseNetwork(applier, plan, current=DENY)
    with pytest.raises(NetworkPolicyError):
        async with network.phase_policy(Phase.RUN):
            pytest.fail("The phase must not start.")
    assert network.current is None
    async with network.phase_policy(Phase.SCORE):
        assert network.current is DENY
    assert applier.applied == [OPEN, DENY, DENY]


class GatedApplier(FakeApplier):
    """Block one call number on a gate, and confirm every policy."""

    def __init__(self, blocked_call: int) -> None:
        """Keep the 1-based call number that waits for the gate.

        Args:
            blocked_call: The call that waits until ``gate`` is set.
        """
        super().__init__()
        self.blocked_call = blocked_call
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    async def apply_policy(self, policy: EgressPolicy) -> EgressPolicy | None:
        """Record the policy, wait for the gate on the blocked call, and confirm.

        Args:
            policy: The policy to apply.

        Returns:
            The policy.
        """
        self.applied.append(policy)
        if len(self.applied) == self.blocked_call:
            self.waiting.set()
            await self.gate.wait()
        return policy


async def test_cancel_during_entry_restores_the_old_policy() -> None:
    """Restore the old policy when the task is cancelled during the entry apply (F1)."""
    applier = GatedApplier(blocked_call=1)
    network = PhaseNetwork(applier, PLAN, current=DENY)

    async def enter() -> None:
        """Enter the run phase."""
        async with network.phase_policy(Phase.RUN):
            pytest.fail("The phase must not start.")

    task = asyncio.create_task(enter())
    await applier.waiting.wait()
    assert network.current is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert network.current is DENY
    assert applier.applied == [RUN, DENY]


async def test_restore_is_shielded_from_a_second_cancel() -> None:
    """Finish the restore when a second cancel arrives during it (F4)."""
    applier = GatedApplier(blocked_call=2)
    network = PhaseNetwork(applier, PLAN, current=DENY)
    inside = asyncio.Event()

    async def enter() -> None:
        """Enter the run phase and wait there until cancelled."""
        async with network.phase_policy(Phase.RUN):
            inside.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(enter())
    await inside.wait()
    task.cancel()
    await applier.waiting.wait()
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    assert network.current is None
    applier.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert network.current is DENY
    assert applier.applied == [RUN, DENY]


async def test_late_restore_cannot_overwrite_a_later_phase() -> None:
    """Keep a later phase's policy after a double cancel during a restore.

    The old code let the cancelled task end while the shielded restore still
    ran, so a later deny-all phase got open egress when the restore landed.
    """
    applier = GatedApplier(blocked_call=2)
    network = PhaseNetwork(applier, NetworkPlan(setup=SETUP, run=RUN, score=DENY), current=OPEN)
    inside = asyncio.Event()

    async def enter() -> None:
        """Enter the run phase and wait there until cancelled."""
        async with network.phase_policy(Phase.RUN):
            inside.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(enter())
    await inside.wait()
    task.cancel()
    await applier.waiting.wait()
    task.cancel()
    with pytest.raises(NetworkPolicyError, match="overlap"):
        async with network.phase_policy(Phase.SCORE):
            pytest.fail("The phase must not start while the restore runs.")
    applier.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with network.phase_policy(Phase.SCORE):
        assert network.current is DENY
        assert applier.applied[-1] is DENY
    assert applier.applied == [RUN, OPEN, DENY, OPEN]


async def test_overlapping_phases_from_another_task_are_refused() -> None:
    """Refuse a phase from a second task while a first task has one active."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, PLAN, current=OPEN)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        """Hold the run phase until released."""
        async with network.phase_policy(Phase.RUN):
            entered.set()
            await release.wait()

    task = asyncio.create_task(first())
    await entered.wait()

    async def second() -> None:
        """Try to enter the score phase."""
        async with network.phase_policy(Phase.SCORE):
            pytest.fail("The overlapping phase must not start.")

    with pytest.raises(NetworkPolicyError, match="overlap"):
        await asyncio.create_task(second())
    assert network.current is RUN
    release.set()
    await task
    assert network.current is OPEN
    assert applier.applied == [RUN, OPEN]
    async with network.phase_policy(Phase.SCORE):
        assert network.current is DENY


async def test_out_of_order_exit_fails_closed() -> None:
    """Fail closed when phases held in two generators close in entry order."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, NetworkPlan(setup=OPEN, run=RUN, score=DENY), current=DENY)

    async def hold(phase: Phase) -> AsyncGenerator[None, None]:
        """Keep a phase active until the generator closes.

        Args:
            phase: The phase to hold.

        Yields:
            Nothing, once the phase is active.
        """
        async with network.phase_policy(phase):
            yield

    outer = hold(Phase.SETUP)
    await anext(outer)
    inner = hold(Phase.RUN)
    await anext(inner)
    with pytest.raises(NetworkPolicyError, match="reverse order"):
        await outer.aclose()
    assert network.current is not None
    assert network.current.mode is NetworkMode.DENY_ALL
    await inner.aclose()
    assert network.current == DENY
    assert OPEN not in applier.applied[2:]
    async with network.phase_policy(Phase.RUN):
        assert network.current is RUN
    assert network.current == DENY


async def test_first_policy_is_not_trusted_until_confirmed() -> None:
    """Send the phase policy even when it equals the policy given at start."""
    applier = FakeApplier()
    network = PhaseNetwork(applier, PLAN, current=DENY)
    assert network.current is None
    async with network.phase_policy(Phase.SCORE):
        assert network.current is DENY
    assert applier.applied == [DENY]


async def test_unknown_state_restores_the_baseline_not_the_phase_policy() -> None:
    """Restore the baseline after a phase that started from an unknown state."""
    applier = ScriptedApplier(2)
    network = PhaseNetwork(applier, NetworkPlan(setup=OPEN, run=OPEN, score=DENY), current=DENY)
    with pytest.raises(NetworkPolicyError):
        async with network.phase_policy(Phase.SETUP):
            pass
    assert network.current is None
    async with network.phase_policy(Phase.RUN):
        assert network.current is OPEN
    assert network.current is DENY
    assert applier.applied == [OPEN, DENY, OPEN, DENY]


async def test_failed_restore_keeps_the_block_exception() -> None:
    """Raise the block's exception with a note when the restore also fails (F4)."""
    applier = ScriptedApplier(2)
    network = PhaseNetwork(applier, PLAN, current=OPEN)
    with pytest.raises(KeyError) as caught:
        async with network.phase_policy(Phase.RUN):
            raise KeyError("boom")
    assert any("Restoring the network policy failed" in note for note in caught.value.__notes__)
    assert network.current is None


async def test_generator_close_with_failed_restore_raises() -> None:
    """Raise when aclose() ends a phase and the restore fails, as aclose() hides GeneratorExit."""
    applier = ScriptedApplier(2)
    network = PhaseNetwork(applier, PLAN, current=DENY)

    async def hold() -> AsyncGenerator[None, None]:
        """Keep the setup phase active until the generator closes.

        Yields:
            Nothing, once the phase is active.
        """
        async with network.phase_policy(Phase.SETUP):
            yield

    held = hold()
    await anext(held)
    with pytest.raises(NetworkPolicyError, match="setup phase failed") as caught:
        await held.aclose()
    assert isinstance(caught.value.__cause__, GeneratorExit)
    assert network.current is None


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
    assert network.current is None


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


@pytest.mark.parametrize(
    "host",
    [
        "*",
        "https://x.com",
        "x.com/p",
        "x.com:443",
        "u:p@x.com",
        "*.",
        "a.*.com",
        "x .com",
        "",
        "*.com",
        "*.com.",
        "0.0.0.0",
        "::",
        "::ffff:0.0.0.0",
        "a.com,b.com",
        ".x.com",
        "x..com",
        "x.com..",
        "x\\y.com",
        "%41.com",
        "fe80::1%eth0",
        "-x.com",
        "x-.com",
        "a_b.com",
        f"{'a' * 64}.com",
        "1.2.3",
        "0x7f000001",
        "1.0x2",
        "x.0XAB",
        "例え.jp",
    ],
)
def test_policy_rejects_host_that_is_not_bare(host: str) -> None:
    """Reject wildcards, unspecified addresses, bad labels and URL parts (F2).

    Args:
        host: The invalid host.
    """
    with pytest.raises(SandboxSpecError):
        EgressPolicy(mode=NetworkMode.ALLOWLIST, allow_hosts=(host,))
    with pytest.raises(SandboxSpecError):
        CredentialRule(host=host, headers={})


@pytest.mark.parametrize(
    "host", ["*.example.com", "10.0.0.1", "::1", "localhost", "xn--r8jz45g.jp", f"{'a' * 63}.com", "a-b.co"]
)
def test_policy_accepts_wildcard_label_and_ip(host: str) -> None:
    """Accept a leading wildcard label, an IP address and a plain name.

    Args:
        host: The valid host.
    """
    assert EgressPolicy(mode=NetworkMode.ALLOWLIST, allow_hosts=(host,)).allow_hosts == (host,)


def test_policy_coerces_a_string_mode() -> None:
    """Check a plain string mode like the enum, so deny-all with hosts is refused."""
    with pytest.raises(SandboxSpecError):
        EgressPolicy(mode="deny_all", allow_hosts=("pypi.org",))  # type: ignore[arg-type]
    assert EgressPolicy(mode="open").mode is NetworkMode.OPEN  # type: ignore[arg-type]


def test_trailing_dot_names_the_same_host() -> None:
    """Drop one trailing dot everywhere, so ``x.com.`` equals ``x.com``."""
    rule = CredentialRule(host="GW.example.com.", headers={})
    policy = EgressPolicy(
        mode=NetworkMode.ALLOWLIST, allow_hosts=(GATEWAY, f"{GATEWAY}.", "*.x.com."), credential_rules=(rule,)
    )
    assert rule.host == GATEWAY
    assert policy.allow_hosts == (GATEWAY, "*.x.com")
    assert merge_allowlists(policy, [f"{GATEWAY}."]).notices == (AllowlistNotice(GATEWAY, NoticeKind.DUPLICATE),)


def test_credential_rule_repr_hides_the_headers() -> None:
    """Keep header values out of the repr of a rule and of its policy."""
    rule = CredentialRule(host=GATEWAY, headers={"Authorization": "Bearer s3cr3t-value"})
    policy = EgressPolicy(mode=NetworkMode.OPEN, credential_rules=(rule,))
    assert "s3cr3t-value" not in repr(rule)
    assert "s3cr3t-value" not in repr(policy)
    assert GATEWAY in repr(rule)


def test_policy_normalizes_and_dedupes_hosts() -> None:
    """Lowercase, strip and dedupe hosts before the credential subset check (F3)."""
    rule = CredentialRule(host=" API.example.com ", headers={"X": "1"})
    policy = EgressPolicy(
        mode=NetworkMode.ALLOWLIST, allow_hosts=("api.example.com", "API.Example.com "), credential_rules=(rule,)
    )
    assert rule.host == "api.example.com"
    assert policy.allow_hosts == ("api.example.com",)


def test_credential_rule_headers_are_a_frozen_copy() -> None:
    """Keep a frozen copy of the headers and allow hashing of policies (F5)."""
    headers = {"Authorization": "Bearer one"}
    rule = CredentialRule(host=GATEWAY, headers=headers)
    headers["Authorization"] = "Bearer two"
    assert rule.headers == {"Authorization": "Bearer one"}
    with pytest.raises(TypeError):
        rule.headers["Authorization"] = "Bearer three"  # type: ignore[index]
    same = CredentialRule(host=GATEWAY, headers={"Authorization": "Bearer one"})
    assert rule == same
    assert hash(rule) == hash(same)
    assert rule != CredentialRule(host=GATEWAY, headers={"Authorization": "Bearer two"})
    assert {RUN: 1}[EgressPolicy(mode=NetworkMode.ALLOWLIST, allow_hosts=(GATEWAY,), credential_rules=(RULE,))] == 1


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
    assert RequestMatch(method="get").method == "GET"
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


def test_merge_drops_invalid_hosts() -> None:
    """Drop a bare wildcard, a URL and a host with userinfo or a port (F2)."""
    merged = merge_allowlists(SETUP, ["*", "https://x.com/p", "u:p@h:1", "*.ok.example.com"])
    assert merged.hosts == ("pypi.org", GATEWAY, "*.ok.example.com")
    assert [notice.kind for notice in merged.notices] == [
        NoticeKind.INVALID,
        NoticeKind.INVALID,
        NoticeKind.INVALID,
        NoticeKind.ADDED,
    ]
    assert all(notice.dropped for notice in merged.notices[:3])


def test_merge_matches_hosts_without_case() -> None:
    """Report a host that differs only in case as a duplicate (F3)."""
    base = EgressPolicy(mode=NetworkMode.ALLOWLIST, allow_hosts=("API.example.com",))
    merged = merge_allowlists(base, ["api.example.com"])
    assert merged.hosts == ("api.example.com",)
    assert merged.notices == (AllowlistNotice("api.example.com", NoticeKind.DUPLICATE),)


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
        ("https://GW.example.com.:443/v1", "gw.example.com"),
        ("http://[::1]:4000/v1", "::1"),
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

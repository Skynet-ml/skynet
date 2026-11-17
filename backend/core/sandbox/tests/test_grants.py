"""Tests for the deny-by-default grant resolver. No provider, no network."""

from __future__ import annotations

import dataclasses

import pytest

from core.sandbox.capabilities import VERCEL_CAPABILITIES, NetworkMode, SandboxSpecError, validate_spec
from core.sandbox.grants import (
    METRIC_BOX_GRANTS,
    OPEN_EGRESS,
    Grant,
    GrantError,
    UnknownCapabilityError,
    resolve_grants,
)
from core.sandbox.resources import SandboxSpec

FAKE_TOKEN = "Bearer fake-token-not-a-secret"
REGISTRY = {
    "api.github.example": {"Authorization": FAKE_TOKEN},
    "api.models.example": {"X-Api-Key": "fake-key-not-a-secret"},
}


def test_an_empty_grant_list_denies_all_network() -> None:
    """Resolve no grants to no network, even with a full registry."""
    resolved = resolve_grants([], REGISTRY)
    assert resolved.mode is NetworkMode.DENY_ALL
    assert resolved.allowed_hosts == ()
    assert dict(resolved.inject_headers) == {}
    with pytest.raises(TypeError):
        resolved.inject_headers["api.example.com"] = {}  # type: ignore[index]
    spec = resolved.apply(SandboxSpec(lifetime_seconds=60))
    assert spec.network_disabled
    validate_spec(spec, VERCEL_CAPABILITIES)


def test_an_empty_grant_list_closes_a_spec_that_was_open() -> None:
    """Replace the open default of a spec with no network."""
    open_spec = SandboxSpec(lifetime_seconds=60, inject_headers={"api.github.example": {"x": "1"}})
    spec = resolve_grants([], REGISTRY).apply(open_spec)
    assert spec.network_disabled
    assert spec.allowed_hosts == ()
    assert dict(spec.inject_headers) == {}


def test_a_metric_box_gets_no_network_by_default() -> None:
    """Resolve the metric-box default grants to no network."""
    assert resolve_grants(METRIC_BOX_GRANTS, REGISTRY).mode is NetworkMode.DENY_ALL


@pytest.mark.parametrize(
    "grants",
    [
        [Grant("files.example.org")],
        [Grant("api.github.example", credential=True)],
        [Grant("*.example.org"), Grant("api.models.example", credential=True)],
    ],
)
def test_no_grant_list_without_the_open_grant_resolves_to_open(grants: list[Grant]) -> None:
    """Resolve every list without the open grant to an allowlist that is not empty."""
    resolved = resolve_grants(grants, REGISTRY)
    assert resolved.mode is NetworkMode.ALLOWLIST
    assert resolved.allowed_hosts


def test_only_the_open_grant_resolves_to_open_egress() -> None:
    """Resolve the explicit open grant to open egress that keeps its credential grants."""
    resolved = resolve_grants([Grant(OPEN_EGRESS), Grant("api.github.example", credential=True)], REGISTRY)
    assert resolved.mode is NetworkMode.OPEN
    assert resolved.allowed_hosts == ()
    assert dict(resolved.inject_headers) == {"api.github.example": {"Authorization": FAKE_TOKEN}}
    validate_spec(resolved.apply(SandboxSpec(lifetime_seconds=60)), VERCEL_CAPABILITIES)


def test_the_open_grant_cannot_carry_a_credential() -> None:
    """Refuse a credential on the grant that opens all egress."""
    with pytest.raises(GrantError, match="open egress"):
        resolve_grants([Grant(OPEN_EGRESS, credential=True)], {OPEN_EGRESS: {"x": "1"}})


def test_a_credential_grant_for_an_unknown_host_names_the_host() -> None:
    """Refuse a credential grant for a host that the registry does not hold."""
    with pytest.raises(UnknownCapabilityError) as caught:
        resolve_grants([Grant("files.example.org"), Grant("API.Unknown.Example", credential=True)], REGISTRY)
    assert caught.value.host == "api.unknown.example"
    assert "api.unknown.example" in str(caught.value)
    assert isinstance(caught.value, SandboxSpecError)


def test_a_wildcard_credential_grant_does_not_take_an_exact_host_credential() -> None:
    """Match a credential grant to the registry by the exact host only."""
    with pytest.raises(UnknownCapabilityError) as caught:
        resolve_grants([Grant("*.github.example", credential=True)], REGISTRY)
    assert caught.value.host == "*.github.example"


def test_a_plain_host_resolves_with_no_registry_entry() -> None:
    """Allow a plain host that no capability holds, and inject no header."""
    resolved = resolve_grants([Grant("files.example.org")], {})
    assert resolved.mode is NetworkMode.ALLOWLIST
    assert resolved.allowed_hosts == ("files.example.org",)
    assert dict(resolved.inject_headers) == {}


def test_headers_go_only_to_hosts_granted_with_a_credential() -> None:
    """Inject a registry credential only for a host whose grant asks for it."""
    grants = [
        Grant("api.github.example", credential=True),
        Grant("api.models.example"),
        Grant("files.example.org"),
    ]
    resolved = resolve_grants(grants, REGISTRY)
    assert resolved.allowed_hosts == ("api.github.example", "api.models.example", "files.example.org")
    assert dict(resolved.inject_headers) == {"api.github.example": {"Authorization": FAKE_TOKEN}}
    spec = resolved.apply(SandboxSpec(lifetime_seconds=60))
    assert set(spec.inject_headers) <= set(spec.allowed_hosts)
    validate_spec(spec, VERCEL_CAPABILITIES)


def test_host_matching_ignores_case_space_and_a_trailing_dot() -> None:
    """Match a grant and a registry host after the run host normalization."""
    resolved = resolve_grants([Grant(" API.GitHub.Example. ", credential=True)], {"Api.Github.Example": {"x": "1"}})
    assert resolved.allowed_hosts == ("api.github.example",)
    assert dict(resolved.inject_headers) == {"api.github.example": {"x": "1"}}


def test_a_plain_and_a_credential_grant_for_one_host_merge() -> None:
    """Keep one host once, with the credential that one of its grants asks for."""
    resolved = resolve_grants([Grant("api.github.example"), Grant("api.github.example", credential=True)], REGISTRY)
    assert resolved.allowed_hosts == ("api.github.example",)
    assert set(resolved.inject_headers) == {"api.github.example"}


@pytest.mark.parametrize(
    "host",
    [
        "api.example.com:443",
        "https://api.example.com",
        "api.example.com/path",
        "*.com",
        "localhost",
        "",
        "  ",
        7,
        "api.example.com\n.",
        "*.example.com\n.",
        "\u212a.example.com",
        "\u00a0api.example.com",
        "api.example.com\u2028",
    ],
)
def test_an_invalid_grant_host_is_refused(host: object) -> None:
    """Refuse ports, schemes, paths, top-level wildcards, line ends, non-ASCII text and values that are not hosts."""
    with pytest.raises(GrantError):
        resolve_grants([Grant(host)], {})  # type: ignore[arg-type]


def test_a_registry_host_with_a_line_end_is_refused() -> None:
    """Refuse a registry host that keeps a line end after its final dot goes."""
    with pytest.raises(GrantError, match="must be a host"):
        resolve_grants([Grant("api.example.com")], {"api.example.com\n.": {"Authorization": "Bearer test"}})


def test_a_registry_host_that_is_not_ascii_is_refused() -> None:
    """Refuse a registry host with a character that lower-cases to an ASCII letter."""
    with pytest.raises(GrantError, match="must be a host"):
        resolve_grants([Grant("k.example.com")], {"\u212a.example.com": {"Authorization": "Bearer test"}})


def test_a_registry_host_that_repeats_after_normalization_is_refused() -> None:
    """Refuse a registry that gives one host two credentials."""
    with pytest.raises(GrantError, match="more than once"):
        resolve_grants(
            [Grant("api.github.example", credential=True)], {"api.github.example": {}, "API.github.example": {}}
        )


def test_the_grant_list_id_does_not_depend_on_order_or_repeats() -> None:
    """Give the same id to the same grants in any order."""
    first = resolve_grants([Grant("files.example.org"), Grant("api.github.example", credential=True)], REGISTRY)
    second = resolve_grants(
        [Grant("api.github.example", credential=True), Grant("FILES.example.org."), Grant("files.example.org")],
        REGISTRY,
    )
    assert first.grant_list_id == second.grant_list_id


def test_the_grant_list_id_changes_with_the_grants() -> None:
    """Give a different id to a different host or credential choice."""
    base = resolve_grants([Grant("api.github.example", credential=True)], REGISTRY).grant_list_id
    plain = resolve_grants([Grant("api.github.example")], REGISTRY).grant_list_id
    other = resolve_grants([Grant("api.models.example", credential=True)], REGISTRY).grant_list_id
    empty = resolve_grants([], REGISTRY).grant_list_id
    assert len({base, plain, other, empty}) == 4


def test_the_grant_list_id_and_repr_hold_no_credential_value() -> None:
    """Keep credential values out of the grant-list id and the repr."""
    grants = [Grant("api.github.example", credential=True)]
    resolved = resolve_grants(grants, REGISTRY)
    rotated = resolve_grants(grants, {"api.github.example": {"Authorization": "Bearer other-fake-token"}})
    assert resolved.grant_list_id == rotated.grant_list_id
    assert FAKE_TOKEN not in repr(resolved)


def test_the_resolved_headers_cannot_change_after_resolution() -> None:
    """Keep a later registry change from reaching a resolved policy."""
    registry = {"api.github.example": {"Authorization": FAKE_TOKEN}}
    resolved = resolve_grants([Grant("api.github.example", credential=True)], registry)
    registry["api.github.example"]["Authorization"] = "Bearer changed-fake-token"
    assert resolved.inject_headers["api.github.example"]["Authorization"] == FAKE_TOKEN
    with pytest.raises(TypeError):
        resolved.inject_headers["api.models.example"] = {}  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.mode = NetworkMode.OPEN  # type: ignore[misc]

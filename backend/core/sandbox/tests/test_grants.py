"""Tests for the deny-by-default grant resolver. No provider, no network."""

from __future__ import annotations

import dataclasses

import pytest

from core.billing.operation_pricing import json_fingerprint
from core.sandbox.capabilities import (
    VERCEL_CAPABILITIES,
    EnvironmentCapabilities,
    NetworkMode,
    SandboxSpecError,
    validate_spec,
)
from core.sandbox.grants import (
    METRIC_BOX_GRANTS,
    OPEN_EGRESS,
    Grant,
    GrantError,
    UnknownCapabilityError,
    resolve_grants,
)
from core.sandbox.providers.docker import DOCKER_CAPABILITIES
from core.sandbox.providers.local import LOCAL_CAPABILITIES
from core.sandbox.providers.modal import MODAL_CAPABILITIES
from core.sandbox.resources import SandboxSpec

FAKE_TOKEN = "Bearer fake-token-not-a-secret"
REGISTRY = {
    "api.github.example": {"/": {"Authorization": FAKE_TOKEN}},
    "api.models.example": {"/": {"X-Api-Key": "fake-key-not-a-secret"}},
}
MCP_REGISTRY = {
    "mcp.example.com": {
        "/team": {"Authorization": "Bearer fake-team-token"},
        "/web": {"Authorization": "Bearer fake-web-token"},
    }
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
    open_spec = SandboxSpec(lifetime_seconds=60, inject_headers={"api.github.example": {"/": {"x": "1"}}})
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
    assert dict(resolved.inject_headers) == {"api.github.example": {"/": {"Authorization": FAKE_TOKEN}}}
    validate_spec(resolved.apply(SandboxSpec(lifetime_seconds=60)), VERCEL_CAPABILITIES)


def test_the_open_grant_cannot_carry_a_credential() -> None:
    """Refuse a credential on the grant that opens all egress."""
    with pytest.raises(GrantError, match="open egress"):
        resolve_grants([Grant(OPEN_EGRESS, credential=True)], {OPEN_EGRESS: {"/": {"x": "1"}}})


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
    assert dict(resolved.inject_headers) == {"api.github.example": {"/": {"Authorization": FAKE_TOKEN}}}
    spec = resolved.apply(SandboxSpec(lifetime_seconds=60))
    assert set(spec.inject_headers) <= set(spec.allowed_hosts)
    validate_spec(spec, VERCEL_CAPABILITIES)


def test_host_matching_ignores_case_space_and_a_trailing_dot() -> None:
    """Match a grant and a registry host after the run host normalization."""
    resolved = resolve_grants(
        [Grant(" API.GitHub.Example. ", credential=True)], {"Api.Github.Example": {"/": {"x": "1"}}}
    )
    assert resolved.allowed_hosts == ("api.github.example",)
    assert dict(resolved.inject_headers) == {"api.github.example": {"/": {"x": "1"}}}


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
        resolve_grants([Grant("api.example.com")], {"api.example.com\n.": {"/": {"Authorization": "Bearer test"}}})


def test_a_registry_host_that_is_not_ascii_is_refused() -> None:
    """Refuse a registry host with a character that lower-cases to an ASCII letter."""
    with pytest.raises(GrantError, match="must be a host"):
        resolve_grants([Grant("k.example.com")], {"\u212a.example.com": {"/": {"Authorization": "Bearer test"}}})


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
    rotated = resolve_grants(grants, {"api.github.example": {"/": {"Authorization": "Bearer other-fake-token"}}})
    assert resolved.grant_list_id == rotated.grant_list_id
    assert FAKE_TOKEN not in repr(resolved)


def test_the_resolved_headers_cannot_change_after_resolution() -> None:
    """Keep a later registry change from reaching a resolved policy."""
    registry = {"api.github.example": {"/": {"Authorization": FAKE_TOKEN}}}
    resolved = resolve_grants([Grant("api.github.example", credential=True)], registry)
    registry["api.github.example"]["/"]["Authorization"] = "Bearer changed-fake-token"
    assert resolved.inject_headers["api.github.example"]["/"]["Authorization"] == FAKE_TOKEN
    with pytest.raises(TypeError):
        resolved.inject_headers["api.models.example"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        resolved.inject_headers["api.github.example"]["/other"] = {}  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.mode = NetworkMode.OPEN  # type: ignore[misc]


def test_a_credential_grant_takes_only_the_headers_of_its_path_prefix() -> None:
    """Resolve a grant with a path to that prefix's headers only, not the host's other prefixes (SKY-833)."""
    resolved = resolve_grants([Grant("mcp.example.com", credential=True, path="/team/")], MCP_REGISTRY)
    assert resolved.allowed_hosts == ("mcp.example.com",)
    assert dict(resolved.inject_headers) == {"mcp.example.com": {"/team": {"Authorization": "Bearer fake-team-token"}}}
    validate_spec(resolved.apply(SandboxSpec(lifetime_seconds=60)), VERCEL_CAPABILITIES)


@pytest.mark.parametrize("caps", [LOCAL_CAPABILITIES, MODAL_CAPABILITIES, DOCKER_CAPABILITIES])
def test_a_provider_without_an_edge_refuses_a_path_credential(caps: EnvironmentCapabilities) -> None:
    """Refuse a credential grant on a provider that cannot add headers at its network edge.

    Args:
        caps: The capabilities of a provider with no header-injecting edge.
    """
    spec = resolve_grants([Grant("mcp.example.com", credential=True, path="/team")], MCP_REGISTRY).apply(
        SandboxSpec(lifetime_seconds=60)
    )
    with pytest.raises(SandboxSpecError, match="does not inject egress headers"):
        validate_spec(spec, caps)


def test_two_credential_prefixes_on_one_host_are_refused() -> None:
    """Refuse two servers with headers on one host: '/team/../web' would take the team headers to web (SKY-833)."""
    grants = [
        Grant("mcp.example.com", credential=True, path="/web"),
        Grant("mcp.example.com", credential=True, path="/team"),
    ]
    with pytest.raises(GrantError, match=r"Host 'mcp.example.com' .* \('/team', '/web'\); one host takes one"):
        resolve_grants(grants, MCP_REGISTRY)


def test_two_grants_for_one_prefix_on_one_host_resolve() -> None:
    """Merge ``/team`` and ``/team/`` into one prefix, since both name the same server."""
    grants = [
        Grant("mcp.example.com", credential=True, path="/team"),
        Grant("mcp.example.com", credential=True, path="/team/"),
    ]
    resolved = resolve_grants(grants, MCP_REGISTRY)
    assert dict(resolved.inject_headers["mcp.example.com"]) == {"/team": {"Authorization": "Bearer fake-team-token"}}


def test_a_credential_grant_for_an_unknown_path_names_the_path() -> None:
    """Refuse a grant whose prefix the registry does not hold, even when the host has other prefixes."""
    with pytest.raises(UnknownCapabilityError) as caught:
        resolve_grants([Grant("mcp.example.com", credential=True)], MCP_REGISTRY)
    assert (caught.value.host, caught.value.path) == ("mcp.example.com", "/")
    with pytest.raises(UnknownCapabilityError, match="path prefix '/admin'"):
        resolve_grants([Grant("mcp.example.com", credential=True, path="/admin")], MCP_REGISTRY)


@pytest.mark.parametrize(
    "paths", [("/", "/team"), ("/team", "/team/inner"), ("/team", "/web"), ("/team", "/team-admin")]
)
def test_any_two_credential_prefixes_on_one_host_are_refused(paths: tuple[str, str]) -> None:
    """Refuse nested and sibling prefixes alike, since the edge cannot bound a path that normalizes elsewhere.

    Args:
        paths: Two credential path prefixes of one host.
    """
    registry = {
        "mcp.example.com": {path: {"Authorization": f"Bearer fake-{index}"} for index, path in enumerate(paths)}
    }
    with pytest.raises(GrantError, match="more than one path prefix"):
        resolve_grants([Grant("mcp.example.com", credential=True, path=path) for path in paths], registry)


@pytest.mark.parametrize(
    "path",
    ["mcp", "/mcp?x=1", "/mcp#a", "/mcp/*", "/a b", "//mcp", "/mcp/../admin", "/./mcp", "/a/%2e%2e/b", "/%2E/mcp", 7],
)
def test_an_invalid_grant_path_is_refused(path: object) -> None:
    """Refuse a path that is not a plain URL path, since an edge path match could not bound it."""
    with pytest.raises(GrantError, match="Grant path"):
        resolve_grants([Grant("mcp.example.com", credential=True, path=path)], MCP_REGISTRY)  # type: ignore[arg-type]


def test_the_grant_list_id_keeps_the_path_but_ignores_a_plain_grant_path() -> None:
    """Give another id to another credential path, and the same id to a plain grant with any path."""
    team = resolve_grants([Grant("mcp.example.com", credential=True, path="/team")], MCP_REGISTRY).grant_list_id
    web = resolve_grants([Grant("mcp.example.com", credential=True, path="/web")], MCP_REGISTRY).grant_list_id
    plain = resolve_grants([Grant("mcp.example.com")], {}).grant_list_id
    assert team != web
    assert plain == resolve_grants([Grant("mcp.example.com", path="/team")], {}).grant_list_id
    # A list with no credential keeps the id it had before paths, so its earlier step commits still match.
    assert plain == json_fingerprint([["mcp.example.com", False]])

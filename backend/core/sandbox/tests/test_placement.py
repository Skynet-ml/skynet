"""Tests for picking the sandbox provider of a box from its grants and quota (SKY-732)."""

from __future__ import annotations

import pytest

from core.sandbox.grants import Grant, GrantError
from core.sandbox.placement import MODAL, VERCEL, Placement, Quota, choose, gpu_type, network_grants

_ROOM = Quota(vercel_cap=10, vercel_in_use=3)


def test_vercel_is_the_default() -> None:
    """A box with no GPU grant goes to Vercel while Vercel has room."""
    assert choose([Grant("pypi.org")], _ROOM) == Placement(VERCEL)
    assert choose([], _ROOM) == Placement(VERCEL)


def test_a_gpu_grant_goes_to_modal_with_its_type() -> None:
    """A gpu:<type> grant places the box on Modal, and the type is normalized."""
    assert choose([Grant("pypi.org"), Grant("gpu:a100")], _ROOM) == Placement(MODAL, "a100")
    assert choose([Grant("gpu: H100 ")], _ROOM) == Placement(MODAL, "h100")


def test_a_gpu_grant_goes_to_modal_also_when_vercel_is_full() -> None:
    """The GPU rule wins over the spill rule, since only Modal has GPUs."""
    full = Quota(vercel_cap=1, vercel_in_use=1, spill="daytona")

    assert choose([Grant("gpu:a10g")], full) == Placement(MODAL, "a10g")


def test_a_full_vercel_spills_to_the_second_provider() -> None:
    """At the Vercel cap the box goes to the spill provider, Modal by default."""
    assert choose([], Quota(vercel_cap=2, vercel_in_use=2)) == Placement(MODAL)
    assert choose([], Quota(vercel_cap=2, vercel_in_use=5, spill="daytona")) == Placement("daytona")
    assert choose([], Quota(vercel_cap=2, vercel_in_use=1)) == Placement(VERCEL)


@pytest.mark.parametrize(
    "grants",
    [
        [Grant("gpu:")],
        [Grant("gpu:a100", credential=True)],
        [Grant("gpu:a100"), Grant("gpu:h100")],
    ],
)
def test_bad_gpu_grants_are_refused(grants: list[Grant]) -> None:
    """A GPU grant needs one type and no credential."""
    with pytest.raises(GrantError):
        choose(grants, _ROOM)


def test_the_same_gpu_type_twice_is_one_gpu() -> None:
    """Two grants for one type ask for that type."""
    assert gpu_type([Grant("gpu:a100"), Grant("gpu:A100")]) == "a100"
    assert gpu_type([Grant("pypi.org")]) is None


def test_network_grants_drop_the_gpu_grants() -> None:
    """Only the host grants reach the network policy."""
    grants = [Grant("pypi.org"), Grant("gpu:a100"), Grant("api.example.com", credential=True)]

    assert network_grants(grants) == (Grant("pypi.org"), Grant("api.example.com", credential=True))


@pytest.mark.parametrize("host", [7, None])
def test_a_host_that_is_not_text_is_a_network_grant(host: object) -> None:
    """Placement does not fail on such a host; the grant resolution refuses it with GrantError."""
    grants = [Grant(host)]  # type: ignore[arg-type]

    assert gpu_type(grants) is None
    assert network_grants(grants) == (Grant(host),)  # type: ignore[arg-type]
    assert choose(grants, _ROOM) == Placement(VERCEL)

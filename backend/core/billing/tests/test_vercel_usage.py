"""Verify protected Vercel admission and stopped-session metering without paid calls."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.billing import vercel_usage
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import UsagePendingError
from core.billing.vercel_usage import (
    PROPOSER_REGISTRY_HOSTS,
    SANDBOX_NETWORK_BYTES_CAP,
    proposer_hosts,
    quote_vercel_sandbox,
    vercel_actual_usd,
    vercel_sandbox_cost_range,
)
from core.config import settings

IMAGE = "vcr.example/skynet/optimizer@sha256:" + "a" * 64
CREATE = {
    "image": IMAGE,
    "lifetime_ms": 120_000,
    "vcpus": 2,
    "network_disabled": True,
    "ports": [],
    "persistent": False,
}
NETWORKED = {
    **CREATE,
    "network_disabled": False,
    "allowed_hosts": ["api.anthropic.com"],
    "network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP,
}
RECEIPT = {
    "id": "session-one",
    "sourceSandboxName": "sandbox-one",
    "status": "stopped",
    "region": "iad1",
    "vcpus": 2,
    "memory": 4096,
    "timeout": 120_000,
    "cwd": "/vercel/sandbox",
    "requestedAt": 1_000,
    "startedAt": 2_000,
    "stoppedAt": 63_000,
    "activeCpuDurationMs": 5_000,
    "networkTransfer": {"ingress": 0, "egress": 0},
}


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup to 1.0 so these tests check settlement arithmetic, not pricing policy."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)


def test_quote_covers_all_regions_and_settles_cpu_separately_from_wall_time() -> None:
    """Cover maximum active CPU, charge actual CPU, and round only memory duration."""
    quote = quote_vercel_sandbox(CREATE)
    minimum_cents, maximum_cents = vercel_sandbox_cost_range(CREATE)
    maximum_usd = (
        Decimal("0.0000006")
        + Decimal(240_000) * Decimal("0.177") / 3_600_000
        + Decimal(480_000) * Decimal("0.0294") / 3_600_000
    )
    assert quote.maximum.wallet == quote.maximum.total == maximum_usd * 100
    assert minimum_cents == Decimal("0.141393334")
    assert maximum_cents == quote.maximum.total
    actual = vercel_actual_usd(RECEIPT, session_id="session-one", vcpus=2)
    expected = (
        Decimal("0.0000006")
        + Decimal(5_000) * Decimal("0.128") / 3_600_000
        + Decimal(480_000) * Decimal("0.0212") / 3_600_000
    )
    assert actual == expected
    assert actual < maximum_usd
    assert quote.price_snapshot["policy"]["kind"] == "sandbox"


@pytest.mark.parametrize(
    "change",
    [
        {"image": "mutable:latest"},
        {"network_disabled": False},
        {"ports": [8080]},
        {"persistent": True},
        {"lifetime_ms": 0},
        {"lifetime_ms": 86_400_001},
        {"vcpus": 3},
    ],
)
def test_unbounded_creation_is_not_quoted(change: dict[str, Any]) -> None:
    """Reject dependencies, network paths, or resource lifetimes without enforceable bounds."""
    with pytest.raises(UnpricedOperationError):
        quote_vercel_sandbox({**CREATE, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"status": "stopping"},
        {"activeCpuDurationMs": None},
        {"activeCpuDurationMs": True},
        {"networkTransfer": {"ingress": 30, "egress": 0}},
        {"networkTransfer": {"ingress": 0, "egress": 5}},
        {"region": "future-region"},
        {"id": "resumed-session"},
        {"memory": 8192},
    ],
)
def test_unconfirmed_usage_is_not_fabricated_or_misclassified(change: dict[str, Any]) -> None:
    """Leave absent metrics and ambiguous transfer pending instead of charging estimates."""
    with pytest.raises(UsagePendingError) as pending:
        vercel_actual_usd({**RECEIPT, **change}, session_id="session-one", vcpus=2)
    assert "Vercel" not in str(pending.value)


def test_pending_usage_messages_never_name_the_sandbox_vendor() -> None:
    """Keep the vendor name out of every pending-usage message, because failed runs show it to users."""
    tree = ast.parse(Path(vercel_usage.__file__).read_text())
    messages = [
        "".join(part.value for part in ast.walk(node.args[0]) if isinstance(part, ast.Constant))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "UsagePendingError"
        and node.args
    ]

    assert len(messages) >= 15
    assert [message for message in messages if "vercel" in message.lower()] == []


def test_control_plane_transfer_settles_only_under_an_offline_admission() -> None:
    """Exclude a deny-all sandbox's control-plane bytes from the charge, never traffic an admission allowed."""
    snapshot = quote_vercel_sandbox(CREATE).price_snapshot
    receipt = {**RECEIPT, "networkTransfer": {"ingress": 21_124, "egress": 7_548}}
    assert vercel_actual_usd(receipt, session_id="session-one", vcpus=2, price_snapshot=snapshot) == vercel_actual_usd(
        RECEIPT, session_id="session-one", vcpus=2, price_snapshot=snapshot
    )
    connected = {**snapshot, "request": {**CREATE, "network_disabled": False}}
    with pytest.raises(UsagePendingError, match="classification"):
        vercel_actual_usd(receipt, session_id="session-one", vcpus=2, price_snapshot=connected)


def test_allowlisted_box_is_funded_for_its_transfer_cap_and_charged_per_byte() -> None:
    """Fund a networked box for its whole byte cap, then charge every byte it moved."""
    offline = quote_vercel_sandbox(CREATE)
    quote = quote_vercel_sandbox(NETWORKED)
    assert quote.maximum.total - offline.maximum.total == Decimal(SANDBOX_NETWORK_BYTES_CAP) * Decimal("0.15") / 10**7
    assert quote.price_snapshot["maximum_billable_network_bytes"] == SANDBOX_NETWORK_BYTES_CAP
    assert offline.price_snapshot["maximum_billable_network_bytes"] == 0
    assert offline.price_snapshot["network_basis"].startswith("deny-all")
    receipt = {**RECEIPT, "networkTransfer": {"ingress": 600_000_000, "egress": 400_000_000}}
    moved = vercel_actual_usd(receipt, session_id="session-one", vcpus=2, price_snapshot=quote.price_snapshot)
    idle = vercel_actual_usd(RECEIPT, session_id="session-one", vcpus=2, price_snapshot=quote.price_snapshot)
    assert moved - idle == Decimal("0.15")


def test_transfer_beyond_the_admitted_cap_stays_pending() -> None:
    """Never settle a networked box that moved more than its admission funded."""
    snapshot = quote_vercel_sandbox(NETWORKED).price_snapshot
    receipt = {**RECEIPT, "networkTransfer": {"ingress": SANDBOX_NETWORK_BYTES_CAP, "egress": 1}}
    with pytest.raises(UsagePendingError, match="allowance"):
        vercel_actual_usd(receipt, session_id="session-one", vcpus=2, price_snapshot=snapshot)


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_hosts": []},
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["b.example", "a.example"]},
        {"network_bytes_cap": None},
        {"network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP + 1},
        {"network_disabled": True},
    ],
)
def test_unbounded_allowlist_is_not_quoted(change: dict[str, Any]) -> None:
    """Refuse an open, unsorted, or uncapped allowlist, and an allowlist on a deny-all box."""
    with pytest.raises(UnpricedOperationError):
        quote_vercel_sandbox({**NETWORKED, **change})


def test_a_proposer_with_no_network_key_reaches_only_the_registries() -> None:
    """Give a proposer the package registries by default, and nothing else (E57)."""
    assert proposer_hosts(None) == PROPOSER_REGISTRY_HOSTS
    assert proposer_hosts(None, "https://pypi.org/simple") == PROPOSER_REGISTRY_HOSTS
    assert "raw.githubusercontent.com" not in proposer_hosts(None)


def test_a_proposer_network_list_adds_hosts_and_an_empty_list_stays_offline() -> None:
    """Add listed hosts to the registries, and treat ``network = []`` as relay only."""
    assert proposer_hosts(()) == ()
    assert proposer_hosts(("docs.python.org",)) == tuple(sorted((*PROPOSER_REGISTRY_HOSTS, "docs.python.org")))


def test_a_custom_index_replaces_the_pypi_hosts() -> None:
    """Reach a saved custom index instead of pypi.org and its file host."""
    assert proposer_hosts(None, "https://pkgs.example.com/simple") == ("pkgs.example.com", "registry.npmjs.org")

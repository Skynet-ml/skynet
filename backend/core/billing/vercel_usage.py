"""Reserve ephemeral Vercel compute and settle only final provider usage."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS
from .budget_amounts import cent_units
from .budgets import BudgetError
from .operation_pricing import (
    ChargePolicy,
    OperationQuote,
    UnpricedOperationError,
    exact_nonnegative,
    json_fingerprint,
    operation_quote,
)
from .runtime import BudgetRuntime, OperationCompletedError, UsagePendingError

_PRICING_URL = "https://vercel.com/docs/sandbox/pricing"
_SCHEMA_URL = "https://github.com/vercel/sandbox/blob/main/packages/vercel-sandbox/src/api-client/validators.ts"
_PRICE_VERSION = "vercel-sandbox-public-pro-2026-09-02"
_REGIONAL_RATES = {
    "iad1": (Decimal("0.128"), Decimal("0.0212")),
    "cle1": (Decimal("0.128"), Decimal("0.0212")),
    "cdg1": (Decimal("0.177"), Decimal("0.0292")),
    "sfo1": (Decimal("0.177"), Decimal("0.0294")),
}
_CREATION_USD = Decimal("0.0000006")
_NETWORK_GB_USD = Decimal("0.15")
_BYTES_PER_GB = Decimal(1_000_000_000)
# Vercel cannot cap a box's transfer, so this is the most a networked box is
# funded for; a box that moves more stays pending for manual reconciliation.
SANDBOX_NETWORK_BYTES_CAP = 2_000_000_000
# The only hosts a repository's setup may reach, and only from the parent's
# scorer box before any version runs in it. Beyond the named registries:
# index.crates.io is cargo's default sparse index, sum.golang.org is the
# checksum database ``go`` verifies modules against, registry.yarnpkg.com is
# where Yarn 1 lockfiles point, and codeload/objects/release-assets serve the
# tarballs a ``git+https`` dependency or a GitHub release download redirects to.
# fonts.googleapis.com and fonts.gstatic.com serve the fonts ``next/font/google``
# downloads during ``next build``, which fails the build when they're unreachable.
PACKAGE_REGISTRY_HOSTS = tuple(
    sorted(
        {
            "codeload.github.com",
            "crates.io",
            "files.pythonhosted.org",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
            "github.com",
            "index.crates.io",
            "objects.githubusercontent.com",
            "proxy.golang.org",
            "pypi.org",
            "registry.npmjs.org",
            "registry.yarnpkg.com",
            "release-assets.githubusercontent.com",
            "static.crates.io",
            "sum.golang.org",
        }
    )
)
# The registries a proposer box reaches by default, so its agent can install
# packages while it stays offline otherwise (E57). files.pythonhosted.org
# serves the files that pypi.org's index points to.
PROPOSER_REGISTRY_HOSTS = ("files.pythonhosted.org", "pypi.org", "registry.npmjs.org")
_PYPI_HOSTS = frozenset({"files.pythonhosted.org", "pypi.org"})
_MS_PER_HOUR = Decimal(3_600_000)
# Assumed snapshot storage price and size bound (SKY-430): the public pricing
# page names no snapshot rate yet, so the hold uses this rate and cap, and a
# larger snapshot stays pending for manual reconciliation.
_SNAPSHOT_GB_MONTH_USD = Decimal("0.08")
_HOURS_PER_MONTH = Decimal(730)
SANDBOX_SNAPSHOT_BYTES_CAP = 32_000_000_000
_SNAPSHOT_RETENTION_CEILING_MS = 3_650 * 86_400_000
_IMMUTABLE_IMAGE = re.compile(r".+@sha256:[0-9a-f]{64}\Z")
_SESSION_FIELDS = (
    "id",
    "status",
    "region",
    "vcpus",
    "memory",
    "timeout",
    "startedAt",
    "stoppedAt",
    "activeCpuDurationMs",
    "networkTransfer",
)


def vercel_sandbox_cost_range(request: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    """Return the current marked-up session floor and enforceable request bound.

    Args:
        request: Final immutable sandbox resource request accepted by
            :func:`quote_vercel_sandbox`.

    Returns:
        The smallest published creation-plus-memory charge and the request's
        maximum covered amount in cents, both including the usage markup.

    Raises:
        UnpricedOperationError: When the request cannot be bounded.
    """
    maximum = quote_vercel_sandbox(request).maximum.total
    vcpus = request["vcpus"]
    minimum_memory_rate = min(memory for _cpu, memory in _REGIONAL_RATES.values())
    minimum_usd = _CREATION_USD + Decimal(vcpus * 2) * minimum_memory_rate / Decimal(60)
    minimum = ChargePolicy("sandbox").convert(minimum_usd).total
    return minimum, maximum


def proposer_hosts(network: tuple[str, ...] | None, index_url: str | None = None) -> tuple[str, ...]:
    """Return the hosts a proposer box may reach besides the relay.

    Args:
        network: The ``[proposer] network`` list, or None when the file sets none.
        index_url: The account's saved package index; PyPI when None.

    Returns:
        Sorted hosts: the registries when ``network`` is None, nothing when it
        is empty, and the registries plus its hosts otherwise.
    """
    if network is not None and not network:
        return ()
    registries = set(PROPOSER_REGISTRY_HOSTS)
    index_host = urlsplit(index_url).hostname if index_url else None
    if index_host and index_host not in _PYPI_HOSTS:
        # A custom index names no file host, so the box reaches only the index;
        # an index that serves its files from another host fails the install.
        registries = (registries - _PYPI_HOSTS) | {index_host}
    return tuple(sorted(registries | set(network or ())))


def _integer(value: Any, name: str) -> int:
    """Require an explicit nonnegative provider counter without truncating decimals.

    Args:
        value: Counter from trusted provider metadata.
        name: Field used to explain incomplete evidence.

    Returns:
        The validated whole-number counter.

    Raises:
        UsagePendingError: When the counter is missing or invalid.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsagePendingError(f"Cloud sandbox usage is awaiting a valid {name} counter.")
    return value


def _memory_ms(duration_ms: int) -> int:
    """Round provisioned memory duration to Vercel's one-minute billing increments."""
    return max(60_000, ((duration_ms + 59_999) // 60_000) * 60_000)


def _network_cap(request: Mapping[str, Any]) -> int:
    """Return the billable transfer a sandbox request admits.

    Args:
        request: Final create parameters.

    Returns:
        0 for a deny-all box, or the byte cap of a box limited to named hosts.

    Raises:
        UnpricedOperationError: When the network, ports, or persistence are unbounded.
    """
    if request.get("ports") == [] and request.get("persistent") is False:
        if request.get("network_disabled") is True and "allowed_hosts" not in request:
            return 0
        hosts = request.get("allowed_hosts")
        cap = request.get("network_bytes_cap")
        if (
            request.get("network_disabled") is False
            and isinstance(hosts, list)
            and hosts
            and all(isinstance(host, str) and host and host != "*" for host in hosts)
            and hosts == sorted(set(hosts))
            and not isinstance(cap, bool)
            and isinstance(cap, int)
            and 0 < cap <= SANDBOX_NETWORK_BYTES_CAP
        ):
            return cap
    raise UnpricedOperationError(
        "Protected Vercel runs require deny-all networking or a capped host allowlist, no ports, and no persistence."
    )


def _maximum_usd(lifetime_ms: int, vcpus: int, network_cap: int) -> Decimal:
    """Bound a box's provider cost in every supported region.

    Args:
        lifetime_ms: Funded lifetime.
        vcpus: CPU allocation; memory follows at 2 GiB per vCPU.
        network_cap: Billable transfer in bytes, 0 for a deny-all box.

    Returns:
        The most the box can cost in USD.
    """
    # Python SDK 0.4.0 cannot select a region. Cover every currently supported
    # region and settle against the region in the final provider receipt.
    max_cpu = max(rates[0] for rates in _REGIONAL_RATES.values())
    max_memory = max(rates[1] for rates in _REGIONAL_RATES.values())
    return (
        _CREATION_USD
        + Decimal(lifetime_ms * vcpus) * max_cpu / _MS_PER_HOUR
        + Decimal(_memory_ms(lifetime_ms) * vcpus * 2) * max_memory / _MS_PER_HOUR
        + Decimal(network_cap) * _NETWORK_GB_USD / _BYTES_PER_GB
    )


def sandbox_hold_cents(*, lifetime_ms: int, vcpus: int, network_cap: int = 0) -> Decimal:
    """Price the charged cents a box of this shape reserves when it opens.

    Args:
        lifetime_ms: Funded lifetime.
        vcpus: CPU allocation.
        network_cap: Billable transfer in bytes, 0 for a deny-all box.

    Returns:
        The box's coverage in charged cents.
    """
    return ChargePolicy("sandbox").convert(_maximum_usd(lifetime_ms, vcpus, network_cap)).total


def quote_vercel_sandbox(request: Mapping[str, Any]) -> OperationQuote:
    """Price an immutable sandbox before allocating any provider resources.

    Args:
        request: Final create parameters with lifetime_ms, vcpus, immutable image, and
            either deny-all networking or a sorted ``allowed_hosts`` list with a
            ``network_bytes_cap``.

    Returns:
        Coverage for creation, maximum active CPU, rounded memory lifetime, and
        capped transfer.

    Raises:
        UnpricedOperationError: When the image, lifetime, or network is unbounded.
    """
    lifetime_ms = request.get("lifetime_ms")
    vcpus = request.get("vcpus")
    if (
        isinstance(lifetime_ms, bool)
        or not isinstance(lifetime_ms, int)
        or not 0 < lifetime_ms <= VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS * 1000
        or isinstance(vcpus, bool)
        or not isinstance(vcpus, int)
        or vcpus not in {1, *range(2, 33, 2)}
    ):
        raise UnpricedOperationError("Vercel requires a bounded lifetime and explicit supported CPU allocation.")
    if not _IMMUTABLE_IMAGE.fullmatch(str(request.get("image", ""))):
        raise UnpricedOperationError("Protected Vercel runs require an immutable prebuilt image digest.")
    network_cap = _network_cap(request)
    maximum = _maximum_usd(lifetime_ms, vcpus, network_cap)
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "vercel",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "rates": {
                region: {"cpu_hour_usd": str(cpu), "memory_gb_hour_usd": str(memory)}
                for region, (cpu, memory) in _REGIONAL_RATES.items()
            },
            "creation_usd": str(_CREATION_USD),
            "network_gb_usd": str(_NETWORK_GB_USD),
            "maximum_billable_network_bytes": network_cap,
            "network_basis": (
                "allowlisted-hosts/no-exposed-ports; ingress and egress both charged, control plane included"
                if network_cap
                else "deny-all/no-exposed-ports; control-plane exclusion inferred from published billing categories"
            ),
            "memory_increment_ms": 60_000,
            "request": dict(request),
        },
    )


def _offline_admission(price_snapshot: Mapping[str, Any] | None) -> bool:
    """Report whether the admitted request denied all sandbox network and exposed no ports.

    Args:
        price_snapshot: Original admission, absent for standalone pricing checks.

    Returns:
        True only when the admitted request left no billable network path.
    """
    if price_snapshot is None:
        return False
    request = price_snapshot.get("request")
    return isinstance(request, Mapping) and request.get("network_disabled") is True and request.get("ports") == []


def _admitted_network_cap(price_snapshot: Mapping[str, Any] | None) -> int:
    """Return the transfer an admission funded, or 0 when it funded none.

    Args:
        price_snapshot: Original admission, absent for standalone pricing checks.

    Returns:
        The admitted byte cap of a box limited to named hosts, otherwise 0.
    """
    if price_snapshot is None:
        return 0
    request = price_snapshot.get("request")
    cap = price_snapshot.get("maximum_billable_network_bytes")
    if not isinstance(request, Mapping) or request.get("network_disabled") is not False:
        return 0
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else 0


def vercel_actual_usd(
    session: Mapping[str, Any], *, session_id: str, vcpus: int, price_snapshot: Mapping[str, Any] | None = None
) -> Decimal:
    """Price a fully stopped exact session from provider CPU and lifetime evidence.

    Args:
        session: Metadata read from Vercel's authenticated control plane.
        session_id: The one admitted runtime session, never a resumed replacement.
        vcpus: Allocation covered by the original reservation.
        price_snapshot: Original admission rates; current rates only for standalone pricing checks.

    Returns:
        Creation plus measured CPU and provisioned memory at the regional rate, plus
        transfer when the admission allowed named hosts.

    Raises:
        UsagePendingError: When final usage or its billing classification is uncertain.
    """
    if session.get("id") != session_id or session.get("status") != "stopped":
        raise UsagePendingError("The cloud sandbox has not confirmed the admitted session is fully stopped.")
    region = session.get("region")
    rates = (
        price_snapshot.get("rates")
        if price_snapshot is not None
        else {
            name: {"cpu_hour_usd": str(cpu), "memory_gb_hour_usd": str(memory)}
            for name, (cpu, memory) in _REGIONAL_RATES.items()
        }
    )
    if not isinstance(region, str) or not isinstance(rates, Mapping) or region not in rates:
        raise UsagePendingError("The cloud sandbox returned a region without a verified price snapshot.")
    if _integer(session.get("vcpus"), "vcpus") != vcpus or _integer(session.get("memory"), "memory") != vcpus * 2048:
        raise UsagePendingError("Cloud sandbox resource allocation differs from the admitted request.")
    started = _integer(session.get("startedAt"), "startedAt")
    stopped = _integer(session.get("stoppedAt"), "stoppedAt")
    if stopped < started:
        raise UsagePendingError("The cloud sandbox returned inconsistent session timestamps.")
    cpu_ms = _integer(session.get("activeCpuDurationMs"), "activeCpuDurationMs")
    network = session.get("networkTransfer")
    if not isinstance(network, Mapping):
        raise UsagePendingError("The cloud sandbox has not reported final network transfer.")
    ingress = _integer(network.get("ingress"), "network ingress")
    egress = _integer(network.get("egress"), "network egress")
    # The public counters are usage, not billing totals. Vercel reports a few
    # KB of control-plane traffic (file uploads, command streams) even for a
    # deny-all sandbox with no exposed ports, which the admitted quote already
    # excludes from billing. Only an unknown admission leaves the transfer
    # unclassified, and charging it as paid egress would overcharge.
    network_cap = _admitted_network_cap(price_snapshot)
    if (ingress or egress) and not network_cap and not _offline_admission(price_snapshot):
        raise UsagePendingError(
            "Offline cloud sandbox transfer needs provider billing classification before settlement."
        )
    # A networked box pays for every byte, control plane included: Vercel's
    # counters do not separate the two, and undercharging is the worse error.
    if network_cap and ingress + egress > network_cap:
        raise UsagePendingError("Cloud sandbox transfer exceeded the admitted network allowance.")
    try:
        regional = rates[region]
        cpu_rate = exact_nonnegative(regional["cpu_hour_usd"])
        memory_rate = exact_nonnegative(regional["memory_gb_hour_usd"])
        creation = exact_nonnegative(price_snapshot["creation_usd"]) if price_snapshot is not None else _CREATION_USD
        network_rate = exact_nonnegative(price_snapshot["network_gb_usd"]) if network_cap else _NETWORK_GB_USD
        if price_snapshot is not None and (
            price_snapshot.get("provider") != "vercel"
            or price_snapshot.get("memory_increment_ms") != 60_000
            or price_snapshot.get("maximum_billable_network_bytes") != network_cap
        ):
            raise ValueError("Unsupported historical billing policy")
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError(
            "The cloud sandbox admission does not contain a usable historical price snapshot."
        ) from error
    return (
        creation
        + Decimal(cpu_ms) * cpu_rate / _MS_PER_HOUR
        + Decimal(_memory_ms(stopped - started) * vcpus * 2) * memory_rate / _MS_PER_HOUR
        + (Decimal(ingress + egress) * network_rate / _BYTES_PER_GB if network_cap else Decimal(0))
    )


def _refused_creation(error: BaseException) -> httpx.Response | None:
    """Return the provider's answer when it refused the create call itself.

    Args:
        error: Failure raised while creating a sandbox.

    Returns:
        The 4xx response to the creation request, or None for any other failure.
    """
    response = getattr(error, "response", None)
    if not isinstance(response, httpx.Response) or not 400 <= response.status_code < 500:
        return None
    request = response.request
    if request.method != "POST" or request.url.host != "vercel.com" or request.url.path != "/api/v3/sandboxes":
        return None
    return response


class VercelUsageReservation:
    """Own coverage and raw control-plane evidence for one sandbox creation."""

    def __init__(self, runtime: BudgetRuntime, request: Mapping[str, Any], *, operation_key: str) -> None:
        """Reserve and claim one physical sandbox creation before any network call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: Final immutable resource and network parameters.
            operation_key: Stable logical identity for creation replay protection.
        """
        self.runtime = runtime
        self.request = dict(request)
        self.vcpus = request["vcpus"]
        self.lifetime_ms = request["lifetime_ms"]
        self.quote = quote_vercel_sandbox(request)
        self.operation = runtime.reserve(self.quote, operation_key=operation_key, cost_kind="sandbox", role="runtime")
        if self.operation.state == "settled":
            raise OperationCompletedError("This sandbox operation already completed; retrieve its saved result.")
        runtime.pin(self.operation.id, cent_units(self.operation.max_cents))
        claimed = runtime.service.mark_dispatched(self.operation.id, runtime.username)
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This sandbox was already dispatched; reconcile its exact session.")
        self.session_id: str | None = None
        self._sessions: dict[str, dict[str, Any]] = {}

    def capture_response(self, response: httpx.Response) -> None:
        """Preserve narrowly scoped provider metadata before Python SDK field filtering.

        Args:
            response: Authenticated Vercel response from this sandbox's dedicated client.
        """
        path = response.request.url.path
        is_create = path == "/api/v3/sandboxes" and response.request.method == "POST"
        # A snapshot stops the session, so its answer carries the final session receipt.
        is_session = (
            re.fullmatch(r"/api/v2/sandboxes/sessions/[^/]+(?:/stop|/extend-timeout|/snapshot)?", path) is not None
        )
        if response.request.url.host != "vercel.com" or not response.is_success or not (is_create or is_session):
            return
        try:
            payload = response.read()
            if len(payload) > 1_000_000:
                return
            body = response.json()
        except (ValueError, httpx.HTTPError):
            return
        session = body.get("session") if isinstance(body, dict) else None
        if isinstance(session, dict) and isinstance(session.get("id"), str):
            self._sessions[session["id"]] = {name: session[name] for name in _SESSION_FIELDS if name in session}
            if is_create:
                self.session_id = session["id"]
                self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, self.session_id)
            # A worker may die between provider shutdown and SDK deserialization.
            # Preserve that receipt before destroying the provider resource.
            self.pending()

    def confirm_created(self, session: Any) -> None:
        """Bind returned provider identity and verify the funded resource envelope.

        Args:
            session: Exact Python SDK runtime-session handle.

        Raises:
            UsagePendingError: When returned provider limits do not match coverage.
        """
        if self.session_id not in {None, session.id}:
            raise UsagePendingError("The cloud sandbox returned a replacement session outside the admitted operation.")
        self.session_id = session.id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, session.id)
        timeout = session.execution_time_limit
        if (
            session.vcpus != self.vcpus
            or session.memory != self.vcpus * 2048
            or session.region not in _REGIONAL_RATES
            or timeout is None
            or math.ceil(timeout.total_seconds() * 1000) > self.lifetime_ms
        ):
            raise UsagePendingError(
                "The cloud sandbox did not confirm the admitted CPU, memory, region, and lifetime limits."
            )

    def extend(self, lifetime_ms: int, *, deadline: float) -> None:
        """Fund the session up to ``lifetime_ms`` before Vercel is asked to keep it alive that long.

        The coverage grows to the quote for the whole extended lifetime, so
        the slices add up to exactly what one up-front quote would hold.

        Args:
            lifetime_ms: The session's total lifetime once extended.
            deadline: ``time.monotonic()`` instant after which waiting for in-flight work stops.

        Raises:
            UnpricedOperationError: When the extended lifetime cannot be bounded.
            UsagePendingError: When covered work did not settle before ``deadline``.
            BudgetReached: When the budget cannot pay for the extension.
        """
        if lifetime_ms <= self.lifetime_ms:
            raise ValueError("An extension must lengthen the funded lifetime.")
        quote = quote_vercel_sandbox({**self.request, "lifetime_ms": lifetime_ms})
        extended = self.runtime.extend(
            self.operation.id,
            quote,
            evidence_key=f"vercel-extend:{lifetime_ms}",
            evidence={"provider": "vercel", "lifetime_ms": lifetime_ms, "request": quote.request_fingerprint},
            deadline=deadline,
        )
        self.runtime.pin(self.operation.id, cent_units(extended.max_cents))
        self.lifetime_ms = lifetime_ms

    def confirm_extended(self, session: Any) -> None:
        """Verify Vercel kept the session within its funded lifetime.

        Args:
            session: The session handle Vercel returned from the extension.

        Raises:
            UsagePendingError: When the returned limit is missing or exceeds the funded lifetime.
        """
        timeout = session.execution_time_limit
        if (
            session.id != self.session_id
            or timeout is None
            or math.ceil(timeout.total_seconds() * 1000) > self.lifetime_ms
        ):
            raise UsagePendingError("The cloud sandbox extended the session beyond its funded lifetime.")

    def pending(self) -> None:
        """Retain covered funding when creation, shutdown, or billing evidence is uncertain."""
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            evidence = {"provider": "vercel", "session_id": self.session_id, "sessions": self._sessions}
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(evidence),
                evidence=evidence,
            )

    def fail(self, error: BaseException) -> None:
        """Release the hold when the provider refused creation outright, otherwise retain it.

        A 4xx answer to the create call proves no session came into being, so
        its coverage goes back to the budget instead of waiting for a
        reconciliation with nothing to reconcile. Every other failure,
        including a lost response, keeps the hold pending.

        Args:
            error: Failure raised while creating or confirming the sandbox.
        """
        response = _refused_creation(error) if self.session_id is None and not self._sessions else None
        if response is not None:
            try:
                self.runtime.service.reject(
                    self.operation.id,
                    self.runtime.username,
                    evidence_key=f"vercel-refusal:{self.operation.id}",
                    evidence={
                        "provider": "vercel",
                        "status_code": response.status_code,
                        "message": str(error)[:1000],
                    },
                )
                self.runtime.unpin(self.operation.id)
                return
            except BudgetError:
                pass
        self.pending()

    def settle(self) -> None:
        """Settle one fully stopped session at actual cost, retaining uncertainty.

        Raises:
            UsagePendingError: When final provider evidence is missing or ambiguous.
            BudgetError: When provider evidence violates reserved funding bounds.
        """
        try:
            if self.session_id is None:
                raise UsagePendingError("Cloud sandbox creation has not returned an exact session identity.")
            session = self._sessions.get(self.session_id, {})
            usd = vercel_actual_usd(
                session, session_id=self.session_id, vcpus=self.vcpus, price_snapshot=self.quote.price_snapshot
            )
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"vercel-stop:{self.session_id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={"provider": "vercel", "source": _SCHEMA_URL, "session": session, "provider_usd": str(usd)},
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending()
            raise


def _snapshot_usd(size_bytes: int, retained_ms: int, rate: Decimal) -> Decimal:
    """Price stored snapshot bytes for a retention time.

    Args:
        size_bytes: Stored snapshot size.
        retained_ms: Time the snapshot existed.
        rate: Storage price in USD per GB-month.

    Returns:
        The storage cost in USD.
    """
    return Decimal(size_bytes) / _BYTES_PER_GB * Decimal(retained_ms) / _MS_PER_HOUR * rate / _HOURS_PER_MONTH


def quote_vercel_snapshot(request: Mapping[str, Any]) -> OperationQuote:
    """Price one run-scoped snapshot before the snapshot call.

    Args:
        request: The snapshot parameters with ``session_id`` and ``retention_ms``,
            the longest time the snapshot can be stored. Vercel counts the
            expiry from the last use, so this is the latest last use plus
            the expiry.

    Returns:
        Coverage for the size cap stored for the whole retention.

    Raises:
        UnpricedOperationError: When the retention is missing or unbounded.
    """
    retention_ms = request.get("retention_ms")
    if (
        isinstance(retention_ms, bool)
        or not isinstance(retention_ms, int)
        or not 0 < retention_ms <= _SNAPSHOT_RETENTION_CEILING_MS
        or not isinstance(request.get("session_id"), str)
    ):
        raise UnpricedOperationError("A Vercel snapshot needs a bounded expiry and an exact source session.")
    maximum = _snapshot_usd(SANDBOX_SNAPSHOT_BYTES_CAP, retention_ms, _SNAPSHOT_GB_MONTH_USD)
    return operation_quote(
        request,
        maximum,
        ChargePolicy("sandbox"),
        {
            "provider": "vercel",
            "resource": "snapshot",
            "price_version": _PRICE_VERSION,
            "source": _PRICING_URL,
            "storage_gb_month_usd": str(_SNAPSHOT_GB_MONTH_USD),
            "hours_per_month": str(_HOURS_PER_MONTH),
            "maximum_snapshot_bytes": SANDBOX_SNAPSHOT_BYTES_CAP,
            "request": dict(request),
        },
    )


def vercel_snapshot_actual_usd(
    snapshot: Mapping[str, Any], *, snapshot_id: str, price_snapshot: Mapping[str, Any]
) -> Decimal:
    """Price a deleted snapshot from its provider size and lifetime.

    Args:
        snapshot: The provider state of the snapshot after the delete call.
        snapshot_id: The one admitted snapshot.
        price_snapshot: The admitted snapshot quote.

    Returns:
        The stored bytes times the time from creation to deletion, capped at the expiry.

    Raises:
        UsagePendingError: When the snapshot is not deleted or its evidence is uncertain.
    """
    if snapshot.get("id") != snapshot_id or snapshot.get("status") != "deleted":
        raise UsagePendingError("The cloud sandbox snapshot is not confirmed deleted.")
    size = _integer(snapshot.get("size_bytes"), "snapshot size")
    created = _integer(snapshot.get("created_at"), "snapshot created_at")
    ended = _integer(snapshot.get("updated_at"), "snapshot updated_at")
    expires = snapshot.get("expires_at")
    if expires is not None:
        ended = min(ended, _integer(expires, "snapshot expires_at"))
    if ended < created:
        raise UsagePendingError("The cloud sandbox snapshot returned inconsistent timestamps.")
    try:
        cap = price_snapshot["maximum_snapshot_bytes"]
        rate = exact_nonnegative(price_snapshot["storage_gb_month_usd"])
        if price_snapshot.get("provider") != "vercel" or price_snapshot.get("resource") != "snapshot":
            raise ValueError("Unsupported snapshot billing policy")
    except (KeyError, TypeError, ValueError) as error:
        raise UsagePendingError("The snapshot admission has no usable price snapshot.") from error
    if size > cap:
        raise UsagePendingError("The cloud sandbox snapshot is larger than its admitted size cap.")
    return _snapshot_usd(size, ended - created, rate)


class VercelSnapshotReservation:
    """Own the sandbox-cost hold of one run-scoped snapshot until it is deleted."""

    def __init__(self, runtime: BudgetRuntime, request: Mapping[str, Any], *, operation_key: str) -> None:
        """Reserve and claim one snapshot before the snapshot call.

        Args:
            runtime: Authenticated budget authority retained outside the guest.
            request: The parameters that :func:`quote_vercel_snapshot` prices.
            operation_key: Stable logical identity of the snapshot.

        Raises:
            OperationCompletedError: The snapshot operation already settled.
            UsagePendingError: Another caller already dispatched it.
        """
        self.runtime = runtime
        self.quote = quote_vercel_snapshot(request)
        self.operation = runtime.reserve(self.quote, operation_key=operation_key, cost_kind="sandbox", role="runtime")
        if self.operation.state == "settled":
            raise OperationCompletedError("This snapshot operation already completed.")
        runtime.pin(self.operation.id, cent_units(self.operation.max_cents))
        claimed = runtime.service.mark_dispatched(self.operation.id, runtime.username)
        if not claimed.dispatch_claimed:
            raise UsagePendingError("This snapshot was already dispatched; reconcile it by hand.")
        self.snapshot_id: str | None = None

    def bind(self, snapshot_id: str) -> None:
        """Record the provider identity of the created snapshot.

        Args:
            snapshot_id: The Vercel snapshot id.
        """
        self.snapshot_id = snapshot_id
        self.runtime.service.mark_dispatched(self.operation.id, self.runtime.username, snapshot_id)

    def pending(self, evidence: Mapping[str, Any] | None = None) -> None:
        """Keep the hold when the snapshot result or its deletion is uncertain.

        Args:
            evidence: The last provider state of the snapshot, or ``None``.
        """
        current = self.runtime.service.get_operation(self.operation.id, self.runtime.username)
        if current.state in {"dispatched", "pending"}:
            record = {"provider": "vercel", "snapshot_id": self.snapshot_id, "snapshot": dict(evidence or {})}
            self.runtime.service.mark_pending(
                self.operation.id,
                self.runtime.username,
                evidence_key=json_fingerprint(record),
                evidence=record,
            )

    def settle(self, snapshot: Mapping[str, Any]) -> None:
        """Settle a deleted snapshot at its stored size and lifetime.

        Args:
            snapshot: The provider state of the snapshot after the delete call.

        Raises:
            UsagePendingError: When the deletion or the size is uncertain.
            BudgetError: When the evidence breaks the reserved bounds.
        """
        try:
            if self.snapshot_id is None:
                raise UsagePendingError("The snapshot call returned no snapshot identity.")
            usd = vercel_snapshot_actual_usd(
                snapshot, snapshot_id=self.snapshot_id, price_snapshot=self.quote.price_snapshot
            )
            charge = vercel_charge_policy(self.quote.price_snapshot).convert(usd)
            self.runtime.service.settle(
                self.operation.id,
                self.runtime.username,
                evidence_key=f"vercel-snapshot-delete:{self.snapshot_id}",
                actual_cents=charge.total,
                actual_wallet_cents=charge.wallet,
                evidence={"provider": "vercel", "snapshot": dict(snapshot), "provider_usd": str(usd)},
            )
            self.runtime.unpin(self.operation.id)
        except BaseException:
            self.pending(snapshot)
            raise


def vercel_charge_policy(price_snapshot: Mapping[str, Any]) -> ChargePolicy:
    """Recover the admitted cent conversion without applying newer defaults.

    Snapshots recorded before the usage markup existed carry a markup of 1.0 and
    keep settling at cost.

    Args:
        price_snapshot: Original immutable sandbox quote.

    Returns:
        The original sandbox-to-wallet conversion.

    Raises:
        UsagePendingError: When the recorded policy cannot be reconstructed safely.
    """
    policy = price_snapshot.get("policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("kind") != "sandbox"
        or policy.get("version") != "skynet-operation-pricing-v1"
    ):
        raise UsagePendingError("The cloud sandbox operation has no supported cent conversion policy.")
    try:
        cent_usd = exact_nonnegative(policy.get("cent_usd"))
        if cent_usd <= 0:
            raise ValueError("Missing positive cent conversion")
        markup = exact_nonnegative(policy.get("model_markup", "1"))
        return ChargePolicy("sandbox", cent_usd=cent_usd, model_markup=markup)
    except ValueError as error:
        raise UsagePendingError("The cloud sandbox operation has no usable historical cent conversion.") from error

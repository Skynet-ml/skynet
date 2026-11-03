"""Price immutable provider operation bounds without estimate-table fallbacks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Literal

from .pricing import BYOK_FEE_FRACTION, CENT_USD_VALUE, usage_markup

_CENT_QUANTUM = Decimal("0.000000001")
_BYOK_FEE = Decimal(str(BYOK_FEE_FRACTION))


class UnpricedOperationError(ValueError):
    """Reject a request whose applicable billable categories cannot be bounded."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Keep a reason and an optional structured route refusal code.

        Args:
            message: Original reason for the refusal.
            code: Stable translation key for the refusal, if available.
        """
        super().__init__(message)
        self.code = code


def exact_nonnegative(value: Any) -> Decimal:
    """Read a provider decimal without treating absent or invalid prices as zero.

    Args:
        value: Explicit finite, nonnegative provider price or measured cost.

    Returns:
        Decimal amount preserving the provider's precision.

    Raises:
        UnpricedOperationError: When evidence is missing, negative, or nonfinite.
    """
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return amount
    except (InvalidOperation, ValueError, TypeError) as error:
        raise UnpricedOperationError("Verified nonnegative provider pricing is required.") from error


def json_fingerprint(value: Any) -> str:
    """Hash the exact resolved JSON request without storing private prompt text."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class OperationCharge:
    """Separate the approved spending scope from the amount payable to Skynet."""

    total: Decimal
    wallet: Decimal


@dataclass(frozen=True)
class ChargePolicy:
    """Retain the approved conversion policy with each operation's price snapshot."""

    kind: Literal["managed_model", "byok_model", "sandbox", "subscription_model"]
    cent_usd: Decimal = Decimal(str(CENT_USD_VALUE))
    # Read at construction, not import, so a policy snapshots the markup in force when it was quoted.
    model_markup: Decimal = field(default_factory=lambda: Decimal(str(usage_markup())))
    byok_fee_fraction: Decimal = _BYOK_FEE

    def convert(self, provider_usd: Decimal) -> OperationCharge:
        """Convert measured or maximum USD consistently without per-call cent rounding.

        Args:
            provider_usd: Applicable raw provider charge, including billable categories.

        Returns:
            Exact scope and wallet amounts rounded only to ledger precision.
        """
        raw = exact_nonnegative(provider_usd) / self.cent_usd
        if self.kind == "subscription_model":
            # The user's own CLI subscription pays the provider; Skynet charges nothing.
            return OperationCharge(Decimal(0), Decimal(0))
        if self.kind == "byok_model":
            # Retain the snapshot fraction for historical operations.
            wallet = total = raw * self.byok_fee_fraction
        else:
            wallet = total = raw * self.model_markup
        return OperationCharge(
            total.quantize(_CENT_QUANTUM, rounding=ROUND_CEILING),
            wallet.quantize(_CENT_QUANTUM, rounding=ROUND_CEILING),
        )

    def snapshot(self) -> dict[str, Any]:
        """Return the immutable conversion inputs used for admission and settlement."""
        return {
            "version": "skynet-operation-pricing-v1",
            "kind": self.kind,
            "cent_usd": str(self.cent_usd),
            "model_markup": str(self.model_markup),
            "byok_fee_fraction": str(self.byok_fee_fraction),
        }


@dataclass(frozen=True)
class OperationQuote:
    """Bind an enforceable physical request to its applicable maximum charges."""

    request_fingerprint: str
    maximum: OperationCharge
    price_snapshot: Mapping[str, Any]


def operation_quote(
    request: Mapping[str, Any], maximum_usd: Decimal, policy: ChargePolicy, evidence: Mapping[str, Any]
) -> OperationQuote:
    """Bind a final request and verified bound to the same recorded pricing policy.

    Args:
        request: Final routed request after every caller and provider override.
        maximum_usd: Verified maximum raw cost enforced by the dispatch adapter.
        policy: Approved model or sandbox conversion.
        evidence: Provider prices, enforced resource limits, and quote provenance.

    Returns:
        Immutable admission inputs with no generic pricing fallback.
    """
    snapshot = {**dict(evidence), "policy": policy.snapshot()}
    snapshot["version"] = json_fingerprint(snapshot)
    return OperationQuote(json_fingerprint(request), policy.convert(maximum_usd), snapshot)

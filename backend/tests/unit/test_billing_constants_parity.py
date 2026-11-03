"""Pin the frontend's copies of billing constants to the backend's.

The wallet endpoint serves the usage markup and fee terms, but the frontend
seeds its first paint from ``DEFAULT_PRICING_TERMS``; top-up packs, the Pro
price and the fallback token rates are hand-copied outright. The backend
charges the real amounts; this test parses the frontend sources and fails the
moment a copy drifts, so the UI never quotes a price it will not charge.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.api.routers.billing import _pricing_terms
from core.billing import pricing, service
from core.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BILLING_LIB = _REPO_ROOT / "frontend" / "src" / "features" / "billing" / "lib"
_PROVISION_STRIPE = _REPO_ROOT / "backend" / "scripts" / "provision_stripe.py"


def _ts_number_constants(filename: str) -> dict[str, float]:
    """Extract ``export const NAME = <number>;`` constants from a billing module.

    Args:
        filename: Module file name under ``frontend/src/features/billing/lib``.

    Returns:
        Mapping of constant name to its numeric value.
    """
    source = (_BILLING_LIB / filename).read_text(encoding="utf-8")
    pairs = re.findall(r"export const (\w+) = ([0-9][0-9_.eE+-]*);", source)
    return {name: float(value.replace("_", "")) for name, value in pairs}


def test_pricing_constants_match_backend() -> None:
    """The run-cost estimate constants equal ``core.billing.pricing``."""
    ts = _ts_number_constants("pricing.ts")
    assert ts["DEFAULT_INPUT_COST_PER_TOKEN"] == pytest.approx(pricing.FALLBACK_INPUT_COST_PER_TOKEN)
    assert ts["DEFAULT_OUTPUT_COST_PER_TOKEN"] == pytest.approx(pricing.FALLBACK_OUTPUT_COST_PER_TOKEN)


def test_default_pricing_terms_match_backend_defaults() -> None:
    """``DEFAULT_PRICING_TERMS`` equals the terms the wallet serves when nothing is overridden."""
    source = (_BILLING_LIB / "wallet.ts").read_text(encoding="utf-8")
    block = re.search(r"DEFAULT_PRICING_TERMS: PricingTerms = \{([^}]*)\}", source)
    assert block is not None
    terms = {name: float(value) for name, value in re.findall(r"(\w+):\s*([0-9.]+)", block.group(1))}
    assert terms["usageMarkup"] == pytest.approx(Settings.model_fields["usage_markup"].default)
    assert terms["byokFeeFraction"] == _pricing_terms().byok_fee_fraction == pricing.BYOK_FEE_FRACTION == 0
    assert terms["purchaseFeeRate"] == pytest.approx(service.PURCHASE_FEE_RATE)
    assert terms["purchaseFeeFixedCents"] == service.PURCHASE_FEE_FIXED_CENTS


def test_money_constants_match_backend() -> None:
    """Cent value and custom top-up bounds equal the backend's."""
    ts = _ts_number_constants("wallet.ts")
    assert ts["CENT_USD_VALUE"] == pytest.approx(pricing.CENT_USD_VALUE)
    assert ts["CUSTOM_CENTS_MIN"] == service.CUSTOM_CENTS_MIN
    assert ts["CUSTOM_CENTS_MAX"] == service.CUSTOM_CENTS_MAX


def test_top_up_packs_match_backend() -> None:
    """``TOP_UP_PACKS`` lists the backend's packs, each priced at its cent value."""
    source = (_BILLING_LIB / "wallet.ts").read_text(encoding="utf-8")
    packs = re.findall(r'\{\s*id:\s*"(\w+)",\s*cents:\s*(\d+),\s*usd:\s*([0-9.]+)', source)
    assert {pack_id: int(cents) for pack_id, cents, _ in packs} == service.PACK_CENTS
    for _, cents, usd in packs:
        assert float(usd) == pytest.approx(int(cents) * pricing.CENT_USD_VALUE)


def test_pro_monthly_price_matches_stripe_provisioning() -> None:
    """``PRO_MONTHLY_USD`` equals the amount the Stripe Pro price is provisioned at."""
    ts = _ts_number_constants("wallet.ts")
    match = re.search(r"_PRO_MONTHLY\b[^=]*=\s*\([^)]*?(\d+)\s*\)", _PROVISION_STRIPE.read_text(encoding="utf-8"))
    assert match is not None
    assert round(ts["PRO_MONTHLY_USD"] * 100) == int(match.group(1))

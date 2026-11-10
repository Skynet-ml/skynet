"""Provision the Stripe products and prices that the billing code expects.

Run once (per Stripe account / mode) after setting ``STRIPE_SECRET_KEY`` in
``backend/.env``. Idempotent: prices are keyed by a stable ``lookup_key``, so
re-running reuses what already exists instead of duplicating. On success it
prints the ``STRIPE_PRICE_*`` env lines to paste back into ``backend/.env``.

    cd backend && python scripts/provision_stripe.py
"""

from __future__ import annotations

import sys

import stripe

from core.brand import APP_NAME
from core.config import settings

# (lookup_key, display name, unit amount in cents). The cents each pack grants
# live in core.billing.service.PACK_CENTS — Stripe only holds the dollar price.
_PACKS: list[tuple[str, str, int]] = [
    ("skynet_pack_starter", f"{APP_NAME} top-up — Starter", 500),
    ("skynet_pack_plus", f"{APP_NAME} top-up — Plus", 2000),
    ("skynet_pack_pro", f"{APP_NAME} top-up — Pro", 5000),
]


def _find_price(lookup_key: str) -> str | None:
    """Return an existing active price id for ``lookup_key``, or None.

    Args:
        lookup_key: Stable lookup key the price was created with.

    Returns:
        The matching price id, or ``None`` when none exists yet.
    """
    existing = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1)
    return existing.data[0].id if existing.data else None


def _ensure_price(lookup_key: str, name: str, unit_amount: int) -> str:
    """Return the price id for a top-up pack, creating it if absent.

    Args:
        lookup_key: Stable idempotency key; a re-run reuses the matching price.
        name: Product display name (an inline product is created with it).
        unit_amount: Price in the smallest currency unit (cents).

    Returns:
        The Stripe price id.
    """
    found = _find_price(lookup_key)
    if found:
        print(f"  reuse  {lookup_key} -> {found}")
        return found
    price = stripe.Price.create(
        currency="usd",
        unit_amount=unit_amount,
        lookup_key=lookup_key,
        product_data={"name": name},
    )
    print(f"  create {lookup_key} -> {price.id}")
    return price.id


def main() -> int:
    """Provision every Stripe price and print the env lines to set.

    Returns:
        Process exit code (0 on success, 1 when Stripe is unconfigured).
    """
    if settings.stripe_secret_key is None:
        print("STRIPE_SECRET_KEY is not set in backend/.env — nothing to do.", file=sys.stderr)
        return 1
    stripe.api_key = settings.stripe_secret_key.get_secret_value()

    print("Provisioning top-up packs ...")
    pack_ids = {key: _ensure_price(key, name, amount) for key, name, amount in _PACKS}

    print("\nDone. Paste these into backend/.env:\n")
    print(f"STRIPE_PRICE_PACK_STARTER={pack_ids['skynet_pack_starter']}")
    print(f"STRIPE_PRICE_PACK_PLUS={pack_ids['skynet_pack_plus']}")
    print(f"STRIPE_PRICE_PACK_PRO={pack_ids['skynet_pack_pro']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

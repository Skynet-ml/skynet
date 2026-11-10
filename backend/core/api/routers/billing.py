"""Prepaid-balance billing routes: wallet, usage, Stripe records, and checkout.

Backs the in-app wallet and the add-funds/paywall page — a pay-as-you-go
prepaid balance is the only plan.
Authenticated routes resolve the caller via the shared session-JWT/PAT
dependency and key every operation on ``user.username`` (the lowercased email).
The webhook route is deliberately unauthenticated — Stripe can't present a
bearer token — and instead verifies the event's signature in the service.

Ledger reads succeed even when Stripe is unconfigured (free-tier zeros);
Stripe-backed reads report unavailability, and mutations raise
``DomainError("billing.not_configured", 503)``.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import AliasChoices, BaseModel, Field

from ...billing import ProviderKeyVault, StripeBillingService, openrouter_oauth
from ...billing.byok_vault import ProviderKeyView, ResolvedConnection
from ...billing.pricing import BYOK_FEE_FRACTION, usage_markup
from ...billing.service import (
    CUSTOM_CENTS_MAX,
    CUSTOM_CENTS_MIN,
    PURCHASE_FEE_FIXED_CENTS,
    PURCHASE_FEE_RATE,
    BillingProfileSnapshot,
)
from ...brand import APP_NAME
from ...config import settings
from ...local import hosted_link
from ...provider_registry import BYOK_TO_LITELLM_PROVIDER
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_catalog import (
    CatalogModel,
    CatalogProvider,
    ModelCatalogResponse,
    get_byok_catalog_cached,
    narrow_models_to_served,
    probe_byok_provider_models,
    with_effort_controls,
)
from .models import discover_models_at_endpoint

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

# Default usage window when the dashboard omits an explicit range.
USAGE_DEFAULT_WINDOW_DAYS = 30


def _parse_instant(value: str | None, default: datetime) -> datetime:
    """Parse an ISO-8601 query value into a UTC-aware instant, or fall back.

    Tolerates a trailing ``Z`` and a naive value (assumed UTC). A malformed or
    missing value yields ``default`` rather than a 422, so a stray query string
    degrades to the default window instead of failing the dashboard read.

    Args:
        value: Raw query-string value, or ``None`` when the param was omitted.
        default: Instant returned when ``value`` is missing or unparseable.

    Returns:
        A timezone-aware UTC datetime.
    """
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# Live ``/models`` listings are refetched per account at most this often; a key
# save/verify/remove clears the entry immediately so the picker never lags a
# mutation. The signature guards against a stale hit after an out-of-band change.
_BYOK_USER_CATALOG_TTL_SECONDS = 300
_byok_user_catalog_cache: dict[str, tuple[tuple, float, ModelCatalogResponse]] = {}
_byok_user_catalog_lock = Lock()


def _invalidate_byok_user_catalog(username: str | None = None) -> None:
    """Drop the cached per-account BYOK catalog for one account, or all.

    Args:
        username: Account whose entry is dropped; ``None`` clears every entry.
    """
    with _byok_user_catalog_lock:
        if username is None:
            _byok_user_catalog_cache.clear()
        else:
            _byok_user_catalog_cache.pop(username, None)


def _live_model_index(view: ProviderKeyView, resolved: ResolvedConnection | None) -> dict[str, dict] | None:
    """Fetch the models one verified connection can actually reach.

    A custom endpoint is discovered through the SSRF-validated path (uncapped,
    since this feeds a user-facing picker rather than an agent tool); a native
    provider key is probed at the provider's own ``/models`` URL.

    Args:
        view: The verified connection's masked view.
        resolved: Its decrypted secret and endpoint, or ``None`` when the vault
            could not decrypt it.

    Returns:
        The reachable model index keyed by ID, or ``None`` when nothing could
        be listed and the caller should fall back to the static registry.
    """
    if resolved is None:
        return None
    if view.api_base:
        discovered = discover_models_at_endpoint(view.api_base, resolved.secret, limit=None)
        if discovered.error:
            logger.warning("BYOK model discovery for %s failed: %s", view.provider, discovered.error)
            return None
        return {model_id: {} for model_id in discovered.models}
    prefix = BYOK_TO_LITELLM_PROVIDER.get(view.provider, view.provider)
    return probe_byok_provider_models(prefix, resolved.secret)


def _custom_endpoint_models(view: ProviderKeyView, model_ids: list[str]) -> list[CatalogModel]:
    """Shape discovered custom-endpoint ids as catalog entries.

    Args:
        view: The custom connection the ids were discovered through.
        model_ids: Raw ids the endpoint listed.

    Returns:
        One ``openai/``- or ``openrouter/``-prefixed entry per non-empty id.
    """
    models: list[CatalogModel] = []
    seen: set[str] = set()
    for model_id in model_ids:
        bare = model_id.strip().strip("/")
        if not bare:
            continue
        if view.provider == "openrouter":
            value = f"openrouter/{bare.removeprefix('openrouter/')}"
        else:
            value = f"openai/{bare.removeprefix('openai/')}"
        if value in seen:
            continue
        seen.add(value)
        models.append(
            CatalogModel(
                value=value,
                label=bare,
                provider=view.provider,
                byok_provider=view.provider,
                available=True,
            )
        )
    return models


def _byok_catalog_for_user(vault: ProviderKeyVault, username: str) -> ModelCatalogResponse:
    """List only the models the account's verified BYOK keys can actually run.

    Each verified connection contributes exactly the models routable through
    it: a custom endpoint's own listing, or the static registry entries for
    that provider narrowed to what its native ``/models`` endpoint reports for
    the user's key. Providers without a verified key contribute nothing, so an
    OpenRouter-served model can never appear under a direct-provider key (or
    vice versa). When a provider exposes no listing (Anthropic, Gemini, Cohere)
    or the probe fails, the static registry list stands in.

    Args:
        vault: Encrypted provider-connection vault.
        username: Account whose verified connections are surfaced.

    Returns:
        Account-scoped catalog; empty when the account has no verified key.
    """
    verified = [view for view in vault.list_keys(username).keys if view.status == "verified"]
    signature = tuple((view.id, view.provider, view.api_base, view.added_at) for view in verified)
    now = time.monotonic()
    with _byok_user_catalog_lock:
        hit = _byok_user_catalog_cache.get(username)
        if hit is not None and hit[0] == signature and now - hit[1] < _BYOK_USER_CATALOG_TTL_SECONDS:
            return hit[2]
    if not verified:
        return ModelCatalogResponse(providers=[], models=[])

    base = get_byok_catalog_cached()
    static_by_prefix: dict[str, list[CatalogModel]] = {}
    for model in base.models:
        static_by_prefix.setdefault(model.provider, []).append(model)
    static_providers = {provider.slug: provider for provider in base.providers}

    # Decrypt on the request thread — the vault's SQL session is not shared
    # across threads — and only fan the network listings out to the pool.
    jobs: list[tuple[ProviderKeyView, ResolvedConnection | None]] = []
    for view in verified:
        try:
            jobs.append((view, vault.resolve_connection(username, view.provider)))
        except DomainError as exc:
            logger.warning("BYOK connection %s is unusable for %s: %s", view.provider, username, exc.detail)
            jobs.append((view, None))
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        listings = list(pool.map(lambda job: _live_model_index(*job), jobs))

    providers: list[CatalogProvider] = []
    models: list[CatalogModel] = []
    for (view, _resolved), listing in zip(jobs, listings, strict=True):
        prefix = BYOK_TO_LITELLM_PROVIDER.get(view.provider, view.provider)
        static = static_by_prefix.get(prefix, [])
        if view.api_base and listing is not None:
            provider_models = _custom_endpoint_models(view, list(listing))
        elif listing is not None:
            provider_models = narrow_models_to_served(prefix, static, listing, byok_provider=view.provider)
        else:
            provider_models = [model.model_copy(update={"byok_provider": view.provider}) for model in static]
        if not provider_models:
            continue
        if view.api_base:
            static_label = static_providers[prefix].label if prefix in static_providers else view.provider
            providers.append(
                CatalogProvider(
                    slug=view.provider,
                    label=view.label or static_label,
                    default_base_url=view.api_base,
                    has_env_key=True,
                )
            )
        elif prefix in static_providers:
            providers.append(static_providers[prefix].model_copy(update={"has_env_key": True}))
        else:
            providers.append(CatalogProvider(slug=prefix, label=view.label or view.provider, has_env_key=True))
        models.extend(provider_models)

    response = with_effort_controls(ModelCatalogResponse(providers=providers, models=models))
    with _byok_user_catalog_lock:
        _byok_user_catalog_cache[username] = (signature, now, response)
    return response


class FreeGrantResponse(BaseModel):
    """The account's one-time free grant."""

    cents_remaining: int = Field(description="Cents left in the grant.")
    cents_total: int = Field(description="Full grant size (500).")


class UsageEntryResponse(BaseModel):
    """One row of the wallet usage ledger."""

    id: str = Field(description="Stable row id.")
    at: str = Field(description="ISO-8601 instant the entry was recorded.")
    label: str = Field(description="Human label for the row (a run name, 'Top-up', etc.).")
    model: str | None = Field(default=None, description="Model id for a run row, else null.")
    cents: int = Field(description="Signed delta in cents: negative for a spend.")
    kind: str = Field(description="Entry kind: 'run', 'topup', or 'grant'.")


class PricingTermsResponse(BaseModel):
    """The platform's live pricing terms, so client estimates never hardcode them."""

    usage_markup: float = Field(description="Multiplier on provider cost for platform-paid tokens and sandbox compute.")
    byok_fee_fraction: float = Field(
        description="Skynet model fee for BYOK calls, as a fraction of provider cost; currently zero."
    )
    purchase_fee_rate: float = Field(description="Top-up service fee, as a fraction of the top-up amount.")
    purchase_fee_fixed_cents: int = Field(description="Flat top-up service fee, in cents.")


def _pricing_terms() -> PricingTermsResponse:
    """Snapshot the current pricing terms from the billing modules.

    Returns:
        The current markup, BYOK fee fraction (zero), and top-up fee.
    """
    return PricingTermsResponse(
        usage_markup=usage_markup(),
        byok_fee_fraction=BYOK_FEE_FRACTION,
        purchase_fee_rate=PURCHASE_FEE_RATE,
        purchase_fee_fixed_cents=PURCHASE_FEE_FIXED_CENTS,
    )


class WalletResponse(BaseModel):
    """The caller's wallet: purchased balance, free grant, recent ledger."""

    paid_balance_cents: int = Field(description="Purchased balance in cents, on top of the free grant.")
    free_grant: FreeGrantResponse
    usage: list[UsageEntryResponse] = Field(default_factory=list, description="Most-recent-first ledger rows.")
    pricing: PricingTermsResponse = Field(default_factory=_pricing_terms)


class UsageDayResponse(BaseModel):
    """One day's billed run spend, in cents."""

    date: str = Field(description="Calendar day (YYYY-MM-DD, UTC).")
    billed_cents: int = Field(description="Gross run spend billed that day, in cents.")
    runs: int = Field(default=0, description="Billed runs (debited ledger rows) that day.")
    input_tokens: int = Field(default=0, description="Measured input tokens behind that day's billed runs.")
    output_tokens: int = Field(default=0, description="Measured output tokens behind that day's billed runs.")


class UsageModelResponse(BaseModel):
    """One model's share of run spend over the window."""

    model: str | None = Field(default=None, description="Model id, or null for runs without one.")
    cents: int = Field(description="Gross run spend billed to this model, in cents.")
    runs: int = Field(description="Billed runs attributed to this model.")
    input_tokens: int = Field(default=0, description="Measured input tokens behind this model's billed runs.")
    output_tokens: int = Field(default=0, description="Measured output tokens behind this model's billed runs.")


class UsageResponse(BaseModel):
    """Date-ranged usage rollup for the billing Usage dashboard."""

    start: str = Field(description="ISO-8601 inclusive window start.")
    end: str = Field(description="ISO-8601 inclusive window end.")
    billed_cents: int = Field(description="Gross run spend billed across the window, in cents.")
    runs: int = Field(description="Billed runs across the window.")
    by_day: list[UsageDayResponse] = Field(default_factory=list, description="Per-day spend series, ascending by date.")
    by_model: list[UsageModelResponse] = Field(
        default_factory=list, description="Per-model spend series, descending by spend."
    )
    entries: list[UsageEntryResponse] = Field(
        default_factory=list, description="Most-recent-first raw ledger rows in the window."
    )


class BillingAddressResponse(BaseModel):
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None


class PaymentMethodResponse(BaseModel):
    id: str
    type: str
    brand: str | None = None
    last4: str | None = None
    exp_month: int | None = None
    exp_year: int | None = None
    is_default: bool = False
    holder_name: str | None = None


# Edit a saved payment method. Expiry is applied only when both month and year
# are sent; a null holder name leaves the stored name unchanged.
class PaymentMethodUpdateRequest(BaseModel):
    exp_month: int | None = Field(default=None, ge=1, le=12)
    exp_year: int | None = Field(default=None, ge=2000, le=2100)
    holder_name: str | None = Field(default=None, max_length=200)
    make_default: bool = False


class BillingProfileResponse(BaseModel):
    available: bool
    has_customer: bool
    email: str | None = None
    name: str | None = None
    phone: str | None = None
    address: BillingAddressResponse = Field(default_factory=BillingAddressResponse)
    payment_methods: list[PaymentMethodResponse] = Field(default_factory=list)


def _profile_response(snapshot: BillingProfileSnapshot) -> BillingProfileResponse:
    """Map a service billing-profile snapshot onto the API response model.

    Args:
        snapshot: Stripe-backed profile read by the billing service.

    Returns:
        The response with masked payment-method metadata.
    """
    return BillingProfileResponse(
        available=snapshot.available,
        has_customer=snapshot.has_customer,
        email=snapshot.email,
        name=snapshot.name,
        phone=snapshot.phone,
        address=BillingAddressResponse(
            line1=snapshot.address.line1,
            line2=snapshot.address.line2,
            city=snapshot.address.city,
            state=snapshot.address.state,
            postal_code=snapshot.address.postal_code,
            country=snapshot.address.country,
        ),
        payment_methods=[
            PaymentMethodResponse(
                id=method.id,
                type=method.type,
                brand=method.brand,
                last4=method.last4,
                exp_month=method.exp_month,
                exp_year=method.exp_year,
                is_default=method.is_default,
                holder_name=method.holder_name,
            )
            for method in snapshot.payment_methods
        ],
    )


class BillingTransactionResponse(BaseModel):
    id: str
    at: str
    amount: int
    currency: str
    status: str
    cents: int | None = None
    pack_id: str | None = None
    document_url: str | None = None


class BillingTransactionsResponse(BaseModel):
    available: bool
    entries: list[BillingTransactionResponse] = Field(default_factory=list)


# Request to start a top-up checkout: either a named pack (starter/plus/pro)
# or a custom amount in cents. Exactly one of the two selects the purchase;
# ``cents`` wins when both are sent. A page loaded before the credits-to-cents
# rename still sends ``credits``, so that name is accepted too.
class CheckoutRequest(BaseModel):
    pack_id: str | None = Field(default=None, description="Top-up pack to buy: 'starter', 'plus', or 'pro'.")
    cents: int | None = Field(
        default=None,
        ge=CUSTOM_CENTS_MIN,
        le=CUSTOM_CENTS_MAX,
        validation_alias=AliasChoices("cents", "credits"),
        description="Custom top-up amount in US cents, instead of a pack.",
    )


class PortalSessionRequest(BaseModel):
    flow: Literal["manage", "payment_method"] = "manage"


class CheckoutSessionResponse(BaseModel):
    """A hosted Stripe URL the client should redirect the browser to."""

    url: str = Field(description="Absolute Stripe-hosted Checkout or portal URL.")


class WebhookAck(BaseModel):
    """Acknowledgement that a webhook event was received and applied."""

    received: bool = Field(description="Always true once the event was verified and applied.")


class ProviderKeyResponse(BaseModel):
    """One stored BYOK provider connection as the settings UI sees it — never the secret."""

    id: str = Field(description="Stable handle for the connection.")
    provider: str = Field(description="Provider slug the connection belongs to (e.g. 'openai').")
    label: str | None = Field(default=None, description="Optional user-facing name for the connection.")
    last4: str = Field(description="Masked tail of the secret, for recognition without revealing it.")
    api_base: str | None = Field(default=None, description="Optional custom endpoint the connection targets.")
    status: str = Field(description="Verification state: 'unverified', 'verified', or 'invalid'.")
    added_at: str = Field(description="ISO-8601 instant the connection was saved.")


class ProviderKeysResponse(BaseModel):
    """The caller's stored BYOK provider keys, masked."""

    keys: list[ProviderKeyResponse] = Field(
        default_factory=list, description="Stored keys, one per provider, ordered by provider."
    )
    openrouter_oauth_available: bool = Field(
        default=False, description="Whether 'Continue with OpenRouter' can mint and store a key on this deployment."
    )


class OpenRouterOAuthStartResponse(BaseModel):
    """Where to send the browser to connect an OpenRouter account."""

    authorize_url: str = Field(description="OpenRouter sign-in URL carrying the PKCE challenge and callback.")


# Body for the signed-in finish step. The state is the encrypted payload from
# the start step; Fernet tokens are several hundred characters long.
class OpenRouterOAuthFinishRequest(BaseModel):
    state: str = Field(min_length=1, max_length=4096)
    code: str = Field(min_length=1, max_length=2048)


# Request to save (or rotate) a provider's BYOK connection. The secret is
# encrypted at rest the instant it lands and is never returned. ``api_base`` and
# ``params`` let a connection target any OpenAI-compatible host; ``api_base`` is
# required when the provider slug is not a known BYOK provider.
class SaveProviderKeyRequest(BaseModel):
    provider: str = Field(description="Provider slug to save the connection for (e.g. 'anthropic').")
    secret: str = Field(description="The plaintext provider key; stored encrypted, never echoed back.")
    label: str | None = Field(default=None, description="Optional user-facing name for the connection.")
    api_base: str | None = Field(
        default=None, description="Optional custom endpoint; required for an unknown provider."
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="Optional extra LiteLLM kwargs for the connection."
    )


def _mac_agent_wallet(engine: Any, username: str) -> dict[str, Any]:
    """Return the Mac agent's wallet: the linked hosted balance, never the local credit.

    Args:
        engine: Local database that holds the hosted link.
        username: The signed-in local user.

    Returns:
        ``{"linked": true, "hosted_balance_cents", "low_balance"}`` when hosted
        answers. With no link (or a revoked one), or when hosted does not
        answer, a ``message`` and no amount.
    """
    not_linked = {
        "linked": False,
        "message": f"No {APP_NAME} account is linked to this Mac; sign in to see the balance.",
    }
    if hosted_link.link_status(engine, username) is None:
        return not_linked
    try:
        result = hosted_link.hosted_get(engine, username, "/desktop/v1/balance")
    except DomainError as exc:
        if exc.status_code in (401, 409):
            return not_linked
        return {
            "linked": True,
            "balance_available": False,
            "message": f"{APP_NAME} did not answer, so the balance is unavailable right now.",
        }
    return {
        "linked": True,
        "hosted_balance_cents": int(result.get("available_cents") or 0),
        "low_balance": bool(result.get("low_balance")),
    }


def create_billing_router(*, job_store) -> APIRouter:
    """Build the prepaid-balance billing router.

    Args:
        job_store: Job-store instance whose ORM engine backs the billing tables.

    Returns:
        A FastAPI ``APIRouter`` exposing wallet, usage, pack checkout, and the
        Stripe webhook receiver.
    """
    router = APIRouter()
    service = StripeBillingService(engine=job_store.engine)
    vault = ProviderKeyVault(engine=job_store.engine)
    # The Mac app's wallet holds the local run credit, which the wizard's budget
    # check needs but which is not money. Its agent reads the linked hosted
    # balance from /billing/wallet/agent instead.
    local = settings.local_mode

    @router.get(
        "/billing/wallet",
        response_model=WalletResponse,
        operation_id="get_local_wallet" if local else "get_wallet_for_agent",
        summary="Return the caller's wallet",
        tags=[] if local else ["agent"],
    )
    def get_wallet(user: AuthenticatedUserDep) -> WalletResponse:
        """Return the caller's purchased balance, free grant, and recent ledger.

        Args:
            user: Authenticated caller whose wallet is read.

        Returns:
            The wallet snapshot; free-tier zeros when the caller has no billing row.
        """
        snapshot = service.get_wallet(user.username)
        return WalletResponse(
            paid_balance_cents=snapshot.paid_balance_cents,
            free_grant=FreeGrantResponse(
                cents_remaining=snapshot.free_grant_remaining,
                cents_total=snapshot.free_grant_total,
            ),
            usage=[
                UsageEntryResponse(
                    id=row.id,
                    at=row.at,
                    label=row.label,
                    model=row.model,
                    cents=row.cents,
                    kind=row.kind,
                )
                for row in snapshot.usage
            ],
        )

    if local:

        @router.get(
            "/billing/wallet/agent",
            operation_id="get_wallet_for_agent",
            summary="Return the balance of the linked Skynet account",
            tags=["agent"],
        )
        def get_mac_agent_wallet(user: AuthenticatedUserDep) -> dict[str, Any]:
            """Return the linked hosted balance, or a sign-in note with no amount.

            Args:
                user: The signed-in local user.

            Returns:
                See :func:`_mac_agent_wallet`.
            """
            return _mac_agent_wallet(job_store.engine, user.username)

    @router.get(
        "/billing/usage",
        response_model=UsageResponse,
        summary="Return a date-ranged usage rollup for the caller",
    )
    def get_usage(
        user: AuthenticatedUserDep,
        start: str | None = None,
        end: str | None = None,
    ) -> UsageResponse:
        """Return the caller's usage rollup over an optional date window.

        ``start``/``end`` are ISO-8601 instants; an omitted or unparseable bound
        defaults the window to the last :data:`USAGE_DEFAULT_WINDOW_DAYS` days
        ending now. A pure ledger read — serves on a key-less deploy (empty
        rollups for an account with no runs).

        Args:
            user: Authenticated caller whose ledger is summarized.
            start: ISO-8601 window start, or null for ``end`` minus the default window.
            end: ISO-8601 window end, or null for now.

        Returns:
            The totals, per-day and per-model series, and recent ledger rows.
        """
        now = datetime.now(UTC)
        end_dt = _parse_instant(end, now)
        start_dt = _parse_instant(start, end_dt - timedelta(days=USAGE_DEFAULT_WINDOW_DAYS))
        snapshot = service.get_usage(user.username, start_dt, end_dt)
        return UsageResponse(
            start=snapshot.start,
            end=snapshot.end,
            billed_cents=snapshot.billed_cents,
            runs=snapshot.runs,
            by_day=[
                UsageDayResponse(
                    date=day.date,
                    billed_cents=day.billed_cents,
                    runs=day.runs,
                    input_tokens=day.input_tokens,
                    output_tokens=day.output_tokens,
                )
                for day in snapshot.by_day
            ],
            by_model=[
                UsageModelResponse(
                    model=row.model,
                    cents=row.cents,
                    runs=row.runs,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                )
                for row in snapshot.by_model
            ],
            entries=[
                UsageEntryResponse(
                    id=row.id,
                    at=row.at,
                    label=row.label,
                    model=row.model,
                    cents=row.cents,
                    kind=row.kind,
                )
                for row in snapshot.entries
            ],
        )

    @router.get(
        "/billing/profile",
        response_model=BillingProfileResponse,
        summary="Return the caller's saved billing details and payment methods",
    )
    def get_billing_profile(user: AuthenticatedUserDep) -> BillingProfileResponse:
        """Return Stripe-backed billing details without sensitive card data.

        Args:
            user: Authenticated caller whose Stripe customer is read.

        Returns:
            Billing contact fields and masked saved payment methods.
        """
        return _profile_response(service.get_billing_profile(user.username))

    @router.patch(
        "/billing/payment-methods/{payment_method_id}",
        response_model=BillingProfileResponse,
        summary="Edit a saved payment method or make it the default",
    )
    def update_payment_method(
        payment_method_id: str,
        body: PaymentMethodUpdateRequest,
        user: AuthenticatedUserDep,
    ) -> BillingProfileResponse:
        """Edit one of the caller's saved payment methods and return the fresh profile.

        Args:
            payment_method_id: Stripe payment method id owned by the caller.
            body: New expiry, holder name, and default flag.
            user: Authenticated owner of the payment method.

        Returns:
            The updated billing profile.
        """
        service.update_payment_method(
            user.username,
            payment_method_id,
            exp_month=body.exp_month,
            exp_year=body.exp_year,
            holder_name=body.holder_name,
            make_default=body.make_default,
        )
        return _profile_response(service.get_billing_profile(user.username))

    @router.delete(
        "/billing/payment-methods/{payment_method_id}",
        response_model=BillingProfileResponse,
        summary="Remove a saved payment method",
    )
    def remove_payment_method(payment_method_id: str, user: AuthenticatedUserDep) -> BillingProfileResponse:
        """Detach one of the caller's saved payment methods and return the fresh profile.

        Args:
            payment_method_id: Stripe payment method id owned by the caller.
            user: Authenticated owner of the payment method.

        Returns:
            The updated billing profile.
        """
        service.remove_payment_method(user.username, payment_method_id)
        return _profile_response(service.get_billing_profile(user.username))

    @router.get(
        "/billing/transactions",
        response_model=BillingTransactionsResponse,
        summary="Return the caller's Stripe purchase history",
    )
    def get_billing_transactions(
        user: AuthenticatedUserDep,
        start: str | None = None,
        end: str | None = None,
    ) -> BillingTransactionsResponse:
        """Return completed top-up purchases over an optional date window.

        Args:
            user: Authenticated caller whose Stripe purchases are read.
            start: ISO-8601 window start, or null for the default window.
            end: ISO-8601 window end, or null for now.

        Returns:
            Most-recent-first purchases with receipt or invoice links.
        """
        now = datetime.now(UTC)
        end_dt = _parse_instant(end, now)
        start_dt = _parse_instant(start, end_dt - timedelta(days=USAGE_DEFAULT_WINDOW_DAYS))
        snapshot = service.get_transactions(user.username, start_dt, end_dt)
        return BillingTransactionsResponse(
            available=snapshot.available,
            entries=[
                BillingTransactionResponse(
                    id=entry.id,
                    at=entry.at,
                    amount=entry.amount,
                    currency=entry.currency,
                    status=entry.status,
                    cents=entry.cents,
                    pack_id=entry.pack_id,
                    document_url=entry.document_url,
                )
                for entry in snapshot.entries
            ],
        )

    @router.post(
        "/billing/checkout",
        response_model=CheckoutSessionResponse,
        summary="Start a Stripe Checkout session for a top-up pack or custom amount",
    )
    def create_checkout(body: CheckoutRequest, user: AuthenticatedUserDep) -> CheckoutSessionResponse:
        """Create a one-time Checkout session for a top-up pack or custom amount.

        Args:
            body: The pack — or custom amount in cents — to buy.
            user: Authenticated buyer; the top-up lands on their balance via webhook.

        Returns:
            The hosted Checkout URL to redirect the buyer to.

        Raises:
            DomainError: 400 when neither a pack nor a custom amount is given.
        """
        if body.cents is not None:
            url = service.create_custom_checkout(user.username, body.cents)
        elif body.pack_id:
            url = service.create_pack_checkout(user.username, body.pack_id)
        else:
            raise DomainError("billing.invalid_amount", status=400)
        return CheckoutSessionResponse(url=url)


    @router.post(
        "/billing/portal",
        response_model=CheckoutSessionResponse,
        summary="Open Stripe-hosted billing and payment-method management",
    )
    def create_billing_portal(
        body: PortalSessionRequest,
        user: AuthenticatedUserDep,
    ) -> CheckoutSessionResponse:
        """Create a customer portal session for the authenticated account.

        Args:
            body: Portal destination: general management or payment-method update.
            user: Authenticated account whose Stripe customer is managed.

        Returns:
            The hosted Customer Portal URL.
        """
        url = service.create_portal_session(
            user.username,
            payment_method_update=body.flow == "payment_method",
        )
        return CheckoutSessionResponse(url=url)

    @router.post(
        "/billing/webhook",
        response_model=WebhookAck,
        include_in_schema=False,
        summary="Receive and apply a Stripe webhook event",
    )
    async def stripe_webhook(request: Request) -> WebhookAck:
        """Verify a Stripe webhook signature and apply the event idempotently.

        Unauthenticated by design: authenticity comes from the Stripe signature
        over the raw request body, not a bearer token.

        Args:
            request: Raw request; its body and ``Stripe-Signature`` header are
                read directly (the signature is over the unparsed bytes).

        Returns:
            An acknowledgement once the event is verified and applied.
        """
        payload = await request.body()
        signature = request.headers.get("stripe-signature")
        service.handle_webhook(payload, signature)
        return WebhookAck(received=True)

    @router.get(
        "/billing/byok/keys",
        response_model=ProviderKeysResponse,
        summary="List the caller's stored BYOK provider keys (masked)",
    )
    def list_provider_keys(user: AuthenticatedUserDep) -> ProviderKeysResponse:
        """Return the caller's stored BYOK keys as masked, secret-free views.

        A pure DB read — no decryption, no provider call — so it serves even when
        the vault key is unconfigured.

        Args:
            user: Authenticated caller whose keys are listed.

        Returns:
            The masked keys, one per provider.
        """
        snapshot = vault.list_keys(user.username)
        return ProviderKeysResponse(
            keys=[
                ProviderKeyResponse(
                    id=k.id,
                    provider=k.provider,
                    label=k.label,
                    last4=k.last4,
                    api_base=k.api_base,
                    status=k.status,
                    added_at=k.added_at,
                )
                for k in snapshot.keys
            ],
            openrouter_oauth_available=openrouter_oauth.oauth_available(),
        )

    @router.get(
        "/billing/byok/models",
        response_model=ModelCatalogResponse,
        summary="List models available through the caller's verified BYOK connections",
    )
    def list_byok_models(user: AuthenticatedUserDep) -> ModelCatalogResponse:
        """Return static BYOK models plus models fetched from custom endpoints.

        Args:
            user: Authenticated owner of the stored connections.

        Returns:
            The account-scoped BYOK model catalog.
        """
        return _byok_catalog_for_user(vault, user.username)

    @router.put(
        "/billing/byok/keys",
        response_model=ProviderKeyResponse,
        summary="Save (or rotate) a BYOK provider key; encrypt at rest and verify on entry",
    )
    def save_provider_key(body: SaveProviderKeyRequest, user: AuthenticatedUserDep) -> ProviderKeyResponse:
        """Encrypt and store a provider secret, verifying it on entry.

        The secret is encrypted before it touches the database and never echoed
        back; the response carries only the masked tail and the entry-time verify
        verdict.

        Args:
            body: The provider slug and its plaintext secret.
            user: Authenticated owner of the key.

        Returns:
            The masked, verified view of the stored key.
        """
        view = vault.save_key(
            user.username,
            body.provider,
            body.secret,
            label=body.label,
            api_base=body.api_base,
            params=body.params,
        )
        _invalidate_byok_user_catalog(user.username)
        return ProviderKeyResponse(
            id=view.id,
            provider=view.provider,
            label=view.label,
            last4=view.last4,
            api_base=view.api_base,
            status=view.status,
            added_at=view.added_at,
        )

    @router.post(
        "/billing/byok/keys/{provider}/verify",
        response_model=ProviderKeyResponse,
        summary="Re-run the verify probe against a stored BYOK key",
    )
    def verify_provider_key(provider: str, user: AuthenticatedUserDep) -> ProviderKeyResponse:
        """Re-verify a stored provider key and persist the fresh verdict.

        Used to re-check a key saved while the provider was unreachable (status
        stuck at ``unverified``). Decrypts the secret only for the probe.

        Args:
            provider: Provider slug whose stored key is re-verified.
            user: Authenticated owner of the key.

        Returns:
            The masked view carrying the fresh verification status.
        """
        view = vault.verify_key(user.username, provider)
        _invalidate_byok_user_catalog(user.username)
        return ProviderKeyResponse(
            id=view.id,
            provider=view.provider,
            label=view.label,
            last4=view.last4,
            api_base=view.api_base,
            status=view.status,
            added_at=view.added_at,
        )

    def _openrouter_callback_url(request: Request) -> str:
        """Resolve the OpenRouter OAuth callback URL for this deployment.

        Args:
            request: The incoming request, used when no explicit URI is set.

        Returns:
            The absolute callback URL, before the encrypted state is appended.
        """
        return settings.openrouter_oauth_redirect_uri or str(request.url_for("openrouter_oauth_callback"))

    def _providers_redirect(error: str | None = None) -> RedirectResponse:
        """Send the browser back to the Settings providers tab, optionally with an error.

        Args:
            error: Domain error code to surface, if connecting failed.

        Returns:
            A 303 redirect into the frontend.
        """
        params = {"settings": "providers"}
        if error:
            params["byok_error"] = error
        return RedirectResponse(f"{settings.app_public_url.rstrip('/')}/?{urlencode(params)}", status_code=303)

    @router.post(
        "/billing/byok/openrouter/oauth/start",
        response_model=OpenRouterOAuthStartResponse,
        summary="Begin 'Continue with OpenRouter', which mints a BYOK key via PKCE",
    )
    def start_openrouter_oauth(request: Request, user: AuthenticatedUserDep) -> OpenRouterOAuthStartResponse:
        """Mint the OpenRouter sign-in URL the browser should visit.

        Args:
            request: Incoming request, for deriving the callback URL.
            user: Authenticated caller connecting their account.

        Returns:
            The authorize URL.

        Raises:
            DomainError: 503 when the vault is not configured.
        """
        return OpenRouterOAuthStartResponse(
            authorize_url=openrouter_oauth.build_authorize_url(user.username, _openrouter_callback_url(request))
        )

    @router.get(
        "/billing/byok/openrouter/oauth/callback",
        name="openrouter_oauth_callback",
        include_in_schema=False,
    )
    def openrouter_oauth_callback(code: str | None = None, state: str | None = None) -> RedirectResponse:
        """Pass the state and code to the web app, which finishes the sign-in.

        This route has no session, because auth is a bearer header. So it
        trades no code itself: the signed-in finish route checks that the
        caller started the sign-in, which blocks an account-link hijack.

        Args:
            code: Authorization code from OpenRouter.
            state: The encrypted state minted at start.

        Returns:
            A redirect with an error code, or with the state and code in the
            URL fragment, which the browser does not send to any server.
        """
        if not code or not state:
            return _providers_redirect("billing.openrouter_oauth_failed")
        fragment = urlencode({"oauth_flow": "openrouter", "oauth_state": state, "oauth_code": code})
        return RedirectResponse(
            f"{settings.app_public_url.rstrip('/')}/?{urlencode({'settings': 'providers'})}#{fragment}",
            status_code=303,
        )

    @router.post(
        "/billing/byok/openrouter/oauth/finish",
        status_code=204,
        summary="Finish 'Continue with OpenRouter' and save the minted key",
    )
    def finish_openrouter_oauth(body: OpenRouterOAuthFinishRequest, user: AuthenticatedUserDep) -> Response:
        """Trade the code for a key when the caller started this sign-in.

        Args:
            body: The state and code from the callback.
            user: Authenticated caller.

        Returns:
            An empty 204 answer.

        Raises:
            DomainError: 400 for a forged or expired state, or a refused code;
                403 when another user started the sign-in; 503 when the vault
                is not configured.
        """
        payload = openrouter_oauth.parse_state(body.state)
        if payload["u"] != user.username:
            raise DomainError("billing.openrouter_oauth_state_invalid", status=403)
        key = openrouter_oauth.exchange_code(body.code, payload["v"])
        vault.save_key(user.username, openrouter_oauth.PROVIDER, key)
        _invalidate_byok_user_catalog(user.username)
        return Response(status_code=204)

    @router.delete(
        "/billing/byok/keys/{provider}",
        response_model=ProviderKeysResponse,
        summary="Forget a stored BYOK provider key",
    )
    def remove_provider_key(provider: str, user: AuthenticatedUserDep) -> ProviderKeysResponse:
        """Forget a stored provider key and return the remaining keys.

        Idempotent: removing a provider with no stored key is a no-op. Returns the
        post-removal list so the client can re-render without a second round-trip.

        Args:
            provider: Provider slug whose key is removed.
            user: Authenticated owner of the key.

        Returns:
            The caller's remaining masked keys.
        """
        vault.remove_key(user.username, provider)
        _invalidate_byok_user_catalog(user.username)
        snapshot = vault.list_keys(user.username)
        return ProviderKeysResponse(
            keys=[
                ProviderKeyResponse(
                    id=k.id,
                    provider=k.provider,
                    label=k.label,
                    last4=k.last4,
                    api_base=k.api_base,
                    status=k.status,
                    added_at=k.added_at,
                )
                for k in snapshot.keys
            ],
            openrouter_oauth_available=openrouter_oauth.oauth_available(),
        )

    return router

"""Stripe-backed billing service: customers, checkout, portal, and webhooks.

This module is the only place that talks to Stripe. The web app calls the
billing router, which delegates here. Stripe is the source of truth for money
(pack charges); the local ``billing_customers`` / ``wallet_ledger`` tables are
a synced cache plus an audit trail, reconciled by
:meth:`StripeBillingService.handle_webhook` on every event Stripe delivers.

Ledger reads work whether or not Stripe is configured, so a deploy without
keys degrades to a read-only free tier. Stripe profile/history reads report
the provider as unavailable, while mutations (checkout and portal) require
``settings.is_stripe_configured`` and raise
``DomainError("billing.not_configured", 503)`` otherwise — never a 500.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any

import stripe
from sqlalchemy import URL, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..api.alerts import send_alert
from ..api.errors import DomainError
from ..brand import APP_NAME
from ..config import settings
from ..constants import (
    TOKEN_SOURCE_BYOK,
    TOKEN_SOURCE_MANAGED,
)
from ..storage.models import (
    BillingCustomerModel,
    BillingWebhookEventModel,
    ExecutionBudgetModel,
    JobModel,
    UserModel,
    WalletLedgerModel,
)
from ..telemetry import record_server_event
from .budget_amounts import wallet_reserved_cents
from .openrouter_float import check_float
from .plans import PAST_DUE_STATUS, PLAN_FREE, PLAN_PRO, is_pro_status
from .pricing import ModelUsage, cents_for_usage

logger = logging.getLogger("app.billing.service")

# Cents granted per one-time pack. Mirrors the frontend TOP_UP_PACKS catalog;
# the dollar price lives in Stripe (the price id), the cents granted live here.
# Packs are at par — one cent of balance per cent paid, exactly the Stripe unit_amount —
# so no pack sells spendable value below what it costs (no bonus subsidy).
PACK_CENTS: dict[str, int] = {"starter": 500, "plus": 2000, "pro": 5000}

# Bounds for a custom (user-chosen) top-up. The balance is kept in
# cents, so the amount doubles as a Stripe ``unit_amount``. The floor matches
# the smallest pack: below $5 the flat part of the fee dominates the charge
# (a $0.50 top-up paid $0.92), and every tiny purchase is one more $15 dispute
# exposure. The ceiling keeps a typo'd amount from becoming a four-figure
# charge. Mirrored by the frontend's CUSTOM_CENTS_MIN/MAX.
CUSTOM_CENTS_MIN = 500
CUSTOM_CENTS_MAX = 100_000

# Platform fee on a top-up, charged on top of the par amount: 12.5% of the
# top-up amount plus a flat 35 cents. It is a platform fee on every purchase,
# not a card surcharge: it never varies by payment method and is never called a
# card or processing fee, because card-network rules cap surcharges (Visa 3%,
# Mastercard 4%) and ban them on debit. The wallet shows it before checkout so
# the first price a buyer sees is all-in (California SB 478). Sized to
# absorb Stripe processing (up to 4.4% + 30c for a non-US card) and OpenRouter's
# 5.5% fee when the platform buys the matching OpenRouter credits, leaving roughly 2%. The buyer pays base + fee;
# the account is granted only the base amount, which is
# already the base value in cents. Mirrored by the frontend's
# purchaseFeeUsd. A $20 (2000-cent) top-up owes 250 + 35 = $2.85.
PURCHASE_FEE_RATE = 0.125
PURCHASE_FEE_FIXED_CENTS = 35


def purchase_fee_cents(cents: int) -> int:
    """Return the service fee for buying ``cents`` of balance, in cents.

    Args:
        cents: Amount being purchased, in cents.

    Returns:
        The fee to add to the charge, in cents: the percentage rounded up to
        the cent, plus the flat part.
    """
    return math.ceil(cents * PURCHASE_FEE_RATE) + PURCHASE_FEE_FIXED_CENTS


# One-time allowance a new account gets. 0 = no free balance: every cent
# spent was paid for, so the platform never subsidizes tokens or compute.
# Grants seeded while this was non-zero were cleared by migration c4f1a7d9e2b5.
FREE_GRANT_CENTS = 0


def free_grant_cents() -> int:
    """Return the one-time allowance a new account gets in this deployment.

    Only a PR preview environment grants credit, so automated checks there can
    pay for a run. The single-user Mac app grants ``local_grant_cents``, because
    its subscription runs cost nothing here. Everywhere else it is
    :data:`FREE_GRANT_CENTS`.
    """
    if settings.local_mode:
        return settings.local_grant_cents
    return settings.preview_grant_cents if settings.is_preview_environment else FREE_GRANT_CENTS


# Prefix on the placeholder ``stripe_customer_id`` of a billing row created by a
# local debit for an account that never reached Stripe. ``get_or_create_customer``
# treats such a row as having no real Stripe customer yet and provisions one.
LOCAL_CUSTOMER_PREFIX = "local:"

# Width of the ``billing_customers.stripe_customer_id`` column.
_CUSTOMER_ID_MAX_LENGTH = 64


def local_customer_id(username: str) -> str:
    """Build the placeholder ``stripe_customer_id`` for a customerless account.

    The column is ``String(64)`` and unique, so the raw username (up to 255
    characters) cannot follow the prefix. A truncated SHA-256 digest fits the
    column and stays unique per username; only the prefix is ever read back.

    Args:
        username: Account identity the placeholder row belongs to.

    Returns:
        ``local:`` followed by a hex digest, at most 64 characters long.
    """
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
    return f"{LOCAL_CUSTOMER_PREFIX}{digest[: _CUSTOMER_ID_MAX_LENGTH - len(LOCAL_CUSTOMER_PREFIX)]}"


def _is_customerless(customer_id: str) -> bool:
    """Return whether a stored ``stripe_customer_id`` names no real Stripe customer.

    An older checkout webhook stored ``""`` for a session without a customer, so
    an empty id counts as customerless like a ``local:`` placeholder.

    Args:
        customer_id: Stored ``billing_customers.stripe_customer_id`` value.

    Returns:
        ``True`` for an empty or placeholder id.
    """
    return not customer_id or customer_id.startswith(LOCAL_CUSTOMER_PREFIX)


# Staging test-credit grants (SKY-573). The test-account tool signs up
# ``staging-<label>-<6 hex>@`` this inbox; nothing else may receive a grant.
# The cap is one shared total per UTC day across all accounts in the staging
# database, so an agent loop cannot drain more than $50. PR previews are
# refused: each preview has its own database on the same RDS instance, and the
# cap and advisory lock are per database, so previews would multiply the cap.
TEST_ACCOUNT_INBOX = "eegeupelae.resend.app"
TEST_GRANT_DAILY_CAP_CENTS = 5000
TEST_GRANT_KEY_PREFIX = "testgrant:"
_TEST_ACCOUNT_ID = re.compile(r"staging-[a-z0-9-]+-[0-9a-f]{6}")
_SETTLEMENT_KEY_MAX_LENGTH = 64
_LEDGER_DESCRIPTION_MAX_LENGTH = 255
# The account sees the ledger description in its wallet history, so it holds
# only this label and the reason. The approval text goes to the operator log.
_TEST_GRANT_LABEL = "Test grant: "
# Continues the 7421370000xx advisory-lock series (worker/engine uses ...07).
# It serializes grants on different accounts, which the per-account row lock
# cannot, so two grants cannot both read a total under the cap and together
# pass it.
_TEST_GRANT_LOCK_ID = 742137000008
# Terraform names each RDS instance ``skynet-<environment>``, and its address
# starts with that identifier. A grant refuses any other PostgreSQL host even
# when ``APP_ENVIRONMENT`` is overridden. A tunnel to ``localhost`` is refused
# too.
_STAGING_DB_HOST_LABEL = "skynet-staging"
# PR previews use ``skynet_pr_<n>`` databases on the same staging RDS instance
# (infra/terraform/scripts/preview.sh), so the host alone cannot tell them
# apart from staging. Only the staging database name passes.
_STAGING_DB_NAME = "skynet"
# libpq reads these to pick a server or service file that the URL does not show.
_LIBPQ_OVERRIDE_ENV = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")


class GrantRefusedError(Exception):
    """A test-credit grant failed a guard; nothing was written."""


@dataclass(frozen=True)
class GrantResult:
    """Outcome of a test-credit grant.

    Attributes:
        credited_cents: Cents this key credited (on a replay, the first grant's cents).
        balance_cents: Account balance after the grant.
        replayed: Whether the key was already used, so nothing new was credited.
    """

    credited_cents: int
    balance_cents: int
    replayed: bool


def check_account_email(account_id: str) -> str:
    """Build the email the test-account tool signs up for an account id.

    Args:
        account_id: Id the tool printed, such as ``staging-label-a1b2c3``.

    Returns:
        The lowercased account email, which is also the username.
    """
    return f"{account_id.strip().lower()}@{TEST_ACCOUNT_INBOX}"


def _test_grant_target() -> str | None:
    """Return the account-id prefix this deployment may grant to.

    Returns:
        ``"staging"`` on staging, else ``None`` (PR previews included).
    """
    return "staging" if settings.app_environment == "staging" else None


def _is_staging_database(url: URL) -> bool:
    """Return whether ``url`` points at the staging database instance.

    SQLite passes, because no deployed environment uses it, so it can never be
    the production database.

    Args:
        url: The engine URL. Only its backend, host, database and query are read.

    Returns:
        ``True`` for SQLite, or for a host whose first label is the staging RDS
        identifier and the staging database name, with no ``host``,
        ``hostaddr`` or ``service`` query key and no libpq override variable
        in the environment.
    """
    if url.get_backend_name() == "sqlite":
        return True
    # libpq lets these query keys and environment variables pick another
    # server, so such a URL could reach it while its host looks like staging.
    if any(key in url.query for key in ("host", "hostaddr", "service")):
        return False
    if any(os.environ.get(name) for name in _LIBPQ_OVERRIDE_ENV):
        return False
    return (url.host or "").lower().split(".")[0] == _STAGING_DB_HOST_LABEL and url.database == _STAGING_DB_NAME


def _is_test_account(username: str, target: str) -> bool:
    """Return whether ``username`` is a test-tool account for this deployment.

    Args:
        username: Account email.
        target: ``staging``, from :func:`_test_grant_target`.

    Returns:
        ``True`` when the email has the tool's shape and this deployment's target.
    """
    local, _, domain = username.partition("@")
    return (
        domain == TEST_ACCOUNT_INBOX
        and _TEST_ACCOUNT_ID.fullmatch(local) is not None
        and local.startswith(f"{target}-")
    )


# Subscription lifecycle events the webhook mirrors onto ``billing_customers``.
# Each one re-reads the subscription, so they are order-independent.
SUBSCRIPTION_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)

# Ceiling handed to fee-less BYOK runs: far above any real run's full cost,
# small enough to stay a safe int everywhere cents are summed.
_BYOK_UNCAPPED_CEILING = 10**9

# Most recent ledger rows the usage dashboard carries back per window. The
# per-day/per-model rollups span every row in range; only the raw activity list
# (and the per-run breakdown derived from it) is bounded, to cap payload size.
USAGE_ENTRY_LIMIT = 200

# Stripe-backed billing surfaces stay intentionally bounded. The UI only needs
# a concise payment-method list and a recent, date-filtered purchase history.
PAYMENT_METHOD_LIMIT = 20
TRANSACTION_LIMIT = 100


@dataclass(frozen=True)
class LedgerRow:
    """One usage-ledger entry as the wallet UI consumes it.

    ``cents`` is signed (negative for a spend), ``model`` is the model id for a
    run row or ``None`` for a top-up/grant, and ``at`` is an ISO-8601 instant.
    """

    id: str
    at: str
    label: str
    model: str | None
    cents: int
    kind: str


@dataclass(frozen=True)
class PlanSnapshot:
    """The account's platform plan as the wallet surfaces show it.

    ``renews_at`` is the ISO-8601 end of the current billing period (when Pro
    renews, or lapses if ``cancel_at_period_end``); ``available`` is whether a
    Pro price is configured on this deploy, so the UI can hide the upgrade.
    """

    plan: str = PLAN_FREE
    status: str | None = None
    renews_at: str | None = None
    cancel_at_period_end: bool = False
    available: bool = False


@dataclass(frozen=True)
class WalletSnapshot:
    """The account's billing state as a single read for the wallet surfaces."""

    paid_balance_cents: int
    free_grant_remaining: int
    free_grant_total: int
    usage: list[LedgerRow] = field(default_factory=list)
    plan: PlanSnapshot = field(default_factory=PlanSnapshot)


@dataclass(frozen=True)
class UsageDayRow:
    """One calendar day's billed run spend, in cents, with its run count and tokens."""

    date: str
    billed_cents: int
    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class UsageModelRow:
    """One model's share of run spend over the window: gross billed cents and run count.

    ``input_tokens``/``output_tokens`` sum the measured usage stamped on the
    model's spend rows; rows written before token metering carry no counts and
    contribute zero.
    """

    model: str | None
    cents: int
    runs: int
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class UsageSnapshot:
    """A date-ranged usage rollup for the billing Usage dashboard.

    Aggregates the wallet ledger over ``[start, end]`` into the totals, per-day,
    and per-model series the dashboard charts, plus the most recent raw ledger
    rows for the activity list. ``billed_cents`` is gross run spend (the
    absolute value of negative run rows). Top-ups and grants are excluded from
    the spend rollups but still surface in ``entries``.
    """

    start: str
    end: str
    billed_cents: int
    runs: int
    by_day: list[UsageDayRow] = field(default_factory=list)
    by_model: list[UsageModelRow] = field(default_factory=list)
    entries: list[LedgerRow] = field(default_factory=list)


@dataclass(frozen=True)
class BillingAddressSnapshot:
    """Represent display-safe billing address fields stored by Stripe."""

    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None


@dataclass(frozen=True)
class PaymentMethodSnapshot:
    """Represent a saved payment method without exposing sensitive details."""

    id: str
    type: str
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None
    is_default: bool
    holder_name: str | None = None


@dataclass(frozen=True)
class BillingProfileSnapshot:
    """Represent the Stripe-backed billing profile shown in Settings."""

    available: bool
    has_customer: bool
    email: str | None = None
    name: str | None = None
    phone: str | None = None
    address: BillingAddressSnapshot = field(default_factory=BillingAddressSnapshot)
    payment_methods: list[PaymentMethodSnapshot] = field(default_factory=list)


@dataclass(frozen=True)
class BillingTransactionSnapshot:
    """Represent one completed Stripe Checkout transaction."""

    id: str
    at: str
    amount: int
    currency: str
    status: str
    cents: int | None
    pack_id: str | None
    document_url: str | None


@dataclass(frozen=True)
class BillingTransactionsSnapshot:
    """Represent a bounded Stripe purchase history for the current account."""

    available: bool
    entries: list[BillingTransactionSnapshot] = field(default_factory=list)


def _stripe_value(obj: Any, key: str, default: Any = None) -> Any:
    """Read one field from a Stripe object or a test mapping.

    Args:
        obj: Stripe resource, mapping, or ``None``.
        key: Field name to read.
        default: Value returned when the field is absent.

    Returns:
        The field value, or ``default``.
    """
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _subscription_period_end(subscription: Any) -> datetime | None:
    """Return when the subscription's current billing period ends.

    Stripe API versions from 2025-03-31 moved ``current_period_end`` from the
    subscription onto each subscription item, so both places are read.

    Args:
        subscription: Stripe Subscription resource or test mapping.

    Returns:
        The period end as a UTC datetime, or ``None`` when Stripe sent none.
    """
    period_end = _stripe_value(subscription, "current_period_end")
    if period_end is None:
        items = _stripe_value(_stripe_value(subscription, "items", {}), "data", []) or []
        if items:
            period_end = _stripe_value(items[0], "current_period_end")
    if period_end is None:
        return None
    return datetime.fromtimestamp(int(period_end), UTC)


def _plan_snapshot(customer: BillingCustomerModel | None) -> PlanSnapshot:
    """Project a billing row's mirrored subscription into the wallet's plan view.

    Args:
        customer: The account's billing row, or ``None`` for a new account.

    Returns:
        The plan the account is on and when it renews or lapses.
    """
    available = bool(settings.stripe_price_pro_monthly) and settings.is_stripe_configured
    if customer is None or not is_pro_status(customer.subscription_status):
        return PlanSnapshot(available=available)
    period_end = customer.subscription_current_period_end
    if period_end is not None and period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=UTC)
    return PlanSnapshot(
        plan=PLAN_PRO,
        status=customer.subscription_status,
        renews_at=period_end.isoformat() if period_end else None,
        cancel_at_period_end=bool(customer.subscription_cancel_at_period_end),
        available=available,
    )


def _stripe_id(value: Any) -> str | None:
    """Return an expandable Stripe field's id without assuming expansion state.

    Args:
        value: Resource object, mapping, string id, or ``None``.

    Returns:
        Stripe id when present, otherwise ``None``.
    """
    if isinstance(value, str):
        return value
    resource_id = _stripe_value(value, "id")
    return str(resource_id) if resource_id else None


def platform_fee_cents_for_usage(usages: Iterable[ModelUsage]) -> int:
    """Return zero for model usage paid by the user's provider key.

    Args:
        usages: Per-model token usage for the run.

    Returns:
        Zero cents.
    """
    return 0


def run_cost_cents(
    usages: Iterable[ModelUsage],
    token_source: str,
    token_sources_by_model: Mapping[str, str] | None = None,
) -> int:
    """Return the cents charged for managed model usage.

    A managed run is charged its marked-up per-model token cost
    (:func:`core.billing.pricing.cents_for_usage`); BYOK model usage is not charged.

    Args:
        usages: Per-model token usage for the run.
        token_source: Fallback source for legacy or unrecognized model rows.
        token_sources_by_model: Optional per-model sources for a mixed job.

    Returns:
        The non-negative cost in cents.
    """
    usage_rows = list(usages)
    if token_sources_by_model is None:
        if token_source == TOKEN_SOURCE_BYOK:
            return platform_fee_cents_for_usage(usage_rows)
        return cents_for_usage(usage_rows)
    managed = [
        usage for usage in usage_rows if token_sources_by_model.get(usage.model, token_source) != TOKEN_SOURCE_BYOK
    ]
    byok = [usage for usage in usage_rows if token_sources_by_model.get(usage.model, token_source) == TOKEN_SOURCE_BYOK]
    return cents_for_usage(managed) + platform_fee_cents_for_usage(byok)


def cost_ceiling_budget(spendable: int, token_source: str) -> int:
    """Return the max per-run cost ceiling (full-cost cents) a balance can back.

    A managed run spends its full per-token cost. BYOK model usage does not
    consume the balance, so it has an effectively unlimited model ceiling.

    Args:
        spendable: The account's spendable cents; non-positive yields ``0``.
        token_source: ``"managed"`` or ``"byok"`` — sets the conversion.

    Returns:
        The non-negative ceiling, in full-cost cents, to clamp the run to.
    """
    if spendable <= 0:
        return 0
    if token_source != TOKEN_SOURCE_BYOK:
        return spendable
    return _BYOK_UNCAPPED_CEILING


def committed_spend_cents(budget: int, token_source: str) -> int:
    """Return the most balance cents a run's cost-ceiling budget can consume.

    The inverse of :func:`cost_ceiling_budget`: given a run's ``max_cost_cents``
    (a full-cost ceiling), how many cents of the account's balance it holds a
    claim on. A managed run can debit up to the full budget; BYOK model calls
    do not debit it. Used at submit time to subtract the
    commitments of still-active runs from the balance, so concurrent submissions
    cannot collectively promise more than the account holds.

    Args:
        budget: The run's cost-ceiling budget in full-cost cents; non-positive
            yields ``0``.
        token_source: ``"managed"`` or ``"byok"`` — sets the conversion.

    Returns:
        The non-negative cents of balance the run can still debit.
    """
    if budget <= 0:
        return 0
    if token_source != TOKEN_SOURCE_BYOK:
        return budget
    return 0


def legacy_job_committed_cents(session: Session, username: str, *, exclude_job_id: str | None = None) -> int:
    """Retain funding promised to active jobs without an authoritative budget attachment.

    Args:
        session: Account-locked transaction or a read-only balance snapshot.
        username: Authoritative wallet owner.
        exclude_job_id: The same legacy root job currently settling its own usage.

    Returns:
        Stamped managed ceilings for active legacy roots; BYOK model commitments are zero.
    """
    attached = (
        select(ExecutionBudgetModel.id)
        .where(
            ExecutionBudgetModel.id == JobModel.execution_budget_id,
            ExecutionBudgetModel.job_id == JobModel.optimization_id,
            ExecutionBudgetModel.username == JobModel.username,
            ExecutionBudgetModel.generation == JobModel.execution_budget_generation,
        )
        .exists()
    )
    statement = select(
        JobModel.payload["max_cost_cents"].as_integer().label("payload_ceiling"),
        JobModel.payload_overview["max_cost_cents"].as_integer().label("overview_ceiling"),
        JobModel.payload["token_source"].as_string().label("payload_source"),
        JobModel.payload_overview["token_source"].as_string().label("overview_source"),
    ).where(
        JobModel.username == username,
        JobModel.status.in_(("pending", "validating", "running", "paused")),
        JobModel.parent_optimization_id.is_(None),
        ~attached,
    )
    if exclude_job_id is not None:
        statement = statement.where(JobModel.optimization_id != exclude_job_id)
    total = 0
    for row in session.execute(statement):
        ceiling = row.payload_ceiling if row.payload_ceiling is not None else row.overview_ceiling
        if ceiling is None:
            continue
        source = str(row.payload_source or row.overview_source or "")
        total += committed_spend_cents(int(ceiling), source)
    return total


def account_committed_cents(session: Session, username: str, *, exclude_job_id: str | None = None) -> int:
    """Combine per-operation holds with legacy job commitments exactly once.

    Args:
        session: Transaction serialized on the account before a monetary mutation.
        username: Account whose spending commitments must remain funded.
        exclude_job_id: Optional finishing legacy job whose own commitment is being consumed.

    Returns:
        Cents unavailable to unrelated new work.
    """
    return wallet_reserved_cents(session, username) + legacy_job_committed_cents(
        session, username, exclude_job_id=exclude_job_id
    )


def _report_uncollected_run(username: str, *, cost: int, charged: int, description: str) -> None:
    """Log and alert on a run charge the account's balance could not cover.

    The balance floor keeps the account at zero, so the difference is revenue
    the platform already paid OpenRouter for and will not recover. It logs at
    ``WARNING`` (below the alert handler's default threshold), so the alert is
    sent explicitly; the title stays constant so the webhook throttle can
    collapse a burst while the body carries the per-run figures.

    Args:
        username: Account the run was billed to.
        cost: Full cost of the run, in cents.
        charged: Cents actually collected.
        description: The ledger row's label, for context.
    """
    uncollected = cost - charged
    logger.warning(
        "debit for %s clamped to balance: cost=%d charged=%d uncollected=%d (%s)",
        username,
        cost,
        charged,
        uncollected,
        description,
    )
    send_alert(
        "Billing: run cost exceeded the account balance",
        body=f"account={username} cost={cost} charged={charged} uncollected={uncollected} run={description}",
        level="WARNING",
    )


class StripeBillingService:
    """Mediates between the billing API, the billing tables, and Stripe."""

    def __init__(self, *, engine: Any) -> None:
        """Bind the service to the ORM engine backing the billing tables.

        Args:
            engine: SQLAlchemy engine (``job_store.engine``) used for every
                billing-table session. Stripe itself is configured lazily, so
                constructing the service never requires Stripe credentials.
        """
        self._engine = engine

    def _resolve_grant(self, customer: BillingCustomerModel | None, now: datetime) -> int:
        """Seed the one-time free grant if unseeded; return the remaining cents.

        Seeds an unseeded row (NULL ``grant_remaining`` — a new account) to the
        one-time :func:`free_grant_cents`. The grant is lifetime — once seeded
        it only ever draws down; there is no renewal. Mutates ``customer`` in
        place; the caller commits. A missing row (``None``) reads a full free
        grant without persisting — the row is created on the first real mutation
        (debit / top-up).

        Args:
            customer: The account's billing row, or ``None`` when it has none yet.
            now: Instant stamped as ``updated_at`` when the seed mutates the row.

        Returns:
            Cents remaining in the account's grant.
        """
        if customer is None:
            return free_grant_cents()
        if customer.grant_remaining is None:
            customer.grant_remaining = free_grant_cents()
            customer.updated_at = now
            return int(customer.grant_remaining)
        return int(customer.grant_remaining)

    def _stripe(self) -> Any:
        """Return the ``stripe`` module configured with the secret key.

        Returns:
            The configured ``stripe`` module.

        Raises:
            DomainError: 503 when no Stripe secret key is configured.
        """
        if settings.stripe_secret_key is None:
            raise DomainError("billing.not_configured", status=503)
        stripe.api_key = settings.stripe_secret_key.get_secret_value()
        return stripe

    def _return_url(self, status: str) -> str:
        """Build a Checkout return URL on the public app origin.

        Args:
            status: Query value appended as ``?billing=`` (``success``/``cancel``).

        Returns:
            An absolute URL back to the app root, where the billing provider
            picks up the ``billing`` param and syncs the wallet.
        """
        return f"{settings.app_public_url.rstrip('/')}/?billing={status}"

    def _billing_settings_url(self) -> str:
        """Build the public return URL that reopens Billing settings.

        Returns:
            An absolute app URL with the billing settings deep link.
        """
        return f"{settings.app_public_url.rstrip('/')}/?settings=billing"

    def _existing_customer_id(self, username: str) -> str | None:
        """Return an account's real Stripe customer id without provisioning one.

        Args:
            username: Account identity whose billing link is read.

        Returns:
            A ``cus_...`` id, or ``None`` for a customerless/local-only account.
        """
        with Session(self._engine) as session:
            row = session.get(BillingCustomerModel, username)
            if row is None or _is_customerless(row.stripe_customer_id):
                return None
            return row.stripe_customer_id

    def get_or_create_customer(self, username: str) -> str:
        """Return the account's Stripe customer id, creating it on first use.

        Persists the ``username -> stripe_customer_id`` mapping so a returning
        buyer reuses one Stripe customer (and one saved card / portal history).

        Args:
            username: Lowercased-email identity the customer is billed under.

        Returns:
            The Stripe customer id (``cus_…``).
        """
        with Session(self._engine) as session:
            row = session.get(BillingCustomerModel, username)
            if row is not None and not _is_customerless(row.stripe_customer_id):
                return row.stripe_customer_id
        stripe_mod = self._stripe()
        customer = stripe_mod.Customer.create(email=username, metadata={"username": username})
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            existing = session.get(BillingCustomerModel, username)
            if existing is not None:
                # A local debit may have created a placeholder row before the
                # account reached Stripe; upgrade it in place to the real id so the
                # accrued grant/balance carry over.
                if _is_customerless(existing.stripe_customer_id):
                    existing.stripe_customer_id = customer.id
                    existing.updated_at = now
                    session.commit()
                    return customer.id
                return existing.stripe_customer_id
            session.add(
                BillingCustomerModel(
                    username=username,
                    stripe_customer_id=customer.id,
                    balance_cents=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
        return customer.id

    def get_billing_profile(self, username: str) -> BillingProfileSnapshot:
        """Return display-safe billing details and saved payment methods from Stripe.

        Opening Settings never provisions a Stripe customer. Accounts that have
        not checked out yet receive an available, empty profile; configured
        accounts read Stripe directly so stale billing details are never cached
        in the application database.

        Args:
            username: Authenticated account whose Stripe profile is read.

        Returns:
            Billing details and masked payment-method metadata.

        Raises:
            DomainError: 502 when Stripe cannot serve the profile read.
        """
        customer_id = self._existing_customer_id(username)
        if customer_id is None:
            return BillingProfileSnapshot(available=settings.is_stripe_configured, has_customer=False)
        if not settings.is_stripe_configured:
            return BillingProfileSnapshot(available=False, has_customer=True)
        stripe_mod = self._stripe()
        try:
            customer = stripe_mod.Customer.retrieve(customer_id)
            methods = stripe_mod.Customer.list_payment_methods(customer_id, limit=PAYMENT_METHOD_LIMIT)
        except stripe.StripeError as exc:
            raise DomainError("billing.provider_unavailable", status=502) from exc

        invoice_settings = _stripe_value(customer, "invoice_settings", {})
        default_method_id = _stripe_id(_stripe_value(invoice_settings, "default_payment_method"))
        payment_methods: list[PaymentMethodSnapshot] = []
        for method in _stripe_value(methods, "data", []) or []:
            method_type = str(_stripe_value(method, "type", "unknown"))
            details = _stripe_value(method, method_type, {})
            method_id = _stripe_id(method)
            if method_id is None:
                continue
            payment_methods.append(
                PaymentMethodSnapshot(
                    id=method_id,
                    type=method_type,
                    brand=_stripe_value(details, "brand"),
                    last4=_stripe_value(details, "last4"),
                    exp_month=_stripe_value(details, "exp_month"),
                    exp_year=_stripe_value(details, "exp_year"),
                    is_default=method_id == default_method_id,
                    holder_name=_stripe_value(_stripe_value(method, "billing_details", {}), "name"),
                )
            )

        address = _stripe_value(customer, "address", {})
        return BillingProfileSnapshot(
            available=True,
            has_customer=True,
            email=_stripe_value(customer, "email"),
            name=_stripe_value(customer, "name"),
            phone=_stripe_value(customer, "phone"),
            address=BillingAddressSnapshot(
                line1=_stripe_value(address, "line1"),
                line2=_stripe_value(address, "line2"),
                city=_stripe_value(address, "city"),
                state=_stripe_value(address, "state"),
                postal_code=_stripe_value(address, "postal_code"),
                country=_stripe_value(address, "country"),
            ),
            payment_methods=payment_methods,
        )

    def _owned_payment_method(self, stripe_mod: Any, username: str, payment_method_id: str) -> tuple[str, Any]:
        """Retrieve a saved payment method and confirm it belongs to the account.

        Args:
            stripe_mod: The configured ``stripe`` module.
            username: Authenticated account that must own the payment method.
            payment_method_id: Stripe ``pm_...`` id sent by the client.

        Returns:
            The account's Stripe customer id and the payment method object.

        Raises:
            DomainError: 404 when the method is unknown or attached to another
                customer, so ids cannot be probed across accounts; 502 when
                Stripe cannot serve the read.
        """
        customer_id = self._existing_customer_id(username)
        if customer_id is None:
            raise DomainError("billing.payment_method_not_found", status=404)
        try:
            method = stripe_mod.PaymentMethod.retrieve(payment_method_id)
        except stripe.InvalidRequestError as exc:
            raise DomainError("billing.payment_method_not_found", status=404) from exc
        except stripe.StripeError as exc:
            raise DomainError("billing.provider_unavailable", status=502) from exc
        if _stripe_id(_stripe_value(method, "customer")) != customer_id:
            raise DomainError("billing.payment_method_not_found", status=404)
        return customer_id, method

    def update_payment_method(
        self,
        username: str,
        payment_method_id: str,
        *,
        exp_month: int | None = None,
        exp_year: int | None = None,
        holder_name: str | None = None,
        make_default: bool = False,
    ) -> None:
        """Edit a saved payment method's expiry and holder name, or make it the default.

        Args:
            username: Authenticated account that owns the payment method.
            payment_method_id: Stripe ``pm_...`` id to edit.
            exp_month: New card expiry month; sent together with ``exp_year``.
            exp_year: New four-digit card expiry year.
            holder_name: New cardholder name; ``None`` leaves it unchanged.
            make_default: Make this the customer's default payment method.

        Raises:
            DomainError: 400 when Stripe rejects the new details or expiry is
                sent for a non-card method; 404 when the method is not the
                account's; 502 when Stripe is unavailable; 503 when Stripe is
                not configured.
        """
        stripe_mod = self._stripe()
        customer_id, method = self._owned_payment_method(stripe_mod, username, payment_method_id)
        changes: dict[str, Any] = {}
        if exp_month is not None and exp_year is not None:
            if _stripe_value(method, "type") != "card":
                raise DomainError("billing.payment_method_invalid", status=400)
            changes["card"] = {"exp_month": exp_month, "exp_year": exp_year}
        if holder_name is not None:
            changes["billing_details"] = {"name": holder_name.strip() or None}
        try:
            if changes:
                stripe_mod.PaymentMethod.modify(payment_method_id, **changes)
            if make_default:
                stripe_mod.Customer.modify(
                    customer_id,
                    invoice_settings={"default_payment_method": payment_method_id},
                )
        except (stripe.InvalidRequestError, stripe.CardError) as exc:
            raise DomainError("billing.payment_method_invalid", status=400) from exc
        except stripe.StripeError as exc:
            raise DomainError("billing.provider_unavailable", status=502) from exc

    def remove_payment_method(self, username: str, payment_method_id: str) -> None:
        """Detach a saved payment method from the account's Stripe customer.

        Args:
            username: Authenticated account that owns the payment method.
            payment_method_id: Stripe ``pm_...`` id to remove.

        Raises:
            DomainError: 404 when the method is not the account's; 502 when
                Stripe is unavailable; 503 when Stripe is not configured.
        """
        stripe_mod = self._stripe()
        self._owned_payment_method(stripe_mod, username, payment_method_id)
        try:
            stripe_mod.PaymentMethod.detach(payment_method_id)
        except stripe.StripeError as exc:
            raise DomainError("billing.provider_unavailable", status=502) from exc

    def get_transactions(self, username: str, start: datetime, end: datetime) -> BillingTransactionsSnapshot:
        """Return completed Checkout purchases for a date window from Stripe.

        Args:
            username: Authenticated account whose purchases are read.
            start: Inclusive lower bound on Checkout Session creation time.
            end: Inclusive upper bound on Checkout Session creation time.

        Returns:
            A bounded, most-recent-first purchase history.

        Raises:
            DomainError: 502 when Stripe cannot serve the history read.
        """
        customer_id = self._existing_customer_id(username)
        if customer_id is None:
            return BillingTransactionsSnapshot(available=settings.is_stripe_configured)
        if not settings.is_stripe_configured:
            return BillingTransactionsSnapshot(available=False)
        stripe_mod = self._stripe()
        try:
            sessions = stripe_mod.checkout.Session.list(
                customer=customer_id,
                status="complete",
                created={"gte": int(start.timestamp()), "lte": int(end.timestamp())},
                limit=TRANSACTION_LIMIT,
                expand=["data.payment_intent.latest_charge", "data.invoice"],
            )
        except stripe.StripeError as exc:
            raise DomainError("billing.provider_unavailable", status=502) from exc

        entries: list[BillingTransactionSnapshot] = []
        for checkout in _stripe_value(sessions, "data", []) or []:
            payment_intent = _stripe_value(checkout, "payment_intent", {})
            charge = _stripe_value(payment_intent, "latest_charge", {})
            invoice = _stripe_value(checkout, "invoice", {})
            amount = int(_stripe_value(checkout, "amount_total", 0) or 0)
            refunded = int(_stripe_value(charge, "amount_refunded", 0) or 0)
            if _stripe_value(charge, "disputed", False):
                status = "disputed"
            elif refunded >= amount > 0:
                status = "refunded"
            elif refunded > 0:
                status = "partially_refunded"
            elif _stripe_value(checkout, "payment_status") == "paid":
                status = "paid"
            else:
                status = "processing"
            metadata = _stripe_value(checkout, "metadata", {})
            # Sessions created before the credits-to-cents rename carry the amount under "credits".
            cents_raw = _stripe_value(metadata, "cents", _stripe_value(metadata, "credits"))
            try:
                cents = int(cents_raw) if cents_raw is not None else None
            except (TypeError, ValueError):
                cents = None
            created = int(_stripe_value(checkout, "created", 0) or 0)
            entries.append(
                BillingTransactionSnapshot(
                    id=str(_stripe_value(checkout, "id", "")),
                    at=datetime.fromtimestamp(created, UTC).isoformat(),
                    amount=amount,
                    currency=str(_stripe_value(checkout, "currency", "usd") or "usd").upper(),
                    status=status,
                    cents=cents,
                    pack_id=_stripe_value(metadata, "pack_id"),
                    document_url=_stripe_value(invoice, "hosted_invoice_url") or _stripe_value(charge, "receipt_url"),
                )
            )
        return BillingTransactionsSnapshot(available=True, entries=entries)

    def create_portal_session(self, username: str, *, payment_method_update: bool) -> str:
        """Create a Stripe-hosted billing-management session for the account.

        Args:
            username: Authenticated account whose customer portal is opened.
            payment_method_update: Deep-link directly into adding/updating a
                payment method when true; otherwise open the portal home.

        Returns:
            The hosted Customer Portal URL.

        Raises:
            DomainError: 502 when Stripe cannot create the portal session; 503
                when Stripe is not configured.
        """
        stripe_mod = self._stripe()
        customer_id = self.get_or_create_customer(username)
        return_url = self._billing_settings_url()
        kwargs: dict[str, Any] = {"customer": customer_id, "return_url": return_url}
        if payment_method_update:
            kwargs["flow_data"] = {
                "type": "payment_method_update",
                "after_completion": {
                    "type": "redirect",
                    "redirect": {"return_url": return_url},
                },
            }
        try:
            portal = stripe_mod.billing_portal.Session.create(**kwargs)
        except stripe.StripeError as exc:
            raise DomainError("billing.provider_unavailable", status=502) from exc
        return str(portal.url)

    def create_pack_checkout(self, username: str, pack_id: str) -> str:
        """Create a one-time Checkout Session for a top-up pack and return its URL.

        Args:
            username: Buyer identity; stamped into session metadata so the
                webhook can credit the right account.
            pack_id: One of :data:`PACK_CENTS` (``starter``/``plus``/``pro``).

        Returns:
            The hosted Stripe Checkout URL to redirect the buyer to.

        Raises:
            DomainError: 400 when ``pack_id`` is unknown or its price id is
                unconfigured; 503 when Stripe is not configured.
        """
        price_id = settings.stripe_pack_price_ids.get(pack_id, "")
        cents = PACK_CENTS.get(pack_id, 0)
        if not price_id or cents <= 0:
            raise DomainError("billing.unknown_pack", status=400, pack_id=pack_id)
        return self._create_checkout(username, {"price": price_id, "quantity": 1}, pack_id, cents)

    def create_custom_checkout(self, username: str, cents: int) -> str:
        """Create a one-time Checkout Session for a user-chosen amount.

        The balance is kept in cents, so ``cents`` is passed to Stripe verbatim as
        the ad-hoc ``unit_amount``; the webhook credits the account from the
        session metadata exactly as for a fixed pack.

        Args:
            username: Buyer identity; stamped into session metadata so the
                webhook can credit the right account.
            cents: Amount to buy, in cents, within
                :data:`CUSTOM_CENTS_MIN`..:data:`CUSTOM_CENTS_MAX`.

        Returns:
            The hosted Stripe Checkout URL to redirect the buyer to.

        Raises:
            DomainError: 400 when ``cents`` is out of bounds; 503 when
                Stripe is not configured.
        """
        if not CUSTOM_CENTS_MIN <= cents <= CUSTOM_CENTS_MAX:
            raise DomainError("billing.invalid_amount", status=400, cents=cents)
        line_item = {
            "price_data": {
                "currency": "usd",
                "unit_amount": cents,
                "product_data": {"name": f"{APP_NAME} balance top-up · ${cents / 100:.2f}"},
            },
            "quantity": 1,
        }
        return self._create_checkout(username, line_item, "custom", cents)

    def create_subscription_checkout(self, username: str) -> str:
        """Create a Checkout Session that subscribes the account to Skynet Pro.

        The subscription carries ``username`` in its own metadata so every
        later lifecycle event resolves the account without the Checkout Session.

        Args:
            username: Subscriber identity.

        Returns:
            The hosted Stripe Checkout URL to redirect the subscriber to.

        Raises:
            DomainError: 503 when Stripe or the Pro price is unconfigured; 409
                when the account is already on Pro; 502 when Stripe refuses.
        """
        price_id = settings.stripe_price_pro_monthly
        if not price_id:
            raise DomainError("billing.plan_unavailable", status=503)
        stripe_mod = self._stripe()
        with Session(self._engine) as session:
            customer = session.get(BillingCustomerModel, username)
            if customer is not None and is_pro_status(customer.subscription_status):
                raise DomainError("billing.already_subscribed", status=409)
        customer_id = self.get_or_create_customer(username)
        metadata = {"username": username, "plan": PLAN_PRO}
        try:
            checkout = stripe_mod.checkout.Session.create(
                customer=customer_id,
                mode="subscription",
                line_items=[{"price": price_id, "quantity": 1}],
                billing_address_collection="required",
                customer_update={"address": "auto", "name": "auto"},
                success_url=self._return_url("pro"),
                cancel_url=self._return_url("cancel"),
                client_reference_id=username,
                metadata=metadata,
                subscription_data={"metadata": metadata},
            )
        except stripe.StripeError as exc:
            raise DomainError("billing.provider_unavailable", status=502) from exc
        return str(checkout.url)

    def _create_checkout(self, username: str, line_item: dict[str, Any], pack_id: str, cents: int) -> str:
        """Create the Stripe Checkout Session shared by pack and custom top-ups.

        The top-up line stays at par; a second line carries the OpenRouter-style
        platform fee (:func:`purchase_fee_cents`) so the buyer pays base + fee while
        the webhook still grants only the base ``cents`` from metadata.

        Args:
            username: Buyer identity; stamped into session metadata so the
                webhook can credit the right account.
            line_item: The top-up line to charge (a fixed price id or ad-hoc
                ``price_data``), before the purchase fee.
            pack_id: Pack id (or ``"custom"``) recorded in metadata for the
                ledger description.
            cents: Cents the webhook grants once the session completes.

        Returns:
            The hosted Stripe Checkout URL.
        """
        stripe_mod = self._stripe()
        customer_id = self.get_or_create_customer(username)
        metadata = {"username": username, "pack_id": pack_id, "cents": str(cents)}
        fee_item = {
            "price_data": {
                "currency": "usd",
                "unit_amount": purchase_fee_cents(cents),
                "product_data": {"name": "Platform fee"},
            },
            "quantity": 1,
        }
        checkout = stripe_mod.checkout.Session.create(
            customer=customer_id,
            mode="payment",
            line_items=[line_item, fee_item],
            billing_address_collection="required",
            customer_update={"address": "auto", "name": "auto"},
            invoice_creation={"enabled": True},
            saved_payment_method_options={
                "payment_method_save": "enabled",
                "payment_method_remove": "enabled",
            },
            success_url=self._return_url("success"),
            cancel_url=self._return_url("cancel"),
            client_reference_id=username,
            metadata=metadata,
            payment_intent_data={"metadata": metadata},
        )
        return str(checkout.url)

    def get_wallet(self, username: str) -> WalletSnapshot:
        """Return the account's wallet snapshot from the billing tables.

        A pure DB read — no Stripe call — so it serves even when Stripe is
        unconfigured (balance 0). The free grant reflects real spend: run
        completions debit it via :meth:`debit_run`. The grant is one-time
        (seeded once, never renewed). A read that seeds an existing row is
        persisted; a brand-new account reads a full grant without creating a
        row until its first real mutation.

        Args:
            username: Account to summarize.

        Returns:
            A :class:`WalletSnapshot` of paid balance, free grant, and the most
            recent ledger rows.
        """
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            customer = session.get(BillingCustomerModel, username)
            grant_remaining = self._resolve_grant(customer, now)
            # Capture the seed mutation before the ledger query autoflushes
            # it — once flushed, ``is_modified`` reads clean and the commit below
            # would be skipped, dropping the seed on session close.
            grant_dirty = customer is not None and session.is_modified(customer)
            rows = (
                session.query(WalletLedgerModel)
                .filter(WalletLedgerModel.username == username)
                .order_by(WalletLedgerModel.created_at.desc())
                .limit(15)
                .all()
            )
            usage = [
                LedgerRow(
                    id=str(row.id),
                    at=row.created_at.isoformat(),
                    label=row.description or row.kind,
                    model=row.model,
                    cents=row.delta_cents,
                    kind=row.kind,
                )
                for row in rows
            ]
            if grant_dirty:
                session.commit()
            return WalletSnapshot(
                paid_balance_cents=customer.balance_cents if customer else 0,
                free_grant_remaining=grant_remaining,
                free_grant_total=free_grant_cents(),
                usage=usage,
                plan=_plan_snapshot(customer),
            )

    def get_usage(self, username: str, start: datetime, end: datetime) -> UsageSnapshot:
        """Aggregate the account's wallet ledger over a date window for the dashboard.

        A pure DB read — no Stripe call. Sums run rows in ``[start, end]`` into
        gross billed spend, a run count, a per-day series of billed cents, runs
        and tokens (ascending by date), and a per-model spend series (descending by spend); top-ups
        and grants are excluded from those rollups. The most recent :data:`USAGE_ENTRY_LIMIT` raw ledger rows in the
        window ride along as ``entries`` for the activity list and the per-run
        breakdown, while the rollups span every row in range regardless of that
        cap.

        Args:
            username: Account to summarize.
            start: Inclusive lower bound on ``created_at``.
            end: Inclusive upper bound on ``created_at``.

        Returns:
            A :class:`UsageSnapshot` of totals, per-day and per-model series, and
            the most recent ledger rows.
        """
        with Session(self._engine) as session:
            # Project only the columns the fold below reads — the full entity
            # (with description text on every row) is materialized for
            # thousands of rows on a wide window, only to be reduced to sums.
            rows = (
                session.query(
                    WalletLedgerModel.id,
                    WalletLedgerModel.created_at,
                    WalletLedgerModel.kind,
                    WalletLedgerModel.model,
                    WalletLedgerModel.delta_cents,
                    WalletLedgerModel.description,
                    WalletLedgerModel.input_tokens,
                    WalletLedgerModel.output_tokens,
                )
                .filter(
                    WalletLedgerModel.username == username,
                    WalletLedgerModel.created_at >= start,
                    WalletLedgerModel.created_at <= end,
                )
                .order_by(WalletLedgerModel.created_at.desc())
                .all()
            )
        billed = 0
        runs = 0
        per_day: dict[str, list[int]] = {}
        per_model: dict[str | None, list[int]] = {}
        # Only debits count as spend; a positive ``run`` row (a legacy
        # correction) is ignored by the rollups but still rides in ``entries``.
        for row in rows:
            if row.kind != "run" or row.delta_cents >= 0:
                continue
            spent = -row.delta_cents
            billed += spent
            runs += 1
            day = row.created_at.date().isoformat()
            day_vals = per_day.setdefault(day, [0, 0, 0, 0])
            day_vals[0] += spent
            day_vals[1] += 1
            day_vals[2] += row.input_tokens or 0
            day_vals[3] += row.output_tokens or 0
            model = per_model.setdefault(row.model, [0, 0, 0, 0])
            model[0] += spent
            model[1] += 1
            model[2] += row.input_tokens or 0
            model[3] += row.output_tokens or 0
        by_day = [
            UsageDayRow(
                date=date,
                billed_cents=vals[0],
                runs=vals[1],
                input_tokens=vals[2],
                output_tokens=vals[3],
            )
            for date, vals in sorted(per_day.items())
        ]
        by_model = [
            UsageModelRow(
                model=model,
                cents=vals[0],
                runs=vals[1],
                input_tokens=vals[2],
                output_tokens=vals[3],
            )
            for model, vals in sorted(per_model.items(), key=lambda kv: kv[1][0], reverse=True)
        ]
        entries = [
            LedgerRow(
                id=str(row.id),
                at=row.created_at.isoformat(),
                label=row.description or row.kind,
                model=row.model,
                cents=row.delta_cents,
                kind=row.kind,
            )
            for row in rows[:USAGE_ENTRY_LIMIT]
        ]
        return UsageSnapshot(
            start=start.isoformat(),
            end=end.isoformat(),
            billed_cents=billed,
            runs=runs,
            by_day=by_day,
            by_model=by_model,
            entries=entries,
        )

    def spendable_cents(self, username: str) -> int:
        """Return the account's total spendable cents (free grant + paid balance).

        Resolves the grant first (seeding a new account's one-time free grant),
        so the figure is never stale. A seeded row is persisted. This is the
        figure the submit gate checks: ``> 0`` means a managed run may start. A
        brand-new account reads a full grant without a row being created.

        Args:
            username: Account to read.

        Returns:
            Free-grant remaining plus purchased balance, never negative.
        """
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            customer = session.get(BillingCustomerModel, username)
            grant_remaining = self._resolve_grant(customer, now)
            paid = int(customer.balance_cents) if customer is not None else 0
            reserved = account_committed_cents(session, username)
            if customer is not None and session.is_modified(customer):
                session.commit()
        return max(grant_remaining + paid - reserved, 0)

    def total_outstanding_cents(self) -> int:
        """Return the total unspent balance liability across every account.

        Sums each account's purchased ``balance_cents`` plus its remaining free
        grant. This is the pool the shared OpenRouter float ultimately backs:
        cents users have paid for (or been granted) but not yet spent on runs.
        A pure DB read — no grant seeding, no Stripe call — so it reflects only
        liability already recorded.

        Returns:
            Sum of paid balances and remaining grants across all customers,
            never negative.
        """
        with Session(self._engine) as session:
            paid, granted = session.query(
                func.coalesce(func.sum(BillingCustomerModel.balance_cents), 0),
                func.coalesce(func.sum(BillingCustomerModel.grant_remaining), 0),
            ).one()
        return max(int(paid) + int(granted), 0)

    def cents_spent_since(self, since: datetime) -> int:
        """Return platform-wide cents spent on runs since a timestamp.

        Sums the magnitude of the negative ``run`` ledger deltas (run charges)
        posted at or after ``since``; top-ups, refund/dispute clawbacks and debt
        repayments are money movements, not token spend, and are excluded. Backs the global daily spend kill-switch,
        which refuses new submissions once a trailing-window total is reached.

        Args:
            since: Lower bound (inclusive) on ``created_at``; pass a timezone-aware
                UTC datetime such as ``now - 24h``.

        Returns:
            Total cents spent in the window, never negative.
        """
        with Session(self._engine) as session:
            spent = (
                session.query(func.coalesce(func.sum(-WalletLedgerModel.delta_cents), 0))
                .filter(
                    WalletLedgerModel.kind == "run",
                    WalletLedgerModel.delta_cents < 0,
                    WalletLedgerModel.created_at >= since,
                )
                .scalar()
            )
        return max(int(spent or 0), 0)

    def debit_run(
        self,
        username: str,
        usages: Iterable[ModelUsage],
        *,
        model: str | None,
        description: str,
        token_source: str = TOKEN_SOURCE_MANAGED,
        token_sources_by_model: Mapping[str, str] | None = None,
        optimization_id: str | None = None,
        settlement_key: str | None = None,
    ) -> int:
        """Charge a finished run's per-model cost to the account, grant first.

        Writes one signed negative ``run`` row to ``wallet_ledger`` and draws the
        cost from the free grant before the purchased balance, mirroring how
        :meth:`get_wallet` reports spendable cents (grant then paid). A **managed**
        run is charged its full per-model token cost; a **BYOK** model call carries
        no Skynet charge (:func:`run_cost_cents`). The provider bills the user's key.
        The grant is
        resolved first (seeding a new account's one-time free grant) so the
        debit lands against a current grant. A caller that may retry passes a
        ``settlement_key``: a second debit with the same key charges nothing and
        returns the first charge, so a redelivered or re-run leg never
        double-charges. A run costing zero cents writes nothing.

        The debit can never drive the account negative: the customer row is read
        under ``FOR UPDATE`` so concurrent debits serialize, and the charge is
        clamped to what the account actually holds — a run that cost more than
        the remaining balance drains it to exactly zero. The shortfall is not
        dropped silently: it is stored as ``uncollected_cents`` on the ledger
        row (a zero-delta row when nothing could be collected), logged, and sent
        as an operator alert. The DB backs the floor up with ``CHECK``
        constraints on the balance columns, so a bug here fails the transaction
        instead of persisting a negative.

        Args:
            username: Account the run is billed to.
            usages: Per-model token usage for the run; priced per-model into the
                cost in cents.
            model: Model id stamped on the ledger row, or ``None``.
            description: Human label for the ledger row (typically the run name).
            token_source: ``"managed"`` (full cost) or ``"byok"`` (no model charge);
                defaults to managed.
            token_sources_by_model: Optional per-model source map for a mixed job.
            optimization_id: Finishing legacy root job, whose own held ceiling may be
                consumed; also stamped on the ledger row so the charge is
                attributable to the job.
            settlement_key: Optional idempotency key stored on the ledger row;
                a debit whose key is already recorded is a no-op.

        Returns:
            The cost in cents actually charged (``0`` when nothing was billed) —
            at most the account's spendable balance, so it can undershoot the
            run's full cost on a depleted account. A repeated ``settlement_key``
            returns the cents its first debit charged.
        """
        usage_rows = list(usages)
        cost = run_cost_cents(usage_rows, token_source, token_sources_by_model)
        if cost <= 0:
            return 0
        # The ledger row records the measured tokens behind the charge — the
        # per-model Usage tab reads these back, so the invoice-side cents
        # figure and the token figure come from the same measurement.
        input_tokens = sum(usage.input_tokens for usage in usage_rows)
        output_tokens = sum(usage.output_tokens for usage in usage_rows)
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            customer = session.get(BillingCustomerModel, username, with_for_update=True)
            if settlement_key is not None:
                # Checked under the customer lock so two workers settling the
                # same leg serialize here and the second sees the first's row.
                prior = session.scalar(
                    select(WalletLedgerModel.delta_cents).where(WalletLedgerModel.settlement_key == settlement_key)
                )
                if prior is not None:
                    return max(-int(prior), 0)
            if customer is None:
                # A run can finish for an account that never touched Stripe; seed a
                # customer-less billing row so the debit lands and the grant tracks.
                # ``stripe_customer_id`` is uniquely indexed, so the placeholder is
                # keyed on the username to avoid colliding across free accounts; the
                # real id replaces it the first time the account reaches Stripe.
                customer = BillingCustomerModel(
                    username=username,
                    stripe_customer_id=local_customer_id(username),
                    balance_cents=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(customer)
            self._resolve_grant(customer, now)
            grant = max(int(customer.grant_remaining or 0), 0)
            paid = max(int(customer.balance_cents), 0)
            charged = min(
                cost,
                max(0, grant + paid - account_committed_cents(session, username, exclude_job_id=optimization_id)),
            )
            uncollected = cost - charged
            from_grant = min(grant, charged)
            customer.grant_remaining = grant - from_grant
            customer.balance_cents = paid - (charged - from_grant)
            customer.updated_at = now
            session.add(
                WalletLedgerModel(
                    username=username,
                    delta_cents=-charged,
                    kind="run",
                    description=description or "Run",
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    settlement_key=settlement_key,
                    uncollected_cents=uncollected or None,
                    optimization_id=optimization_id,
                    from_grant_cents=from_grant,
                )
            )
            session.commit()
        if uncollected > 0:
            _report_uncollected_run(username, cost=cost, charged=charged, description=description or "Run")
        return charged

    def grant_test_credit(
        self,
        username: str,
        cents: int,
        *,
        reason: str,
        approval: str,
        idempotency_key: str,
    ) -> GrantResult:
        """Credit a staging test account, once per idempotency key.

        This is the only allowed way to fund a check account. It raises the
        purchased balance and writes one ``grant`` ledger row in one
        transaction. The row's description holds the reason, which the account
        sees in its wallet history. The approval text goes only to the operator
        log, so the account never sees it. The row's ``settlement_key``
        (``testgrant:<key>``) makes a retry a no-op. A grant row carries no
        payment intent and no ``topup`` kind, so revenue reports and refund
        clawbacks never count it. A grant does not repay ``debt_cents``, so an
        account with debt is refused.

        Args:
            username: Test-account email, as built by :func:`check_account_email`.
            cents: Amount to credit, in cents.
            reason: Why the check needs the money.
            approval: The approval text for this grant, logged as given.
            idempotency_key: Caller key; a repeated key credits nothing.

        Returns:
            The credited cents, the new balance, and whether the key was a replay.

        Raises:
            GrantRefusedError: The environment, database, account, amount, text,
                key or daily cap fails a guard, the key was used for a different
                account or amount, the account has debt, or concurrent writes
                conflicted twice.
                Nothing is written.
        """
        target = _test_grant_target()
        if target is None:
            raise GrantRefusedError("APP_ENVIRONMENT must be staging; PR preview environments are refused")
        if not _is_staging_database(self._engine.url):
            raise GrantRefusedError("the database is not the staging database")
        reason, approval, idempotency_key = reason.strip(), approval.strip(), idempotency_key.strip()
        if not approval:
            raise GrantRefusedError("approval text is required")
        if not reason:
            raise GrantRefusedError("reason text is required")
        if cents <= 0:
            raise GrantRefusedError("amount must be positive")
        settlement_key = f"{TEST_GRANT_KEY_PREFIX}{idempotency_key}"
        if not idempotency_key or len(settlement_key) > _SETTLEMENT_KEY_MAX_LENGTH:
            raise GrantRefusedError(
                f"idempotency key must be 1-{_SETTLEMENT_KEY_MAX_LENGTH - len(TEST_GRANT_KEY_PREFIX)} characters"
            )
        description = f"{_TEST_GRANT_LABEL}{reason}"
        if len(description) > _LEDGER_DESCRIPTION_MAX_LENGTH:
            text_limit = _LEDGER_DESCRIPTION_MAX_LENGTH - len(_TEST_GRANT_LABEL)
            raise GrantRefusedError(f"reason is too long: it may have at most {text_limit} characters")
        username = username.strip().lower()
        if not _is_test_account(username, target):
            raise GrantRefusedError(f"the account is not a {target} test account from the test-account tool")
        try:
            result = self._apply_test_grant(username, cents, description, settlement_key)
        except IntegrityError:
            # Another writer created this account's billing row (or used this
            # key) between our read and our insert. The transaction rolled back
            # whole, so one fresh attempt reads that row and stays correct.
            try:
                result = self._apply_test_grant(username, cents, description, settlement_key)
            except IntegrityError as error:
                raise GrantRefusedError("concurrent writes conflicted twice; run the grant again") from error
        if not result.replayed:
            logger.warning(
                "Test credit grant: %s cents, key %s, reason %r, approval %r", cents, settlement_key, reason, approval
            )
        return result

    def _apply_test_grant(self, username: str, cents: int, description: str, settlement_key: str) -> GrantResult:
        """Write one checked test grant in one transaction, or replay its key.

        Args:
            username: Checked test-account email.
            cents: Positive amount to credit, in cents.
            description: Ledger description the account sees.
            settlement_key: ``testgrant:<key>`` for the ledger row.

        Returns:
            The credited cents, the new balance, and whether the key was a replay.

        Raises:
            GrantRefusedError: The account has no user row, the key was used for
                a different account or amount, the account has debt, or the
                daily cap would pass.
            IntegrityError: A concurrent writer inserted the same billing row or
                key first; nothing from this attempt is kept.
        """
        with Session(self._engine) as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(select(func.pg_advisory_xact_lock(_TEST_GRANT_LOCK_ID)))
            # Read the clock after the lock wait, so the UTC day matches the
            # moment the cap total is read.
            now = datetime.now(UTC)
            if session.get(UserModel, username) is None:
                raise GrantRefusedError("the test account has no user row; sign it up first")
            customer = session.get(BillingCustomerModel, username, with_for_update=True)
            prior = session.execute(
                select(WalletLedgerModel.username, WalletLedgerModel.delta_cents).where(
                    WalletLedgerModel.settlement_key == settlement_key
                )
            ).first()
            if prior is not None:
                if prior.username != username or int(prior.delta_cents) != cents:
                    raise GrantRefusedError("idempotency key already used for a different account or amount")
                balance = int(customer.balance_cents) if customer is not None else 0
                return GrantResult(credited_cents=int(prior.delta_cents), balance_cents=balance, replayed=True)
            # A top-up repays debt first, but a grant does not, so a grant to an
            # account with debt would leave it spendable while it still owes.
            if customer is not None and int(customer.debt_cents) > 0:
                raise GrantRefusedError("the account has unpaid debt; a test grant does not repay debt")
            day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
            granted_today = int(
                session.scalar(
                    select(func.coalesce(func.sum(WalletLedgerModel.delta_cents), 0)).where(
                        WalletLedgerModel.kind == "grant",
                        WalletLedgerModel.settlement_key.like(f"{TEST_GRANT_KEY_PREFIX}%"),
                        WalletLedgerModel.created_at >= day_start,
                    )
                )
            )
            if granted_today + cents > TEST_GRANT_DAILY_CAP_CENTS:
                raise GrantRefusedError(
                    f"the grant passes the ${TEST_GRANT_DAILY_CAP_CENTS / 100:.2f} daily cap: "
                    f"${granted_today / 100:.2f} already granted today (UTC)"
                )
            if customer is None:
                customer = BillingCustomerModel(
                    username=username,
                    stripe_customer_id=local_customer_id(username),
                    balance_cents=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(customer)
            customer.balance_cents = int(customer.balance_cents) + cents
            customer.updated_at = now
            session.add(
                WalletLedgerModel(
                    username=username,
                    delta_cents=cents,
                    kind="grant",
                    description=description,
                    settlement_key=settlement_key,
                    created_at=now,
                )
            )
            balance = int(customer.balance_cents)
            session.commit()
        return GrantResult(credited_cents=cents, balance_cents=balance, replayed=False)

    def handle_webhook(self, payload: bytes, sig_header: str | None) -> None:
        """Verify and apply a Stripe webhook event, exactly once.

        Records the event id before applying its effect in the same transaction,
        so a redelivered event (Stripe guarantees at-least-once) is a no-op
        instead of a double-credit.

        Args:
            payload: Raw request body bytes (signature is over the raw bytes).
            sig_header: The ``Stripe-Signature`` header value.

        Raises:
            DomainError: 503 when the webhook secret is unconfigured; 400 when
                the signature does not verify.
        """
        if settings.stripe_webhook_secret is None:
            raise DomainError("billing.not_configured", status=503)
        stripe_mod = self._stripe()
        try:
            event = stripe_mod.Webhook.construct_event(
                payload, sig_header or "", settings.stripe_webhook_secret.get_secret_value()
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise DomainError("billing.webhook_invalid", status=400) from exc
        event_id = str(event["id"])
        with Session(self._engine) as session:
            if session.get(BillingWebhookEventModel, event_id) is not None:
                return
            session.add(BillingWebhookEventModel(event_id=event_id, event_type=str(event["type"])))
            purchase = self._apply_event(session, event)
            session.commit()
        # After the top-up commits — so a fresh read sees the new liability — check
        # that the shared OpenRouter float still covers what users are owed. Runs
        # only on the freshly-applied path (a redelivery returns above), and never
        # on the money path itself: the purchase is already committed.
        if purchase is not None:
            self._after_purchase(event_id, *purchase)
        if str(event["type"]) == "checkout.session.completed":
            self._monitor_float()

    def _after_purchase(self, event_id: str, username: str, cents: int, pack_id: str) -> None:
        """Log and record the funnel milestone for a credited top-up.

        Runs after the top-up has committed and never on the money path: a
        logging or telemetry failure cannot undo or block the purchase.

        Args:
            event_id: Stripe event id, for correlating the log line with Stripe.
            username: Buyer account.
            cents: Cents granted.
            pack_id: Pack purchased (empty for a custom amount).
        """
        logger.info(
            "Top-up credited: user=%s cents=%d pack=%s event=%s",
            username,
            cents,
            pack_id or "custom",
            event_id,
        )
        try:
            record_server_event(
                self._engine,
                username=username,
                name="purchase_completed",
                properties={"pack_id": pack_id or "custom", "cents": cents},
            )
        except Exception:  # isolation boundary: telemetry must never surface into the webhook response
            logger.debug("purchase telemetry failed for event %s", event_id, exc_info=True)

    def _monitor_float(self) -> None:
        """Check the OpenRouter float after a purchase, swallowing every failure.

        A best-effort tripwire: reads the master-account balance and warns when
        it has fallen below the configured floor. Wrapped so a monitor failure
        (HTTP timeout, DB hiccup on the liability sum) can never propagate into
        the webhook handler, which has already committed the top-up.
        """
        try:
            check_float(self.total_outstanding_cents())
        except Exception:
            logger.exception("OpenRouter float monitor failed")

    def _apply_event(self, session: Session, event: Any) -> tuple[str, int, str] | None:
        """Dispatch a verified event to its handler; unknown types are no-ops.

        Args:
            session: Open session; the caller commits (event row + effect land
                atomically).
            event: The verified Stripe event object.

        Returns:
            ``(username, cents, pack_id)`` when the event credited a top-up,
            otherwise ``None``.
        """
        event_type = str(event["type"])
        obj = event["data"]["object"]
        event_id = str(event["id"])
        if event_type == "checkout.session.completed":
            return self._on_checkout_completed(session, event_id, obj)
        if event_type == "charge.refunded":
            self._on_charge_refunded(session, event_id, obj)
        elif event_type == "charge.dispute.created":
            self._on_dispute_created(session, event_id, obj)
        elif event_type in SUBSCRIPTION_EVENTS:
            self._on_subscription_changed(session, obj)
        return None

    def _on_subscription_changed(self, session: Session, obj: Any) -> None:
        """Mirror a Pro subscription's current state onto the account's billing row.

        Stripe delivers events out of order, so the handler re-reads the
        subscription and writes its latest state rather than the event's
        snapshot; the event copy is only a fallback when Stripe is unreachable.

        Args:
            session: Open session (caller commits).
            obj: The Subscription object from the event.
        """
        subscription = self._latest_subscription(obj)
        subscription_id = str(_stripe_value(subscription, "id", "") or "")
        if not subscription_id:
            return
        metadata = _stripe_value(subscription, "metadata", {}) or {}
        username = str(_stripe_value(metadata, "username", "") or "").lower()
        customer_id = _stripe_id(_stripe_value(subscription, "customer"))
        customer = session.get(BillingCustomerModel, username) if username else None
        if customer is None and customer_id:
            customer = session.scalars(
                select(BillingCustomerModel).where(BillingCustomerModel.stripe_customer_id == customer_id)
            ).first()
        if customer is None:
            logger.warning("Subscription %s has no matching billing account", subscription_id)
            return
        status = str(_stripe_value(subscription, "status", "") or "")
        # A second, abandoned subscription must not overwrite the one the
        # account is actually paying for.
        if (
            customer.stripe_subscription_id
            and customer.stripe_subscription_id != subscription_id
            and is_pro_status(customer.subscription_status)
            and not is_pro_status(status)
        ):
            return
        now = datetime.now(UTC)
        if status != PAST_DUE_STATUS:
            customer.subscription_past_due_since = None
        elif customer.subscription_status != PAST_DUE_STATUS or customer.subscription_past_due_since is None:
            customer.subscription_past_due_since = now
        customer.stripe_subscription_id = subscription_id
        customer.subscription_status = status
        customer.subscription_current_period_end = _subscription_period_end(subscription)
        customer.subscription_cancel_at_period_end = bool(
            _stripe_value(subscription, "cancel_at_period_end", False) or _stripe_value(subscription, "cancel_at")
        )
        customer.updated_at = now
        logger.info(
            "Subscription synced: user=%s status=%s subscription=%s",
            customer.username,
            status,
            subscription_id,
        )

    def _latest_subscription(self, obj: Any) -> Any:
        """Return the subscription's current state from Stripe, or the event copy.

        Args:
            obj: The Subscription object carried by the event.

        Returns:
            The freshly retrieved subscription, or ``obj`` when the retrieve fails.
        """
        subscription_id = _stripe_value(obj, "id")
        if not subscription_id:
            return obj
        try:
            return self._stripe().Subscription.retrieve(str(subscription_id))
        except stripe.StripeError:
            logger.warning("Subscription %s re-read failed; applying the event copy", subscription_id)
            return obj

    def _on_checkout_completed(self, session: Session, event_id: str, obj: Any) -> tuple[str, int, str] | None:
        """Credit a completed one-time pack purchase to the buyer's balance.

        An account carrying refund/chargeback debt repays it from the purchase
        first; only the remainder becomes spendable.

        Args:
            session: Open session (caller commits).
            event_id: Stripe event id, recorded on the ledger row for traceability.
            obj: The Checkout Session object from the event.

        Returns:
            ``(username, cents, pack_id)`` for a credited purchase, or ``None``
            when the session was not a paid one-time payment worth crediting.
        """
        if obj.get("mode") != "payment" or obj.get("payment_status") != "paid":
            return None
        metadata = obj.get("metadata") or {}
        username = str(metadata.get("username") or obj.get("client_reference_id") or "").lower()
        # A checkout opened before the credits-to-cents rename completes with the old key.
        cents = int(metadata.get("cents") or metadata.get("credits") or 0)
        pack_id = str(metadata.get("pack_id") or "")
        payment_intent = str(obj.get("payment_intent") or "") or None
        if not username or cents <= 0:
            return None
        customer = session.get(BillingCustomerModel, username)
        if customer is None:
            customer = BillingCustomerModel(
                username=username,
                # A session without a customer must not store "": the column is
                # unique, so a second customerless buyer would fail the webhook.
                stripe_customer_id=_stripe_id(obj.get("customer")) or local_customer_id(username),
                balance_cents=0,
            )
            session.add(customer)
        repaid = min(int(customer.debt_cents or 0), cents)
        customer.debt_cents = int(customer.debt_cents or 0) - repaid
        customer.balance_cents = int(customer.balance_cents) + cents - repaid
        customer.updated_at = datetime.now(UTC)
        session.add(
            WalletLedgerModel(
                username=username,
                delta_cents=cents,
                kind="topup",
                description=f"Top-up · {pack_id}" if pack_id else "Top-up",
                stripe_event_id=event_id,
                stripe_payment_intent_id=payment_intent,
            )
        )
        if repaid > 0:
            # A separate row (no payment intent) so the top-up row keeps the
            # full granted amount a later refund of this purchase caps against.
            session.add(
                WalletLedgerModel(
                    username=username,
                    delta_cents=-repaid,
                    kind="debt_repayment",
                    description="Refund/chargeback debt repaid",
                    stripe_event_id=event_id,
                )
            )
        return username, cents, pack_id

    def _pi_clawback_context(self, session: Session, payment_intent: str) -> tuple[str | None, int, int]:
        """Sum a PaymentIntent's ledger into (account, cents granted, cents already reversed).

        Reads every ledger row stamped with ``payment_intent`` — the top-up that
        granted the cents plus any earlier refund/dispute clawbacks against it —
        so a handler can cap a new clawback at what the top-up granted and net out
        what was already reversed. Resolving the account from the top-up row (not
        the charge's ``customer`` field) is what lets a dispute event, which carries
        no customer, still find its account.

        Args:
            session: Open session to read the ledger under.
            payment_intent: The Stripe PaymentIntent id (``pi_…``) to sum over.

        Returns:
            ``(username, granted, clawed)`` — the account the PaymentIntent's
            top-up credited (``None`` when no top-up row matches), the cents that
            top-up granted, and the cents already reversed by refunds/disputes
            (collected plus any carried as debt).
        """
        rows = (
            session.query(
                WalletLedgerModel.username,
                WalletLedgerModel.kind,
                WalletLedgerModel.delta_cents,
                WalletLedgerModel.uncollected_cents,
            )
            .filter(WalletLedgerModel.stripe_payment_intent_id == payment_intent)
            .all()
        )
        username: str | None = None
        granted = 0
        clawed = 0
        for row in rows:
            if row.kind == "topup":
                username = row.username
                granted += int(row.delta_cents)
            elif row.kind in ("refund", "dispute"):
                username = username or row.username
                clawed += -int(row.delta_cents) + int(row.uncollected_cents or 0)
        return username, granted, clawed

    def _on_charge_refunded(self, session: Session, event_id: str, obj: Any) -> None:
        """Claw back the balance for a refunded charge, netting out earlier partial refunds.

        Fires on every ``charge.refunded`` event. ``amount_refunded`` is the
        charge's cumulative refunded cents, so the cents to
        remove now are that cumulative figure — capped at what the top-up granted —
        minus what earlier refunds already reversed. A charge Skynet never credited
        (no matching top-up) is logged and skipped.

        Args:
            session: Open session (caller commits).
            event_id: Stripe event id, recorded on the clawback ledger row.
            obj: The Charge object from the event.
        """
        payment_intent = str(obj.get("payment_intent") or "")
        refunded = int(obj.get("amount_refunded") or 0)
        if not payment_intent or refunded <= 0:
            return
        username, granted, clawed = self._pi_clawback_context(session, payment_intent)
        if username is None:
            logger.warning(
                "refund for unrecognized payment_intent %s (event %s); nothing to claw back",
                payment_intent,
                event_id,
            )
            return
        delta = max(0, min(refunded, granted) - clawed)
        if delta <= 0:
            return
        self._write_clawback(
            session,
            event_id=event_id,
            payment_intent=payment_intent,
            username=username,
            cents=delta,
            kind="refund",
            description="Refund",
        )

    def _on_dispute_created(self, session: Session, event_id: str, obj: Any) -> None:
        """Claw back the balance when a charge is disputed — the chargeback pulls the funds back.

        Fires on ``charge.dispute.created``. ``amount`` is the disputed cents; the
        funds have left Skynet's Stripe balance, so the
        matching cents are removed, capped at the top-up's still-unreversed cents
        so a dispute after a partial refund never double-counts. A dispute on a charge
        Skynet never credited is logged and skipped.

        Args:
            session: Open session (caller commits).
            event_id: Stripe event id, recorded on the clawback ledger row.
            obj: The Dispute object from the event.
        """
        payment_intent = str(obj.get("payment_intent") or "")
        disputed = int(obj.get("amount") or 0)
        if not payment_intent or disputed <= 0:
            return
        username, granted, clawed = self._pi_clawback_context(session, payment_intent)
        if username is None:
            logger.warning(
                "dispute for unrecognized payment_intent %s (event %s); nothing to claw back",
                payment_intent,
                event_id,
            )
            return
        delta = max(0, min(disputed, granted - clawed))
        if delta <= 0:
            return
        self._write_clawback(
            session,
            event_id=event_id,
            payment_intent=payment_intent,
            username=username,
            cents=delta,
            kind="dispute",
            description="Chargeback",
        )

    def _write_clawback(
        self,
        session: Session,
        *,
        event_id: str,
        payment_intent: str,
        username: str,
        cents: int,
        kind: str,
        description: str,
    ) -> None:
        """Remove ``cents`` from the account, carrying what it no longer holds as debt.

        A refund or dispute returns money that bought purchased balance, so the
        clawback draws from ``balance_cents`` first, under ``FOR UPDATE`` so it
        serializes with concurrent debits. When those cents were already spent,
        the platform is out of pocket for the tokens they bought: the remainder
        is taken from the free grant (which then cannot be spent on top of the
        reversed money) and whatever is still owed becomes ``debt_cents``.
        Both balances are then zero, so every spend gate refuses until a top-up
        repays the debt (:meth:`_on_checkout_completed`). The ledger row records
        the collected delta and the debt as ``uncollected_cents`` — a zero-delta
        row when nothing was collected — so :meth:`_pi_clawback_context` counts the
        full reversal and a later event cannot claw it twice. A debt is logged and
        sent as an operator alert.

        Args:
            session: Open session (caller commits).
            event_id: Stripe event id, recorded on the ledger row.
            payment_intent: The PaymentIntent id, stamped on the ledger row so a
                later clawback nets against it.
            username: Account to draw the cents back from.
            cents: Cents owed back to Stripe (positive).
            kind: Ledger kind (``"refund"`` or ``"dispute"``).
            description: Human label for the ledger row.
        """
        now = datetime.now(UTC)
        customer = session.get(BillingCustomerModel, username, with_for_update=True)
        if customer is None:
            customer = BillingCustomerModel(
                username=username,
                stripe_customer_id=local_customer_id(username),
                balance_cents=0,
                debt_cents=0,
                created_at=now,
                updated_at=now,
            )
            session.add(customer)
        paid = max(int(customer.balance_cents), 0)
        # SKY-567: account deletion forfeits the balance with a negative
        # closure row. A refund of those cents returns them to the card, so
        # it reverses the forfeit instead of recording debt on a closed wallet.
        forfeited = -int(
            session.scalar(
                select(func.coalesce(func.sum(WalletLedgerModel.delta_cents), 0)).where(
                    WalletLedgerModel.username == username, WalletLedgerModel.kind == "closure"
                )
            )
        )
        restored = min(max(cents - paid, 0), max(forfeited, 0))
        if restored:
            session.add(
                WalletLedgerModel(
                    username=username,
                    delta_cents=restored,
                    kind="closure",
                    description="Forfeit reversed by refund",
                    created_at=now,
                )
            )
            paid += restored
        from_paid = min(cents, paid)
        grant = max(self._resolve_grant(customer, now), 0)
        from_grant = min(cents - from_paid, grant)
        debt = cents - from_paid - from_grant
        customer.balance_cents = paid - from_paid
        customer.grant_remaining = grant - from_grant
        customer.debt_cents = int(customer.debt_cents or 0) + debt
        customer.updated_at = now
        session.add(
            WalletLedgerModel(
                username=username,
                delta_cents=-(from_paid + from_grant),
                kind=kind,
                description=description,
                stripe_event_id=event_id,
                stripe_payment_intent_id=payment_intent,
                uncollected_cents=debt or None,
            )
        )
        if debt > 0:
            logger.warning(
                "%s clawback for %s exceeded the balance: owed=%d collected=%d debt=%d (pi %s)",
                kind,
                username,
                cents,
                from_paid + from_grant,
                debt,
                payment_intent,
            )
            send_alert(
                "Billing: refund/dispute left an account in debt",
                body=(
                    f"account={username} kind={kind} owed={cents} collected={from_paid + from_grant} "
                    f"debt={debt} total_debt={customer.debt_cents} payment_intent={payment_intent}"
                ),
                level="WARNING",
            )

"""Stripe-backed prepaid-balance billing.

Owns customers, top-up checkout, and webhook reconciliation. The rest of
the app reaches billing only through :class:`StripeBillingService`; nothing
else imports ``stripe``.
"""

from __future__ import annotations

from .byok_bridge import (
    byok_prefix_routable,
    inject_byok_connections,
    payload_uses_token_source,
    provider_slug_for_model,
    resolve_byok_model_config,
)
from .byok_vault import (
    ProviderKeyVault,
    ProviderKeyView,
    VaultSnapshot,
    byok_provider_for_litellm,
)
from .cli_dispatch import CliDispatcher, SubscriptionRateLimitedError, local_subscription_route
from .issuing_float import FundingResult, IssuingFundingSweeper, fund_issuing_once, start_issuing_funding_sweeper
from .openrouter_float import (
    FloatStatus,
    OpenRouterFloatSweeper,
    check_float,
    notify_low_float,
    read_account_balance_cents,
    start_openrouter_float_sweeper,
    warn_if_local_key_uncapped,
)
from .openrouter_keys import OpenRouterKeyProvisioner, inject_provisioned_openrouter_key
from .service import (
    GRANT_CENTS,
    PACK_CENTS,
    LedgerRow,
    StripeBillingService,
    WalletSnapshot,
    committed_spend_cents,
    cost_ceiling_budget,
)

__all__ = [
    "GRANT_CENTS",
    "PACK_CENTS",
    "CliDispatcher",
    "FloatStatus",
    "FundingResult",
    "IssuingFundingSweeper",
    "LedgerRow",
    "OpenRouterFloatSweeper",
    "OpenRouterKeyProvisioner",
    "ProviderKeyVault",
    "ProviderKeyView",
    "StripeBillingService",
    "SubscriptionRateLimitedError",
    "VaultSnapshot",
    "WalletSnapshot",
    "byok_prefix_routable",
    "byok_provider_for_litellm",
    "check_float",
    "committed_spend_cents",
    "cost_ceiling_budget",
    "fund_issuing_once",
    "inject_byok_connections",
    "inject_provisioned_openrouter_key",
    "local_subscription_route",
    "notify_low_float",
    "payload_uses_token_source",
    "provider_slug_for_model",
    "read_account_balance_cents",
    "resolve_byok_model_config",
    "start_issuing_funding_sweeper",
    "start_openrouter_float_sweeper",
    "warn_if_local_key_uncapped",
]

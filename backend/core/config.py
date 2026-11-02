"""Centralized configuration management via pydantic-settings.

Replaces scattered os.getenv() calls with a single Settings class that
validates environment variables at startup and provides typed access.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .local.vault_key import load_or_create_vault_key

_ENV_FILE = Path(__file__).parent.parent / ".env"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# With CODE_AGENT_MODEL / GENERALIST_AGENT_MODEL unset, both agents follow
# the model catalog's best-value default (see
# ``model_catalog.agent_model_id``). This id only runs until the boot-time
# catalog build lands, or when the catalog flags no default. Requires
# OPENROUTER_API_KEY.
DEFAULT_AGENT_MODEL_ID = "openrouter/z-ai/glm-5.3-flash"

# Vercel's API refuses any sandbox timeout above 24 hours (raised from five in
# June 2026, Pro and Enterprise only), so a larger configured ceiling can never
# be admitted and would fail at creation.
VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS = 86_400

# Deployments (and the pinned sandbox image) still set the pre-rename
# ``SKYNET_*`` variables, so every ``APP_*`` read falls back to its old name.
ENV_PREFIX = "APP_"
LEGACY_ENV_PREFIX = "SKYNET_"

# Local mode must stay reachable from this machine only. The API also accepts
# only these names in the Host header (DNS rebinding). Starlette's host check
# splits on the first ":", so an IPv6 bind such as "::1" could never match.
LOCAL_MODE_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_API_HOST = "0.0.0.0"

# Hosted Skynet the Mac app links to (SKY-124). Gilad's Q2 decision: release
# builds link to production, dev builds to staging. A build with no
# ``APP_HOSTED_TARGET`` and no stamp file is treated as a release build, so a
# packaging slip can never send real users to staging. Dev builds opt in with
# ``APP_HOSTED_TARGET=staging`` (see ``.env.example``). The production API
# origin is the Railway production backend behind ``api.skynetml.com``.
HOSTED_TARGET_STAMP = Path(__file__).parent / "hosted_target.txt"
HOSTED_TARGET_URLS = {
    "production": ("https://skynetml.com", "https://api.skynetml.com"),
    "staging": ("https://staging.skynetml.com", "https://api-staging.skynetml.com"),
}


def _is_safe_hosted_origin(url: str, *, allow_insecure: bool) -> bool:
    """Return whether a hosted link URL is a plain origin the Mac may send tokens to.

    Args:
        url: The configured URL, without a trailing slash.
        allow_insecure: Whether the dev override allows ``http`` on a loopback host.

    Returns:
        True for ``https://host[:port]`` with no user info, path, query or fragment,
        and for ``http`` on a loopback host when the override is on.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        _ = parts.port
    except ValueError:
        return False
    if not host or "@" in parts.netloc or parts.path or parts.query or parts.fragment:
        return False
    if parts.scheme == "https":
        return True
    return allow_insecure and parts.scheme == "http" and host in LOCAL_MODE_LOOPBACK_HOSTS


def legacy_env_name(name: str) -> str:
    """Return the pre-rename ``SKYNET_*`` spelling of an ``APP_*`` variable.

    Args:
        name: Variable name starting with ``APP_``.

    Returns:
        The same name with ``SKYNET_`` in place of ``APP_``.

    Raises:
        ValueError: When ``name`` does not start with ``APP_``.
    """
    if not name.startswith(ENV_PREFIX):
        raise ValueError(f"{name!r} is not an {ENV_PREFIX}* variable")
    return LEGACY_ENV_PREFIX + name.removeprefix(ENV_PREFIX)


def app_env(name: str, default: str | None = None, environ: Mapping[str, str] | None = None) -> str | None:
    """Read an ``APP_*`` variable, falling back to its ``SKYNET_*`` name.

    An empty value counts as unset, so a blank new name never hides a real old one.

    Args:
        name: Variable name starting with ``APP_``.
        default: Value returned when neither name is set.
        environ: Mapping to read instead of ``os.environ``.

    Returns:
        The new name's value, else the old name's, else ``default``.
    """
    source = os.environ if environ is None else environ
    return source.get(name) or source.get(legacy_env_name(name)) or default


def with_legacy_env(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy each ``APP_*`` entry under its ``SKYNET_*`` name as well.

    For environments handed to code that may predate the rename, such as the
    pinned sandbox image or a user's own command. The new value overwrites a
    stale legacy one, so both spellings always agree.

    Args:
        environment: Child-process environment using the new names.

    Returns:
        A new mapping carrying both spellings of every ``APP_*`` variable.
    """
    result = dict(environment)
    for name, value in environment.items():
        if name.startswith(ENV_PREFIX):
            result[legacy_env_name(name)] = value
    return result


# Keyed on the raw CSV rather than cached on the instance: tests monkeypatch
# the source strings at runtime, and an instance-level cache would go stale.
@lru_cache(maxsize=16)
def _csv_lower_set(raw: str) -> frozenset[str]:
    """Split a comma-separated string into a lowercase frozenset of trimmed, non-empty items.

    Args:
        raw: Comma-separated text, possibly with blanks and surrounding whitespace.

    Returns:
        The distinct lowercase items.
    """
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


class Settings(BaseSettings):
    """Application configuration loaded from environment variables.

    All settings have sensible defaults for local development.
    Production deployments should override via .env file or environment.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    remote_db_url: SecretStr | None = Field(default=None, description="PostgreSQL connection string for remote storage")

    openai_api_key: SecretStr | None = Field(default=None, description="OpenAI API key for model access")
    openai_api_base: str | None = Field(
        default=None,
        description=("Base URL of the OpenAI-compatible LLM gateway (LiteLLM reads the same env var for serving)."),
    )
    transcription_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "OpenRouter API key for dictation speech-to-text (Whisper large-v3-turbo "
            "through OpenRouter) — the platform's sole STT provider. Unset disables "
            "dictation with a typed 503."
        ),
    )
    anthropic_api_key: SecretStr | None = Field(default=None, description="Anthropic API key for Claude models")

    stripe_secret_key: SecretStr | None = Field(
        default=None,
        alias="STRIPE_SECRET_KEY",
        description="Stripe secret API key (sk_test_… in test mode). Unset disables every billing mutation; reads still work.",
    )
    stripe_webhook_secret: SecretStr | None = Field(
        default=None,
        alias="STRIPE_WEBHOOK_SECRET",
        description="Stripe webhook signing secret (whsec_…) used to verify event payload authenticity.",
    )
    stripe_price_pack_starter: str = Field(
        default="",
        alias="STRIPE_PRICE_PACK_STARTER",
        description="Stripe price id for the 'starter' one-time top-up pack.",
    )
    stripe_price_pack_plus: str = Field(
        default="", alias="STRIPE_PRICE_PACK_PLUS", description="Stripe price id for the 'plus' one-time top-up pack."
    )
    stripe_price_pack_pro: str = Field(
        default="", alias="STRIPE_PRICE_PACK_PRO", description="Stripe price id for the 'pro' one-time top-up pack."
    )
    stripe_price_pro_monthly: str = Field(
        default="",
        alias="STRIPE_PRICE_PRO_MONTHLY",
        description="Stripe recurring price id for the Skynet Pro monthly plan. Unset hides the upgrade.",
    )
    app_public_url: str = Field(
        default="http://localhost:3000",
        alias="APP_PUBLIC_URL",
        description="Public origin of the web app, used to build Stripe Checkout success/cancel return URLs.",
    )
    mcp_resource_url: str = Field(
        default="",
        alias="MCP_RESOURCE_URL",
        description="Public URL of the hosted remote MCP server, e.g. https://mcp.skynetml.com/mcp. Its origin "
        "is the OAuth issuer and tokens are bound to it. Empty turns the remote MCP server off.",
    )
    app_environment: str = Field(
        default="",
        alias="APP_ENVIRONMENT",
        description="Deployment environment name. A name ending in -pr-<number> marks a PR preview environment.",
    )
    app_git_sha: str = Field(
        default="",
        alias="APP_GIT_SHA",
        description="Commit the deployed image was built from.",
    )
    preview_grant_cents: int = Field(
        default=0,
        ge=0,
        alias="PREVIEW_GRANT_CENTS",
        description="One-time credit each new account gets in a PR preview environment, so automated "
        "checks can pay for a run there. Ignored outside preview environments.",
    )
    local_mode: bool = Field(
        default=False,
        validation_alias=AliasChoices("APP_LOCAL_MODE", "SKYNET_LOCAL_MODE"),
        description="Run as a single-user app on this machine (the Mac app). Never valid on a hosted deployment.",
    )
    local_state_dir: str = Field(
        default="",
        validation_alias=AliasChoices("APP_LOCAL_STATE_DIR", "SKYNET_LOCAL_STATE_DIR"),
        description="Directory for local-mode state files, such as CLI rate-limit records.",
    )
    local_grant_cents: int = Field(
        default=100_000_000,
        ge=0,
        validation_alias=AliasChoices("APP_LOCAL_GRANT_CENTS", "SKYNET_LOCAL_GRANT_CENTS"),
        description="Credit the local user gets in local mode, so budget checks never block a run.",
    )
    local_user_email: str = Field(
        default="local@skynet.local",
        validation_alias=AliasChoices("APP_LOCAL_USER_EMAIL", "SKYNET_LOCAL_USER_EMAIL"),
        description="Email of the single local-mode user.",
    )
    hosted_target: str = Field(
        default="",
        validation_alias="APP_HOSTED_TARGET",
        description="Hosted Skynet the Mac app links to: production or staging. Unset reads the build stamp; "
        "no stamp means production. Dev builds set staging.",
    )
    hosted_app_url: str = Field(
        default="",
        validation_alias="APP_HOSTED_APP_URL",
        description="Hosted web app origin for the Mac account link. Unset uses the hosted target default.",
    )
    hosted_api_url: str = Field(
        default="",
        validation_alias="APP_HOSTED_API_URL",
        description="Hosted API origin for the Mac account link. Unset uses the hosted target default.",
    )
    hosted_allow_insecure_url: bool = Field(
        default=False,
        validation_alias="APP_HOSTED_ALLOW_INSECURE_URL",
        description="Dev only: let local mode use an http hosted URL on a loopback host, for a local hosted stack.",
    )
    local_sandbox_image: str = Field(
        default="",
        pattern=r"^(sha256:[0-9a-f]{64})?$",
        validation_alias=AliasChoices("APP_LOCAL_SANDBOX_IMAGE", "SKYNET_LOCAL_SANDBOX_IMAGE"),
        description="Docker image id (sha256:<64 hex>) of the local proposer sandbox. Empty means not built.",
    )
    docker_bin: str = Field(
        default="docker",
        validation_alias=AliasChoices("APP_DOCKER_BIN", "SKYNET_DOCKER_BIN"),
        description="Docker CLI used by local mode.",
    )
    codex_bin: str = Field(
        default="codex",
        validation_alias=AliasChoices("APP_CODEX_BIN", "SKYNET_CODEX_BIN"),
        description="Codex CLI used for subscription calls in local mode.",
    )
    claude_bin: str = Field(
        default="claude",
        validation_alias=AliasChoices("APP_CLAUDE_BIN", "SKYNET_CLAUDE_BIN"),
        description="Claude Code CLI used for subscription calls in local mode.",
    )
    cli_max_concurrency: int = Field(
        default=2,
        ge=1,
        le=16,
        validation_alias=AliasChoices("APP_CLI_MAX_CONCURRENCY", "SKYNET_CLI_MAX_CONCURRENCY"),
        description="Most CLI subscription calls that run at the same time in local mode.",
    )
    cli_fallback_order: str = Field(
        default="",
        validation_alias=AliasChoices("APP_CLI_FALLBACK", "SKYNET_CLI_FALLBACK"),
        description="Comma-separated CLIs to try, in order, when a CLI is rate limited. Empty disables fallback.",
    )
    cli_call_timeout_seconds: float = Field(
        default=600.0,
        gt=0,
        validation_alias=AliasChoices("APP_CLI_CALL_TIMEOUT", "SKYNET_CLI_CALL_TIMEOUT"),
        description="Wall-clock limit for one CLI subscription model call.",
    )
    host_agent_timeout_seconds: float = Field(
        default=3600.0,
        gt=0,
        validation_alias=AliasChoices("APP_HOST_AGENT_TIMEOUT", "SKYNET_HOST_AGENT_TIMEOUT"),
        description="Wall-clock limit for one host CLI proposer session.",
    )
    smtp_host: str | None = Field(
        default=None,
        alias="SMTP_HOST",
        description="SMTP relay host for outbound mail (email one-time sign-in codes). Unset disables email-based 2FA with a typed error.",
    )
    smtp_port: int = Field(default=587, alias="SMTP_PORT", description="SMTP relay port.")
    smtp_username: str | None = Field(
        default=None, alias="SMTP_USERNAME", description="SMTP auth username; unset sends unauthenticated."
    )
    smtp_password: SecretStr | None = Field(default=None, alias="SMTP_PASSWORD", description="SMTP auth password.")
    smtp_from: str | None = Field(
        default=None,
        alias="SMTP_FROM",
        description="From address for outbound mail; falls back to SMTP_USERNAME.",
    )
    smtp_starttls: bool = Field(
        default=True, alias="SMTP_STARTTLS", description="Upgrade the SMTP connection with STARTTLS."
    )
    vapid_public_key: str | None = Field(
        default=None,
        alias="VAPID_PUBLIC_KEY",
        description="Web Push VAPID public key (urlsafe base64, uncompressed P-256 point) browsers subscribe with. Unset turns the Chrome notification channel off.",
    )
    vapid_private_key: SecretStr | None = Field(
        default=None,
        alias="VAPID_PRIVATE_KEY",
        description="Web Push VAPID private key (urlsafe base64 raw P-256 scalar, or a PEM) that signs push requests.",
    )
    vapid_subject: str | None = Field(
        default=None,
        alias="VAPID_SUBJECT",
        description="VAPID contact claim, a mailto: or https: URL push services can reach the sender at. Unset falls back to APP_PUBLIC_URL.",
    )
    slack_client_id: str | None = Field(
        default=None,
        alias="SLACK_CLIENT_ID",
        description="Client ID of the Skynet Slack app. Unset (or SLACK_CLIENT_SECRET / BYOK_VAULT_KEY unset) turns the Slack notification channel off.",
    )
    slack_client_secret: SecretStr | None = Field(
        default=None,
        alias="SLACK_CLIENT_SECRET",
        description="Client secret of the Skynet Slack app, used to trade the OAuth code for the bot token.",
    )
    slack_redirect_url: str | None = Field(
        default=None,
        alias="SLACK_REDIRECT_URL",
        description="Absolute URL of this backend's /notifications/slack/oauth/callback as registered on the Slack app. Unset derives it from the incoming request.",
    )
    webauthn_rp_id: str | None = Field(
        default=None,
        alias="WEBAUTHN_RP_ID",
        description="WebAuthn relying-party id (the site's registrable domain, e.g. 'skynet.example.com'). Unset derives it from APP_PUBLIC_URL's hostname.",
    )
    webauthn_origins: str = Field(
        default="",
        alias="WEBAUTHN_ORIGINS",
        description="Comma-separated browser origins accepted in passkey ceremonies. Unset allows APP_PUBLIC_URL plus the localhost dev origins.",
    )
    byok_vault_key: SecretStr | None = Field(
        default=None,
        alias="BYOK_VAULT_KEY",
        description="Fernet key (urlsafe base64, 32 bytes) that encrypts stored BYOK provider secrets at rest. Unset disables saving keys (the vault degrades to read-only); reads of already-stored masked metadata still work.",
    )
    hf_oauth_client_id: str | None = Field(
        default=None,
        alias="HF_OAUTH_CLIENT_ID",
        description="Client id of the Hugging Face OAuth app (huggingface.co/settings/applications). Unset hides 'Continue with Hugging Face'; users can still paste an access token.",
    )
    hf_oauth_client_secret: SecretStr | None = Field(
        default=None,
        alias="HF_OAUTH_CLIENT_SECRET",
        description="Client secret of the Hugging Face OAuth app. Optional: a public (secret-less) app authenticates the token exchange with PKCE alone.",
    )
    hf_oauth_redirect_uri: str | None = Field(
        default=None,
        alias="HF_OAUTH_REDIRECT_URI",
        description="Absolute URL of this backend's /connectors/huggingface/oauth/callback as registered on the OAuth app. Unset derives it from the incoming request, which is right whenever the proxy forwards scheme and host.",
    )
    google_oauth_client_id: str | None = Field(
        default=None,
        alias="GOOGLE_OAUTH_CLIENT_ID",
        description="Client id of the Google OAuth client shared by the Google Sheets and Google Drive connectors. Unset hides 'Continue with Google'; users can still paste a service-account key.",
    )
    google_oauth_client_secret: SecretStr | None = Field(
        default=None,
        alias="GOOGLE_OAUTH_CLIENT_SECRET",
        description="Client secret of the Google OAuth client (web-application clients always have one).",
    )
    google_oauth_redirect_uri: str | None = Field(
        default=None,
        alias="GOOGLE_OAUTH_REDIRECT_URI",
        description="Absolute URL of this backend's /connectors/google_sheets/oauth/callback as registered on the Google client. The Drive callback is derived from it by swapping the provider segment (/connectors/google_drive/), so register that on the client too. Unset derives every callback from the incoming request.",
    )
    google_picker_api_key: SecretStr | None = Field(
        default=None,
        alias="GOOGLE_PICKER_API_KEY",
        description="Browser API key with the Google Picker API enabled, from the same Cloud project as the Google OAuth client. The Drive and Sheets connectors only reach the files a user picks, so unset leaves OAuth-linked users with nothing to import; restrict the key to the frontend's HTTP referrers.",
    )
    github_oauth_client_id: str | None = Field(
        default=None,
        alias="GITHUB_OAUTH_CLIENT_ID",
        description="Client id of the GitHub OAuth app used by the GitHub connector. Unset hides 'Continue with GitHub'; users can still paste a personal access token.",
    )
    github_oauth_client_secret: SecretStr | None = Field(
        default=None,
        alias="GITHUB_OAUTH_CLIENT_SECRET",
        description="Client secret of the GitHub OAuth app. GitHub requires it for the code exchange.",
    )
    github_oauth_redirect_uri: str | None = Field(
        default=None,
        alias="GITHUB_OAUTH_REDIRECT_URI",
        description="Absolute URL of this backend's /connectors/github/oauth/callback as registered on the GitHub app. Unset derives it from the incoming request.",
    )
    microsoft_oauth_client_id: str | None = Field(
        default=None,
        alias="MICROSOFT_OAUTH_CLIENT_ID",
        description="Application (client) id of the Microsoft Entra app shared by the OneDrive and Azure Blob Storage connectors. Unset hides 'Continue with Microsoft'; OneDrive has no pasted-credential fallback, Azure Blob still takes a connection string or SAS URL.",
    )
    microsoft_oauth_client_secret: SecretStr | None = Field(
        default=None,
        alias="MICROSOFT_OAUTH_CLIENT_SECRET",
        description="Client secret of the Microsoft Entra app.",
    )
    microsoft_oauth_redirect_uri: str | None = Field(
        default=None,
        alias="MICROSOFT_OAUTH_REDIRECT_URI",
        description="Absolute URL of this backend's /connectors/onedrive/oauth/callback as registered on the Entra app. The Azure Blob callback is derived from it by swapping in /connectors/azure_blob/, so register that on the app too. Unset derives every callback from the incoming request.",
    )
    notion_oauth_client_id: str | None = Field(
        default=None,
        alias="NOTION_OAUTH_CLIENT_ID",
        description="OAuth client id of the Notion public integration used by the Notion connector. Unset hides 'Continue with Notion'; users can still paste an internal-integration token.",
    )
    notion_oauth_client_secret: SecretStr | None = Field(
        default=None,
        alias="NOTION_OAUTH_CLIENT_SECRET",
        description="OAuth client secret of the Notion public integration. Notion requires it for the code exchange.",
    )
    notion_oauth_redirect_uri: str | None = Field(
        default=None,
        alias="NOTION_OAUTH_REDIRECT_URI",
        description="Absolute URL of this backend's /connectors/notion/oauth/callback as registered on the Notion integration. Unset derives it from the incoming request.",
    )
    supabase_oauth_client_id: str | None = Field(
        default=None,
        alias="SUPABASE_OAUTH_CLIENT_ID",
        description="OAuth client id of the Supabase OAuth app used by the Supabase connector. Unset hides 'Continue with Supabase'; users can still paste a database connection URL.",
    )
    supabase_oauth_client_secret: SecretStr | None = Field(
        default=None,
        alias="SUPABASE_OAUTH_CLIENT_SECRET",
        description="OAuth client secret of the Supabase OAuth app. Supabase requires it for the code exchange.",
    )
    supabase_oauth_redirect_uri: str | None = Field(
        default=None,
        alias="SUPABASE_OAUTH_REDIRECT_URI",
        description="Absolute URL of this backend's /connectors/supabase/oauth/callback as registered on the Supabase OAuth app. Unset derives it from the incoming request.",
    )
    openrouter_oauth_redirect_uri: str | None = Field(
        default=None,
        alias="OPENROUTER_OAUTH_REDIRECT_URI",
        description="Absolute URL of this backend's /billing/byok/openrouter/oauth/callback, used as the 'Continue with OpenRouter' callback. Unset derives it from the incoming request, which is right whenever the proxy forwards scheme and host.",
    )
    litellm_proxy_url: str | None = Field(
        default=None,
        alias="LITELLM_PROXY_URL",
        description="Base URL of the self-hosted LiteLLM proxy that fronts managed inference (e.g. https://proxy.internal/v1). Unset (the default) routes managed runs directly to providers via process env keys; set it to flow all managed traffic through the metered gateway. BYOK runs always bypass the proxy and reach their provider directly.",
    )
    litellm_proxy_api_key: SecretStr | None = Field(
        default=None,
        alias="LITELLM_PROXY_API_KEY",
        description="Virtual key the backend presents to the LiteLLM proxy for managed runs. Only consulted when LITELLM_PROXY_URL is set.",
    )
    vercel_token: SecretStr | None = Field(
        default=None,
        alias="VERCEL_TOKEN",
        description="Vercel access token used by the control plane to create one managed sandbox for each paid setup check and optimization run. Unset disables protected execution with a typed error; VERCEL_TEAM_ID and VERCEL_PROJECT_ID must be set alongside it.",
    )
    vercel_team_id: str | None = Field(
        default=None, alias="VERCEL_TEAM_ID", description="Vercel team that owns the optimization sandboxes."
    )
    vercel_project_id: str | None = Field(
        default=None,
        alias="VERCEL_PROJECT_ID",
        description="Vercel project the optimization sandboxes are created under.",
    )
    vercel_sandbox_image: str = Field(
        default="vercel/sandbox/universal:latest",
        alias="VERCEL_SANDBOX_IMAGE",
        description="Immutable container image the protected optimization sandbox boots from. The SDK's universal default does not contain Skynet's pinned optimizer, harness, and offline dependency set.",
    )
    dspy_sandbox_image: str | None = Field(
        default=None,
        alias="DSPY_SANDBOX_IMAGE",
        description="Immutable prebuilt backend image, addressed by @sha256, containing the exact deployed Python and optimizer dependencies for protected DSPy execution.",
    )
    vercel_sandbox_max_lifetime_seconds: float = Field(
        default=2_700.0,
        alias="VERCEL_SANDBOX_MAX_LIFETIME_SECONDS",
        gt=0.0,
        le=VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS,
        description="Ceiling on a sandbox's lifetime in seconds. 2700 (45 minutes) is the Hobby plan limit; Pro and Enterprise teams can raise it to 86400 (24 hours), the most Vercel accepts. A job that outlives its box reopens one on its next call.",
    )
    blackbox_agent_gateway_url: str | None = Field(
        default=None,
        alias="BLACKBOX_AGENT_GATEWAY_URL",
        description="OpenAI-compatible base URL the coding harnesses inside agent sandboxes send their model calls to (e.g. https://proxy.example.com/v1). Must be reachable from Vercel's network. Unset falls back to LITELLM_PROXY_URL.",
    )
    blackbox_agent_gateway_api_key: SecretStr | None = Field(
        default=None,
        alias="BLACKBOX_AGENT_GATEWAY_API_KEY",
        description="API key the sandboxed harnesses present to BLACKBOX_AGENT_GATEWAY_URL. Unset falls back to LITELLM_PROXY_API_KEY. TODO: mint a per-run virtual key so sandbox usage is attributable to the job.",
    )
    openrouter_provisioning_key: SecretStr | None = Field(
        default=None,
        alias="OPENROUTER_PROVISIONING_KEY",
        description="OpenRouter key-management (provisioning) API key. When set, managed runs authenticate with a per-user OpenRouter runtime key whose spend limit is synced to the account's balance before each dispatch, capping upstream spend at the provider itself. Unset (the default) sends managed runs through the shared gateway key. Requires BYOK_VAULT_KEY to encrypt the minted secrets at rest.",
    )
    usage_markup: float = Field(
        default=1.15,
        ge=1.0,
        alias="USAGE_MARKUP",
        description="Multiplier applied to the provider cost of every platform-paid metered charge (managed LLM tokens and Vercel sandbox compute) before converting to cents. 1.15 bills cost plus 15%. BYOK runs are unaffected: they pay only the 5% platform fee on model cost. Holds, pre-run estimates and displayed model prices include it.",
    )
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        alias="OPENROUTER_API_KEY",
        description="OpenRouter master-account (inference) API key. Read-only here: the float monitor uses it to read the shared prepaid balance (GET /api/v1/credits) so it can warn when the OpenRouter float runs thin against outstanding customer balances. Managed inference itself routes through the LiteLLM proxy or per-user provisioned keys, not this field.",
    )
    firecrawl_api_key: SecretStr | None = Field(
        default=None,
        alias="FIRECRAWL_API_KEY",
        description="Firecrawl API key. When set, the wizard agent gets web_search, fetch_url and crawl_site tools so it can search the web and read links the user gives it. Unset leaves the agent without web access.",
    )
    # Every *_CENTS variable still reads its pre-rename *_CREDITS name so existing deployments keep their values.
    openrouter_balance_floor_cents: int = Field(
        default=1000,
        validation_alias=AliasChoices("OPENROUTER_BALANCE_FLOOR_CENTS", "OPENROUTER_BALANCE_FLOOR_CREDITS"),
        description="Low-water mark for the OpenRouter master-account balance, in cents (default 1000 = $10). When the balance falls below this floor the float monitor logs a WARNING — native Auto Top-Up may have failed or demand is outrunning refills. 0 disables the monitor. Only consulted when OPENROUTER_API_KEY is set.",
    )
    openrouter_float_check_interval_seconds: float = Field(
        default=900.0,
        ge=0.0,
        alias="OPENROUTER_FLOAT_CHECK_INTERVAL_SECONDS",
        description="Seconds between periodic OpenRouter float checks on the API pods (advisory-lock-gated so one replica per tick reads the balance). Complements the post-purchase check, which only fires when a customer tops up — a failed Auto Top-Up on a quiet day would otherwise go unnoticed until the next 402. 0 disables the periodic check; values below 60 are raised to 60.",
    )
    openrouter_float_alert_email: str = Field(
        default="",
        alias="OPENROUTER_FLOAT_ALERT_EMAIL",
        description="Operator address that receives an email when the OpenRouter float falls below OPENROUTER_BALANCE_FLOOR_CENTS (requires SMTP_HOST). Empty disables the email; the WARNING log and ALERT_WEBHOOK_URL forward still fire.",
    )
    openrouter_float_alert_cooldown_seconds: float = Field(
        default=21600.0,
        ge=0.0,
        alias="OPENROUTER_FLOAT_ALERT_COOLDOWN_SECONDS",
        description="Minimum seconds between two low-float notifications (email + webhook). Shared across replicas through Redis when REDIS_URL is set, per-process otherwise, so a 15-minute check loop can't page every tick. 0 sends on every breach.",
    )
    issuing_balance_floor_cents: int = Field(
        default=0,
        validation_alias=AliasChoices("ISSUING_BALANCE_FLOOR_CENTS", "ISSUING_BALANCE_FLOOR_CREDITS"),
        description="Low-water mark for the Stripe Issuing balance that funds the provider card, in cents. When the Issuing balance plus pending Skynet top-ups falls below it, the funding loop refills to ISSUING_BALANCE_TARGET_CENTS: first from the Stripe payments balance, then from the linked bank. 0 (the default) disables the loop.",
    )
    issuing_balance_target_cents: int = Field(
        default=0,
        validation_alias=AliasChoices("ISSUING_BALANCE_TARGET_CENTS", "ISSUING_BALANCE_TARGET_CREDITS"),
        description="Level the funding loop refills the Stripe Issuing balance to, in cents. Must sit above ISSUING_BALANCE_FLOOR_CENTS or the loop stays off. Bank top-ups take up to five business days, so size the gap to cover about a week of provider spend.",
    )
    issuing_funding_check_interval_seconds: float = Field(
        default=900.0,
        ge=0.0,
        alias="ISSUING_FUNDING_CHECK_INTERVAL_SECONDS",
        description="Seconds between Issuing balance checks on the API pods (advisory-lock-gated, one replica per tick). Failures alert through ALERT_WEBHOOK_URL and OPENROUTER_FLOAT_ALERT_EMAIL. 0 disables the loop; values below 60 are raised to 60.",
    )
    worker_enabled: bool = Field(
        default=True,
        alias="WORKER_ENABLED",
        description=(
            "Start the in-process job worker alongside the API. Set false on "
            "API-only pods when job execution runs in dedicated worker "
            "replicas (worker_main.py)."
        ),
    )
    worker_threads: int = Field(
        default=4, ge=1, le=32, description="Number of concurrent worker threads", alias="WORKER_CONCURRENCY"
    )
    job_admission_max_memory_fraction: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Container memory usage fraction above which idle workers defer "
            "claiming new jobs (running jobs are unaffected); the job waits in "
            "the Postgres queue instead of OOM-killing the pod. 0 disables; "
            "also inert where no cgroup limit is readable (e.g. bare-metal dev)."
        ),
        alias="JOB_ADMISSION_MAX_MEMORY_FRACTION",
    )
    worker_poll_interval: float = Field(
        default=1.0, ge=0.1, le=60.0, description="Seconds between queue polling cycles"
    )
    worker_stale_threshold: float = Field(
        default=600.0, ge=60.0, description="Seconds of inactivity before worker flagged as stuck"
    )
    job_max_attempts: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum orphan-recovery attempts before a job is marked failed",
        alias="JOB_MAX_ATTEMPTS",
    )
    orphan_sweep_interval_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=600.0,
        description="Seconds between periodic orphan-recovery sweeps (advisory-lock-gated)",
        alias="ORPHAN_SWEEP_INTERVAL",
    )
    stale_conversation_threshold_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        description="Days of inactivity before an unpinned agent conversation is auto-purged",
        alias="STALE_CONVERSATION_THRESHOLD_DAYS",
    )
    stale_conversation_sweep_interval_seconds: float = Field(
        default=86400.0,
        ge=300.0,
        description="Seconds between stale-conversation purge sweeps (advisory-lock-gated)",
        alias="STALE_CONVERSATION_SWEEP_INTERVAL",
    )
    staged_dataset_ttl_seconds: float = Field(
        default=600.0,
        ge=60.0,
        description="Seconds past a wizard staged-dataset's creation before it is auto-purged",
        alias="STAGED_DATASET_TTL_SECONDS",
    )
    staged_dataset_sweep_interval_seconds: float = Field(
        default=300.0,
        ge=60.0,
        description="Seconds between staged-dataset TTL purge sweeps (advisory-lock-gated)",
        alias="STAGED_DATASET_SWEEP_INTERVAL",
    )
    event_loop_lag_monitor_enabled: bool = Field(
        default=False,
        description=(
            "Enable an asyncio task that measures event-loop scheduling lag. "
            "Lag > threshold means a handler is doing sync CPU work and "
            "blocking the loop — diagnostic only, leave off in prod."
        ),
        alias="EVENT_LOOP_LAG_MONITOR",
    )
    telemetry_enabled: bool = Field(
        default=True,
        description=(
            "Accept first-party product-telemetry events at POST /telemetry/events. "
            "When off, ingestion is a silent no-op (events are dropped, never "
            "stored) — an ops kill switch for incident response or write-volume "
            "control. The read endpoints are unaffected."
        ),
        alias="TELEMETRY_ENABLED",
    )
    sentry_dsn: SecretStr | None = Field(
        default=None,
        alias="SENTRY_DSN",
        description="Sentry DSN for backend and worker error reporting. Unset disables the SDK.",
    )
    sentry_environment: str = Field(
        default="development",
        alias="SENTRY_ENVIRONMENT",
        description="Deployment environment attached to Sentry events.",
    )
    sentry_traces_sample_rate: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        alias="SENTRY_TRACES_SAMPLE_RATE",
        description="Fraction of backend performance traces exported to Sentry.",
    )
    event_loop_lag_threshold_ms: float = Field(
        default=100.0,
        ge=10.0,
        le=10000.0,
        description="Warn when event-loop lag exceeds this many milliseconds.",
        alias="EVENT_LOOP_LAG_THRESHOLD_MS",
    )
    progress_events_per_job_cap: int = Field(
        default=5000,
        ge=1,
        description="Maximum stored progress events per optimization job before old events are evicted",
    )
    dataset_max_file_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1,
        description="Per-file cap on compressed dataset bytes saved to a user's library",
    )
    connector_import_max_rows: int = Field(
        default=20000,
        ge=1,
        alias="CONNECTOR_IMPORT_MAX_ROWS",
        description="Row cap for connectors that import records rather than files (database tables, LLM traces, Notion databases).",
    )
    user_storage_quota_bytes: int = Field(
        default=250 * 1024 * 1024,
        ge=1,
        description="Per-user unified storage budget in bytes across all of their Skynet data",
    )
    pro_storage_quota_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,
        ge=1,
        description="Unified storage budget in bytes for an account on the Skynet Pro plan",
    )
    cancel_poll_interval: float = Field(
        default=1.0, ge=0.1, le=10.0, description="Seconds between cancel signal checks"
    )
    lm_request_timeout_seconds: float = Field(
        default=600.0,
        ge=1.0,
        le=3600.0,
        description=(
            "Per-request timeout (seconds) applied to every dspy.LM call so a stalled "
            "provider response can't wedge a run forever on a socket read. Overridable "
            "per-model via ModelConfig.extra['timeout']."
        ),
        alias="LM_REQUEST_TIMEOUT",
    )
    agent_request_timeout_seconds: float = Field(
        default=120.0,
        ge=1.0,
        le=600.0,
        description=(
            "Per-request LM timeout (seconds) for interactive agent turns (chat). "
            "Separate from lm_request_timeout_seconds, which is sized for batch "
            "optimization runs — a stalled provider should fail a chat turn in "
            "minutes, not tens of minutes."
        ),
        alias="AGENT_REQUEST_TIMEOUT",
    )
    job_stall_timeout_seconds: float = Field(
        default=1800.0,
        ge=0.0,
        description=(
            "Watchdog: fail a running optimization whose subprocess emits no "
            "progress/log/result event for this many seconds. Backstops a wedged child "
            "that the lease heartbeat would otherwise keep alive indefinitely. Keep it "
            "comfortably above the *retried* LM-call ceiling — build_language_model caps "
            "num_retries so (retries + 1) * lm_request_timeout_seconds stays under this "
            "with margin, so a hung call times out first; 0 disables the watchdog."
        ),
        alias="JOB_STALL_TIMEOUT",
    )
    gepa_eval_num_threads: int = Field(
        default=8,
        ge=1,
        le=64,
        description=(
            "Eval/rollout thread count injected into GEPA when the submission "
            "doesn't set num_threads. GEPA's own default is sequential "
            "candidate evaluation, and runs are LM-latency-bound, so parallel "
            "eval cuts wall-clock roughly linearly. An explicit user-supplied "
            "num_threads always wins; job_lm_max_concurrency bounds the "
            "multiplied total either way."
        ),
        alias="GEPA_EVAL_NUM_THREADS",
    )
    gepa_pxn_parents: int = Field(
        default=1,
        ge=1,
        le=16,
        description=(
            "GEPA PxN batched sampling — parent count (p). Each reflective "
            "iteration mutates p distinct parent candidates, drawing "
            "gepa_pxn_proposals (n) proposals from each, and evaluates all p*n "
            "as one batch. The default of 1 reproduces GEPA's classic "
            "single-mutation sampling; raising p (or n) above 1 switches GEPA to "
            "PxNSampling(p, n) — better wall-clock and generalization at higher "
            "LM cost, bounded per job by job_lm_max_concurrency. A submission "
            "that supplies its own gepa_kwargs.sampling_strategy always wins."
        ),
        alias="GEPA_PXN_PARENTS",
    )
    gepa_pxn_proposals: int = Field(
        default=1,
        ge=1,
        le=16,
        description=(
            "GEPA PxN batched sampling — proposals per parent (n); see "
            "gepa_pxn_parents. Raising n (or p) above 1 activates "
            "PxNSampling(p, n). Defaults to 1 (classic single-mutation GEPA)."
        ),
        alias="GEPA_PXN_PROPOSALS",
    )
    react_native_tool_calling: bool = Field(
        default=False,
        description=(
            "Route ReActV2 tool calls through the provider's native "
            "function-calling API instead of DSPy's text tool protocol. When "
            "on, the process installs a global ChatAdapter with "
            "use_native_function_calling=True; tools ride the provider's "
            "tools= parameter and turns come back as structured tool_calls "
            "(provider-default parallel calling included). The adapter is a "
            "no-op for signatures without a ToolCalls field, so only ReAct "
            "programs are affected. Off (default) keeps the text tool protocol "
            "that every model — including the flaky MiniMax student — parses "
            "reliably; only enable it for a deployment whose models all "
            "support native function calling."
        ),
        alias="REACT_NATIVE_TOOL_CALLING",
    )
    job_lm_max_concurrency: int = Field(
        default=16,
        ge=0,
        le=256,
        description=(
            "Per-job-child ceiling on concurrent LM calls. Grid pair threads "
            "times GEPA eval threads multiply, and a user can pass any "
            "num_threads in optimizer kwargs; this gate keeps one job from "
            "spraying the provider with enough parallel calls to trip rate "
            "limits and fail the run. 0 disables the gate."
        ),
        alias="JOB_LM_MAX_CONCURRENCY",
    )
    grid_pair_max_workers: int = Field(
        default=4,
        ge=1,
        le=16,
        description=(
            "Concurrent (generation, reflection) pairs per grid-search job "
            "child. Total LM concurrency in the child is still capped by "
            "job_lm_max_concurrency regardless of this value."
        ),
        alias="GRID_PAIR_MAX_WORKERS",
    )
    grid_distributed_pairs: bool = Field(
        default=True,
        description=(
            "Fan a multi-pair grid search out as one claimable job row per "
            "pair so pairs spread across the whole worker fleet instead of "
            "sharing one child process. Off = classic all-pairs-in-one-child "
            "execution; flipping the flag never re-shapes a grid already in "
            "flight."
        ),
        alias="GRID_DISTRIBUTED_PAIRS",
    )
    job_run_start_method: Literal["fork", "spawn", "forkserver"] = Field(
        default="fork", description="Multiprocessing start method for job execution"
    )
    # Ceiling (pool_size + max_overflow = 20 + 20 = 40) is aligned with
    # Starlette's anyio threadpool width (40) so a burst of concurrent sync DB
    # handlers can't exhaust the pool and stall on pool_timeout. Tune via
    # DB_POOL_SIZE to keep total connections (× pod count) under the Postgres
    # max_connections budget.
    db_pool_size: int = Field(default=20, ge=1, le=200, description="SQLAlchemy pool size", alias="DB_POOL_SIZE")
    db_pool_max_overflow: int = Field(
        default=20,
        ge=0,
        le=200,
        description="SQLAlchemy max overflow connections",
        alias="DB_POOL_MAX_OVERFLOW",
    )
    db_pool_recycle_seconds: int = Field(
        default=3600,
        ge=60,
        description="Seconds before SQLAlchemy recycles a pooled DB connection",
        alias="DB_POOL_RECYCLE",
    )
    db_pool_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Seconds SQLAlchemy waits for a free pool connection before erroring",
        alias="DB_POOL_TIMEOUT",
    )
    db_pgbouncer_transaction_mode: bool = Field(
        default=False,
        description="Disable driver features incompatible with PgBouncer transaction pooling",
        alias="DB_PGBOUNCER_TRANSACTION_MODE",
    )
    app_code_version: str = Field(
        default="",
        description="Build git SHA used to keep queued jobs on compatible workers",
        validation_alias=AliasChoices("APP_CODE_VERSION", "SKYNET_CODE_VERSION"),
    )
    require_code_version: bool = Field(
        default=False,
        description="Refuse to start if code_version can't be resolved (production safety)",
        alias="REQUIRE_CODE_VERSION",
    )

    artifacts_dir: str = Field(default="artifacts", description="Directory for storing optimized program artifacts")
    logs_dir: str = Field(default="logs", description="Directory for job execution logs")

    default_timeout: float = Field(default=30.0, ge=1.0, description="Standard request timeout in seconds")

    host: str = Field(default="0.0.0.0", description="Server bind address")
    # Read through Settings, not os.environ, so the local-mode guard and the
    # server bind see the same value: any case, and backend/.env as well.
    api_host: str = Field(
        default=DEFAULT_API_HOST,
        description="Address main.run_server and the local-mode image command bind",
        validation_alias="API_HOST",
    )
    # One port source for the bind and the agent's MCP self-dial URL (SKY-111
    # D2). main.py binds this port, and API_PORT comes first so a PORT left in
    # a dev shell (Node tools) does not move it. The image binds the platform
    # PORT and exports that same value as API_PORT, so both agree there too.
    port: int = Field(
        default=8000,
        ge=1024,
        le=65535,
        description="Server port",
        validation_alias=AliasChoices("API_PORT", "PORT"),
    )
    reload: bool = Field(default=False, description="Enable auto-reload on code changes (dev only)")

    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:3001",
        description="Comma-separated list of allowed CORS origins",
        alias="ALLOWED_ORIGINS",
    )

    # A local stack (docker-compose service names resolve to RFC1918 addresses)
    # may need to reach private gateways and MCP tools, but the default has to
    # fail closed: an unauthenticated caller otherwise gets a /v1/models scan of
    # the deploy network (incl. cloud metadata services).
    discover_allow_private: bool = Field(
        default=False,
        description=(
            "Allow POST /models/discover to probe link-local / RFC1918 / "
            "ULA / reserved / multicast addresses. Default off blocks "
            "SSRF-style scans of the deploy network. Loopback (127.0.0.0/8, "
            "::1) is always allowed so Ollama-on-localhost works without a "
            "flag. Cloud metadata service IPs (169.254.169.254 / "
            "fd00:ec2::254) are blocked unconditionally regardless."
        ),
    )
    program_cache_max_entries: int = Field(
        # 32 (was 128): each entry is a full deserialized program (MBs), the
        # cache lives in the fork-parent API process, and beta traffic serves
        # far fewer distinct programs than 32 at a time.
        default=32,
        ge=1,
        description=(
            "Maximum number of compiled DSPy programs held in the in-process "
            "LRU cache. Each entry is roughly the size of one optimized "
            "program; set to bound the API process resident set."
        ),
    )
    model_catalog_ttl_seconds: float = Field(
        default=1800.0,
        ge=0.0,
        description=(
            "How long the curated model catalog stays cached before "
            "re-evaluating provider key availability. 0 disables caching."
        ),
    )

    log_level: str = Field(default="INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)")

    alert_webhook_url: str = Field(
        default="",
        alias="ALERT_WEBHOOK_URL",
        description=(
            'Incoming chat webhook (Slack-compatible {"text": …} payload; also '
            "accepted by Mattermost and Google Chat) that receives operational "
            "alerts — unhandled 500s, a dead worker, and code paths that call "
            "send_alert() directly. Unset (the default) disables outbound "
            "alerting: records still reach the logs, they just aren't forwarded."
        ),
    )
    alert_email: str = Field(
        default="",
        alias="ALERT_EMAIL",
        description=(
            "Operator address that receives every operational alert by email "
            "(requires SMTP_HOST), alongside or instead of ALERT_WEBHOOK_URL. "
            "Empty disables email alerts."
        ),
    )
    alert_email_max_per_hour: int = Field(
        default=20,
        ge=0,
        alias="ALERT_EMAIL_MAX_PER_HOUR",
        description=(
            "Per-process cap on alert emails in any rolling hour, so a burst of "
            "distinct errors can't flood the inbox. Alerts past the cap still "
            "reach the logs and the webhook."
        ),
    )
    alert_min_level: str = Field(
        default="ERROR",
        alias="ALERT_MIN_LEVEL",
        description=(
            "Minimum log level forwarded to ALERT_WEBHOOK_URL by the log handler "
            "(DEBUG/INFO/WARNING/ERROR/CRITICAL). Effective only at or above "
            "LOG_LEVEL, which gates the root logger first. Direct send_alert() "
            "calls ignore this threshold."
        ),
    )
    alert_environment: str = Field(
        default="",
        alias="ALERT_ENVIRONMENT",
        description=(
            "Short label prefixed to every alert (e.g. 'prod', 'staging') so a "
            "shared channel can attribute an alert to its source. Empty omits "
            "the prefix."
        ),
    )
    alert_throttle_seconds: float = Field(
        default=300.0,
        ge=0.0,
        alias="ALERT_THROTTLE_SECONDS",
        description=(
            "Cooldown during which an identical alert (same level, title and "
            "body) is forwarded at most once, so an error loop can't flood the "
            "webhook. 0 disables throttling."
        ),
    )

    @field_validator("alert_min_level")
    @classmethod
    def _validate_alert_min_level(cls, v: str) -> str:
        """Normalise ALERT_MIN_LEVEL to a canonical upper-case logging level name.

        Args:
            v: Raw ALERT_MIN_LEVEL value from the environment.

        Returns:
            The upper-cased level name.

        Raises:
            ValueError: When the value is not a standard logging level name.
        """
        name = v.strip().upper()
        if name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("ALERT_MIN_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return name

    code_agent_model: str = Field(
        default="",
        description=(
            "LiteLLM model id used by the submit-wizard code agent. Empty "
            "follows the model catalog's best-value default."
        ),
    )
    code_agent_base_url: str = Field(
        default="",
        description="Optional custom base URL for the code agent LM (an OpenAI-compatible gateway)",
    )

    generalist_agent_mcp_url: str = Field(
        default="",
        description=(
            "URL of the MCP server the generalist agent connects to (usually the "
            "same app's /mcp mount). When unset it is derived from HOST/PORT so "
            "changing the API port doesn't strand the agent on a dead :8000."
        ),
    )
    generalist_agent_model: str = Field(
        default="",
        description=(
            "LiteLLM model id used by the generalist agent (Cmd/Ctrl+J "
            "panel). Empty follows the model catalog's best-value default."
        ),
    )
    generalist_agent_base_url: str = Field(
        default="",
        description="Optional custom base URL for the generalist agent LM (an OpenAI-compatible gateway)",
    )

    interview_agent_model: str = Field(
        default="",
        description=(
            "LiteLLM model id every agent interview runs when the user picks "
            "none (onboarding, code interview, tagger dataset interview). Empty "
            "follows the catalog's measured fastest small featured model."
        ),
    )

    rule_watch_model: str = Field(
        default="",
        description=(
            "Model id of the fast judge that watches a repository run's recent "
            "actions against its plain-language rules. Empty follows the model "
            "catalog's best-value default."
        ),
    )
    rule_judge_model: str = Field(
        default="",
        description=(
            "Model id of the judge that reads each repository version's full diff "
            "against its plain-language rules before it is scored. Empty follows "
            "the model catalog's strongest model."
        ),
    )

    tagger_assist_model: str = Field(
        default="",
        description=(
            "LiteLLM model id used by the tagger's AI co-tagging assist "
            "(interview, calibration predictions, auto-tagging). Empty falls "
            "back to generalist_agent_model."
        ),
    )
    tagger_assist_base_url: str = Field(
        default="",
        description=(
            "Optional custom base URL for the tagging-assist LM. Empty falls back to generalist_agent_base_url."
        ),
    )
    # The backend sends POST {base_url}/embeddings with {model, input}; no model
    # weights are bundled in this repo.
    embeddings_base_url: str = Field(
        default="",
        description="OpenAI-compatible embedding API base URL, e.g. https://llm.example.com/v1",
    )
    embeddings_model: str = Field(
        default="jina-embeddings-v4",
        description=(
            "Embedding model id exposed by embeddings_base_url. "
            "The model must return at least embeddings_dim values. "
            "Jina v4 is multilingual (89 languages incl. Hebrew) and supports "
            "asymmetric retrieval LoRA adapters via the request ``task`` field — "
            "the gateway passes ``retrieval.query`` for searches and "
            "``retrieval.passage`` for indexed summaries so Hebrew↔English and "
            "same-language pairs all score correctly."
        ),
    )
    embeddings_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Optional bearer token for the embedding API. Falls back to OPENAI_API_KEY "
            "when unset so a shared internal gateway secret can be reused."
        ),
    )
    embeddings_dim: int = Field(
        default=512,
        ge=64,
        le=2048,
        description=(
            "Head-truncated dimension stored in job_embeddings.embedding_summary. "
            "Must match the schema; changing requires a migration."
        ),
    )
    embeddings_summary_model: str = Field(
        default="",
        description=(
            "LiteLLM model id used to summarise a finished job before embedding. "
            "Falls back to code_agent_model when empty."
        ),
    )
    embedding_index_sweep_interval_seconds: float = Field(
        default=60.0,
        ge=5.0,
        le=3600.0,
        description=("Seconds between bounded repair passes for missing or stale Explore embeddings."),
        alias="EMBEDDING_INDEX_SWEEP_INTERVAL",
    )
    embedding_index_sweep_batch_size: int = Field(
        default=25,
        ge=1,
        le=500,
        description=("Maximum number of successful jobs re-indexed during one Explore embedding repair pass."),
        alias="EMBEDDING_INDEX_SWEEP_BATCH_SIZE",
    )
    search_backend: Literal["lexical", "bm25", "semantic"] = Field(
        default="lexical",
        alias="SEARCH_BACKEND",
        description=(
            "Explore search engine — the single knob that also decides which "
            "Postgres extension (if any) the deployment requires:\n"
            "  lexical  (default): vanilla Postgres, no extension. ILIKE substring "
            "search. The safe choice for a database that can't add extensions — "
            "neither the migrate Job nor the app runs CREATE EXTENSION.\n"
            "  bm25: requires the pg_search extension. Ranks lexical search with "
            "BM25 relevance; degrades to ILIKE when pg_search is absent.\n"
            "  semantic: requires the pgvector extension. Embedding (vector) "
            "search; also enables the embedding pipeline and the job_embeddings "
            "migration schema, so only this profile runs CREATE EXTENSION vector. "
            "Set EMBEDDINGS_BASE_URL/MODEL alongside it.\n"
            "Accepts the synonyms vanilla/ilike->lexical, pg_search/paradedb->bm25, "
            "embeddings/vector/pgvector->semantic."
        ),
    )
    # Derived from search_backend by _resolve_search_backend below — SEARCH_BACKEND
    # is the only switch operators set. Kept as plain fields (not properties) so the
    # test suite can patch them per-case; any EMBEDDINGS_ENABLED / SEARCH_BM25_ENABLED
    # left in the environment is overridden by the value derived from SEARCH_BACKEND.
    embeddings_enabled: bool = Field(default=False)
    search_bm25_enabled: bool = Field(default=False)

    @field_validator("search_backend", mode="before")
    @classmethod
    def _normalize_search_backend(cls, value: object) -> object:
        """Trim, lower-case and map synonyms for SEARCH_BACKEND before validation.

        Lets operators write 'vanilla' / 'pgvector' / ' BM25 ' and still land on
        one of the canonical lexical/bm25/semantic values the Literal accepts.

        Args:
            value: Raw SEARCH_BACKEND input (string from env, or anything else).

        Returns:
            The canonical backend name when a string synonym matches, otherwise
            the value unchanged for the Literal validator to accept or reject.
        """
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        synonyms = {
            "": "lexical",
            "vanilla": "lexical",
            "ilike": "lexical",
            "none": "lexical",
            "off": "lexical",
            "pg_search": "bm25",
            "pgsearch": "bm25",
            "paradedb": "bm25",
            "embeddings": "semantic",
            "embedding": "semantic",
            "vector": "semantic",
            "pgvector": "semantic",
        }
        return synonyms.get(normalized, normalized)

    @model_validator(mode="after")
    def _resolve_search_backend(self) -> Settings:
        """Derive the embedding / BM25 flags from the single SEARCH_BACKEND knob.

        SEARCH_BACKEND is authoritative so the three profiles stay mutually
        exclusive: ``embeddings_enabled`` (pgvector + embedding pipeline + the
        job_embeddings migration schema) is on only for 'semantic', and
        ``search_bm25_enabled`` (pg_search ranking) only for 'bm25'. 'lexical'
        leaves both off, so neither the migrate Job nor the running app issues a
        CREATE EXTENSION against a vanilla Postgres.

        Returns:
            This settings instance with the derived flags applied.
        """
        self.embeddings_enabled = self.search_backend == "semantic"
        self.search_bm25_enabled = self.search_backend == "bm25"
        return self

    max_jobs_per_user: int = Field(default=100, ge=1, description="Default per-user job cap")
    pro_max_jobs_per_user: int = Field(
        default=1000,
        ge=1,
        description="Saved-job cap for an account on the Skynet Pro plan (admin overrides still win)",
    )
    max_total_users: int = Field(
        default=0,
        ge=0,
        description=(
            "Hard cap on total registered accounts; sign-ups are refused at or above it "
            "to bound platform cost. 0 disables the cap."
        ),
    )
    max_monthly_active_users: int = Field(
        default=0,
        ge=0,
        description=(
            "Hard cap on distinct non-admin identities admitted during each UTC calendar "
            "month. Users already admitted that month continue normally; new identities are "
            "refused once the cap is reached. 0 disables the cap."
        ),
    )
    max_concurrent_jobs_per_user: int = Field(
        default=5,
        ge=0,
        description=(
            "Cap on a single user's concurrently active runs (pending/validating/running/"
            "paused); further submissions are refused until one finishes. 0 disables the cap."
        ),
    )
    pro_max_concurrent_jobs_per_user: int = Field(
        default=20,
        ge=0,
        description="Concurrent active-run cap for an account on the Skynet Pro plan. 0 disables the cap.",
    )
    pro_past_due_grace_days: int = Field(
        default=7,
        ge=0,
        description=(
            "Days a Pro subscription keeps its limits after Stripe marks it past_due (a failed renewal). "
            "Past the grace the account falls back to the free limits until payment succeeds."
        ),
    )
    global_daily_spend_ceiling_cents: int = Field(
        validation_alias=AliasChoices("GLOBAL_DAILY_SPEND_CEILING_CENTS", "GLOBAL_DAILY_SPEND_CEILING_CREDITS"),
        default=5000,
        ge=0,
        description=(
            "Platform-wide spend backstop over a trailing 24h ($50 at the default); new "
            "submissions are refused with a 503 and an ERROR alert once reached. 0 disables the "
            "kill-switch (per-user balance gate still applies)."
        ),
    )
    wizard_agent_daily_turns: int = Field(
        default=50,
        ge=0,
        description=(
            "Turns of the black-box wizard agent (its interview and chat) one account may start per "
            "UTC day. The wizard agent is free, so this cap replaces the credit charge. 0 disables "
            "the cap."
        ),
    )
    interactive_min_balance_cents: int = Field(
        validation_alias=AliasChoices("INTERACTIVE_MIN_BALANCE_CENTS", "INTERACTIVE_MIN_BALANCE_CREDITS"),
        default=5,
        ge=0,
        description=(
            "Minimum spendable balance, in cents, an account must hold before an interactive LLM turn "
            "(agent chat, interview, tagging) starts. A turn is billed after it finishes, so a "
            "1-cent balance would otherwise buy an arbitrarily expensive turn. Values below 1 "
            "still require a positive balance."
        ),
    )
    submissions_paused: bool = Field(
        default=False,
        description=(
            "Operator kill-switch: when true, all new run/grid-search submissions are refused "
            "(in-flight runs continue). An instant emergency brake independent of spend."
        ),
    )
    redis_url: str | None = Field(
        default=None,
        description=(
            "Redis connection URL backing the cross-replica rate limiter and login lockout. "
            "When unset the limiters fail open (allow) and the login lockout falls back to "
            "per-process in-memory state — correct for a single backend instance."
        ),
    )
    rate_limit_submissions_per_minute: int = Field(
        default=30,
        ge=0,
        description=(
            "Per-account cap on run/grid-search submissions per rolling minute, enforced across "
            "replicas via Redis. Bounds a runaway script or abusive key. 0 disables the cap."
        ),
    )
    rate_limit_transcriptions_per_hour: int = Field(
        default=120,
        ge=0,
        description=(
            "Per-account cap on dictation clips per hour, enforced across replicas via Redis. "
            "Dictation runs on the platform's transcription key and is not billed to user balances. 0 disables the cap."
        ),
    )
    rate_limit_attachments_per_hour: int = Field(
        default=300,
        ge=0,
        description=(
            "Per-account cap on files read for agent chats per hour, enforced across replicas via Redis. "
            "Bounds the parsing work one account can cause. 0 disables the cap."
        ),
    )
    transcription_monthly_budget_usd: float = Field(
        default=10.0,
        ge=0,
        description=(
            "Platform-wide monthly cap on dictation spend in dollars, counted in Redis from the "
            "cost OpenRouter reports for each clip. Dictation answers a typed 503 once it is reached. 0 disables the cap."
        ),
    )
    docs_agent_monthly_budget_usd: float = Field(
        default=20.0,
        ge=0,
        description=(
            "Platform-wide monthly cap on docs agent model spend in dollars, counted in Redis from the "
            "priced token usage of each answer. The docs agent answers a typed 503 once it is reached. "
            "0 disables the cap."
        ),
    )
    rate_limit_docs_agent_anonymous_per_hour: int = Field(
        default=20,
        ge=0,
        description=(
            "Per-IP cap on docs agent questions per hour for signed-out callers, enforced across replicas "
            "via Redis. 0 disables the cap."
        ),
    )
    rate_limit_docs_agent_user_per_hour: int = Field(
        default=60,
        ge=0,
        description=(
            "Per-account cap on docs agent questions per hour for signed-in callers, enforced across "
            "replicas via Redis. 0 disables the cap."
        ),
    )
    docs_agent_content_dir: str = Field(
        default="",
        description=(
            "Folder of docs Markdown pages (one sub-folder per locale) the docs agent searches. Empty uses "
            "the repository's frontend docs folder, and without it the web app's /llms-full.txt."
        ),
    )
    embeddings_monthly_token_cap: int = Field(
        default=50_000_000,
        ge=0,
        description=(
            "Platform-wide monthly cap on embedding API tokens, counted in Redis. Once reached, new "
            "embeddings are skipped and search falls back as if the embedder were down. 0 disables the cap."
        ),
    )
    embeddings_user_monthly_token_cap: int = Field(
        default=2_000_000,
        ge=0,
        description=(
            "Per-account monthly cap on embedding API tokens (job summaries, conversation indexing, search "
            "queries), counted in Redis. Anonymous explore searches share one bucket. Past it that account's "
            "embeddings are skipped exactly like the platform cap. 0 disables the cap."
        ),
    )
    platform_budget_fallback_fraction: float = Field(
        default=0.1,
        ge=0,
        le=1,
        description=(
            "Share of each platform budget (dictation, embeddings, per-account embeddings) a single "
            "process may spend per month when Redis is unset or unreachable, counted in memory. 0 refuses "
            "every budgeted call without Redis."
        ),
    )
    rate_limit_account_requests_per_hour: int = Field(
        default=20,
        ge=0,
        description=(
            "Per-email cap on account-action requests (register, password-reset, email-verify) "
            "per rolling hour, enforced across replicas via Redis. 0 disables the cap."
        ),
    )
    backend_auth_secret: SecretStr | None = Field(
        default=None,
        description="Shared HS256 secret used by the frontend to sign backend API tokens",
    )
    admin_usernames: str = Field(
        default="",
        description="Break-glass comma-separated usernames that grant backend admin access",
    )
    admin_groups: str = Field(
        default="",
        description="Comma-separated IdP groups that grant backend admin access",
    )
    quota_overrides_json: str = Field(
        default="{}",
        description='Per-user quota overrides as JSON, e.g. \'{"power_user": 500, "researcher": null}\'',
        alias="QUOTA_OVERRIDES",
    )

    @field_validator("quota_overrides_json")
    @classmethod
    def _validate_quota_overrides_json(cls, v: str) -> str:
        """Validate that QUOTA_OVERRIDES is a JSON object of {username: int|null}.

        Args:
            v: Raw env value (a JSON string).

        Returns:
            The validated JSON string with lowercase keys, normalised to ``"{}"``
            when blank. Lower-casing here keeps the wire-level representation
            stable across reads of ``quota_overrides_json`` and makes lookups
            in ``get_user_quota`` cheap.

        Raises:
            ValueError: When the JSON is malformed, not an object, or contains
                values that are not ``int`` or ``null``.
        """
        if not v.strip():
            return "{}"
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"QUOTA_OVERRIDES is not valid JSON: {exc}") from exc
        # Pydantic field_validator surfaces only ValueError as a ValidationError,
        # so keep the type-check failures as ValueError despite TRY004 preferring
        # TypeError for type errors.
        if not isinstance(parsed, dict):
            raise ValueError(  # noqa: TRY004
                "QUOTA_OVERRIDES must be a JSON object mapping usernames to int|null"
            )
        normalised: dict[str, int | None] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                raise ValueError(  # noqa: TRY004
                    f"QUOTA_OVERRIDES keys must be strings, got {type(key).__name__}"
                )
            # bool is an int subclass, so reject it explicitly to avoid silently
            # treating ``true``/``false`` as quota 1/0.
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"QUOTA_OVERRIDES['{key}'] must be int or null, got {type(value).__name__}")
            normalised[key.strip().lower()] = value
        return json.dumps(normalised)

    @model_validator(mode="after")
    def _derive_generalist_mcp_url(self) -> Settings:
        """Fill an unset generalist MCP URL from the server's own HOST/PORT.

        Hardcoding ``http://localhost:8000/mcp/`` strands the agent on a dead
        port whenever the API runs on a non-default PORT, surfacing a raw network
        error instead of a config diagnostic. Deriving the default keeps the
        agent pointed at this app's own ``/mcp`` mount; a bind-all ``0.0.0.0``
        host is dialled as ``127.0.0.1`` — the IPv4 literal, not ``localhost``,
        because some container runtimes resolve ``localhost`` to
        ``::1`` while Uvicorn listens on IPv4 only, turning the self-dial into
        a raw ``ConnectError``. Local mode binds ``api_host`` instead, so the
        agent dials that loopback host.

        Returns:
            The settings instance with ``generalist_agent_mcp_url`` populated.
        """
        if not self.generalist_agent_mcp_url:
            host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
            if self.local_mode:
                host = self.api_host
            self.generalist_agent_mcp_url = f"http://{host}:{self.port}/mcp/"
        return self

    @model_validator(mode="after")
    def _guard_local_mode(self) -> Settings:
        """Refuse local mode on any hosted or network-reachable deployment.

        Local mode grants a large credit, skips budget checks and signs in a
        single user without a password. On a hosted deployment that would give
        every caller free spend, so the app must not start.

        Returns:
            The settings instance, unchanged.

        Raises:
            ValueError: When local mode is on and the process runs in a deployed
                environment (staging, production or a PR preview), or binds a
                non-loopback API_HOST.
        """
        if not self.local_mode:
            return self
        # Every deployed service sets APP_ENVIRONMENT; a PR preview is one too.
        if self.app_environment:
            raise ValueError("APP_LOCAL_MODE is not allowed in a deployed environment")
        if self.api_host not in LOCAL_MODE_LOOPBACK_HOSTS:
            raise ValueError(f"APP_LOCAL_MODE requires a loopback API_HOST, got {self.api_host!r}")
        return self

    @model_validator(mode="after")
    def _resolve_hosted_link(self) -> Settings:
        """Fill the hosted link URLs from the build target and check them in local mode.

        The Mac sends its desktop tokens to these URLs, so local mode accepts
        only a plain ``https`` origin. The dev override opens ``http`` for a
        loopback hosted stack only.

        Returns:
            The settings instance with the hosted target and URLs set.

        Raises:
            ValueError: When the target or the stamp names an unknown target,
                or when local mode gets an unsafe hosted URL.
        """
        target = self.hosted_target.strip().lower()
        if not target and HOSTED_TARGET_STAMP.is_file():
            target = HOSTED_TARGET_STAMP.read_text(encoding="utf-8").strip().lower()
        target = target or "production"
        if target not in HOSTED_TARGET_URLS:
            raise ValueError(f"unknown hosted target {target!r}; expected production or staging")
        self.hosted_target = target
        app_default, api_default = HOSTED_TARGET_URLS[target]
        self.hosted_app_url = (self.hosted_app_url.strip() or app_default).rstrip("/")
        self.hosted_api_url = (self.hosted_api_url.strip() or api_default).rstrip("/")
        if self.local_mode:
            for name, url in (("APP_HOSTED_APP_URL", self.hosted_app_url), ("APP_HOSTED_API_URL", self.hosted_api_url)):
                if url and not _is_safe_hosted_origin(url, allow_insecure=self.hosted_allow_insecure_url):
                    raise ValueError(f"{name} must be a plain https origin, got {url!r}")
        return self

    @model_validator(mode="after")
    def _local_vault_key(self) -> Settings:
        """Give the Mac app a BYOK vault key from its state directory.

        Without a vault key the user cannot save a provider key, and the Mac
        app has no operator to set ``BYOK_VAULT_KEY``. An explicit key wins.

        Returns:
            The settings instance, with ``byok_vault_key`` set in local mode
            when a state directory is configured.
        """
        if self.local_mode and self.byok_vault_key is None and self.local_state_dir:
            self.byok_vault_key = SecretStr(load_or_create_vault_key(self.local_state_dir))
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        """Return ``cors_origins`` parsed into a list of trimmed, non-empty origins."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def admin_usernames_set(self) -> frozenset[str]:
        """Return break-glass admin usernames as a lowercase frozenset."""
        return _csv_lower_set(self.admin_usernames)

    @property
    def admin_groups_set(self) -> frozenset[str]:
        """Return admin IdP groups as a lowercase frozenset."""
        return _csv_lower_set(self.admin_groups)

    @property
    def is_preview_environment(self) -> bool:
        """Return whether this deployment is a PR preview environment."""
        return re.search(r"-pr-\d+$", self.app_environment) is not None

    @property
    def is_stripe_configured(self) -> bool:
        """Return whether a Stripe secret key is present (billing mutations enabled)."""
        return self.stripe_secret_key is not None

    @property
    def is_byok_vault_configured(self) -> bool:
        """Return whether a BYOK vault key is present (saving provider keys enabled)."""
        return self.byok_vault_key is not None

    @property
    def stripe_pack_price_ids(self) -> dict[str, str]:
        """Return the one-time top-up pack Stripe price ids keyed by pack id."""
        return {
            "starter": self.stripe_price_pack_starter,
            "plus": self.stripe_price_pack_plus,
            "pro": self.stripe_price_pack_pro,
        }

    @cached_property
    def quota_overrides(self) -> dict[str, int | None]:
        """Return parsed quota overrides keyed by lowercase username.

        Cached because every job-submission goes through ``get_user_quota`` and
        re-parsing JSON on each call is wasteful; the validator already
        normalises the JSON to lowercase keys.
        """
        return json.loads(self.quota_overrides_json)

    @cached_property
    def code_version(self) -> str:
        """Return the build version used for job-claim compatibility checks.

        Resolution order: ``APP_CODE_VERSION`` env (or the legacy
        ``SKYNET_CODE_VERSION``), then a local git lookup, then the literal
        ``"unknown"``. With ``REQUIRE_CODE_VERSION=true``,
        falling through to ``"unknown"`` raises — production builds must bake
        the SHA into the image so staged rollouts don't have multiple pods
        claiming each other's ``"unknown"``-tagged work.

        Raises:
            RuntimeError: When the fallback resolves to ``"unknown"`` and
                ``require_code_version`` is enabled.
        """
        configured = self.app_code_version.strip()
        if configured:
            return configured[:40]
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=_REPO_ROOT,
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
            resolved = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            resolved = ""
        if resolved:
            return resolved[:40]
        if self.require_code_version:
            raise RuntimeError(
                "APP_CODE_VERSION is unset and no git checkout is available — "
                "set REQUIRE_CODE_VERSION=false for local dev, or bake the SHA "
                "into the image (Dockerfile passes GIT_SHA build arg).",
            )
        return "unknown"

    def get_user_quota(self, username: str) -> int | None:
        """Return the effective job quota for a user.

        Users with a ``null`` override receive ``None`` (unlimited). Per-user
        overrides in ``quota_overrides_json`` take precedence over
        ``max_jobs_per_user``. Lookup is case-insensitive to match how override
        keys are stored.

        Args:
            username: The username to look up.

        Returns:
            Maximum number of allowed jobs, or ``None`` for unlimited access.
        """
        normalised = (username or "").strip().lower()
        if normalised in self.quota_overrides:
            return self.quota_overrides[normalised]
        return self.max_jobs_per_user


settings = Settings()


def local_bind_host(config: Settings | None = None) -> str:
    """Return the address the Docker image binds in local mode.

    The image command calls this instead of reading shell variables, so it
    sees local mode exactly as ``Settings`` does.

    Args:
        config: Settings to read; the process settings when omitted.

    Returns:
        The checked loopback ``API_HOST`` in local mode, or ``""`` when local
        mode is off and the hosted bind applies.
    """
    config = config or settings
    return config.api_host if config.local_mode else ""


def embeddings_schema_enabled() -> bool:
    """Report whether migrations should manage the pgvector embedding schema.

    True only for the semantic search backend. The lexical and bm25 backends run
    on a vanilla Postgres with no pgvector extension, so the baseline and every
    downstream embedding migration skip the ``job_embeddings`` table, its Vector
    columns and the HNSW indexes — which is what keeps the migrate Job from
    issuing ``CREATE EXTENSION vector`` on a database that doesn't have it. The
    migrate Job inherits SEARCH_BACKEND from the backend ConfigMap, so this reads
    the same value the application pods do.

    Returns:
        True when SEARCH_BACKEND selects semantic search, else False.
    """
    return settings.embeddings_enabled

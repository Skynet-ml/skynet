"""Lookup table from provider name to connector module.

Every module here exposes ``PROVIDER``, ``verify_credentials``, ``browse``,
``preview`` and ``import_ref``; the OAuth-capable ones (Google Sheets,
Google Drive, OneDrive, GitHub, GitLab, Bitbucket, Azure Blob, Notion,
Supabase) additionally expose ``oauth_app``, ``oauth_available`` and
``fetch_account_label``, and Azure Blob also ``oauth_account`` for the storage
account named before sign-in. Hugging Face keeps its own module and routes.
"""

from __future__ import annotations

from types import ModuleType

from ..api.errors import DomainError
from ..config import settings
from . import (
    azure_blob,
    bigquery,
    bitbucket,
    braintrust,
    foundry,
    gcs,
    github,
    gitlab,
    google_drive,
    google_sheets,
    kaggle,
    langfuse,
    langsmith,
    mysql,
    notion,
    onedrive,
    postgres,
    s3,
    snowflake,
    supabase,
)

PROVIDERS: dict[str, ModuleType] = {
    kaggle.PROVIDER: kaggle,
    google_sheets.PROVIDER: google_sheets,
    google_drive.PROVIDER: google_drive,
    onedrive.PROVIDER: onedrive,
    github.PROVIDER: github,
    gitlab.PROVIDER: gitlab,
    bitbucket.PROVIDER: bitbucket,
    s3.PROVIDER: s3,
    gcs.PROVIDER: gcs,
    azure_blob.PROVIDER: azure_blob,
    postgres.PROVIDER: postgres,
    mysql.PROVIDER: mysql,
    supabase.PROVIDER: supabase,
    bigquery.PROVIDER: bigquery,
    snowflake.PROVIDER: snowflake,
    foundry.PROVIDER: foundry,
    langfuse.PROVIDER: langfuse,
    langsmith.PROVIDER: langsmith,
    braintrust.PROVIDER: braintrust,
    notion.PROVIDER: notion,
}
OAUTH_PROVIDERS = frozenset(
    {
        google_sheets.PROVIDER,
        google_drive.PROVIDER,
        onedrive.PROVIDER,
        github.PROVIDER,
        gitlab.PROVIDER,
        bitbucket.PROVIDER,
        azure_blob.PROVIDER,
        notion.PROVIDER,
        supabase.PROVIDER,
    }
)
# OAuth links to these reach only the files picked in the Google Picker (drive.file).
PICKER_PROVIDERS = frozenset({google_sheets.PROVIDER, google_drive.PROVIDER})


def get_provider(name: str) -> ModuleType:
    """Resolve a provider module by name.

    Args:
        name: The provider slug from the URL.

    Returns:
        The module.

    Raises:
        DomainError: 404 for an unknown slug.
    """
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise DomainError("connectors.unknown_provider", status=404, provider=name) from exc


def oauth_available(name: str) -> bool:
    """Report whether a provider offers an OAuth flow right now.

    Args:
        name: Provider slug.

    Returns:
        ``True`` only for configured OAuth providers.
    """
    return name in OAUTH_PROVIDERS and PROVIDERS[name].oauth_available()


def picker_available(name: str) -> bool:
    """Report whether the Google Picker can be offered for a provider.

    Args:
        name: Provider slug.

    Returns:
        ``True`` when the provider picks files and both the Google OAuth client
        and the Picker API key are configured.
    """
    return name in PICKER_PROVIDERS and oauth_available(name) and settings.google_picker_api_key is not None


def oauth_config_problems() -> list[str]:
    """List OAuth providers whose settings are only half filled in.

    A client id without its secret still shows the sign-in button, then fails at
    the token exchange, so the operator needs to hear about it at startup.

    Returns:
        One human-readable line per problem; empty when every provider is either
        fully configured or fully unset.
    """
    problems: list[str] = []
    for name in sorted(OAUTH_PROVIDERS):
        app = PROVIDERS[name].oauth_app()
        if app.client_id and not app.client_secret:
            problems.append(f"{name}: client id is set but the client secret is missing")
        elif app.client_secret and not app.client_id:
            problems.append(f"{name}: client secret is set but the client id is missing")
        elif app.client_id and settings.byok_vault_key is None:
            problems.append(f"{name}: client is set but BYOK_VAULT_KEY is not, so sign-in stays hidden")
    if settings.google_oauth_client_id and settings.google_picker_api_key is None:
        problems.append("google_drive, google_sheets: GOOGLE_PICKER_API_KEY is unset, so OAuth users cannot pick files")
    return problems

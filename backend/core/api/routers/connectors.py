"""Connector endpoints: link outside data accounts and import from them.

The Hugging Face connector lets a user browse and import Hub datasets with
their own account. Linking works two ways: the OAuth flow when the deployment
has registered an OAuth app, or a pasted access token as the fallback. Either
way the credential lands encrypted in :class:`core.connectors.vault.ConnectorVault`
and only ever leaves the server inside requests to Hugging Face.

The other providers share one generic set of routes keyed by provider slug:
save credentials, browse, preview, import, plus OAuth start/callback for the
ones in :data:`core.connectors.registry.OAUTH_PROVIDERS`.

Imports reuse the library's gated save, so the per-file cap, dedupe and the
storage quota apply exactly as they do to an upload.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ...config import settings
from ...connectors import github, registry
from ...connectors import huggingface as hf
from ...connectors import oauth as generic_oauth
from ...connectors.pruning import browse_importable
from ...connectors.transport import label
from ...connectors.vault import ConnectorSecret, ConnectorVault
from ...storage.dataset_library import DatasetLibraryStore, PostgresDatasetBlobStore
from ..auth import AuthenticatedUser, get_authenticated_user
from ..dataset_access import ShareRole
from ..errors import DomainError
from .dataset_library import SaveDatasetResponse, _summary, save_rows_gated

SEARCH_LIMIT_MAX = 100
GITHUB_FILE_MAX_BYTES = 1024 * 1024

# Some providers resolve a folder's web link with its own API call (OneDrive),
# so it runs beside the listing instead of after it.
_web_links = ThreadPoolExecutor(max_workers=8, thread_name_prefix="connector-web-url")


class ConnectorStatus(BaseModel):
    """One provider's link state for the caller, never carrying the secret."""

    provider: str
    connected: bool
    status: str | None = None
    account_label: str | None = None
    auth_method: str | None = None
    oauth_available: bool
    picker_available: bool = Field(
        default=False, description="Whether the Google Picker can choose the files this OAuth link may read."
    )
    connected_at: str | None = None


class ConnectorListResponse(BaseModel):
    """Envelope for ``GET /connectors``."""

    connectors: list[ConnectorStatus]


class SaveTokenRequest(BaseModel):
    """Body for the pasted-token fallback."""

    token: str = Field(min_length=1, max_length=512)


class PickerResponse(BaseModel):
    """What the browser needs to open the Google Picker as the caller."""

    access_token: str
    developer_key: str
    app_id: str


class OAuthStartResponse(BaseModel):
    """Where to send the browser to start the OAuth flow."""

    authorize_url: str


class HubDataset(BaseModel):
    """One Hub search hit."""

    id: str
    author: str | None = None
    downloads: int = 0
    likes: int = 0
    private: bool = False
    gated: bool = False
    last_modified: str | None = None


class HubSearchResponse(BaseModel):
    """Envelope for the Hub dataset search."""

    datasets: list[HubDataset]


class HubSplit(BaseModel):
    """One ``(config, split)`` of a Hub dataset with its known size."""

    config: str
    split: str
    num_rows: int | None = None
    num_bytes: int | None = None


class HubSplitsResponse(BaseModel):
    """Envelope for the split listing."""

    splits: list[HubSplit]


class HubColumn(BaseModel):
    """A column of a split as the dataset viewer describes it."""

    name: str
    type: str


class HubPreviewResponse(BaseModel):
    """The first rows of a split."""

    columns: list[HubColumn]
    rows: list[dict[str, Any]]
    num_rows_total: int | None = None


class ImportRequest(BaseModel):
    """Body for importing one split into the caller's library."""

    repo_id: str = Field(min_length=1, max_length=255)
    config: str = Field(min_length=1, max_length=255)
    split: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=255)


class CredentialsRequest(BaseModel):
    """Body for linking a keys-based provider (or a pasted token)."""

    fields: dict[str, str] = Field(default_factory=dict)


class BrowseEntry(BaseModel):
    """One folder or file in a provider's listing."""

    ref: str
    name: str
    kind: str
    size: int | None = None
    modified: str | None = None


class BrowseResponse(BaseModel):
    """Envelope for ``GET /connectors/{provider}/browse``."""

    entries: list[BrowseEntry]
    location_url: str | None = Field(
        default=None, description="Where this location opens on the provider's own site, when it has one."
    )


class RefImportRequest(BaseModel):
    """Body for importing one browsed file into the caller's library."""

    ref: str = Field(min_length=1, max_length=2048)
    name: str | None = Field(default=None, max_length=255)


class GithubRepository(BaseModel):
    """One repository the GitHub picker offers."""

    full_name: str
    private: bool = False
    description: str | None = None
    language: str | None = None
    default_branch: str | None = None
    pushed_at: str | None = None


class GithubRepositoriesResponse(BaseModel):
    """Envelope for ``GET /connectors/github/repos``."""

    repositories: list[GithubRepository]


class GithubBranchesResponse(BaseModel):
    """A repository's branch names and its default branch."""

    default_branch: str | None = None
    branches: list[str]


class GithubTreeEntry(BaseModel):
    """One file or folder of a repository tree."""

    path: str
    type: Literal["file", "dir"]


class GithubTreeResponse(BaseModel):
    """Every file and folder of a repository at one branch."""

    entries: list[GithubTreeEntry]
    truncated: bool = Field(default=False, description="GitHub listed only part of a very large tree.")


class GithubFileResponse(BaseModel):
    """One repository file's text, or why it has none."""

    path: str
    text: str | None = Field(
        default=None, description="The file as UTF-8 text; empty for a binary, oversized or missing file."
    )
    binary: bool = Field(default=False, description="The content holds a NUL byte or is not valid UTF-8.")
    too_large: bool = Field(default=False, description="The file is over the 1 MiB read cap.")
    missing: bool = Field(default=False, description="No regular file exists at this path and ref.")


def connector_secret(vault: ConnectorVault, username: str, provider: str) -> ConnectorSecret:
    """Load an account's credential for a generic provider, refreshing OAuth tokens.

    Args:
        vault: Encrypted connector credential store.
        username: The account owner.
        provider: Provider slug.

    Returns:
        The decrypted credential.

    Raises:
        DomainError: 404 for an unknown provider; 409 when unlinked or
            the OAuth grant expired.
    """
    module = registry.get_provider(provider)
    secret = vault.resolve(username, provider)
    if secret is None:
        raise DomainError("connectors.not_connected", status=409, provider=label(provider))
    if secret.auth_method == "oauth":
        token = generic_oauth.current_oauth_token(module.oauth_app(), vault, username)
        secret = ConnectorSecret(
            access_token=token,
            refresh_token=secret.refresh_token,
            expires_at=secret.expires_at,
            auth_method=secret.auth_method,
            account_label=secret.account_label,
        )
    return secret


def create_connectors_router(*, job_store) -> APIRouter:
    """Build the connectors router.

    Args:
        job_store: Storage backend whose ``engine`` carries ``user_connectors``
            and the dataset library tables.

    Returns:
        A configured :class:`APIRouter` under ``/connectors``.
    """
    vault = ConnectorVault(job_store.engine)
    library = DatasetLibraryStore(job_store.engine, PostgresDatasetBlobStore(job_store.engine))
    router = APIRouter()

    def _status(username: str) -> ConnectorListResponse:
        """Describe every provider's link state for ``username``.

        Args:
            username: The caller.

        Returns:
            The list envelope; one entry per supported provider.
        """
        entries: list[ConnectorStatus] = []
        for provider, available in [(hf.PROVIDER, hf.oauth_available())] + [
            (name, registry.oauth_available(name)) for name in registry.PROVIDERS
        ]:
            view = vault.get(username, provider)
            entries.append(
                ConnectorStatus(
                    provider=provider,
                    connected=view is not None,
                    status=view.status if view else None,
                    account_label=view.account_label if view else None,
                    auth_method=view.auth_method if view else None,
                    oauth_available=available,
                    picker_available=registry.picker_available(provider),
                    connected_at=view.connected_at.isoformat() if view else None,
                )
            )
        return ConnectorListResponse(connectors=entries)

    def _redirect_uri(request: Request) -> str:
        """Resolve the OAuth callback URL for this deployment.

        Args:
            request: The incoming request, used when no explicit URI is set.

        Returns:
            The absolute callback URL registered on the OAuth app.
        """
        return settings.hf_oauth_redirect_uri or str(request.url_for("huggingface_oauth_callback"))

    def _settings_redirect(error: str | None = None) -> RedirectResponse:
        """Send the browser back to the Connectors tab, optionally with an error.

        Args:
            error: Domain error code to surface, if the link failed.

        Returns:
            A 303 redirect into the frontend.
        """
        params = {"settings": "connectors"}
        if error:
            params["connector_error"] = error
        return RedirectResponse(f"{settings.app_public_url.rstrip('/')}/?{urlencode(params)}", status_code=303)

    @router.get("/connectors", response_model=ConnectorListResponse, summary="List the caller's linked accounts")
    def list_connectors(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> ConnectorListResponse:
        """Return every supported provider with the caller's link state.

        Args:
            user: Authenticated caller.

        Returns:
            The connector list.
        """
        return _status(user.username)

    @router.put(
        "/connectors/huggingface/token",
        response_model=ConnectorListResponse,
        summary="Link Hugging Face with a pasted access token",
    )
    def save_huggingface_token(
        body: SaveTokenRequest,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> ConnectorListResponse:
        """Verify a personal access token against Hugging Face and store it.

        Args:
            body: The token.
            user: Authenticated caller.

        Returns:
            The updated connector list.

        Raises:
            DomainError: 400 when Hugging Face rejects the token; 503 when the
                vault is not configured.
        """
        token = body.token.strip()
        account = hf.verify_token(token)
        vault.save(user.username, hf.PROVIDER, access_token=token, auth_method="token", account_label=account)
        return _status(user.username)

    @router.delete(
        "/connectors/huggingface",
        response_model=ConnectorListResponse,
        summary="Unlink Hugging Face",
    )
    def remove_huggingface(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> ConnectorListResponse:
        """Forget the caller's Hugging Face credentials; a no-op when unlinked.

        Args:
            user: Authenticated caller.

        Returns:
            The updated connector list.
        """
        vault.remove(user.username, hf.PROVIDER)
        return _status(user.username)

    @router.post(
        "/connectors/huggingface/oauth/start",
        response_model=OAuthStartResponse,
        summary="Begin the Hugging Face OAuth flow",
    )
    def start_huggingface_oauth(
        request: Request,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> OAuthStartResponse:
        """Mint the authorization URL the browser should visit.

        Args:
            request: Incoming request, for deriving the callback URL.
            user: Authenticated caller.

        Returns:
            The authorize URL.

        Raises:
            DomainError: 503 when the OAuth app or the vault is not configured.
        """
        return OAuthStartResponse(authorize_url=hf.build_authorize_url(user.username, _redirect_uri(request)))

    @router.get(
        "/connectors/huggingface/oauth/callback",
        name="huggingface_oauth_callback",
        include_in_schema=False,
    )
    def huggingface_oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Finish the OAuth flow and bounce the browser back to Settings.

        This is the one unauthenticated route here: the browser arrives from
        huggingface.co without the app's bearer token, so the encrypted
        ``state`` is what ties the code to the user who started the flow.

        Args:
            request: Incoming request.
            code: Authorization code from Hugging Face.
            state: The encrypted state minted at start.
            error: Hugging Face's error code when the user declined.

        Returns:
            A redirect to the Connectors tab, carrying an error code on failure.
        """
        if error or not code or not state:
            return _settings_redirect("connectors.hf_oauth_failed")
        try:
            payload = hf.parse_state(state)
            tokens = hf.exchange_code(code, payload["v"], payload["r"])
        except DomainError as exc:
            return _settings_redirect(str(exc.code))
        label = hf.fetch_userinfo(tokens.access_token)
        vault.save(
            payload["u"],
            hf.PROVIDER,
            access_token=tokens.access_token,
            auth_method="oauth",
            account_label=label,
            refresh_token=tokens.refresh_token,
            expires_at=tokens.expires_at,
            scopes=tokens.scope,
        )
        return _settings_redirect()

    @router.get(
        "/connectors/huggingface/datasets",
        response_model=HubSearchResponse,
        summary="Search Hugging Face Hub datasets",
    )
    def search_huggingface_datasets(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        search: str = Query(default="", max_length=200),
        limit: int = Query(default=30, ge=1, le=SEARCH_LIMIT_MAX),
    ) -> HubSearchResponse:
        """Search the Hub as the caller (anonymously when unlinked).

        Args:
            user: Authenticated caller.
            search: Free-text query.
            limit: Maximum hits.

        Returns:
            The matching datasets, most downloaded first.
        """
        token = hf.current_token(vault, user.username)
        return HubSearchResponse(datasets=[HubDataset(**d) for d in hf.search_datasets(token, search, limit)])

    @router.get(
        "/connectors/huggingface/datasets/{repo_id:path}/splits",
        response_model=HubSplitsResponse,
        summary="List a Hub dataset's configs and splits",
    )
    def list_huggingface_splits(
        repo_id: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> HubSplitsResponse:
        """List the importable splits of one dataset.

        Args:
            repo_id: Hub repo id, e.g. ``owner/name``.
            user: Authenticated caller.

        Returns:
            The splits with row and byte counts when known.
        """
        token = hf.current_token(vault, user.username)
        return HubSplitsResponse(splits=[HubSplit(**s) for s in hf.list_splits(token, repo_id)])

    @router.get(
        "/connectors/huggingface/datasets/{repo_id:path}/preview",
        response_model=HubPreviewResponse,
        summary="Preview the first rows of a split",
    )
    def preview_huggingface_split(
        repo_id: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        config: str = Query(min_length=1, max_length=255),
        split: str = Query(min_length=1, max_length=255),
    ) -> HubPreviewResponse:
        """Show a handful of rows before importing.

        Args:
            repo_id: Hub repo id.
            user: Authenticated caller.
            config: Dataset config.
            split: Split name.

        Returns:
            Columns, the first rows and the split's total row count.
        """
        token = hf.current_token(vault, user.username)
        return HubPreviewResponse(**hf.preview_rows(token, repo_id, config, split))

    @router.post(
        "/connectors/huggingface/import",
        response_model=SaveDatasetResponse,
        summary="Import a Hub split into the caller's library",
    )
    def import_huggingface_split(
        body: ImportRequest,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> SaveDatasetResponse:
        """Download a split's parquet files and save the rows as a library dataset.

        Args:
            body: Which split to import and what to call it.
            user: Authenticated caller.

        Returns:
            The saved entry and whether an identical one already existed.

        Raises:
            DomainError: 413 when the split exceeds the file cap; 409 over the
                storage quota or when the split cannot be served.
        """
        token = hf.current_token(vault, user.username)
        rows, column_schema = hf.import_split(token, body.repo_id, body.config, body.split)
        name = (body.name or "").strip() or f"{body.repo_id} · {body.split}"
        record, deduped = save_rows_gated(
            job_store,
            library,
            owner=user.username,
            name=name,
            source=hf.PROVIDER,
            rows=rows,
            column_schema=column_schema,
        )
        return SaveDatasetResponse(dataset=_summary(record, ShareRole.owner), deduplicated=deduped)

    def _provider_redirect_uri(request: Request, provider: str) -> str:
        """Resolve a generic provider's OAuth callback URL.

        Args:
            request: The incoming request, used when no explicit URI is set.
            provider: Provider slug.

        Returns:
            The absolute callback URL registered on that provider's OAuth app.
        """
        google = settings.google_oauth_redirect_uri
        microsoft = settings.microsoft_oauth_redirect_uri
        # One Google client serves Sheets and Drive, and one Microsoft app serves
        # OneDrive and Azure Blob; each derived callback must be registered on the shared client too.
        derived = {
            "google_drive": (google, "google_sheets"),
            "azure_blob": (microsoft, "onedrive"),
        }
        if provider in derived:
            base, segment = derived[provider]
            configured = base.replace(segment, provider) if base else None
        else:
            configured = {
                "google_sheets": google,
                "onedrive": microsoft,
                "github": settings.github_oauth_redirect_uri,
                "notion": settings.notion_oauth_redirect_uri,
                "supabase": settings.supabase_oauth_redirect_uri,
            }.get(provider)
        return configured or str(request.url_for("connector_oauth_callback", provider=provider))

    def _secret(username: str, provider: str) -> ConnectorSecret:
        """Load the caller's credential for a generic provider; see :func:`connector_secret`."""
        return connector_secret(vault, username, provider)

    @router.get(
        "/connectors/github/repos",
        response_model=GithubRepositoriesResponse,
        summary="List the caller's GitHub repositories for the repository picker",
        tags=["agent"],
    )
    def list_github_repositories(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        search: str = Query(default="", max_length=200),
    ) -> GithubRepositoriesResponse:
        """List repositories the linked account can see, most recently pushed first.

        Args:
            user: Authenticated caller.
            search: Optional ``owner/name`` filter; a full ``owner/name`` also
                offers that public repository when the account lacks it.

        Returns:
            The repositories with their visibility, description, language,
            default branch and last push.
        """
        repos = github.list_repositories(_secret(user.username, github.PROVIDER), search)
        return GithubRepositoriesResponse(repositories=[GithubRepository(**r) for r in repos])

    @router.get(
        "/connectors/github/branches",
        response_model=GithubBranchesResponse,
        summary="List a GitHub repository's branches",
    )
    def list_github_branches(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        repo: str = Query(min_length=3, max_length=201),
    ) -> GithubBranchesResponse:
        """List a repository's branch names with its default branch.

        Args:
            user: Authenticated caller.
            repo: The repository as ``owner/name``.

        Returns:
            The default branch and up to 300 branch names.
        """
        return GithubBranchesResponse(**github.list_branches(_secret(user.username, github.PROVIDER), repo))

    @router.get(
        "/connectors/github/tree",
        response_model=GithubTreeResponse,
        summary="List every file and folder of a GitHub repository at a branch",
    )
    def github_repository_tree(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        repo: str = Query(min_length=3, max_length=201),
        branch: str = Query(default="", max_length=255),
    ) -> GithubTreeResponse:
        """List a repository's whole tree, for choosing the paths an agent may edit.

        Args:
            user: Authenticated caller.
            repo: The repository as ``owner/name``.
            branch: Branch name or commit id; empty for the default branch.

        Returns:
            Every file and folder path, and whether GitHub truncated the tree.
        """
        return GithubTreeResponse(**github.repository_tree(_secret(user.username, github.PROVIDER), repo, branch))

    @router.get(
        "/connectors/github/file",
        response_model=GithubFileResponse,
        summary="Read one file of a GitHub repository at a branch or commit",
    )
    def github_repository_file(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        repo: str = Query(min_length=3, max_length=201),
        path: str = Query(min_length=1, max_length=github.FILE_PATH_MAX),
        ref: str = Query(default="", max_length=255),
    ) -> GithubFileResponse:
        """Read one repository file for the read-only code view, opened lazily per file.

        Args:
            user: Authenticated caller.
            repo: The repository as ``owner/name``.
            path: File path inside the repository.
            ref: Branch name or commit id; empty for the default branch.

        Returns:
            The file's text, or flags for a binary, oversized or missing file.

        Raises:
            DomainError: 400 ``connectors.invalid_ref`` for a path that is
                absolute, walks out with ``..`` or points into ``.git``.
        """
        secret = _secret(user.username, github.PROVIDER)
        return GithubFileResponse(**github.read_repo_file(secret.access_token, repo, ref, path, GITHUB_FILE_MAX_BYTES))

    @router.put(
        "/connectors/{provider}/credentials",
        response_model=ConnectorListResponse,
        summary="Link a provider with pasted credentials",
    )
    def save_connector_credentials(
        provider: str,
        body: CredentialsRequest,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> ConnectorListResponse:
        """Validate the credentials against the provider and store them encrypted.

        Args:
            provider: Provider slug.
            body: Provider-specific credential fields.
            user: Authenticated caller.

        Returns:
            The updated connector list.

        Raises:
            DomainError: 400 when the provider rejects the credentials; 503
                when the vault key is unset.
        """
        module = registry.get_provider(provider)
        credential = module.verify_credentials(body.fields)
        vault.save(
            user.username,
            provider,
            access_token=credential.secret,
            auth_method=credential.auth_method,
            account_label=credential.account_label,
        )
        return _status(user.username)

    @router.delete(
        "/connectors/{provider}",
        response_model=ConnectorListResponse,
        summary="Unlink a provider",
    )
    def remove_connector(
        provider: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> ConnectorListResponse:
        """Forget the caller's credentials for a provider; a no-op when unlinked.

        Args:
            provider: Provider slug.
            user: Authenticated caller.

        Returns:
            The updated connector list.
        """
        registry.get_provider(provider)
        vault.remove(user.username, provider)
        return _status(user.username)

    @router.post(
        "/connectors/{provider}/oauth/start",
        response_model=OAuthStartResponse,
        summary="Begin a provider's OAuth flow",
    )
    def start_connector_oauth(
        provider: str,
        request: Request,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        body: CredentialsRequest | None = None,
    ) -> OAuthStartResponse:
        """Mint the authorization URL the browser should visit.

        Args:
            provider: Provider slug; one of the OAuth-capable providers.
            request: Incoming request, for deriving the callback URL.
            user: Authenticated caller.
            body: Fields named before sign-in; only Azure Blob uses them
                (the storage account and optional container).

        Returns:
            The authorize URL.

        Raises:
            DomainError: 400 when the pre-sign-in fields are invalid; 404 when
                the provider has no OAuth flow; 503 when its OAuth app or the
                vault is not configured.
        """
        module = registry.get_provider(provider)
        if provider not in registry.OAUTH_PROVIDERS:
            raise DomainError("connectors.unknown_provider", status=404, provider=provider)
        account = module.oauth_account(body.fields if body else {}) if hasattr(module, "oauth_account") else None
        url = generic_oauth.build_authorize_url(
            module.oauth_app(), user.username, _provider_redirect_uri(request, provider), account_label=account
        )
        return OAuthStartResponse(authorize_url=url)

    @router.get(
        "/connectors/{provider}/oauth/callback",
        name="connector_oauth_callback",
        include_in_schema=False,
    )
    def connector_oauth_callback(
        provider: str,
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Finish a provider's OAuth flow and bounce the browser back to Settings.

        Unauthenticated like the Hugging Face callback: the encrypted ``state``
        ties the code to the user who started the flow.

        Args:
            provider: Provider slug.
            request: Incoming request.
            code: Authorization code.
            state: The encrypted state minted at start.
            error: The provider's error code when the user declined.

        Returns:
            A redirect to the Connectors tab, carrying an error code on failure.
        """
        if provider not in registry.OAUTH_PROVIDERS:
            return _settings_redirect("connectors.unknown_provider")
        if error or not code or not state:
            return _settings_redirect("connectors.oauth_failed")
        module = registry.get_provider(provider)
        app = module.oauth_app()
        try:
            payload = generic_oauth.parse_state(app, state)
            tokens = generic_oauth.exchange_code(app, code, payload["v"], payload["r"])
        except DomainError as exc:
            return _settings_redirect(str(exc.code))
        vault.save(
            payload["u"],
            provider,
            access_token=tokens.access_token,
            auth_method="oauth",
            account_label=payload.get("a") or module.fetch_account_label(tokens.access_token),
            refresh_token=tokens.refresh_token,
            expires_at=tokens.expires_at,
            scopes=tokens.scope,
        )
        return _settings_redirect()

    @router.get(
        "/connectors/{provider}/browse",
        response_model=BrowseResponse,
        summary="List folders and importable files at a location",
    )
    def browse_connector(
        provider: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        location: str = Query(default="", max_length=2048),
        search: str = Query(default="", max_length=200),
    ) -> BrowseResponse:
        """Browse one level of the provider as the caller.

        Args:
            provider: Provider slug.
            user: Authenticated caller.
            location: Empty for the root, else a folder ref from a previous listing.
            search: Optional name filter.

        Returns:
            Folders first, then importable files.
        """
        module = registry.get_provider(provider)
        secret = _secret(user.username, provider)
        link = _web_links.submit(module.web_url, secret, location) if hasattr(module, "web_url") else None
        entries = browse_importable(module, secret, location, search)
        location_url = None
        if link is not None:
            try:
                location_url = link.result()
            # The link is a convenience; a provider hiccup must not fail the listing.
            except DomainError:
                location_url = None
        return BrowseResponse(entries=[BrowseEntry(**vars(e)) for e in entries], location_url=location_url)

    @router.get(
        "/connectors/{provider}/picker",
        response_model=PickerResponse,
        summary="Hand the browser a short-lived token for the Google Picker",
    )
    def connector_picker(
        provider: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> PickerResponse:
        """Return the caller's access token with the Picker's key and app id.

        The OAuth link asks only for ``drive.file``, so the token can read
        nothing but the files the caller picks, which is why handing it to the
        browser is safe.

        Args:
            provider: ``google_drive`` or ``google_sheets``.
            user: Authenticated caller.

        Returns:
            The token, the Picker API key and the Cloud project number.

        Raises:
            DomainError: 404 for a provider without a picker; 503 when the
                Picker is not configured; 409 when the link is not OAuth.
        """
        registry.get_provider(provider)
        if provider not in registry.PICKER_PROVIDERS:
            raise DomainError("connectors.unknown_provider", status=404, provider=provider)
        key, client_id = settings.google_picker_api_key, settings.google_oauth_client_id
        if key is None or not client_id:
            raise DomainError("connectors.picker_unavailable", status=503)
        secret = _secret(user.username, provider)
        if secret.auth_method != "oauth":
            raise DomainError("connectors.picker_needs_oauth", status=409, provider=label(provider))
        # A Google client id starts with its Cloud project number, which is the Picker's app id.
        return PickerResponse(
            access_token=secret.access_token,
            developer_key=key.get_secret_value(),
            app_id=client_id.split("-", 1)[0],
        )

    @router.get(
        "/connectors/{provider}/preview",
        response_model=HubPreviewResponse,
        summary="Preview the first rows of a file",
    )
    def preview_connector_ref(
        provider: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        ref: str = Query(min_length=1, max_length=2048),
    ) -> HubPreviewResponse:
        """Show a handful of rows before importing.

        Args:
            provider: Provider slug.
            user: Authenticated caller.
            ref: A file ref from a listing.

        Returns:
            Columns and the first rows; the total is unknown for files.
        """
        module = registry.get_provider(provider)
        return HubPreviewResponse(**module.preview(_secret(user.username, provider), ref))

    @router.post(
        "/connectors/{provider}/import",
        response_model=SaveDatasetResponse,
        summary="Import a file into the caller's library",
    )
    def import_connector_ref(
        provider: str,
        body: RefImportRequest,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> SaveDatasetResponse:
        """Download a file and save its rows as a library dataset.

        Args:
            provider: Provider slug.
            body: Which file to import and what to call it.
            user: Authenticated caller.

        Returns:
            The saved entry and whether an identical one already existed.

        Raises:
            DomainError: 413 when the file exceeds the cap; 409 over the storage
                quota or when the file cannot be decoded.
        """
        module = registry.get_provider(provider)
        rows, column_schema, default_name = module.import_ref(_secret(user.username, provider), body.ref)
        record, deduped = save_rows_gated(
            job_store,
            library,
            owner=user.username,
            name=(body.name or "").strip() or default_name,
            source=provider,
            rows=rows,
            column_schema=column_schema,
        )
        return SaveDatasetResponse(dataset=_summary(record, ShareRole.owner), deduplicated=deduped)

    return router

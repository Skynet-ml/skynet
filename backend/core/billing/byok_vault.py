"""Encrypt-at-rest vault for bring-your-own-key (BYOK) provider secrets.

When an account runs in ``byok`` token source, the provider bills model tokens
directly to the user's key while the Skynet balance funds the managed sandbox.
Skynet charges no BYOK model fee. This module is the only place that holds those secrets: it
encrypts them with Fernet under
``settings.byok_vault_key`` before they touch the database, decrypts them only
to run a verify probe or hand them to a run, and never returns plaintext to the
API surface. The stored row keeps only the ciphertext, a masked tail (``last4``)
for display, and a verification ``status`` — so a database dump never leaks a
usable key.

The verify probe makes one lightweight authenticated request to the provider on
key entry, so a typo'd or revoked key is caught before a job ever runs rather
than failing mid-optimization. A successful probe flips the stored status to
``verified``; an auth rejection flips it to ``invalid``; a transient/network
error leaves it ``unverified`` (the probe couldn't reach a verdict, not a bad
key).

Reads of the masked metadata work whether or not the vault is configured;
saving a key requires ``settings.is_byok_vault_configured`` and raises
``DomainError("billing.byok_not_configured", 503)`` otherwise — never a 500.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from ..api.errors import DomainError
from ..config import settings
from ..provider_registry import LITELLM_TO_BYOK_PROVIDER
from ..storage.models import BillingProviderKeyModel
from .byok_upstreams import UPSTREAMS, canonical_base, deepseek_base, validate_custom_base
from .mcp_broker import EndpointResolutionError, _pinned_address, _PinnedTransport

# Verification states a stored key can carry. ``unverified`` is the default and
# also the resting state after a probe that couldn't reach a verdict (network /
# transient error); ``verified`` and ``invalid`` are set only by a probe that
# got a definitive answer from the provider.
STATUS_UNVERIFIED = "unverified"
STATUS_VERIFIED = "verified"
STATUS_INVALID = "invalid"
logger = logging.getLogger(__name__)


def _bearer_probe(url: str) -> dict[str, str]:
    """Build a verify probe for an OpenAI-style ``Authorization: Bearer`` API.

    Most providers authenticate a bare ``GET`` of their model-list endpoint with
    a bearer token and answer ``401`` to a bad one — exactly the verdict shape
    :meth:`ProviderKeyVault._probe` needs — so their probe differs only in URL.

    Args:
        url: The provider's model-list endpoint (its key-introspection endpoint
            when it has one), reached with the user's key as a bearer token.

    Returns:
        A probe mapping with the URL and a secret-templated ``Authorization``
        header.
    """
    return {"url": url, "header_name": "Authorization", "header_value": "Bearer {secret}"}


# Providers a user may bring a key for. Mirrors the frontend ``BYOK_PROVIDERS``
# catalog (keyed by the same vault slugs the registry pins); the value is how the
# verify probe reaches each provider. Most authenticate a bare model-list GET
# with a bearer token (:func:`_bearer_probe`); OpenRouter uses its
# key-introspection endpoint, and Anthropic/Google carry their own auth-header
# shapes. ``header_value`` is templated with the secret at probe time;
# ``extra_header_value`` is a fixed header sent verbatim.
_PROVIDER_PROBES: dict[str, dict[str, str]] = {
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/key",
        "header_name": "Authorization",
        "header_value": "Bearer {secret}",
    },
    "openai": _bearer_probe("https://api.openai.com/v1/models"),
    "anthropic": {
        "url": "https://api.anthropic.com/v1/models",
        "header_name": "x-api-key",
        "header_value": "{secret}",
        "extra_header_name": "anthropic-version",
        "extra_header_value": "2023-06-01",
    },
    "google": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models",
        "header_name": "x-goog-api-key",
        "header_value": "{secret}",
    },
    "xai": _bearer_probe("https://api.x.ai/v1/models"),
    "groq": _bearer_probe("https://api.groq.com/openai/v1/models"),
    "deepseek": _bearer_probe("https://api.deepseek.com/models"),
    "together": _bearer_probe(f"{UPSTREAMS['together'].base_url}/models"),
    "mistral": _bearer_probe("https://api.mistral.ai/v1/models"),
    "moonshot": _bearer_probe("https://api.moonshot.ai/v1/models"),
    "cohere": _bearer_probe("https://api.cohere.ai/v1/models"),
    "fireworks": _bearer_probe("https://api.fireworks.ai/inference/v1/models"),
    "cerebras": _bearer_probe("https://api.cerebras.ai/v1/models"),
    "novita": _bearer_probe("https://api.novita.ai/v3/openai/models"),
    "deepinfra": _bearer_probe("https://api.deepinfra.com/v1/openai/models"),
    "sambanova": _bearer_probe("https://api.sambanova.ai/v1/models"),
    "nebius": _bearer_probe("https://api.studio.nebius.ai/v1/models"),
    "minimax": _bearer_probe("https://api.minimax.io/v1/models"),
    "zai": _bearer_probe("https://api.z.ai/api/paas/v4/models"),
    "meta": _bearer_probe("https://api.llama.com/compat/v1/models"),
    "gmi": _bearer_probe("https://api.gmi-serving.com/v1/models"),
    "crusoe": _bearer_probe("https://managed-inference-api-proxy.crusoecloud.com/v1/models"),
    "friendliai": _bearer_probe("https://api.friendli.ai/serverless/v1/models"),
    "morph": _bearer_probe("https://api.morphllm.com/v1/models"),
}

# How long a verify probe waits for the provider before giving up. A timeout is
# treated as "couldn't reach a verdict" (status stays ``unverified``), never as
# an invalid key.
_PROBE_TIMEOUT_SECONDS = 8.0
_RESERVED_CONNECTION_PARAMS = frozenset({"apikey", "apibase", "baseurl", "model"})
_CREDENTIAL_PARAM_SUFFIXES = (
    "apikey",
    "authorization",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "bearertoken",
    "sessiontoken",
    "securitytoken",
    "clientsecret",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "privatekey",
    "secretkey",
    "sslkey",
    "clientkey",
    "signingkey",
    "accesskey",
    "accesskeyid",
    "credential",
    "credentials",
    "cookie",
    "cookies",
    "auth",
    "token",
    "bearer",
    "jwt",
)
_CREDENTIAL_PARAM_PREFIXES = (
    "authorizationheader",
    "authheader",
    "apikeyheader",
    "xapikeyheader",
)


def _normalized_connection_param_name(name: Any) -> str:
    """Normalize a connection parameter name for security matching.

    Args:
        name: Caller-supplied parameter or nested mapping key.

    Returns:
        Case-folded alphanumeric text without separator differences.
    """
    return "".join(character for character in str(name).casefold() if character.isalnum())


def _is_credential_param(name: Any) -> bool:
    """Return whether a parameter name can carry authentication material.

    Args:
        name: Caller-supplied parameter or nested mapping key.

    Returns:
        Whether the parameter must stay out of plaintext connection metadata.
    """
    normalized = _normalized_connection_param_name(name)
    return normalized.endswith(_CREDENTIAL_PARAM_SUFFIXES) or normalized.startswith(_CREDENTIAL_PARAM_PREFIXES)


def _safe_connection_value(value: Any) -> Any:
    """Recursively remove credential-shaped keys from a parameter value.

    Args:
        value: JSON-compatible connection parameter fragment.

    Returns:
        A copied fragment without nested authentication material.
    """
    if isinstance(value, dict):
        return {key: _safe_connection_value(item) for key, item in value.items() if not _is_credential_param(key)}
    if isinstance(value, (list, tuple)):
        return [_safe_connection_value(item) for item in value]
    return value


def safe_connection_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Remove connection overrides and nested plaintext credentials.

    Args:
        params: Optional extra LiteLLM keyword arguments from the caller.

    Returns:
        A copy containing only non-reserved, credential-free runtime parameters.
    """
    return {
        key: _safe_connection_value(value)
        for key, value in (params or {}).items()
        if _normalized_connection_param_name(key) not in _RESERVED_CONNECTION_PARAMS and not _is_credential_param(key)
    }


def byok_provider_for_litellm(prefix: str) -> str:
    """Return the BYOK vault slug a LiteLLM provider prefix resolves a key for.

    The run path extracts a model's leading ``provider/`` segment (a LiteLLM
    prefix such as ``gemini`` or ``together_ai``); this maps it back to the vault
    slug the user saved their key under (``google`` / ``together``). Identity for
    prefixes whose vault slug already matches.

    Args:
        prefix: The LiteLLM provider prefix from a model id.

    Returns:
        The vault provider slug to resolve a connection for.
    """
    return LITELLM_TO_BYOK_PROVIDER.get(prefix, prefix)


@dataclass(frozen=True)
class ProviderKeyView:
    """One stored BYOK connection as the API surface sees it — never the secret.

    ``id`` is the connection's stable handle, ``label`` an optional user-facing
    name, ``last4`` the recognizable tail for masked display, ``api_base`` the
    optional custom endpoint, ``status`` the verification state, and ``added_at``
    the ISO-8601 instant the connection was saved. The plaintext secret never
    appears here.
    """

    id: str
    provider: str
    label: str | None
    last4: str
    api_base: str | None
    status: str
    added_at: str


@dataclass(frozen=True)
class ResolvedConnection:
    """A decrypted BYOK connection for the run path — secret plus its endpoint.

    Handed only to the in-process run pipeline (never the HTTP surface) so a BYOK
    job can authenticate against the user's own provider key, custom ``api_base``,
    and extra LiteLLM ``params``.
    """

    provider: str
    secret: str
    api_base: str | None
    params: dict[str, Any]


@dataclass(frozen=True)
class VaultSnapshot:
    """The account's stored BYOK connections as a single read for the settings surface."""

    keys: list[ProviderKeyView] = field(default_factory=list)


def key_last4(secret: str) -> str:
    """Return the recognizable tail of a secret for masked display.

    Args:
        secret: The plaintext provider key.

    Returns:
        The last four characters, or a dotted placeholder when the secret is
        shorter than that.
    """
    tail = secret[-4:]
    return tail if len(tail) == 4 else "····"


def _row_view(row: BillingProviderKeyModel) -> ProviderKeyView:
    """Project a stored connection row onto its masked, secret-free view.

    Args:
        row: The persisted connection row (already flushed so ``id`` is set).

    Returns:
        The masked :class:`ProviderKeyView` the API surface returns.
    """
    return ProviderKeyView(
        id=row.id,
        provider=row.provider,
        label=row.label,
        last4=row.last4,
        api_base=row.api_base,
        status=row.status,
        added_at=row.created_at.isoformat(),
    )


class ProviderKeyVault:
    """Stores, verifies, and retrieves BYOK provider secrets, encrypted at rest."""

    def __init__(self, *, engine: Any) -> None:
        """Bind the vault to the ORM engine backing the provider-key table.

        Args:
            engine: SQLAlchemy engine (``job_store.engine``) used for every
                vault session. The Fernet cipher is built lazily per mutation,
                so constructing the vault never requires the vault key.
        """
        self._engine = engine

    def _provider_row(self, session: Session, username: str, provider: str) -> BillingProviderKeyModel | None:
        """Return the account's primary connection for a provider, or ``None``.

        When several connections exist for one provider the oldest wins, so the
        provider-addressed helpers (rotate, verify, reveal) operate on a stable
        choice. ``id``-addressed callers bypass this.

        Args:
            session: Open ORM session bound to the vault engine.
            username: Account whose connection is sought.
            provider: Provider slug to match.

        Returns:
            The matching row, or ``None`` when the account has no connection for
            the provider.
        """
        return (
            session.query(BillingProviderKeyModel)
            .filter(
                BillingProviderKeyModel.username == username,
                BillingProviderKeyModel.provider == provider,
            )
            .order_by(BillingProviderKeyModel.created_at)
            .first()
        )

    def _cipher(self) -> Fernet:
        """Return a Fernet cipher built from the configured vault key.

        Returns:
            The Fernet cipher used to encrypt/decrypt stored secrets.

        Raises:
            DomainError: 503 when no BYOK vault key is configured.
        """
        if settings.byok_vault_key is None:
            raise DomainError("billing.byok_not_configured", status=503)
        return Fernet(settings.byok_vault_key.get_secret_value().encode("utf-8"))

    def list_keys(self, username: str) -> VaultSnapshot:
        """Return the account's stored keys as masked, secret-free views.

        A pure DB read — no decryption, no provider call — so it serves even when
        the vault key is unconfigured (the ciphertext is never touched). Results
        are ordered by provider for a stable settings list.

        Args:
            username: Account whose keys are listed.

        Returns:
            A :class:`VaultSnapshot` of masked key views.
        """
        with Session(self._engine) as session:
            rows = (
                session.query(BillingProviderKeyModel)
                .filter(BillingProviderKeyModel.username == username)
                .order_by(BillingProviderKeyModel.provider, BillingProviderKeyModel.created_at)
                .all()
            )
            keys = [_row_view(row) for row in rows]
        return VaultSnapshot(keys=keys)

    def has_verified_connection(self, username: str, provider: str) -> bool:
        """Return whether the account has a verified connection for a provider.

        Args:
            username: Account to check.
            provider: Provider slug to look for.

        Returns:
            True when at least one verified connection is stored.
        """
        with Session(self._engine) as session:
            return (
                session.query(BillingProviderKeyModel.id)
                .filter(
                    BillingProviderKeyModel.username == username,
                    BillingProviderKeyModel.provider == provider,
                    BillingProviderKeyModel.status == STATUS_VERIFIED,
                )
                .first()
                is not None
            )

    def save_key(
        self,
        username: str,
        provider: str,
        secret: str,
        *,
        label: str | None = None,
        api_base: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ProviderKeyView:
        """Encrypt and store a provider connection, then verify it on entry.

        The secret is Fernet-encrypted before it touches the database and the
        plaintext is dropped immediately; only the ciphertext, the masked tail,
        the optional endpoint metadata, and a verification status are persisted.
        Saving rotates the account's existing connection for the same provider in
        place (the simple one-per-provider path); the verify probe runs
        synchronously — against ``api_base`` when supplied, so a custom endpoint
        is checked too — so the returned view already carries the entry-time
        verdict.

        Args:
            username: Account the connection belongs to.
            provider: Provider slug — a known BYOK provider, or any slug when a
                custom ``api_base`` is supplied.
            secret: The plaintext provider key; never persisted in the clear.
            label: Optional user-facing name for the connection.
            api_base: Optional custom endpoint; required for an unknown provider.
            params: Optional extra LiteLLM kwargs to carry with the connection.

        Returns:
            The masked, verified view of the stored connection.

        Raises:
            DomainError: 400 when the provider or secret is invalid or the custom
                endpoint is unsafe; 503 when the vault key is unconfigured.
        """
        secret = secret.strip()
        api_base = (api_base or "").strip() or None
        if provider not in _PROVIDER_PROBES and api_base is None:
            raise DomainError("billing.byok_unknown_provider", status=400, provider=provider)
        if not secret:
            raise DomainError("billing.byok_empty_secret", status=400, provider=provider)
        resolution_failed = False
        try:
            if api_base:
                validate_custom_base(api_base)
                if not (
                    provider == "openrouter"
                    and canonical_base(api_base) in {"https://openrouter.ai", "https://openrouter.ai/api/v1"}
                ):
                    _pinned_address(api_base, False, label="custom provider endpoint")
        except EndpointResolutionError:
            resolution_failed = True
        except ValueError as error:
            logger.warning("Custom provider endpoint validation failed: %s", error)
            raise DomainError("billing.byok_invalid_endpoint", status=400) from error
        cipher = self._cipher()
        ciphertext = cipher.encrypt(secret.encode("utf-8"))
        status = STATUS_UNVERIFIED if resolution_failed else self._probe(provider, secret, api_base)
        safe_params = safe_connection_params(params)
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = self._provider_row(session, username, provider)
            if row is None:
                row = BillingProviderKeyModel(
                    username=username,
                    provider=provider,
                    label=label,
                    api_base=api_base,
                    params=safe_params,
                    secret_ciphertext=ciphertext,
                    last4=key_last4(secret),
                    status=status,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.secret_ciphertext = ciphertext
                row.last4 = key_last4(secret)
                row.status = status
                row.label = label
                row.api_base = api_base
                row.params = safe_params
                row.updated_at = now
            session.flush()
            view = _row_view(row)
            session.commit()
        return view

    def verify_key(self, username: str, provider: str) -> ProviderKeyView:
        """Re-run the verify probe against a stored key and persist the verdict.

        Decrypts the stored secret in memory only for the duration of the probe,
        updates the stored status to the probe's verdict, and returns the masked
        view. Used to re-check a key that was saved while the provider was
        unreachable (status stuck at ``unverified``), and to re-confirm a working
        key on demand. A re-test never demotes a ``verified`` key for a mere
        outage: only a definitive rejection unseats it; an inconclusive probe
        leaves the prior verdict intact.

        Args:
            username: Account the key belongs to.
            provider: Provider slug whose stored key is re-verified.

        Returns:
            The masked view carrying the fresh verification status.

        Raises:
            DomainError: 404 when no key is stored for the provider; 503 when the
                vault key is unconfigured.
        """
        cipher = self._cipher()
        with Session(self._engine) as session:
            row = self._provider_row(session, username, provider)
            if row is None:
                raise DomainError("billing.byok_key_not_found", status=404, provider=provider)
            try:
                secret = cipher.decrypt(row.secret_ciphertext).decode("utf-8")
            except InvalidToken as exc:
                # The stored ciphertext can't be decrypted with the current vault
                # key (the key was rotated out from under the row). Mark it invalid
                # so the UI prompts a re-entry rather than wedging on a dead probe.
                row.status = STATUS_INVALID
                row.updated_at = datetime.now(UTC)
                session.commit()
                raise DomainError("billing.byok_key_undecryptable", status=409, provider=provider) from exc
            probe_status = self._probe(provider, secret, row.api_base)
            # A manual re-test must never demote a working key for a mere outage:
            # only a definitive rejection (401/403 -> invalid) unseats a verified
            # key; an inconclusive probe leaves the prior verdict intact.
            if probe_status == STATUS_UNVERIFIED and row.status == STATUS_VERIFIED:
                probe_status = STATUS_VERIFIED
            row.status = probe_status
            row.updated_at = datetime.now(UTC)
            view = _row_view(row)
            session.commit()
        return view

    def remove_key(self, username: str, provider: str) -> None:
        """Forget a stored provider key.

        A no-op when no key is stored for the provider, so the call is idempotent.
        Requires no vault key (it never decrypts), so a key can be removed even on
        a deploy whose vault key was lost.

        Args:
            username: Account the connection belongs to.
            provider: Provider slug whose connection(s) are removed.
        """
        with Session(self._engine) as session:
            rows = (
                session.query(BillingProviderKeyModel)
                .filter(
                    BillingProviderKeyModel.username == username,
                    BillingProviderKeyModel.provider == provider,
                )
                .all()
            )
            for row in rows:
                session.delete(row)
            if rows:
                session.commit()

    def resolve_connection(
        self, username: str, provider: str, *, verified_only: bool = False
    ) -> ResolvedConnection | None:
        """Decrypt the account's best connection for a provider, for the run path.

        Picks the connection most likely to work — a ``verified`` one first, then
        the most recently updated — and decrypts it in memory only. Used by the
        run pipeline (BYOK bridge) to authenticate a job against the user's own
        key and endpoint; never exposed through the HTTP surface.

        Args:
            username: Account the connection belongs to.
            provider: Provider slug whose connection is resolved.
            verified_only: Require verification in the same database read that selects the key.

        Returns:
            The decrypted :class:`ResolvedConnection`, or ``None`` when the
            account has no eligible connection for the provider.

        Raises:
            DomainError: 503 when the vault key is unconfigured; 409 when the
                stored ciphertext can't be decrypted with the current vault key.
        """
        cipher = self._cipher()
        with Session(self._engine) as session:
            query = session.query(BillingProviderKeyModel).filter(
                BillingProviderKeyModel.username == username,
                BillingProviderKeyModel.provider == provider,
            )
            if verified_only:
                query = query.filter(BillingProviderKeyModel.status == STATUS_VERIFIED)
            rows = query.all()
            if not rows:
                return None
            rows.sort(key=lambda r: (r.status != STATUS_VERIFIED, -r.updated_at.timestamp()))
            row = rows[0]
            ciphertext = row.secret_ciphertext
            api_base = row.api_base
            params = safe_connection_params(dict(row.params or {}))
        try:
            secret = cipher.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise DomainError("billing.byok_key_undecryptable", status=409, provider=provider) from exc
        return ResolvedConnection(provider=provider, secret=secret, api_base=api_base, params=params)

    def reveal_secret(self, username: str, provider: str) -> str | None:
        """Decrypt and return a stored secret for a run that bills the user's key.

        Thin wrapper over :meth:`resolve_connection` for callers that need only
        the plaintext key. Returns ``None`` when no connection is stored.

        Args:
            username: Account the connection belongs to.
            provider: Provider slug whose secret is needed.

        Returns:
            The plaintext provider key, or ``None`` when none is stored.

        Raises:
            DomainError: 503 when the vault key is unconfigured; 409 when the
                stored ciphertext can't be decrypted with the current vault key.
        """
        resolved = self.resolve_connection(username, provider)
        return resolved.secret if resolved is not None else None

    def _probe(self, provider: str, secret: str, api_base: str | None = None) -> str:
        """Make one authenticated request to a provider and classify the verdict.

        A ``2xx`` response means the key works (:data:`STATUS_VERIFIED`); a
        ``401``/``403`` means the provider rejected it (:data:`STATUS_INVALID`).
        Any other status, a timeout, or a network error means the probe couldn't
        reach a verdict, so the status stays :data:`STATUS_UNVERIFIED` — the key
        is not condemned for an outage on the provider's side. A custom
        ``api_base`` is probed at ``{api_base}/models`` with OpenAI-compatible
        bearer authentication and a pinned destination.

        Args:
            provider: Provider slug.
            secret: The plaintext key to authenticate the probe with.
            api_base: Optional custom endpoint to probe instead of the provider's
                default. Canonical OpenRouter bases use the ``/key`` endpoint.

        Returns:
            One of :data:`STATUS_VERIFIED`, :data:`STATUS_INVALID`, or
            :data:`STATUS_UNVERIFIED`.
        """
        canonical_openrouter = False
        address = None
        try:
            if api_base:
                validate_custom_base(api_base)
                canonical_openrouter = provider == "openrouter" and canonical_base(api_base) in {
                    "https://openrouter.ai",
                    "https://openrouter.ai/api/v1",
                }
                if not canonical_openrouter:
                    address = _pinned_address(api_base, False, label="custom provider endpoint")
        except EndpointResolutionError:
            return STATUS_UNVERIFIED
        except ValueError as error:
            logger.warning("Custom provider endpoint probe validation failed: %s", error)
            return STATUS_INVALID
        probe = _PROVIDER_PROBES.get(provider)
        if probe is not None:
            normalized_base = canonical_base(api_base) if api_base else ""
            if provider == "openrouter" and normalized_base in {
                "https://openrouter.ai",
                "https://openrouter.ai/api/v1",
            }:
                root = normalized_base if normalized_base.endswith("/v1") else normalized_base + "/api/v1"
                urls = [f"{root}/key"]
            elif provider == "deepseek" and (resolved := deepseek_base(api_base)):
                urls = [f"{resolved}/models" if api_base else probe["url"]]
            else:
                urls = self._model_probe_urls(api_base) if api_base else [probe["url"]]
            canonical_provider = provider in UPSTREAMS and normalized_base == canonical_base(
                UPSTREAMS[provider].base_url
            )
            if canonical_provider and not canonical_openrouter:
                upstream = UPSTREAMS[provider]
                headers = dict(upstream.extra_headers)
                if upstream.auth == "x-api-key":
                    headers["x-api-key"] = secret
                else:
                    headers["Authorization"] = f"Bearer {secret}"
            elif api_base and not canonical_openrouter:
                headers = {"Authorization": f"Bearer {secret}"}
            else:
                headers = {probe["header_name"]: probe["header_value"].format(secret=secret)}
                extra_name = probe.get("extra_header_name")
                if extra_name:
                    headers[extra_name] = probe["extra_header_value"]
        elif api_base:
            urls = self._model_probe_urls(api_base)
            headers = {"Authorization": f"Bearer {secret}"}
        else:
            return STATUS_UNVERIFIED
        for url in urls:
            try:
                if address is None:
                    response = httpx.get(url, headers=headers, timeout=_PROBE_TIMEOUT_SECONDS)
                else:
                    with httpx.Client(
                        transport=_PinnedTransport(api_base, address),
                        follow_redirects=False,
                        trust_env=False,
                        timeout=_PROBE_TIMEOUT_SECONDS,
                    ) as client:
                        response = client.get(url, headers=headers)
            except httpx.HTTPError:
                return STATUS_UNVERIFIED
            if response.is_success:
                return STATUS_VERIFIED
            if response.status_code in (401, 403):
                return STATUS_INVALID
        return STATUS_UNVERIFIED

    @staticmethod
    def _model_probe_urls(api_base: str) -> list[str]:
        """Return model-list probe URLs for an OpenAI-compatible API base.

        Args:
            api_base: User-supplied endpoint root or versioned API base.

        Returns:
            One or two candidate ``/models`` URLs in preferred order.
        """
        base = api_base.rstrip("/")
        if base.endswith("/v1"):
            return [f"{base}/models"]
        return [f"{base}/v1/models", f"{base}/models"]

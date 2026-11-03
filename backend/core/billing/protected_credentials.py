"""Resolve account-owned model credentials in the trusted parent without choosing a spending policy."""

from __future__ import annotations

import copy
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..api.errors import DomainError
from ..config import settings
from ..storage.models import ExecutionBudgetModel, ProtectedCredentialModel
from .byok_bridge import provider_slug_for_model
from .byok_upstreams import UPSTREAMS
from .byok_vault import STATUS_VERIFIED, ProviderKeyVault, byok_provider_for_litellm
from .credential_safety import endpoint_has_private_components, public_endpoint, scrub_model_config
from .operation_pricing import json_fingerprint

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
_CONNECTION_FIELDS = frozenset(
    {
        "api_key",
        "api_base",
        "base_url",
        "headers",
        "extra_headers",
        "default_headers",
        "authorization",
        "authorization_header",
        "_skynet_budget_route",
    }
)
MCP_AUTH_HEADER_FIELD = "mcp_auth_header"
MCP_CREDENTIAL_REF_FIELD = "_mcp_credential_ref"
MCP_CREDENTIAL_REVISION_FIELD = "_mcp_credential_revision"
MCP_URL_REF_FIELD = "_mcp_url_ref"
MCP_URL_REVISION_FIELD = "_mcp_url_revision"
SCORER_SECRET_FIELD = "secret"
SCORER_CREDENTIAL_REF_FIELD = "_scorer_credential_ref"
SCORER_CREDENTIAL_REVISION_FIELD = "_scorer_credential_revision"
SCORER_URL_REF_FIELD = "_scorer_url_ref"
SCORER_URL_REVISION_FIELD = "_scorer_url_revision"
_MCP_PURPOSE = "mcp_auth_header"
_MCP_URL_PURPOSE = "mcp_endpoint_url"
_SCORER_PURPOSE = "remote_evaluator_secret"
_SCORER_URL_PURPOSE = "remote_evaluator_endpoint_url"
REPO_SECRET_REF_FIELD = "credential_ref"
REPO_SECRET_REVISION_FIELD = "credential_revision"


@dataclass(frozen=True)
class ProtectedCredentialRef:
    """Identify one execution-scoped secret without exposing its value."""

    id: str
    revision: int


class ProtectedCredentialVault:
    """Encrypt execution-scoped secrets for resolution by trusted parent relays."""

    def __init__(self, *, engine: Any) -> None:
        """Bind the vault to the execution-budget database.

        Args:
            engine: SQLAlchemy engine containing protected credential rows.
        """
        self._engine = engine

    @staticmethod
    def _cipher() -> Fernet:
        """Build the deployment's existing encrypt-at-rest vault cipher.

        Returns:
            Configured Fernet cipher shared with the BYOK vault.

        Raises:
            DomainError: When encrypted credential storage is unavailable.
        """
        if settings.byok_vault_key is None:
            raise DomainError("billing.byok_not_configured", status=503)
        return Fernet(settings.byok_vault_key.get_secret_value().encode("utf-8"))

    def store_secret(
        self,
        username: str,
        binding_id: str,
        audience_hash: str,
        purpose: str,
        secret: str,
    ) -> ProtectedCredentialRef:
        """Encrypt or idempotently rotate an execution-scoped credential.

        Args:
            username: Authenticated owner of the execution budget.
            binding_id: Budget identity confining use of the credential.
            audience_hash: Digest of the selected endpoint.
            purpose: Fixed credential role within the execution.
            secret: Raw credential received over the authenticated API.

        Returns:
            Stable opaque reference and current revision.

        Raises:
            ValueError: When the submitted header is empty or an existing row
                cannot be decrypted with the current vault key.
        """
        secret = secret.strip()
        if not secret:
            raise ValueError("The execution credential cannot be empty.")
        cipher = self._cipher()
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            if self._engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            budget = session.execute(
                select(ExecutionBudgetModel)
                .where(
                    ExecutionBudgetModel.id == binding_id,
                    ExecutionBudgetModel.username == username,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if budget is None:
                raise ValueError("The execution credential budget is unavailable.")
            row = session.execute(
                select(ProtectedCredentialModel)
                .where(
                    ProtectedCredentialModel.username == username,
                    ProtectedCredentialModel.binding_id == binding_id,
                    ProtectedCredentialModel.purpose == purpose,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                row = ProtectedCredentialModel(
                    username=username,
                    binding_id=binding_id,
                    purpose=purpose,
                    audience_hash=audience_hash,
                    revision=1,
                    secret_ciphertext=cipher.encrypt(secret.encode("utf-8")),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            else:
                try:
                    current = cipher.decrypt(row.secret_ciphertext).decode("utf-8")
                except InvalidToken as error:
                    raise ValueError("The saved execution credential can no longer be decrypted.") from error
                if row.audience_hash != audience_hash or not hmac.compare_digest(current, secret):
                    row.audience_hash = audience_hash
                    row.secret_ciphertext = cipher.encrypt(secret.encode("utf-8"))
                    row.revision += 1
                    row.updated_at = now
            reference = ProtectedCredentialRef(id=row.id, revision=row.revision)
            session.commit()
        return reference

    def current_reference(
        self,
        username: str,
        binding_id: str,
        audience_hash: str,
        purpose: str,
    ) -> ProtectedCredentialRef | None:
        """Return the current reference only when it targets the same endpoint and purpose.

        Args:
            username: Authenticated execution owner.
            binding_id: Budget identity confining the credential.
            audience_hash: Digest of the selected endpoint.
            purpose: Fixed credential role within the execution.

        Returns:
            Existing opaque reference, or None when no matching credential exists.
        """
        with Session(self._engine) as session:
            row = session.execute(
                select(ProtectedCredentialModel).where(
                    ProtectedCredentialModel.username == username,
                    ProtectedCredentialModel.binding_id == binding_id,
                    ProtectedCredentialModel.purpose == purpose,
                    ProtectedCredentialModel.audience_hash == audience_hash,
                )
            ).scalar_one_or_none()
            return ProtectedCredentialRef(id=row.id, revision=row.revision) if row is not None else None

    def resolve_secret(
        self,
        username: str,
        binding_id: str,
        audience_hash: str,
        purpose: str,
        reference: ProtectedCredentialRef,
    ) -> str:
        """Decrypt the exact current credential for its owner, budget, endpoint, and purpose.

        Args:
            username: Authenticated execution owner.
            binding_id: Budget identity attached to the run.
            audience_hash: Digest of the endpoint requesting the credential.
            purpose: Fixed credential role within the execution.
            reference: Opaque id and revision persisted on the run.

        Returns:
            Raw credential for immediate parent-relay configuration.

        Raises:
            ValueError: When ownership, binding, endpoint, revision, or decryption fails.
        """
        cipher = self._cipher()
        with Session(self._engine) as session:
            row = session.execute(
                select(ProtectedCredentialModel).where(
                    ProtectedCredentialModel.id == reference.id,
                    ProtectedCredentialModel.username == username,
                    ProtectedCredentialModel.binding_id == binding_id,
                    ProtectedCredentialModel.purpose == purpose,
                    ProtectedCredentialModel.audience_hash == audience_hash,
                    ProtectedCredentialModel.revision == reference.revision,
                )
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("The saved execution credential changed; test setup again.")
            ciphertext = row.secret_ciphertext
        try:
            return cipher.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as error:
            raise ValueError("The saved execution credential can no longer be decrypted.") from error


def _mcp_audience(tool_source: dict[str, Any]) -> str:
    """Bind an MCP credential to the selected remote endpoint.

    Args:
        tool_source: Live MCP source carrying the endpoint URL.

    Returns:
        Stable digest that reveals no endpoint metadata from the vault row.
    """
    return json_fingerprint({"kind": "live_mcp", "mcp_url": public_endpoint(str(tool_source.get("mcp_url") or ""))})


def _scorer_audience(scorer: dict[str, Any]) -> str:
    """Bind a remote evaluator credential to its selected endpoint.

    Args:
        scorer: Remote scorer configuration carrying its endpoint URL.

    Returns:
        Stable digest that reveals no endpoint metadata from the vault row.
    """
    return json_fingerprint({"kind": "remote", "url": public_endpoint(str(scorer.get("url") or ""))})


def _repo_secret_slot(repository: str, name: str) -> tuple[str, str]:
    """Name the vault purpose and audience of one repository secret.

    The purpose column is short and unique per budget, so it carries a digest
    of the secret's name rather than the name itself.

    Args:
        repository: ``owner/name`` the secret is used with.
        name: Environment variable name.

    Returns:
        The purpose and the audience hash.
    """
    return f"repo_secret:{json_fingerprint(name)[:20]}", json_fingerprint({"kind": "repo", "repository": repository})


def _repo_secrets(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Find the secret entries of a repository target.

    Args:
        payload: Black-box run payload.

    Returns:
        The repository name and its mutable secret mappings; empty when the
        payload has no repository target.
    """
    target = payload.get("target")
    repo = target.get("repo") if isinstance(target, dict) else None
    if not isinstance(repo, dict) or not isinstance(repo.get("secrets"), list):
        return "", []
    return str(repo.get("repository") or ""), [entry for entry in repo["secrets"] if isinstance(entry, dict)]


def _protect_repo_secrets(
    payload: dict[str, Any],
    *,
    username: str,
    binding_id: str,
    vault: ProtectedCredentialVault,
) -> None:
    """Vault inline repository secret values, leaving opaque references behind.

    Args:
        payload: Black-box run payload to mutate.
        username: Authenticated owner.
        binding_id: Execution budget binding setup and run together.
        vault: Encrypted execution credential store.

    Raises:
        ValueError: When a secret value is not text, or an entry without a
            value has no current reference to reuse.
    """
    repository, entries = _repo_secrets(payload)
    for entry in entries:
        raw = entry.pop("value", None)
        entry.pop(REPO_SECRET_REF_FIELD, None)
        entry.pop(REPO_SECRET_REVISION_FIELD, None)
        if entry.get("saved_secret_id") is not None:
            continue
        if raw is not None and not isinstance(raw, str):
            raise ValueError("A repository secret value must be text.")
        purpose, audience = _repo_secret_slot(repository, str(entry.get("name") or ""))
        reference = (
            vault.store_secret(username, binding_id, audience, purpose, raw)
            if raw is not None
            else vault.current_reference(username, binding_id, audience, purpose)
        )
        if reference is None:
            raise ValueError(f"Enter the value of the secret '{entry.get('name')}' again.")
        entry[REPO_SECRET_REF_FIELD] = reference.id
        entry[REPO_SECRET_REVISION_FIELD] = reference.revision


def resolve_repo_secrets(
    payload: dict[str, Any],
    *,
    username: str,
    binding_id: str,
    vault: ProtectedCredentialVault,
    saved: Any,
) -> dict[str, str]:
    """Decrypt a repository target's secrets for the scorer's sandbox only.

    Args:
        payload: Stored black-box run payload carrying references.
        username: Persisted execution owner.
        binding_id: Execution budget attached to the run.
        vault: Encrypted execution credential store.
        saved: Account secret store resolving ``saved_secret_id`` entries.

    Returns:
        Environment variable name to value; empty without a repository target.

    Raises:
        ValueError: When a reference is incomplete or no longer current.
    """
    repository, entries = _repo_secrets(payload)
    values: dict[str, str] = {}
    for entry in entries:
        name = str(entry.get("name") or "")
        if entry.get("saved_secret_id") is not None:
            values[name] = saved.resolve(username, str(entry["saved_secret_id"]))
            continue
        identity = entry.get(REPO_SECRET_REF_FIELD)
        revision = entry.get(REPO_SECRET_REVISION_FIELD)
        if identity is None or revision is None:
            raise ValueError(f"The secret '{name}' has no saved value; enter it again.")
        purpose, audience = _repo_secret_slot(repository, name)
        values[name] = vault.resolve_secret(
            username, binding_id, audience, purpose, ProtectedCredentialRef(str(identity), int(revision))
        )
    return values


def _scrubbable_model_configs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect every model configuration whose arbitrary extras cross a trust boundary.

    Args:
        payload: Run, grid, black-box, or preview payload.

    Returns:
        Mutable model configuration mappings found in known schema positions.
    """
    configs = [
        payload[key]
        for key in (
            "model_config",
            "model_settings",
            "reflection_model_config",
            "reflection_model_settings",
            "task_model_config",
            "task_model_settings",
        )
        if isinstance(payload.get(key), dict)
    ]
    scorer = payload.get("scorer")
    if isinstance(scorer, dict) and isinstance(scorer.get("model"), dict):
        configs.append(scorer["model"])
    for key in ("generation_models", "reflection_models", "extra_reflection_model_configs"):
        if isinstance(payload.get(key), list):
            configs.extend(config for config in payload[key] if isinstance(config, dict))
    return configs


def _scrub_payload_model_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove inline credentials from known model configuration positions.

    Args:
        payload: Public or stored optimization payload.

    Returns:
        Deep copy retaining only non-credential model options.
    """
    result = copy.deepcopy(payload)
    for config in _scrubbable_model_configs(result):
        cleaned = scrub_model_config(config)
        config.clear()
        config.update(cleaned)
    return result


def _protect_endpoint(
    container: dict[str, Any],
    *,
    field: str,
    reference_field: str,
    revision_field: str,
    purpose: str,
    audience_hash: str,
    username: str,
    binding_id: str,
    vault: ProtectedCredentialVault,
) -> None:
    """Vault private endpoint components while retaining a safe display URL.

    Args:
        container: MCP source or remote scorer mapping to mutate.
        field: Endpoint field name.
        reference_field: Opaque vault id field name.
        revision_field: Vault revision field name.
        purpose: Fixed endpoint credential role.
        audience_hash: Digest of the safe endpoint identity.
        username: Authenticated execution owner.
        binding_id: Owned execution budget id.
        vault: Encrypted execution credential store.
    """
    endpoint = container.get(field)
    container.pop(reference_field, None)
    container.pop(revision_field, None)
    if not isinstance(endpoint, str):
        return
    container[field] = public_endpoint(endpoint)
    if not endpoint_has_private_components(endpoint):
        return
    reference = vault.store_secret(username, binding_id, audience_hash, purpose, endpoint)
    container[reference_field] = reference.id
    container[revision_field] = reference.revision


def _resolve_endpoint(
    container: dict[str, Any],
    *,
    field: str,
    reference_field: str,
    revision_field: str,
    purpose: str,
    audience_hash: str,
    username: str,
    binding_id: str,
    vault: ProtectedCredentialVault,
) -> None:
    """Restore a private endpoint only in the trusted parent copy.

    Args:
        container: Persisted MCP source or remote scorer mapping to mutate.
        field: Endpoint field name.
        reference_field: Opaque vault id field name.
        revision_field: Vault revision field name.
        purpose: Fixed endpoint credential role.
        audience_hash: Digest of the safe endpoint identity.
        username: Persisted execution owner.
        binding_id: Attached execution budget id.
        vault: Encrypted execution credential store.

    Raises:
        ValueError: When the endpoint reference is incomplete or no longer current.
    """
    identity = container.pop(reference_field, None)
    revision = container.pop(revision_field, None)
    if identity is None and revision is None:
        return
    if not isinstance(identity, str) or not isinstance(revision, int):
        raise TypeError("The saved endpoint credential reference is invalid; test setup again.")
    container[field] = vault.resolve_secret(
        username,
        binding_id,
        audience_hash,
        purpose,
        ProtectedCredentialRef(identity, revision),
    )


def protect_execution_credentials(
    payload: dict[str, Any],
    *,
    username: str,
    binding_id: str,
    vault: ProtectedCredentialVault,
) -> dict[str, Any]:
    """Replace relay credentials with execution-scoped opaque references.

    Caller-supplied opaque fields are always discarded. When a repeated request
    omits the header, the current credential is reused only for the same owner,
    budget, and endpoint; clones with a new budget therefore remain credentialless.

    Args:
        payload: Public preflight or submission inputs.
        username: Authenticated owner.
        binding_id: Execution budget binding setup and run together.
        vault: Encrypted execution credential store.

    Returns:
        Deep copy safe for fingerprinting and job persistence.
    """
    result = _scrub_payload_model_credentials(payload)
    source = result.get("tool_source")
    if isinstance(source, dict):
        raw = source.pop(MCP_AUTH_HEADER_FIELD, None)
        source.pop(MCP_CREDENTIAL_REF_FIELD, None)
        source.pop(MCP_CREDENTIAL_REVISION_FIELD, None)
        if source.get("kind") == "live_mcp":
            _protect_endpoint(
                source,
                field="mcp_url",
                reference_field=MCP_URL_REF_FIELD,
                revision_field=MCP_URL_REVISION_FIELD,
                purpose=_MCP_URL_PURPOSE,
                audience_hash=_mcp_audience(source),
                username=username,
                binding_id=binding_id,
                vault=vault,
            )
            if raw is not None and not isinstance(raw, str):
                raise ValueError("The MCP authorization header must be text.")
            audience = _mcp_audience(source)
            reference = (
                vault.store_secret(username, binding_id, audience, _MCP_PURPOSE, raw)
                if raw is not None
                else vault.current_reference(username, binding_id, audience, _MCP_PURPOSE)
            )
            if reference is not None:
                source[MCP_CREDENTIAL_REF_FIELD] = reference.id
                source[MCP_CREDENTIAL_REVISION_FIELD] = reference.revision
    scorer = result.get("scorer")
    if isinstance(scorer, dict):
        raw = scorer.pop(SCORER_SECRET_FIELD, None)
        scorer.pop(SCORER_CREDENTIAL_REF_FIELD, None)
        scorer.pop(SCORER_CREDENTIAL_REVISION_FIELD, None)
        if scorer.get("kind") == "remote":
            _protect_endpoint(
                scorer,
                field="url",
                reference_field=SCORER_URL_REF_FIELD,
                revision_field=SCORER_URL_REVISION_FIELD,
                purpose=_SCORER_URL_PURPOSE,
                audience_hash=_scorer_audience(scorer),
                username=username,
                binding_id=binding_id,
                vault=vault,
            )
            if raw is not None and not isinstance(raw, str):
                raise ValueError("The remote evaluator secret must be text.")
            audience = _scorer_audience(scorer)
            reference = (
                vault.store_secret(username, binding_id, audience, _SCORER_PURPOSE, raw)
                if raw is not None
                else vault.current_reference(username, binding_id, audience, _SCORER_PURPOSE)
            )
            if reference is not None:
                scorer[SCORER_CREDENTIAL_REF_FIELD] = reference.id
                scorer[SCORER_CREDENTIAL_REVISION_FIELD] = reference.revision
    _protect_repo_secrets(result, username=username, binding_id=binding_id, vault=vault)
    return result


def resolve_execution_credentials(
    payload: dict[str, Any],
    *,
    username: str,
    binding_id: str,
    vault: ProtectedCredentialVault,
) -> dict[str, Any]:
    """Resolve persisted relay references into parent-only credentials.

    Args:
        payload: Secret-free canonical setup or job payload.
        username: Authenticated or persisted execution owner.
        binding_id: Execution budget attached to the payload.
        vault: Encrypted execution credential store.

    Returns:
        Deep copy carrying the header and no opaque reference fields.
    """
    result = copy.deepcopy(payload)
    source = result.get("tool_source")
    if isinstance(source, dict):
        source.pop(MCP_AUTH_HEADER_FIELD, None)
        _resolve_endpoint(
            source,
            field="mcp_url",
            reference_field=MCP_URL_REF_FIELD,
            revision_field=MCP_URL_REVISION_FIELD,
            purpose=_MCP_URL_PURPOSE,
            audience_hash=_mcp_audience(source),
            username=username,
            binding_id=binding_id,
            vault=vault,
        )
        identity = source.pop(MCP_CREDENTIAL_REF_FIELD, None)
        revision = source.pop(MCP_CREDENTIAL_REVISION_FIELD, None)
        if identity is not None or revision is not None:
            if not isinstance(identity, str) or not isinstance(revision, int):
                raise ValueError("The saved MCP credential reference is invalid; test setup again.")
            source[MCP_AUTH_HEADER_FIELD] = vault.resolve_secret(
                username,
                binding_id,
                _mcp_audience(source),
                _MCP_PURPOSE,
                ProtectedCredentialRef(identity, revision),
            )
    scorer = result.get("scorer")
    if isinstance(scorer, dict):
        scorer.pop(SCORER_SECRET_FIELD, None)
        _resolve_endpoint(
            scorer,
            field="url",
            reference_field=SCORER_URL_REF_FIELD,
            revision_field=SCORER_URL_REVISION_FIELD,
            purpose=_SCORER_URL_PURPOSE,
            audience_hash=_scorer_audience(scorer),
            username=username,
            binding_id=binding_id,
            vault=vault,
        )
        identity = scorer.pop(SCORER_CREDENTIAL_REF_FIELD, None)
        revision = scorer.pop(SCORER_CREDENTIAL_REVISION_FIELD, None)
        if identity is not None or revision is not None:
            if not isinstance(identity, str) or not isinstance(revision, int):
                raise ValueError("The saved evaluator credential reference is invalid; test setup again.")
            scorer[SCORER_SECRET_FIELD] = vault.resolve_secret(
                username,
                binding_id,
                _scorer_audience(scorer),
                _SCORER_PURPOSE,
                ProtectedCredentialRef(identity, revision),
            )
    return result


def _scrub_execution_value(value: Any) -> Any:
    """Recursively remove protected relay credentials from one JSON-compatible value.

    Args:
        value: Payload fragment to copy and inspect.

    Returns:
        Secret-free copy of mappings and lists, or the original scalar.
    """
    if isinstance(value, list):
        return [_scrub_execution_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    sensitive: set[str] = set()
    if value.get("kind") == "live_mcp":
        sensitive.update(
            {
                MCP_AUTH_HEADER_FIELD,
                MCP_CREDENTIAL_REF_FIELD,
                MCP_CREDENTIAL_REVISION_FIELD,
                MCP_URL_REF_FIELD,
                MCP_URL_REVISION_FIELD,
            }
        )
        if isinstance(value.get("mcp_url"), str):
            value = {**value, "mcp_url": public_endpoint(value["mcp_url"])}
    if value.get("kind") == "remote":
        sensitive.update(
            {
                SCORER_SECRET_FIELD,
                SCORER_CREDENTIAL_REF_FIELD,
                SCORER_CREDENTIAL_REVISION_FIELD,
                SCORER_URL_REF_FIELD,
                SCORER_URL_REVISION_FIELD,
            }
        )
        if isinstance(value.get("url"), str):
            value = {**value, "url": public_endpoint(value["url"])}
    return {key: _scrub_execution_value(item) for key, item in value.items() if key not in sensitive}


def scrub_execution_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove raw and opaque relay credentials from an outbound or cloned payload.

    Args:
        payload: Stored submission payload.

    Returns:
        Deep copy whose external services require credential re-entry.
    """
    result = _scrub_payload_model_credentials(_scrub_execution_value(payload))
    for entry in _repo_secrets(result)[1]:
        for field in ("value", REPO_SECRET_REF_FIELD, REPO_SECRET_REVISION_FIELD):
            entry.pop(field, None)
    return result


def has_exposed_execution_credentials(payload: dict[str, Any], *, allow_parent_model_routes: bool = False) -> bool:
    """Detect raw credentials before an optimization payload crosses into a guest.

    Args:
        payload: Candidate guest payload.
        allow_parent_model_routes: Preserve already-issued scoped model capabilities.

    Returns:
        Whether scrubbing would remove or redact any credential material.
    """
    candidate = copy.deepcopy(payload)
    if allow_parent_model_routes:
        for config in _scrubbable_model_configs(candidate):
            config.pop("_skynet_budget_route", None)
            extra = config.get("extra")
            if isinstance(extra, dict):
                extra.pop("_skynet_budget_route", None)
    return scrub_execution_credentials(candidate) != candidate


def _openrouter_endpoint(endpoint: str | None) -> bool:
    """Recognize the canonical provider endpoint without trusting a lookalike host or path.

    Args:
        endpoint: Saved or caller-supplied endpoint, with None selecting the provider default.

    Returns:
        Whether the endpoint refers to the HTTPS OpenRouter API without embedded credentials.
    """
    if endpoint is None:
        return True
    try:
        url = urlsplit(endpoint)
        return (
            url.scheme == "https"
            and url.hostname == "openrouter.ai"
            and url.port in {None, 443}
            and url.username is None
            and url.password is None
            and url.path.rstrip("/") in {"", "/api/v1"}
            and not url.query
            and not url.fragment
        )
    except ValueError:
        return False


def _model_configs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect executable model configurations from supported submission and preview shapes.

    Args:
        payload: Parent-owned copy of a run, grid, black-box, or workflow preview.

    Returns:
        Known model-role dictionaries, excluding arbitrary user dataset and code content.
    """
    configs = [
        payload[key]
        for key in ("model_config", "task_model_config", "reflection_model_config")
        if isinstance(payload.get(key), dict)
    ]
    scorer = payload.get("scorer")
    if isinstance(scorer, dict) and scorer.get("kind") != "remote" and isinstance(scorer.get("model"), dict):
        configs.append(scorer["model"])
    for key in ("generation_models", "reflection_models", "extra_reflection_model_configs"):
        if isinstance(payload.get(key), list):
            configs.extend(config for config in payload[key] if isinstance(config, dict))
    return configs


def prepare_protected_credentials(
    payload: dict[str, Any],
    *,
    username: str,
    vault: ProviderKeyVault,
    default_token_source: str | None = None,
) -> dict[str, Any]:
    """Resolve verified vault credentials for the parent gateway without mutating persisted inputs.

    The returned copy contains secrets and must pass through ModelGateway before
    any guest, response, persistence, logging, or fingerprint operation receives it.

    Args:
        payload: Unresolved submission or preview with stable provider references.
        username: Authenticated owner of every connection being resolved.
        vault: Account-scoped encrypted credential storage.
        default_token_source: Optional trusted fallback for legacy model configurations.

    Returns:
        Parent-only copy with authoritative BYOK credentials and endpoint metadata.

    Raises:
        ValueError: When a role has no verified connection, attempts an unsupported managed endpoint,
            or names a subscription model outside local mode.
        TypeError: When model parameters do not have their required dictionary shape.
    """
    result = copy.deepcopy(payload)
    fallback = default_token_source or result.get("token_source") or "managed"
    for config in _model_configs(result):
        source = config.get("token_source") or fallback
        if source == "subscription" and not settings.local_mode:
            raise ValueError("Subscription models run only in local mode.")
        if source not in {"managed", "byok", "subscription"}:
            raise ValueError("The model credential source must be managed, byok or subscription.")
        config["token_source"] = source
        extra = config.get("extra") or {}
        if not isinstance(extra, dict):
            raise TypeError("Model parameters must be a dictionary.")
        if source == "managed" and any(
            value and not _openrouter_endpoint(str(value))
            for value in (config.get("base_url"), extra.get("api_base"), extra.get("base_url"))
        ):
            raise ValueError("A managed model cannot silently replace an explicit custom endpoint.")
        for field in _CONNECTION_FIELDS:
            config.pop(field, None)
        config["extra"] = {key: value for key, value in extra.items() if key not in _CONNECTION_FIELDS | {"model"}}
        # A subscription role runs on the user's own CLI login through the
        # gateway's subscription route, so it takes no provider key or endpoint.
        if source in {"managed", "subscription"}:
            config.pop("byok_provider", None)
            continue
        provider = str(config.get("byok_provider") or "").strip()
        if not provider:
            prefix = provider_slug_for_model(str(config.get("name") or ""))
            provider = byok_provider_for_litellm(prefix) if prefix else ""
        if not provider:
            raise ValueError("A BYOK model must identify its saved provider connection.")
        connection = vault.resolve_connection(username, provider, verified_only=True)
        if connection is None:
            raise ValueError(f"No verified {provider} connection exists for this account.")
        endpoint = connection.api_base
        if endpoint is None and provider == "openrouter":
            endpoint = OPENROUTER_API_BASE
        if not endpoint and provider not in UPSTREAMS:
            raise ValueError("This BYOK provider has no saved execution endpoint.")
        config["byok_provider"] = provider
        if endpoint:
            config["base_url"] = endpoint
        config["extra"].update(
            {key: value for key, value in connection.params.items() if key not in _CONNECTION_FIELDS | {"model"}}
        )
        config["extra"]["api_key"] = connection.secret
    return result


def resolve_current_openrouter_key(
    username: str,
    digest: str,
    *,
    vault: ProviderKeyVault,
    managed_key: str | None = None,
) -> str | None:
    """Return only an available original OpenRouter credential for usage reconciliation.

    Args:
        username: Authoritative operation owner, never a caller-supplied alternate account.
        digest: Credential fingerprint persisted when the operation was admitted.
        vault: Account-scoped encrypted credential storage.
        managed_key: Optional current managed provider credential retained in the parent.

    Returns:
        Matching current managed or verified owner key, or None after rotation, removal, or invalidation.
    """
    if managed_key and json_fingerprint(managed_key) == digest:
        return managed_key
    providers = {"openrouter"}
    providers.update(
        view.provider
        for view in vault.list_keys(username).keys
        if view.status == STATUS_VERIFIED and view.api_base and _openrouter_endpoint(view.api_base)
    )
    for provider in sorted(providers):
        try:
            connection = vault.resolve_connection(username, provider, verified_only=True)
        except DomainError:
            continue
        if (
            connection is not None
            and _openrouter_endpoint(connection.api_base)
            and json_fingerprint(connection.secret) == digest
        ):
            return connection.secret
    return None

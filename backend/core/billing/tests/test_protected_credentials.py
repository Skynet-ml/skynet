"""Verify parent-only BYOK resolution using encrypted local records and no provider calls."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from ...config import settings
from ...connectors.saved_secrets import SavedSecretStore
from ...models.blackbox import BlackboxRepoSecret
from ...provider_registry import BYOK_PROVIDER_SLUGS
from ...storage.models import (
    Base,
    BillingCustomerModel,
    BillingProviderKeyModel,
    ProtectedCredentialModel,
)
from ..budgets import BudgetService
from ..byok_upstreams import UPSTREAMS, resolve_upstream
from ..byok_vault import ProviderKeyVault
from ..operation_pricing import json_fingerprint
from ..protected_credentials import (
    MCP_CREDENTIAL_REF_FIELD,
    MCP_URL_REF_FIELD,
    OPENROUTER_API_BASE,
    REPO_SECRET_REF_FIELD,
    REPO_SECRET_REVISION_FIELD,
    SCORER_CREDENTIAL_REF_FIELD,
    SCORER_URL_REF_FIELD,
    ProtectedCredentialVault,
    has_exposed_execution_credentials,
    prepare_protected_credentials,
    protect_execution_credentials,
    repo_secret_allowed_hosts,
    resolve_current_openrouter_key,
    resolve_execution_credentials,
    resolve_repo_secrets,
    scrub_execution_credentials,
)


@dataclass
class _Harness:
    """Keep the encrypted fixture database and its parent credential resolver together."""

    engine: Engine
    vault: ProviderKeyVault
    cipher: Fernet

    def execution_budget(self, *, key: str) -> str:
        """Create an owned execution binding for a relay credential fixture.

        Args:
            key: Stable idempotency key unique within the fixture.

        Returns:
            Newly created execution budget id.
        """
        return BudgetService(engine=self.engine).create("alice", 20, idempotency_key=key).id

    def store(
        self,
        *,
        username: str = "alice",
        provider: str = "openrouter",
        secret: str = "fixture-alice-key",
        status: str = "verified",
        api_base: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Insert an encrypted verified fixture without using a live verification endpoint.

        Args:
            username: Owner of the fixture connection.
            provider: Saved provider reference.
            secret: Synthetic credential encrypted before insertion.
            status: Verification state selected by the test.
            api_base: Optional saved endpoint.
            params: Optional saved runtime parameters.
        """
        with Session(self.engine) as session:
            session.add(
                BillingProviderKeyModel(
                    username=username,
                    provider=provider,
                    secret_ciphertext=self.cipher.encrypt(secret.encode()),
                    last4=secret[-4:],
                    status=status,
                    api_base=api_base,
                    params=params or {},
                )
            )
            session.commit()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """Create local encrypted storage and fail any accidental provider verification call.

    Args:
        monkeypatch: Fixture restoring the temporary vault key and network guard.

    Yields:
        An isolated encrypted credential store.
    """
    key = Fernet.generate_key()
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(key.decode()))

    def no_probe(*args: Any, **kwargs: Any) -> None:
        """Reject network verification during parent credential resolution."""
        pytest.fail("Credential resolution must not probe providers.")

    monkeypatch.setattr(ProviderKeyVault, "_probe", no_probe)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="alice",
                stripe_customer_id="fixture",
                balance_cents=100,
                grant_remaining=0,
            )
        )
        session.commit()
    yield _Harness(engine, ProviderKeyVault(engine=engine), Fernet(key))
    engine.dispose()


def test_external_relay_credentials_round_trip_only_through_encrypted_references(harness: _Harness) -> None:
    """Encrypt both relay secrets and resolve them only in a parent-owned copy."""
    vault = ProtectedCredentialVault(engine=harness.engine)
    binding_id = harness.execution_budget(key="relay-round-trip")
    payload = {
        "tool_source": {
            "kind": "live_mcp",
            "mcp_url": "https://tools.example/mcp?access_token=mcp-query-secret",
            "mcp_auth_header": "Bearer mcp-secret",
            "tool_filter": ["lookup"],
        },
        "scorer": {
            "kind": "remote",
            "url": "https://evaluator.example/score?api_key=evaluator-query-secret",
            "secret": "evaluator-secret",
        },
    }
    original = copy.deepcopy(payload)

    persisted = protect_execution_credentials(
        payload,
        username="alice",
        binding_id=binding_id,
        vault=vault,
    )

    assert payload == original
    assert "mcp-secret" not in repr(persisted)
    assert "evaluator-secret" not in repr(persisted)
    assert "mcp-query-secret" not in repr(persisted)
    assert "evaluator-query-secret" not in repr(persisted)
    assert MCP_CREDENTIAL_REF_FIELD in persisted["tool_source"]
    assert MCP_URL_REF_FIELD in persisted["tool_source"]
    assert SCORER_CREDENTIAL_REF_FIELD in persisted["scorer"]
    assert SCORER_URL_REF_FIELD in persisted["scorer"]
    with Session(harness.engine) as session:
        rows = list(session.scalars(select(ProtectedCredentialModel).order_by(ProtectedCredentialModel.purpose)))
    assert len(rows) == 4
    assert all(
        secret.encode() not in row.secret_ciphertext
        for secret in ("mcp-secret", "evaluator-secret", "mcp-query-secret", "evaluator-query-secret")
        for row in rows
    )

    parent = resolve_execution_credentials(
        persisted,
        username="alice",
        binding_id=binding_id,
        vault=vault,
    )
    assert parent["tool_source"]["mcp_auth_header"] == "Bearer mcp-secret"
    assert parent["tool_source"]["mcp_url"].endswith("access_token=mcp-query-secret")
    assert parent["scorer"]["secret"] == "evaluator-secret"
    assert parent["scorer"]["url"].endswith("api_key=evaluator-query-secret")
    assert MCP_CREDENTIAL_REF_FIELD not in parent["tool_source"]
    assert MCP_URL_REF_FIELD not in parent["tool_source"]
    assert SCORER_CREDENTIAL_REF_FIELD not in parent["scorer"]
    assert SCORER_URL_REF_FIELD not in parent["scorer"]


def test_relay_reference_is_stable_but_revision_fences_rotation(harness: _Harness) -> None:
    """Reuse identical setup inputs and reject an old reference after secret rotation."""
    vault = ProtectedCredentialVault(engine=harness.engine)
    binding_id = harness.execution_budget(key="relay-rotation")
    initial = {"scorer": {"kind": "remote", "url": "https://evaluator.example/score", "secret": "first"}}
    persisted = protect_execution_credentials(initial, username="alice", binding_id=binding_id, vault=vault)
    replay = protect_execution_credentials(
        {"scorer": {"kind": "remote", "url": "https://evaluator.example/score"}},
        username="alice",
        binding_id=binding_id,
        vault=vault,
    )
    assert replay == persisted

    rotated = protect_execution_credentials(
        {"scorer": {"kind": "remote", "url": "https://evaluator.example/score", "secret": "second"}},
        username="alice",
        binding_id=binding_id,
        vault=vault,
    )
    assert rotated["scorer"]["_scorer_credential_revision"] == 2
    with pytest.raises(ValueError, match="budget is unavailable"):
        protect_execution_credentials(initial, username="bob", binding_id=binding_id, vault=vault)
    with pytest.raises(ValueError, match="changed"):
        resolve_execution_credentials(persisted, username="alice", binding_id=binding_id, vault=vault)
    with pytest.raises(ValueError, match="changed"):
        resolve_execution_credentials(rotated, username="bob", binding_id=binding_id, vault=vault)


def test_recursive_outbound_scrubber_removes_relay_secrets_and_references(harness: _Harness) -> None:
    """Scrub credentials from nested owner, share, public, and clone payload shapes."""
    del harness
    payload = {
        "nested": [
            {
                "kind": "live_mcp",
                "mcp_url": "https://tools.example/mcp?access_token=query-secret",
                "mcp_auth_header": "Bearer hidden",
                "_mcp_credential_ref": "opaque",
                "_mcp_credential_revision": 1,
            },
            {
                "kind": "remote",
                "url": "https://evaluator.example/score?api_key=query-secret",
                "secret": "hidden",
                "_scorer_credential_ref": "opaque",
                "_scorer_credential_revision": 1,
            },
        ]
    }

    scrubbed = scrub_execution_credentials(payload)

    assert has_exposed_execution_credentials(payload)
    assert "hidden" not in repr(scrubbed)
    assert "opaque" not in repr(scrubbed)
    assert "query-secret" not in repr(scrubbed)
    assert scrubbed["nested"][0] == {"kind": "live_mcp", "mcp_url": "https://tools.example/mcp"}
    assert scrubbed["nested"][1] == {"kind": "remote", "url": "https://evaluator.example/score"}


def test_model_config_credentials_are_removed_recursively_without_touching_dataset_columns(
    harness: _Harness,
) -> None:
    """Strip nested model credentials at persistence and outbound boundaries."""
    binding_id = harness.execution_budget(key="model-config-scrub")
    payload = {
        "model_config": {
            "name": "fixture/model",
            "base_url": "https://provider.example/v1?api_key=url-secret",
            "extra": {
                "api_key": "api-secret",
                "nested": {
                    "Access-Token": "access-secret",
                    "CLIENT_SECRET": "client-secret",
                    "apiKey": "camel-api-secret",
                    "accessToken": "camel-access-secret",
                    "clientSecret": "camel-client-secret",
                    "refreshToken": "refresh-secret",
                    "gatewayToken": "gateway-secret",
                    "password": "password-secret",
                    "Authorization": "Bearer authorization-secret",
                },
                "temperature": 0.2,
            },
        },
        "dataset": [
            {
                "api_key": "dataset-api-value",
                "password": "dataset-password-value",
            }
        ],
    }

    persisted = protect_execution_credentials(
        payload,
        username="alice",
        binding_id=binding_id,
        vault=ProtectedCredentialVault(engine=harness.engine),
    )
    exposed = scrub_execution_credentials(payload)

    for result in (persisted, exposed):
        serialized = repr(result["model_config"])
        assert all(
            secret not in serialized
            for secret in (
                "url-secret",
                "api-secret",
                "access-secret",
                "client-secret",
                "camel-api-secret",
                "camel-access-secret",
                "camel-client-secret",
                "refresh-secret",
                "gateway-secret",
                "password-secret",
                "authorization-secret",
            )
        )
        assert result["model_config"]["base_url"] == "https://provider.example/v1"
        assert result["model_config"]["extra"] == {"nested": {}, "temperature": 0.2}
        assert result["dataset"] == payload["dataset"]


@pytest.mark.parametrize(
    "location",
    [
        "model_config",
        "task_model_config",
        "reflection_model_config",
        "scorer",
        "generation_models",
        "reflection_models",
    ],
)
def test_resolve_every_executable_role_without_mutating_persisted_inputs(harness: _Harness, location: str) -> None:
    """Resolve only the owner's verified key for every supported model-role shape.

    Args:
        harness: Local encrypted vault fixture.
        location: Model role or collection appearing in the request.
    """
    harness.store(params={"organization": "saved-org", "api_key": "forged-saved", "model": "wrong-model"})
    harness.store(username="bob", secret="fixture-bob-key")
    config = {
        "name": "openrouter/openai/test-model",
        "base_url": "https://inline.invalid/v1",
        "api_key": "top-level-inline",
        "extra": {
            "api_key": "inline",
            "api_base": "https://override.invalid/v1",
            "headers": {"Authorization": "Bearer inline"},
            "extra_headers": {"Authorization": "Bearer inline"},
            "_skynet_budget_route": {"token": "forged"},
            "model": "another-model",
            "temperature": 0.2,
        },
    }
    value = {"model": config} if location == "scorer" else [config] if location.endswith("models") else config
    payload = {"token_source": "byok", location: value}
    original = copy.deepcopy(payload)

    resolved = prepare_protected_credentials(payload, username="alice", vault=harness.vault)
    actual = resolved[location]
    actual = actual["model"] if location == "scorer" else actual[0] if location.endswith("models") else actual

    assert payload == original
    assert actual["base_url"] == OPENROUTER_API_BASE
    assert actual["byok_provider"] == "openrouter"
    assert actual["token_source"] == "byok"
    assert actual["name"] == config["name"]
    assert "api_key" not in actual
    assert actual["extra"] == {"api_key": "fixture-alice-key", "organization": "saved-org", "temperature": 0.2}


@pytest.mark.parametrize("status", ["unverified", "invalid"])
def test_reject_unverified_owner_keys_even_when_another_owner_is_verified(harness: _Harness, status: str) -> None:
    """Prevent an invalid owner connection from falling back to another account's key.

    Args:
        harness: Local encrypted vault fixture.
        status: Non-verified connection state.
    """
    harness.store(status=status)
    harness.store(username="bob", secret="fixture-bob-key")
    payload = {"model_config": {"name": "openrouter/test/model", "token_source": "byok"}}

    with pytest.raises(ValueError, match="No verified openrouter connection"):
        prepare_protected_credentials(payload, username="alice", vault=harness.vault)
    assert harness.vault.resolve_connection("alice", "openrouter") is not None
    assert harness.vault.resolve_connection("alice", "openrouter", verified_only=True) is None


@pytest.mark.parametrize("provider", sorted(UPSTREAMS))
def test_verified_no_base_provider_resolves_to_upstream(harness: _Harness, provider: str) -> None:
    """Resolve every built-in provider without a saved custom base."""
    harness.store(provider=provider)
    prefix = next(prefix for slug, prefix in BYOK_PROVIDER_SLUGS if slug == provider)
    payload = {"model_config": {"name": f"{prefix}/fixture", "token_source": "byok"}}
    config = prepare_protected_credentials(payload, username="alice", vault=harness.vault)["model_config"]
    assert config["extra"]["api_key"] == "fixture-alice-key"
    assert resolve_upstream(config) == UPSTREAMS[provider]


def test_saved_custom_endpoint_is_preserved_for_explicit_adapter_validation(harness: _Harness) -> None:
    """Resolve a custom connection without substituting the managed provider or authorizing spending."""
    harness.store(provider="custom", secret="fixture-custom", api_base="https://private.example/v1")
    payload = {
        "model_config": {"name": "openai/private-model", "token_source": "byok", "byok_provider": "custom"},
        "reflection_model_config": {
            "name": "openrouter/test/model",
            "token_source": "managed",
            "extra": {"api_key": "inline"},
        },
    }

    resolved = prepare_protected_credentials(payload, username="alice", vault=harness.vault)

    assert resolved["model_config"]["base_url"] == "https://private.example/v1"
    assert resolved["model_config"]["extra"]["api_key"] == "fixture-custom"
    assert resolved["reflection_model_config"]["extra"] == {}


def test_managed_custom_endpoint_cannot_be_silently_rerouted(harness: _Harness) -> None:
    """Preserve the existing refusal to replace an explicit managed custom endpoint."""
    payload = {"model_config": {"name": "openai/private-model", "base_url": "https://private.example/v1"}}
    with pytest.raises(ValueError, match="cannot silently replace"):
        prepare_protected_credentials(payload, username="alice", vault=harness.vault)


def test_bare_byok_model_requires_an_explicit_provider_reference(harness: _Harness) -> None:
    """Refuse an ambiguous BYOK model even when the account has a default-looking key."""
    harness.store()
    with pytest.raises(ValueError, match="identify its saved provider"):
        prepare_protected_credentials(
            {"model_config": {"name": "bare-model", "token_source": "byok"}}, username="alice", vault=harness.vault
        )


def test_remote_evaluator_ignores_an_unused_python_scorer_model(harness: _Harness) -> None:
    """Avoid resolving a stale Python judge when only a remote evaluator will execute."""
    payload = {
        "scorer": {
            "kind": "remote",
            "url": "https://evaluator.example/score",
            "model": {"name": "openrouter/test/model", "token_source": "byok"},
        }
    }
    assert prepare_protected_credentials(payload, username="alice", vault=harness.vault) == payload


def test_reconciliation_accepts_verified_owner_alias_for_the_same_provider_endpoint(harness: _Harness) -> None:
    """Find the original key stored as a custom connection to the canonical OpenRouter API."""
    harness.store(provider="custom", api_base=OPENROUTER_API_BASE)
    assert (
        resolve_current_openrouter_key("alice", json_fingerprint("fixture-alice-key"), vault=harness.vault)
        == "fixture-alice-key"
    )


def test_reconciliation_matches_current_managed_or_verified_owner_key(harness: _Harness) -> None:
    """Keep managed and BYOK receipt lookup bound to the original credential digest."""
    harness.store()
    harness.store(username="bob", secret="fixture-bob-key")
    assert (
        resolve_current_openrouter_key(
            "alice", json_fingerprint("fixture-managed"), vault=harness.vault, managed_key="fixture-managed"
        )
        == "fixture-managed"
    )
    assert (
        resolve_current_openrouter_key(
            "alice", json_fingerprint("fixture-alice-key"), vault=harness.vault, managed_key="fixture-managed"
        )
        == "fixture-alice-key"
    )
    assert resolve_current_openrouter_key("alice", json_fingerprint("fixture-bob-key"), vault=harness.vault) is None


@pytest.mark.parametrize("change", ["rotate", "remove", "invalidate", "unverifiable", "custom_endpoint"])
def test_reconciliation_retains_coverage_when_original_key_is_unavailable(harness: _Harness, change: str) -> None:
    """Return no substitute credential after a connection changes or becomes unusable.

    Args:
        harness: Local encrypted vault fixture.
        change: Mutation that invalidates original-key reconciliation.
    """
    harness.store()
    digest = json_fingerprint("fixture-alice-key")
    with Session(harness.engine) as session:
        row = session.scalar(select(BillingProviderKeyModel).where(BillingProviderKeyModel.username == "alice"))
        if change == "rotate":
            row.secret_ciphertext = harness.cipher.encrypt(b"fixture-rotated")
        elif change == "remove":
            session.delete(row)
        elif change == "invalidate":
            row.status = "invalid"
        elif change == "unverifiable":
            row.secret_ciphertext = b"cannot-decrypt"
        else:
            row.api_base = "https://private.example/v1"
        session.commit()

    assert resolve_current_openrouter_key("alice", digest, vault=harness.vault, managed_key="unrelated") is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://openrouter.ai/api/v1",
        "https://openrouter.ai/other-api",
        "https://openrouter.ai.evil.invalid/api/v1",
        "https://key@openrouter.ai/api/v1",
    ],
)
def test_reconciliation_refuses_noncanonical_saved_endpoints(harness: _Harness, endpoint: str) -> None:
    """Do not reinterpret a different saved service as the provider that owns the original receipt.

    Args:
        harness: Local encrypted vault fixture.
        endpoint: Lookalike or noncanonical saved endpoint.
    """
    harness.store(api_base=endpoint)
    assert resolve_current_openrouter_key("alice", json_fingerprint("fixture-alice-key"), vault=harness.vault) is None


def _repo_payload(secrets: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a black-box payload whose repository target carries ``secrets``.

    Args:
        secrets: Secret entries for the target.

    Returns:
        The payload.
    """
    return {"target": {"kind": "repo", "repo": {"repository": "acme/app", "secrets": secrets}}}


def test_repo_secrets_are_vaulted_at_submit_and_resolved_only_in_the_parent(harness: _Harness) -> None:
    """Keep inline values out of the stored payload, resolve them with saved secrets, and scrub every trace."""
    vault = ProtectedCredentialVault(engine=harness.engine)
    binding_id = harness.execution_budget(key="repo-secrets")
    saved = SavedSecretStore(harness.engine).save("alice", "NPM_TOKEN", "npm-saved-value")
    payload = _repo_payload(
        [{"name": "API_KEY", "value": "inline-api-value"}, {"name": "NPM_TOKEN", "saved_secret_id": saved.id}]
    )

    stored = protect_execution_credentials(payload, username="alice", binding_id=binding_id, vault=vault)

    assert "inline-api-value" not in repr(stored)
    inline = stored["target"]["repo"]["secrets"][0]
    assert set(inline) == {"name", REPO_SECRET_REF_FIELD, REPO_SECRET_REVISION_FIELD}
    assert resolve_repo_secrets(
        stored, username="alice", binding_id=binding_id, vault=vault, saved=SavedSecretStore(harness.engine)
    ) == {"API_KEY": "inline-api-value", "NPM_TOKEN": "npm-saved-value"}
    scrubbed = scrub_execution_credentials(stored)
    assert REPO_SECRET_REF_FIELD not in repr(scrubbed)
    assert has_exposed_execution_credentials(stored)


def test_repo_secret_without_value_or_reference_is_refused(harness: _Harness) -> None:
    """Ask for the value again rather than running with a secret that silently went missing."""
    vault = ProtectedCredentialVault(engine=harness.engine)
    binding_id = harness.execution_budget(key="repo-secret-missing")

    with pytest.raises(ValueError, match="API_KEY"):
        protect_execution_credentials(
            _repo_payload([{"name": "API_KEY"}]), username="alice", binding_id=binding_id, vault=vault
        )


def test_local_subscription_model_passes_without_provider_credentials(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a local subscription role on its CLI and drop any endpoint or key it carries.

    Args:
        harness: Encrypted vault with no saved connections.
        monkeypatch: Turns local mode on.
    """
    monkeypatch.setattr(settings, "local_mode", True)
    payload = {
        "model_config": {
            "name": "anthropic/claude-haiku-4.5",
            "token_source": "subscription",
            "cli_provider": "claude",
            "base_url": "https://openrouter.ai/api/v1",
            "byok_provider": "openrouter",
            "extra": {"api_key": "fixture-leak", "temperature": 0},
        }
    }

    resolved = prepare_protected_credentials(payload, username="alice", vault=harness.vault)

    assert resolved["model_config"] == {
        "name": "anthropic/claude-haiku-4.5",
        "token_source": "subscription",
        "cli_provider": "claude",
        "extra": {"temperature": 0},
    }


def test_hosted_subscription_model_is_refused(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a subscription role outside local mode, where no CLI can serve it.

    Args:
        harness: Encrypted vault with no saved connections.
        monkeypatch: Keeps local mode off.
    """
    monkeypatch.setattr(settings, "local_mode", False)
    payload = {"model_config": {"name": "anthropic/claude-haiku-4.5", "token_source": "subscription"}}

    with pytest.raises(ValueError, match="only in local mode"):
        prepare_protected_credentials(payload, username="alice", vault=harness.vault)


def test_repo_secret_allowed_hosts_reads_each_secret_list() -> None:
    """Return each secret's allowed hosts, an empty tuple when it has none, and nothing without a repository."""
    payload = {
        "target": {
            "repo": {
                "repository": "acme/bot",
                "secrets": [
                    {"name": "JUDGE_KEY", "allowed_hosts": ["api.judge.example", "*.judge.example"]},
                    {"name": "OTHER_KEY"},
                ],
            }
        }
    }

    assert repo_secret_allowed_hosts(payload) == {
        "JUDGE_KEY": ("api.judge.example", "*.judge.example"),
        "OTHER_KEY": (),
    }
    assert repo_secret_allowed_hosts({"target": {"kind": "prompt"}}) == {}


def test_repo_secret_allowed_hosts_survive_vaulting(harness: _Harness) -> None:
    """Keep each secret's allowed hosts next to its reference, so the setup check can read them (E52)."""
    vault = ProtectedCredentialVault(engine=harness.engine)
    binding_id = harness.execution_budget(key="repo-secret-hosts")
    payload = _repo_payload([{"name": "API_KEY", "value": "inline-api-value", "allowed_hosts": ["api.example.com"]}])

    stored = protect_execution_credentials(payload, username="alice", binding_id=binding_id, vault=vault)

    assert repo_secret_allowed_hosts(stored) == {"API_KEY": ("api.example.com",)}


def test_repo_secret_allowed_hosts_are_cleaned() -> None:
    """Lower-case each host, drop a trailing dot and repeats, and default to no host."""
    secret = BlackboxRepoSecret(
        name="API_KEY", value="v", allowed_hosts=[" API.Example.com. ", "api.example.com", "*.Example.com"]
    )

    assert secret.allowed_hosts == ["api.example.com", "*.example.com"]
    assert BlackboxRepoSecret(name="API_KEY", value="v").allowed_hosts == []


@pytest.mark.parametrize(
    "hosts",
    [
        ["*.com"],
        ["localhost"],
        ["https://api.example.com"],
        ["api.example.com/v1"],
        ["*.*.example.com"],
        ["a.example.com"] * 21,
    ],
)
def test_repo_secret_allowed_hosts_refuse_a_bad_entry(hosts: list[str]) -> None:
    """Refuse a top-level-domain wildcard, a bare name, a url, a path, a double wildcard and more than 20 entries."""
    with pytest.raises(ValidationError):
        BlackboxRepoSecret(name="API_KEY", value="v", allowed_hosts=hosts)

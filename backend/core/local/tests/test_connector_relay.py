"""Tests for OAuth connectors that the Mac app copies from the linked hosted account (SKY-986)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from ...api.errors import DomainError
from ...api.routers.connectors import connector_secret
from ...config import settings
from ...connectors import github, oauth
from ...connectors import huggingface as hf
from ...connectors.vault import ConnectorVault
from ...storage.models import UserConnectorModel
from .. import connector_relay, hosted_link
from .fake_hosted import API_ORIGIN, APP_ORIGIN, FakeHosted

_USER = "local@skynet.local"


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> FakeHosted:
    """Point the link client at a fresh fake hosted server in local mode.

    Args:
        monkeypatch: Pytest fixture used to change settings and the transport.

    Returns:
        The fake server.
    """
    fake = FakeHosted()
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(settings, "api_host", "127.0.0.1")
    monkeypatch.setattr(settings, "port", 8110)
    monkeypatch.setattr(settings, "hosted_app_url", APP_ORIGIN)
    monkeypatch.setattr(settings, "hosted_api_url", API_ORIGIN)
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr(hosted_link, "_TRANSPORT", fake.transport())
    hosted_link.clear_used_states()
    return fake


@pytest.fixture
def vault(hosted: FakeHosted) -> ConnectorVault:
    """Return a vault on an in-memory database, with the Mac linked to the fake.

    Args:
        hosted: The fake hosted server.

    Returns:
        The vault.
    """
    engine: Engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    UserConnectorModel.__table__.create(engine)
    url = hosted_link.start_link(_USER, device_name="Test Mac")
    _redirect, code, state = hosted.authorize(url)
    hosted_link.finish_link(engine, code=code, state=state)
    return ConnectorVault(engine)


def _hosted_token(token: str, expires_in: timedelta | None = timedelta(hours=1)) -> dict[str, object]:
    """Build the body hosted sends for one connector.

    Args:
        token: The access token.
        expires_in: Time until expiry, or ``None`` for a token without one.

    Returns:
        The JSON body.
    """
    expires_at = (datetime.now(UTC) + expires_in).isoformat() if expires_in is not None else None
    return {
        "provider": "github",
        "access_token": token,
        "expires_at": expires_at,
        "account_label": "dana",
        "scopes": "repo",
    }


def test_pull_stores_a_relay_token_without_a_refresh_token(vault: ConnectorVault, hosted: FakeHosted) -> None:
    """A pull saves the hosted access token, its label and scopes, and no refresh token.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
    """
    hosted.connectors["github"] = _hosted_token("gho_first")

    assert connector_relay.pull(vault, _USER, "github") == "gho_first"

    secret = vault.resolve(_USER, "github")
    view = vault.get(_USER, "github")
    assert secret is not None
    assert view is not None
    assert secret.auth_method == connector_relay.RELAY_AUTH_METHOD
    assert secret.refresh_token is None
    assert secret.expires_at is not None
    assert (view.account_label, view.scopes) == ("dana", "repo")


def test_pull_answers_none_when_hosted_has_no_connection(vault: ConnectorVault) -> None:
    """Hosted's 404 ``not_connected`` is an answer, not an error, and stores nothing.

    Args:
        vault: Linked local vault.
    """
    assert connector_relay.pull(vault, _USER, "github") is None
    assert vault.resolve(_USER, "github") is None


def test_fresh_relay_token_is_used_without_a_hosted_call(vault: ConnectorVault, hosted: FakeHosted) -> None:
    """A relay token that is still fresh needs no new pull.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
    """
    hosted.connectors["github"] = _hosted_token("gho_first")
    connector_relay.pull(vault, _USER, "github")
    calls = len(hosted.calls)

    assert oauth.current_oauth_token(github.oauth_app(), vault, _USER) == "gho_first"
    assert len(hosted.calls) == calls


def test_stale_relay_token_pulls_again(vault: ConnectorVault, hosted: FakeHosted) -> None:
    """A stale relay token is replaced by hosted's fresh copy.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
    """
    hosted.connectors["github"] = _hosted_token("gho_old", timedelta(seconds=10))
    connector_relay.pull(vault, _USER, "github")
    hosted.connectors["github"] = _hosted_token("gho_new")

    assert oauth.current_oauth_token(github.oauth_app(), vault, _USER) == "gho_new"
    assert connector_secret(vault, _USER, "github").access_token == "gho_new"


def test_stale_relay_token_without_hosted_connection_expires(vault: ConnectorVault, hosted: FakeHosted) -> None:
    """When hosted lost the connection, the local copy is marked invalid and the caller gets 409.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
    """
    hosted.connectors["github"] = _hosted_token("gho_old", timedelta(seconds=10))
    connector_relay.pull(vault, _USER, "github")
    del hosted.connectors["github"]

    with pytest.raises(DomainError) as caught:
        oauth.current_oauth_token(github.oauth_app(), vault, _USER)

    assert (caught.value.status_code, caught.value.code) == (409, "connectors.expired")
    view = vault.get(_USER, "github")
    assert view is not None
    assert view.status != "connected"


def test_connector_secret_reports_a_relay_token_as_oauth(vault: ConnectorVault, hosted: FakeHosted) -> None:
    """Provider code branches on ``oauth``, so a relay token must look like one.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
    """
    hosted.connectors["github"] = _hosted_token("gho_first")
    connector_relay.pull(vault, _USER, "github")

    secret = connector_secret(vault, _USER, "github")

    assert (secret.auth_method, secret.access_token, secret.account_label) == ("oauth", "gho_first", "dana")


def test_hugging_face_relay_token_refreshes_from_hosted(vault: ConnectorVault, hosted: FakeHosted) -> None:
    """Hugging Face has its own token path, with the same relay rules.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
    """
    hosted.connectors[hf.PROVIDER] = _hosted_token("hf_old", timedelta(seconds=10))
    connector_relay.pull(vault, _USER, hf.PROVIDER)
    hosted.connectors[hf.PROVIDER] = _hosted_token("hf_new")

    assert hf.current_token(vault, _USER) == "hf_new"

    hosted.connectors[hf.PROVIDER] = _hosted_token("hf_stale", timedelta(seconds=10))
    connector_relay.pull(vault, _USER, hf.PROVIDER)
    del hosted.connectors[hf.PROVIDER]
    with pytest.raises(DomainError) as caught:
        hf.current_token(vault, _USER)
    assert caught.value.code == "connectors.hf_expired"


def test_token_without_expiry_is_checked_again_after_the_relay_ttl(vault: ConnectorVault, hosted: FakeHosted) -> None:
    """A hosted disconnect must reach the Mac, also for tokens that never expire.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
    """
    hosted.connectors["github"] = _hosted_token("gho_forever", None)
    connector_relay.pull(vault, _USER, "github")
    secret = vault.resolve(_USER, "github")
    assert secret is not None
    assert secret.expires_at is not None
    assert secret.expires_at <= datetime.now(UTC) + connector_relay.RELAY_TTL

    vault.save(
        _USER,
        "github",
        access_token="gho_forever",
        auth_method=connector_relay.RELAY_AUTH_METHOD,
        account_label="dana",
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    del hosted.connectors["github"]

    with pytest.raises(DomainError) as caught:
        oauth.current_oauth_token(github.oauth_app(), vault, _USER)
    assert caught.value.code == "connectors.expired"


def test_link_to_another_account_drops_the_old_relay_tokens(vault: ConnectorVault, hosted: FakeHosted) -> None:
    """Tokens copied from account A must not survive a link to account B.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
    """
    hosted.connectors["github"] = _hosted_token("gho_account_a")
    connector_relay.pull(vault, _USER, "github")

    hosted.email = "other@example.test"
    url = hosted_link.start_link(_USER, device_name="Test Mac")
    _redirect, code, state = hosted.authorize(url)
    hosted_link.finish_link(vault.engine, code=code, state=state)

    link = hosted_link.link_status(vault.engine, _USER)
    assert link is not None
    assert link.account_label == "other@example.test"
    assert vault.resolve(_USER, "github") is None


def test_a_failed_relink_keeps_the_old_link_and_its_tokens(
    vault: ConnectorVault, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad hosted token answer must not unlink a Mac that was linked before.

    Args:
        vault: Linked local vault.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to break the hosted token answer.
    """
    hosted.connectors["github"] = _hosted_token("gho_account_a")
    connector_relay.pull(vault, _USER, "github")
    url = hosted_link.start_link(_USER, device_name="Test Mac")
    _redirect, code, state = hosted.authorize(url)
    monkeypatch.setattr(hosted, "_pair", lambda: {"access_token": "not-a-pair"})

    with pytest.raises(DomainError) as caught:
        hosted_link.finish_link(vault.engine, code=code, state=state)

    assert caught.value.code == "hosted_link.failed"
    assert hosted_link.link_status(vault.engine, _USER) is not None
    assert oauth.current_oauth_token(github.oauth_app(), vault, _USER) == "gho_account_a"

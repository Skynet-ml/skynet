"""Tests for the local hosted-link client against a fake of the hosted routes."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from ...api.errors import DomainError
from ...config import settings
from ...connectors import oauth
from ...connectors.vault import ConnectorVault, vault_cipher
from ...notifications import slack
from ...storage.models import UserConnectorModel
from .. import hosted_link
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
def engine() -> Engine:
    """Return an in-memory database with the connector table.

    Returns:
        The engine.
    """
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    UserConnectorModel.__table__.create(eng)
    return eng


def _link(engine: Engine, hosted: FakeHosted) -> None:
    """Run the full link flow, as the Mac UI and the browser do.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    url = hosted_link.start_link(_USER, device_name="Test Mac")
    _redirect, code, state = hosted.authorize(url)
    hosted_link.finish_link(engine, code=code, state=state)


def test_start_builds_a_pkce_authorize_url(hosted: FakeHosted) -> None:
    """The URL opens the hosted consent page with S256 PKCE and the loopback callback.

    Args:
        hosted: The fake hosted server.
    """
    url = hosted_link.start_link(_USER, device_name="Test Mac")

    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{APP_ORIGIN}/desktop/link"
    assert query["response_type"] == "code"
    assert query["client_id"] == "skynet-desktop"
    assert query["redirect_uri"] == "http://127.0.0.1:8110/local/hosted-link/callback"
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43
    assert query["scope"] == "identity balance:read sync models connectors web"
    assert query["device_name"] == "Test Mac"
    assert "v" not in query
    assert len(query["state"]) > 40


def test_start_with_app_callback_uses_the_app_url(engine: Engine, hosted: FakeHosted) -> None:
    """The Mac app's flow sends the result to ``skynet://link/callback`` and the exchange repeats it.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    url = hosted_link.start_link(_USER, device_name="Test Mac", app_callback=True)

    redirect, code, state = hosted.authorize(url)
    assert redirect == "skynet://link/callback"
    view = hosted_link.finish_link(engine, code=code, state=state)
    assert view.account_label == "dana@example.test"


def test_ipv6_loopback_uses_the_bracket_form(hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch) -> None:
    """An API bound on ``::1`` gets the ``[::1]`` callback the hosted rule accepts.

    Args:
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to change the bind host.
    """
    monkeypatch.setattr(settings, "api_host", "::1")

    assert hosted_link.callback_uri() == "http://[::1]:8110/local/hosted-link/callback"


def test_link_saves_the_pair_encrypted(engine: Engine, hosted: FakeHosted) -> None:
    """The callback trades the code and keeps the pair only as ciphertext.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)

    status = hosted_link.link_status(engine, _USER)
    assert status is not None
    assert status.account_label == "dana@example.test"
    assert status.scopes == hosted.scopes
    secret = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert secret is not None
    assert secret.access_token in hosted.access
    assert secret.refresh_token in hosted.refresh
    assert secret.auth_method == "desktop_link"
    with engine.connect() as conn:
        raw = conn.exec_driver_sql("SELECT secret_ciphertext, refresh_ciphertext FROM user_connectors").one()
    assert secret.access_token.encode() not in raw[0]
    assert secret.refresh_token.encode() not in raw[1]


def test_state_is_single_use(engine: Engine, hosted: FakeHosted) -> None:
    """A replayed callback is refused before any call to hosted.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    url = hosted_link.start_link(_USER, device_name="Test Mac")
    _redirect, code, state = hosted.authorize(url)
    hosted_link.finish_link(engine, code=code, state=state)
    calls = len(hosted.calls)

    with pytest.raises(DomainError) as exc:
        hosted_link.finish_link(engine, code=code, state=state)

    assert exc.value.code == "hosted_link.state_invalid"
    assert len(hosted.calls) == calls


def test_forged_state_is_refused(engine: Engine, hosted: FakeHosted) -> None:
    """A state that the local vault did not encrypt fails.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    with pytest.raises(DomainError) as exc:
        hosted_link.finish_link(engine, code="abc", state="not-a-state")

    assert exc.value.code == "hosted_link.state_invalid"
    assert hosted.calls == []


def test_expired_state_is_refused(engine: Engine, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch) -> None:
    """A state older than 600 seconds fails.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to shorten the TTL.
    """
    url = hosted_link.start_link(_USER, device_name="Test Mac")
    _redirect, code, state = hosted.authorize(url)
    monkeypatch.setattr(hosted_link, "STATE_TTL_SECONDS", -1)

    with pytest.raises(DomainError) as exc:
        hosted_link.finish_link(engine, code=code, state=state)

    assert exc.value.code == "hosted_link.state_invalid"


def test_code_from_another_flow_fails_the_verifier(engine: Engine, hosted: FakeHosted) -> None:
    """An injected code from another PKCE flow fails at hosted, and nothing is saved.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _redirect, attacker_code, _state = hosted.authorize(hosted_link.start_link(_USER, device_name="x"))
    victim_url = hosted_link.start_link(_USER, device_name="Test Mac")
    _redirect, _code, victim_state = hosted.authorize(victim_url)

    with pytest.raises(DomainError) as exc:
        hosted_link.finish_link(engine, code=attacker_code, state=victim_state)

    assert exc.value.code == "hosted_link.failed"
    assert hosted_link.link_status(engine, _USER) is None


def test_start_needs_a_hosted_api_url(hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch) -> None:
    """A build with no hosted API URL refuses to start a link.

    Args:
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to clear the URL.
    """
    monkeypatch.setattr(settings, "hosted_api_url", "")

    with pytest.raises(DomainError) as exc:
        hosted_link.start_link(_USER, device_name="Test Mac")

    assert exc.value.code == "hosted_link.not_configured"
    assert exc.value.status_code == 503


def test_fresh_token_is_used_without_refresh(engine: Engine, hosted: FakeHosted) -> None:
    """A token with more than 5 minutes left goes out as is.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)

    assert hosted_link.hosted_get(engine, _USER, "/desktop/v1/balance")["available_cents"] == 1234
    assert hosted.refresh_count == 0


def test_token_near_expiry_is_refreshed_and_rotated(engine: Engine, hosted: FakeHosted) -> None:
    """Less than 5 minutes left: the client refreshes and saves the new pair.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    hosted.access_ttl = 120
    _link(engine, hosted)
    old = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert old is not None

    token = hosted_link.access_token(engine, _USER)

    new = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert new is not None
    assert hosted.refresh_count == 1
    assert token == new.access_token != old.access_token
    assert new.refresh_token != old.refresh_token
    assert hosted.refresh[old.refresh_token].used_at is not None


def test_token_expired_answer_refreshes_once(engine: Engine, hosted: FakeHosted) -> None:
    """A 401 ``desktop.token_expired`` makes one refresh and one retry.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    secret = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert secret is not None
    hosted.expired_access.add(secret.access_token)

    who = hosted_link.hosted_get(engine, _USER, "/desktop/v1/whoami")

    assert who["email"] == "dana@example.test"
    assert hosted.refresh_count == 1


def test_waiting_caller_reads_the_new_pair(engine: Engine, hosted: FakeHosted) -> None:
    """A caller that comes after a refresh with the old token reads the new pair (sequential).

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    hosted.access_ttl = 120
    _link(engine, hosted)
    stale = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert stale is not None
    first = hosted_link.access_token(engine, _USER)
    hosted.access_ttl = 3600

    second = hosted_link.refresh_after(engine, _USER, stale_access_token=stale.access_token)

    assert second == first
    assert hosted.refresh_count == 1


def test_revoked_link_clears_local_data(engine: Engine, hosted: FakeHosted) -> None:
    """When hosted says the link is revoked, the Mac deletes its link data.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    vault = ConnectorVault(engine)
    vault.save(_USER, "google_drive", access_token="ya29.fake", auth_method="oauth_relay", account_label="d")
    vault.save(_USER, "notion", access_token="secret_fake", auth_method="token", account_label="n")
    hosted.revoked = True

    with pytest.raises(DomainError) as exc:
        hosted_link.hosted_get(engine, _USER, "/desktop/v1/whoami")

    assert exc.value.code == "hosted_link.revoked"
    assert exc.value.status_code == 401
    assert hosted_link.link_status(engine, _USER) is None
    assert vault.get(_USER, "google_drive") is None
    assert vault.get(_USER, "notion") is not None


def test_offline_is_a_clear_error(engine: Engine, hosted: FakeHosted) -> None:
    """No network: 503 ``hosted_link.unreachable``, and the link stays.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    hosted.reachable = False

    with pytest.raises(DomainError) as exc:
        hosted_link.hosted_get(engine, _USER, "/desktop/v1/balance")

    assert exc.value.code == "hosted_link.unreachable"
    assert exc.value.status_code == 503
    assert hosted_link.link_status(engine, _USER) is not None


def test_not_linked_is_a_clear_error(engine: Engine, hosted: FakeHosted) -> None:
    """Calls before a link fail with 409 ``hosted_link.not_linked``.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    with pytest.raises(DomainError) as exc:
        hosted_link.access_token(engine, _USER)

    assert exc.value.code == "hosted_link.not_linked"
    assert exc.value.status_code == 409


def test_unlink_revokes_and_deletes(engine: Engine, hosted: FakeHosted) -> None:
    """Unlink sends the refresh token to revoke, then deletes the local link.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    secret = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert secret is not None

    assert hosted_link.unlink(engine, _USER) is True

    assert hosted.revoke_bodies == [{"token": secret.refresh_token}]
    assert hosted_link.link_status(engine, _USER) is None


def test_unlink_offline_still_deletes_local_data(engine: Engine, hosted: FakeHosted) -> None:
    """Offline unlink deletes the local data and reports that hosted was not told.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    hosted.reachable = False

    assert hosted_link.unlink(engine, _USER) is False
    assert hosted_link.link_status(engine, _USER) is None


@pytest.mark.parametrize("new_key", [Fernet.generate_key().decode(), None], ids=["new_vault_key", "no_vault_key"])
def test_unlink_with_an_unreadable_link_still_deletes_local_data(
    engine: Engine, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch, new_key: str | None
) -> None:
    """A stored link the vault cannot decrypt is still deleted, and hosted is not told.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to change the vault key.
        new_key: The replacement vault key, or ``None`` for no key.
    """
    _link(engine, hosted)
    monkeypatch.setattr(settings, "byok_vault_key", SecretStr(new_key) if new_key else None)

    assert hosted_link.unlink(engine, _USER) is False

    assert hosted.revoke_bodies == []
    assert hosted_link.link_status(engine, _USER) is None


def test_unlink_waits_for_a_refresh_in_flight(
    tmp_path: Path, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unlink during a refresh leaves no link behind when the refresh ends.

    Args:
        tmp_path: Folder for a file database that two threads can share.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to slow the refresh answer.
    """
    file_engine = create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    UserConnectorModel.__table__.create(file_engine)
    hosted.access_ttl = 120
    _link(file_engine, hosted)
    refresh_sent = threading.Event()
    unlink_done = threading.Event()
    post_refresh = hosted_link._post_refresh

    def slow_answer(refresh_token: str) -> httpx.Response:
        """Let hosted rotate the pair, then hold the answer while unlink runs.

        Args:
            refresh_token: The stored refresh token.

        Returns:
            The hosted response.
        """
        response = post_refresh(refresh_token)
        refresh_sent.set()
        unlink_done.wait(0.5)
        return response

    monkeypatch.setattr(hosted_link, "_post_refresh", slow_answer)

    def run_unlink() -> bool:
        """Unlink once the refresh is in flight.

        Returns:
            Whether hosted confirmed the revoke.
        """
        refresh_sent.wait(5)
        try:
            return hosted_link.unlink(file_engine, _USER)
        finally:
            unlink_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(hosted_link.access_token, file_engine, _USER)
        unlinked = pool.submit(run_unlink)
        refresh.result(timeout=10)
        assert unlinked.result(timeout=10) is True

    assert hosted_link.link_status(file_engine, _USER) is None
    assert hosted.revoked


def test_expiry_is_saved(engine: Engine, hosted: FakeHosted) -> None:
    """The saved expiry follows ``expires_in``.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    before = datetime.now(UTC)
    _link(engine, hosted)

    secret = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert secret is not None
    assert secret.expires_at is not None
    assert before + timedelta(seconds=3590) < secret.expires_at < before + timedelta(seconds=3610)


def test_tokens_never_reach_the_logs(engine: Engine, hosted: FakeHosted, caplog: pytest.LogCaptureFixture) -> None:
    """The full flow, a refresh, a revoke and an error log no token.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        caplog: Pytest fixture that captures log records.
    """
    caplog.set_level(logging.DEBUG)
    hosted.access_ttl = 120
    _link(engine, hosted)
    hosted_link.access_token(engine, _USER)
    hosted.reachable = False
    with pytest.raises(DomainError):
        hosted_link.hosted_get(engine, _USER, "/desktop/v1/balance")
    hosted.reachable = True
    hosted_link.unlink(engine, _USER)

    assert "skyl_" not in caplog.text
    for token in hosted.access | set(hosted.refresh):
        assert token not in caplog.text


def _state_from(url: str) -> str:
    """Return the ``state`` query value of an authorize URL.

    Args:
        url: An authorize URL.

    Returns:
        The state.
    """
    return parse_qs(urlsplit(url).query)["state"][0]


def test_connector_oauth_state_is_refused(engine: Engine, hosted: FakeHosted) -> None:
    """A connector OAuth state, sealed with the same vault key, is not a link state.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    app = oauth.OAuthApp(
        provider="google_drive",
        authorize_url="https://accounts.example.test/authorize",
        token_url="https://accounts.example.test/token",
        scopes="drive.readonly",
        client_id="client-id",
        client_secret=None,
        extra_authorize_params={},
    )
    state = _state_from(oauth.build_authorize_url(app, _USER, "http://127.0.0.1:8110/connectors/callback"))

    with pytest.raises(DomainError) as exc:
        hosted_link.finish_link(engine, code="abc", state=state)

    assert exc.value.code == "hosted_link.state_invalid"
    assert exc.value.status_code == 400
    assert hosted.calls == []


def test_slack_state_is_refused(engine: Engine, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Slack state has a nonce too, but it is not a link state.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to configure the Slack app.
    """
    monkeypatch.setattr(settings, "slack_client_id", "slack-client")
    monkeypatch.setattr(settings, "slack_client_secret", SecretStr("slack-secret"))
    state = _state_from(slack.build_authorize_url(_USER, "http://127.0.0.1:8110/notifications/slack/callback"))

    with pytest.raises(DomainError) as exc:
        hosted_link.finish_link(engine, code="abc", state=state)

    assert exc.value.code == "hosted_link.state_invalid"
    assert hosted.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "dict"],
        {"t": "hosted_link", "v": "x", "n": 5, "r": "http://127.0.0.1:8110/cb", "u": _USER},
        {"t": "hosted_link", "v": "", "n": "nonce", "r": "http://127.0.0.1:8110/cb", "u": _USER},
        {"v": "x", "n": "nonce", "r": "http://127.0.0.1:8110/cb", "u": _USER},
    ],
)
def test_badly_shaped_state_is_refused(engine: Engine, hosted: FakeHosted, payload: object) -> None:
    """A state with no link tag or with a wrong field type gives 400, not a crash.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        payload: A sealed payload of the wrong shape.
    """
    state = vault_cipher().encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")

    with pytest.raises(DomainError) as exc:
        hosted_link.finish_link(engine, code="abc", state=state)

    assert exc.value.code == "hosted_link.state_invalid"
    assert hosted.calls == []


def test_unknown_access_token_refreshes_and_retries(engine: Engine, hosted: FakeHosted) -> None:
    """Hosted forgot the access token (401 ``auth.invalid_token``): one refresh, one retry.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    hosted.access.clear()

    who = hosted_link.hosted_get(engine, _USER, "/desktop/v1/whoami")

    assert who["email"] == "dana@example.test"
    assert hosted.refresh_count == 1
    assert hosted_link.link_status(engine, _USER) is not None


def test_unknown_access_token_after_refresh_clears_the_link(engine: Engine, hosted: FakeHosted) -> None:
    """``auth.invalid_token`` again after a good refresh: the link is gone, so the Mac clears it.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    hosted.reject_all_access = True

    with pytest.raises(DomainError) as exc:
        hosted_link.hosted_get(engine, _USER, "/desktop/v1/balance")

    assert exc.value.code == "hosted_link.revoked"
    assert exc.value.status_code == 401
    assert hosted.refresh_count == 1
    assert hosted_link.link_status(engine, _USER) is None


def test_unknown_pair_clears_the_link(engine: Engine, hosted: FakeHosted) -> None:
    """Hosted forgot the whole pair: the refresh gets ``invalid_grant`` and the Mac clears the link.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    hosted.access.clear()
    hosted.refresh.clear()

    with pytest.raises(DomainError) as exc:
        hosted_link.hosted_get(engine, _USER, "/desktop/v1/whoami")

    assert exc.value.code == "hosted_link.revoked"
    assert hosted_link.link_status(engine, _USER) is None


def test_fake_copies_the_hosted_reuse_rules(engine: Engine, hosted: FakeHosted) -> None:
    """A refresh token sent again after the 30 s grace revokes the link, as on #782.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    secret = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert secret is not None
    now = [1000.0]
    hosted.clock = lambda: now[0]
    transport = hosted.transport()
    request = {"grant_type": "refresh_token", "refresh_token": secret.refresh_token}

    with httpx.Client(base_url=API_ORIGIN, transport=transport) as client:
        first = client.post("/desktop/link/token", json=request)
        retry = client.post("/desktop/link/token", json=request)
        now[0] += 31
        late = client.post("/desktop/link/token", json=request)

    assert first.status_code == retry.status_code == 200
    assert first.json()["access_token"] not in hosted.access
    assert late.status_code == 401
    assert late.json()["code"] == "desktop.link_revoked"
    assert hosted.reuse_notices == 1


def test_parallel_callers_refresh_once(tmp_path: Path, hosted: FakeHosted) -> None:
    """Two callers that find the token old at the same time send one refresh, and the link lives.

    Args:
        tmp_path: Folder for a file database that two threads can share.
        hosted: The fake hosted server.
    """
    file_engine = create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    UserConnectorModel.__table__.create(file_engine)
    hosted.access_ttl = 120
    _link(file_engine, hosted)
    hosted.access_ttl = 3600
    hosted.refresh_delay = 0.3

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _: hosted_link.access_token(file_engine, _USER), range(2)))

    hosted.refresh_delay = 0.0
    assert tokens[0] == tokens[1]
    assert hosted.refresh_count == 1
    assert hosted.reuse_notices == 0
    assert hosted_link.hosted_get(file_engine, _USER, "/desktop/v1/whoami")["email"] == "dana@example.test"


def test_lost_refresh_response_is_sent_again_inside_the_grace(engine: Engine, hosted: FakeHosted) -> None:
    """Hosted rotates the pair but the answer is lost: one resend gets a working pair, with no theft notice.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    hosted.access_ttl = 120
    _link(engine, hosted)
    old = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert old is not None
    hosted.access_ttl = 3600
    hosted.lose_refresh_responses = 1

    token = hosted_link.access_token(engine, _USER)

    new = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert new is not None
    assert token == new.access_token != old.access_token
    assert new.refresh_token != old.refresh_token
    assert hosted.refresh_count == 2
    assert hosted.reuse_notices == 0
    assert hosted_link.hosted_get(engine, _USER, "/desktop/v1/whoami")["email"] == "dana@example.test"
    assert hosted_link.access_token(engine, _USER) == token
    assert hosted.reuse_notices == 0
    assert not hosted.revoked


def test_both_refresh_attempts_end_inside_the_grace(engine: Engine, hosted: FakeHosted) -> None:
    """The worst-case time of the first attempt plus the resend stays under the hosted 30 s grace.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    hosted.access_ttl = 120
    _link(engine, hosted)
    hosted.lose_refresh_responses = 1
    hosted.refresh_timeouts.clear()

    hosted_link.access_token(engine, _USER)

    first, retry = hosted.refresh_timeouts
    assert first["read"] is not None
    assert all(value is not None for value in retry.values())
    # The deadline counts from when the first body is sent, and hosted starts the
    # grace only after it has that body. httpcore applies the connect timeout to
    # the TCP connect and again to the TLS handshake. The resend reaches hosted
    # after connect + TLS + write; its read time does not matter to hosted.
    assert hosted_link.REFRESH_RESEND_DEADLINE + 2 * retry["connect"] + retry["write"] < 30.0


class _DropFirstAnswer:
    """A real HTTP/1.1 socket server that reads each request and drops the first answer.

    It closes the first connection after it reads the whole request, as when
    hosted commits a rotation and the answer is lost. It answers later requests.
    """

    def __init__(self) -> None:
        """Listen on a free loopback port and serve in a background thread."""
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.bodies: list[dict[str, str]] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        """Read whole requests until the socket closes."""
        while True:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if b"\r\n\r\n" not in data:
                    continue
                head, _sep, rest = data.partition(b"\r\n\r\n")
                match = re.search(rb"content-length: *(\d+)", head, re.IGNORECASE)
                length = int(match.group(1)) if match else 0
                while len(rest) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                if len(rest) < length:
                    continue
                self.bodies.append(json.loads(rest))
                if len(self.bodies) > 1:
                    payload = b'{"ok": true}'
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(payload) + payload
                    )

    def close(self) -> None:
        """Stop the server."""
        # On Linux, close() alone does not wake a thread blocked in accept().
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.thread.join(timeout=5)


@pytest.mark.parametrize(
    ("ticks", "resent"),
    [
        # A slow DNS lookup and connect (13 s) before the body is sent, then a lost answer 10 s later.
        ([0.0, 13.0, 23.0], True),
        # A fast connect, then a lost answer after the resend deadline has passed.
        ([0.0, 1.0, 1.0 + hosted_link.REFRESH_RESEND_DEADLINE + 0.5], False),
    ],
)
def test_resend_deadline_counts_from_the_sent_request(
    monkeypatch: pytest.MonkeyPatch, ticks: list[float], resent: bool
) -> None:
    """The resend deadline counts from when the real HTTP client sent the whole first request.

    Hosted starts the 30 s grace only when it has the request, so time spent on
    the connect must not stop the resend.

    Args:
        monkeypatch: Pytest fixture used to point the client at the server and move its clock.
        ticks: Client clock values: start, first body sent, failure seen.
        resent: Whether the client must send the refresh grant again.
    """
    server = _DropFirstAnswer()
    try:
        monkeypatch.setattr(settings, "hosted_api_url", server.url)
        monkeypatch.setattr(hosted_link, "_TRANSPORT", None)
        clock = iter(ticks)
        monkeypatch.setattr(hosted_link, "_clock", lambda: next(clock))

        if resent:
            assert hosted_link._post_refresh("skyl_rt_old").json() == {"ok": True}
            assert len(server.bodies) == 2
            assert server.bodies[0] == server.bodies[1]
        else:
            with pytest.raises(DomainError) as exc:
                hosted_link._post_refresh("skyl_rt_old")
            assert exc.value.code == "hosted_link.unreachable"
            assert len(server.bodies) == 1
    finally:
        server.close()


def test_late_lost_refresh_response_is_not_sent_again(
    engine: Engine, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first attempt that fails after the resend deadline makes no resend, so no late reuse.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to move the client clock.
    """
    hosted.access_ttl = 120
    _link(engine, hosted)
    hosted.lose_refresh_responses = 1
    ticks = iter([0.0, hosted_link.REFRESH_RESEND_DEADLINE + 1.0])
    monkeypatch.setattr(hosted_link, "_clock", lambda: next(ticks))

    with pytest.raises(DomainError) as exc:
        hosted_link.access_token(engine, _USER)

    assert exc.value.code == "hosted_link.unreachable"
    assert hosted.refresh_count == 1
    assert hosted.reuse_notices == 0


def test_connect_error_on_refresh_is_not_sent_again(
    engine: Engine, hosted: FakeHosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect error means hosted never got the request, so the client sends it only once.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
        monkeypatch: Pytest fixture used to swap the transport.
    """
    hosted.access_ttl = 120
    _link(engine, hosted)
    attempts: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        """Count one attempt and fail to connect.

        Args:
            request: The outgoing request.

        Raises:
            httpx.ConnectError: Always.
        """
        attempts.append(request.url.path)
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(hosted_link, "_TRANSPORT", httpx.MockTransport(refuse))

    with pytest.raises(DomainError) as exc:
        hosted_link.access_token(engine, _USER)

    assert exc.value.code == "hosted_link.unreachable"
    assert attempts == ["/desktop/link/token"]
    assert hosted_link.link_status(engine, _USER) is not None


def test_two_lost_refresh_responses_are_a_clear_error(engine: Engine, hosted: FakeHosted) -> None:
    """When the resend also loses its answer, the client stops with 503 and keeps the link.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    hosted.access_ttl = 120
    _link(engine, hosted)
    hosted.lose_refresh_responses = 2

    with pytest.raises(DomainError) as exc:
        hosted_link.access_token(engine, _USER)

    assert exc.value.code == "hosted_link.unreachable"
    assert hosted.refresh_count == 2
    assert hosted_link.link_status(engine, _USER) is not None


def test_hosted_request_posts_json_and_accepts_empty_answer(engine: Engine, hosted: FakeHosted) -> None:
    """A POST sends the JSON body after one refresh; a 204 DELETE answers ``None``.

    Args:
        engine: Local database.
        hosted: The fake hosted server.
    """
    _link(engine, hosted)
    secret = ConnectorVault(engine).resolve(_USER, hosted_link.PROVIDER)
    assert secret is not None
    hosted.expired_access.add(secret.access_token)

    body = {"name": "T", "items": []}
    answer = hosted_link.hosted_request(engine, _USER, "POST", "/desktop/v1/echo", body)

    assert answer == {"ok": True}
    assert hosted.echo_bodies == [body]
    assert hosted.refresh_count == 1
    assert hosted_link.hosted_request(engine, _USER, "DELETE", "/desktop/v1/echo/c-1") is None

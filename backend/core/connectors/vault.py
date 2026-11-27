"""Encrypted storage for connector credentials.

Mirrors the BYOK key vault: tokens are Fernet-encrypted under
``settings.byok_vault_key`` before they reach the database and the plaintext is
dropped immediately. Reads of the masked metadata (who is linked, how, since
when) work without the key; anything that needs the token itself requires it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ..api.errors import DomainError
from ..config import settings
from ..storage.models import UserConnectorModel

STATUS_CONNECTED = "connected"
STATUS_INVALID = "invalid"


@dataclass(frozen=True)
class ConnectorView:
    """Secret-free description of one linked account, safe to return to clients."""

    provider: str
    auth_method: str
    account_label: str | None
    scopes: str | None
    status: str
    connected_at: datetime


@dataclass(frozen=True)
class ConnectorSecret:
    """Decrypted credentials of one linked account, for outbound calls only."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    auth_method: str
    # Azure Blob's OAuth link keeps the storage account it opens here; the token alone doesn't name one.
    account_label: str | None = None


def _view(row: UserConnectorModel) -> ConnectorView:
    """Project a stored row onto its secret-free view.

    Args:
        row: The persisted connector.

    Returns:
        The masked view.
    """
    return ConnectorView(
        provider=row.provider,
        auth_method=row.auth_method,
        account_label=row.account_label,
        scopes=row.scopes,
        status=row.status,
        connected_at=row.created_at,
    )


def vault_cipher() -> Fernet:
    """Return the Fernet cipher shared with the BYOK vault.

    Returns:
        The cipher used for connector tokens and OAuth state.

    Raises:
        DomainError: 503 when no vault key is configured.
    """
    if settings.byok_vault_key is None:
        raise DomainError("connectors.vault_not_configured", status=503)
    return Fernet(settings.byok_vault_key.get_secret_value().encode("utf-8"))


class ConnectorVault:
    """Owner-scoped CRUD over encrypted connector credentials."""

    def __init__(self, engine: Engine) -> None:
        """Bind the vault to the engine holding ``user_connectors``.

        Args:
            engine: SQLAlchemy engine for the application database.
        """
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """Return the engine the vault reads and writes."""
        return self._engine

    @staticmethod
    def _row(session: Session, username: str, provider: str) -> UserConnectorModel | None:
        """Fetch the user's row for a provider inside ``session``.

        Args:
            session: Open session to query in.
            username: Owner of the connector.
            provider: Provider slug.

        Returns:
            The row, or ``None`` when the user has not linked that provider.
        """
        return (
            session.query(UserConnectorModel)
            .filter(UserConnectorModel.username == username, UserConnectorModel.provider == provider)
            .first()
        )

    def get(self, username: str, provider: str) -> ConnectorView | None:
        """Return the masked view of a linked account, if any.

        Args:
            username: Owner of the connector.
            provider: Provider slug.

        Returns:
            The view, or ``None`` when nothing is linked.
        """
        with Session(self._engine) as session:
            row = self._row(session, username, provider)
            return _view(row) if row is not None else None

    def save(
        self,
        username: str,
        provider: str,
        *,
        access_token: str,
        auth_method: str,
        account_label: str | None,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
        scopes: str | None = None,
    ) -> ConnectorView:
        """Encrypt and store (or replace) the user's link to a provider.

        Args:
            username: Owner of the connector.
            provider: Provider slug.
            access_token: Plaintext bearer token; never persisted in the clear.
            auth_method: ``oauth`` or ``token``.
            account_label: Remote account name for display.
            refresh_token: Plaintext refresh token for OAuth links.
            expires_at: When ``access_token`` stops working, if known.
            scopes: Space-separated granted scopes, if known.

        Returns:
            The masked view of the stored link.

        Raises:
            DomainError: 503 when the vault key is unconfigured.
        """
        cipher = vault_cipher()
        secret = cipher.encrypt(access_token.encode("utf-8"))
        refresh = cipher.encrypt(refresh_token.encode("utf-8")) if refresh_token else None
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = self._row(session, username, provider)
            if row is None:
                row = UserConnectorModel(username=username, provider=provider, created_at=now)
                session.add(row)
            row.auth_method = auth_method
            row.account_label = account_label
            row.scopes = scopes
            row.secret_ciphertext = secret
            row.refresh_ciphertext = refresh
            row.token_expires_at = expires_at
            row.status = STATUS_CONNECTED
            row.updated_at = now
            session.flush()
            view = _view(row)
            session.commit()
        return view

    def remove(self, username: str, provider: str) -> None:
        """Forget the user's link to a provider; a no-op when none exists.

        Args:
            username: Owner of the connector.
            provider: Provider slug.
        """
        with Session(self._engine) as session:
            row = self._row(session, username, provider)
            if row is not None:
                session.delete(row)
                session.commit()

    def mark_invalid(self, username: str, provider: str) -> None:
        """Record that the remote rejected the stored credential.

        Args:
            username: Owner of the connector.
            provider: Provider slug.
        """
        with Session(self._engine) as session:
            row = self._row(session, username, provider)
            if row is not None:
                row.status = STATUS_INVALID
                row.updated_at = datetime.now(UTC)
                session.commit()

    def resolve(self, username: str, provider: str) -> ConnectorSecret | None:
        """Decrypt the user's stored credentials for an outbound call.

        Args:
            username: Owner of the connector.
            provider: Provider slug.

        Returns:
            The plaintext credentials, or ``None`` when nothing is linked.

        Raises:
            DomainError: 503 when the vault key is unconfigured; 409 when the
                ciphertext no longer decrypts (the vault key was rotated), in
                which case the user has to reconnect.
        """
        with Session(self._engine) as session:
            row = self._row(session, username, provider)
            if row is None:
                return None
            cipher = vault_cipher()
            try:
                access = cipher.decrypt(row.secret_ciphertext).decode("utf-8")
                refresh = cipher.decrypt(row.refresh_ciphertext).decode("utf-8") if row.refresh_ciphertext else None
            except InvalidToken as exc:
                raise DomainError("connectors.reconnect_required", status=409, provider=provider) from exc
            expires_at = row.token_expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                # SQLite hands back naive datetimes; treat them as the UTC we wrote.
                expires_at = expires_at.replace(tzinfo=UTC)
            return ConnectorSecret(
                access_token=access,
                refresh_token=refresh,
                expires_at=expires_at,
                auth_method=row.auth_method,
                account_label=row.account_label,
            )

"""Encrypted secrets a user saves on their account for reuse across runs.

Repository runs read them as environment variables inside the sandbox. Values
are Fernet-encrypted under the shared vault key before they reach the
database; listing returns names and hosts only, and decryption happens solely in the
trusted parent when a run needs the value. Each secret also keeps the hosts
the user approved for it, and a run may send it to those hosts only (SKY-641).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import InvalidToken
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ..api.errors import DomainError
from ..models.blackbox import REPO_SECRET_HOST_PATTERN, REPO_SECRET_NAME_PATTERN
from ..storage.models import UserSecretModel
from .vault import vault_cipher

MAX_SECRETS_PER_USER = 200
MAX_SECRET_BYTES = 16_384
_NAME = re.compile(REPO_SECRET_NAME_PATTERN)


@dataclass(frozen=True)
class SavedSecretView:
    """Value-free description of one saved secret, safe to return to clients."""

    id: str
    name: str
    allowed_hosts: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


def _view(row: UserSecretModel) -> SavedSecretView:
    """Project a stored row onto its value-free view.

    Args:
        row: The persisted secret.

    Returns:
        The masked view.
    """
    return SavedSecretView(
        id=row.id,
        name=row.name,
        allowed_hosts=tuple(row.allowed_hosts or ()),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def clean_allowed_hosts(hosts: Sequence[str]) -> list[str]:
    """Normalize the hosts a saved secret may go to, as a run's ``allowed_hosts`` does.

    Args:
        hosts: Exact hosts or ``*.suffix`` patterns, as given.

    Returns:
        The lower-case hosts without a trailing dot, in order, without repeats.

    Raises:
        ValueError: When an entry is not an exact host or ``*.`` and a suffix
            of two or more labels.
    """
    cleaned = [host.strip().rstrip(".").lower() for host in hosts]
    for host in cleaned:
        if len(host) > 253 or not REPO_SECRET_HOST_PATTERN.match(host):
            raise ValueError(f"Allowed host '{host}' must be a host such as api.example.com, or *.example.com.")
    return list(dict.fromkeys(cleaned))


class SavedSecretStore:
    """Owner-scoped CRUD over encrypted saved secrets."""

    def __init__(self, engine: Engine) -> None:
        """Bind the store to the engine holding ``user_secrets``.

        Args:
            engine: SQLAlchemy engine for the application database.
        """
        self._engine = engine

    def list(self, username: str) -> list[SavedSecretView]:
        """Return the user's saved secrets, without values, sorted by name.

        Args:
            username: Owner of the secrets.

        Returns:
            The masked views.
        """
        with Session(self._engine) as session:
            rows = (
                session.query(UserSecretModel)
                .filter(UserSecretModel.username == username)
                .order_by(UserSecretModel.name)
                .all()
            )
            return [_view(row) for row in rows]

    def save(self, username: str, name: str, value: str, allowed_hosts: Sequence[str] = ()) -> SavedSecretView:
        """Create the named secret, or replace its value and hosts when it exists.

        Args:
            username: Owner of the secret.
            name: Environment variable name.
            value: Plaintext value; encrypted before it is stored.
            allowed_hosts: The hosts the user approved for the secret,
                already cleaned by :func:`clean_allowed_hosts`.

        Returns:
            The masked view of the saved secret.

        Raises:
            DomainError: 422 for a bad name, an empty or oversized value, or
                once the per-user cap is reached; 503 without a vault key.
        """
        if not _NAME.match(name):
            raise DomainError("secrets.invalid_name", status=422, name=name)
        if not value or len(value.encode("utf-8")) > MAX_SECRET_BYTES:
            raise DomainError("secrets.invalid_value", status=422, limit=MAX_SECRET_BYTES)
        ciphertext = vault_cipher().encrypt(value.encode("utf-8"))
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = (
                session.query(UserSecretModel)
                .filter(UserSecretModel.username == username, UserSecretModel.name == name)
                .first()
            )
            if row is None:
                count = session.query(UserSecretModel).filter(UserSecretModel.username == username).count()
                if count >= MAX_SECRETS_PER_USER:
                    raise DomainError("secrets.limit_reached", status=422, limit=MAX_SECRETS_PER_USER)
                row = UserSecretModel(username=username, name=name, secret_ciphertext=ciphertext, created_at=now)
                session.add(row)
            else:
                row.secret_ciphertext = ciphertext
            row.allowed_hosts = list(allowed_hosts)
            row.updated_at = now
            session.commit()
            session.refresh(row)
            return _view(row)

    def remove(self, username: str, secret_id: str) -> bool:
        """Delete one of the user's saved secrets.

        Args:
            username: Owner of the secret.
            secret_id: Identifier of the secret.

        Returns:
            True when a row was deleted.
        """
        with Session(self._engine) as session:
            deleted = (
                session.query(UserSecretModel)
                .filter(UserSecretModel.username == username, UserSecretModel.id == secret_id)
                .delete()
            )
            session.commit()
            return bool(deleted)

    def resolve(self, username: str, secret_id: str, hosts: Collection[str] = ()) -> str:
        """Decrypt one saved secret for use inside a run.

        Args:
            username: Owner the secret must belong to.
            secret_id: Identifier of the secret.
            hosts: The hosts the run asks to send the secret to.

        Returns:
            The plaintext value.

        Raises:
            DomainError: 404 when the user has no such secret; 409 when the
                stored value no longer decrypts under the current vault key.
            ValueError: When the run asks for a host the user did not approve
                for the secret.
        """
        with Session(self._engine) as session:
            row = (
                session.query(UserSecretModel)
                .filter(UserSecretModel.username == username, UserSecretModel.id == secret_id)
                .first()
            )
            if row is None:
                raise DomainError("secrets.not_found", status=404)
            # The run payload names these hosts, and an agent can write the
            # optimize.toml they came from; only the user's stored list counts.
            refused = sorted(set(hosts) - set(row.allowed_hosts or ()))
            if refused:
                raise ValueError(
                    f"The saved secret '{row.name}' may not go to {', '.join(refused)}. "
                    "Save it again in Settings with that host."
                )
            ciphertext = row.secret_ciphertext
        try:
            return vault_cipher().decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise DomainError("secrets.undecryptable", status=409) from exc

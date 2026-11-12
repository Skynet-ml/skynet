"""Saved secret endpoints: named values a user reuses across repository runs.

Values go in and never come back out: every response carries names and
timestamps only. A run references a saved secret by id, and the trusted
parent decrypts it just before injecting it into the sandbox.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from ...connectors.saved_secrets import MAX_SECRET_BYTES, SavedSecretStore, SavedSecretView, clean_allowed_hosts
from ...models.blackbox import REPO_SECRET_NAME_PATTERN
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError


class SavedSecret(BaseModel):
    """One saved secret, described without its value."""

    id: str
    name: str
    allowed_hosts: list[str]
    created_at: str
    updated_at: str


class SavedSecretListResponse(BaseModel):
    """Envelope for ``GET /secrets``."""

    secrets: list[SavedSecret]


class SaveSecretRequest(BaseModel):
    """Body for ``PUT /secrets``: a value saved under an environment variable name."""

    name: str = Field(pattern=REPO_SECRET_NAME_PATTERN)
    value: str = Field(min_length=1, max_length=MAX_SECRET_BYTES)
    # The only hosts a run may send this secret to (SKY-641). The server
    # enforces this list, never the hosts a run payload or optimize.toml names.
    allowed_hosts: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_hosts")
    @classmethod
    def _clean_hosts(cls, hosts: list[str]) -> list[str]:
        """Normalize the approved hosts and refuse a bad one.

        Args:
            hosts: Exact hosts or ``*.suffix`` patterns.

        Returns:
            The cleaned hosts.
        """
        return clean_allowed_hosts(hosts)


def _public(view: SavedSecretView) -> SavedSecret:
    """Convert a store view into the response model.

    Args:
        view: Masked store view.

    Returns:
        The API representation.
    """
    return SavedSecret(
        id=view.id,
        name=view.name,
        allowed_hosts=list(view.allowed_hosts),
        created_at=view.created_at.isoformat(),
        updated_at=view.updated_at.isoformat(),
    )


def create_saved_secrets_router(*, job_store) -> APIRouter:
    """Build the saved secrets router.

    Args:
        job_store: Storage backend whose ``engine`` carries ``user_secrets``.

    Returns:
        A configured :class:`APIRouter` under ``/secrets``.
    """
    store = SavedSecretStore(job_store.engine)
    router = APIRouter()

    @router.get("/secrets", response_model=SavedSecretListResponse, summary="List the caller's saved secrets")
    def list_secrets(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> SavedSecretListResponse:
        """Return the caller's saved secrets without their values.

        Args:
            user: Authenticated caller.

        Returns:
            The secret list.
        """
        return SavedSecretListResponse(secrets=[_public(view) for view in store.list(user.username)])

    @router.put("/secrets", response_model=SavedSecret, summary="Save or replace a secret")
    def save_secret(
        body: SaveSecretRequest,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> SavedSecret:
        """Encrypt and save a secret, replacing the value of one with the same name.

        Args:
            body: Name, value and the hosts the caller approves for it.
            user: Authenticated caller.

        Returns:
            The saved secret, without its value.
        """
        return _public(store.save(user.username, body.name, body.value, body.allowed_hosts))

    @router.delete("/secrets/{secret_id}", status_code=204, summary="Delete a saved secret")
    def delete_secret(
        secret_id: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> None:
        """Delete one of the caller's saved secrets.

        Args:
            secret_id: Identifier of the secret.
            user: Authenticated caller.

        Raises:
            DomainError: 404 when the caller has no such secret.
        """
        if not store.remove(user.username, secret_id):
            raise DomainError("secrets.not_found", status=404)

    return router

"""Saved secret endpoints: named values a user reuses across repository runs.

Values go in and never come back out: every response carries names and
timestamps only. A run references a saved secret by id, and the trusted
parent decrypts it just before injecting it into the sandbox.

In local mode, two more routes import the root ``.env`` file of a local
folder (SKY-994). The server reads the file itself, so its values never
cross the API, and a name that is already saved with another value changes
only when the caller names it in ``replace``.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator

from ...connectors.env_file import EnvFile, EnvFileError, read_env_file
from ...connectors.saved_secrets import (
    MAX_SECRET_BYTES,
    MAX_SECRETS_PER_USER,
    SavedSecretStore,
    SavedSecretView,
    clean_allowed_hosts,
)
from ...models.blackbox import REPO_SECRET_NAME_PATTERN
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from .local_runtime import require_local_mode

_FOLDER_PATH_MAX = 4096
LocalEnvStatus = Literal["found", "missing", "unsafe", "too_large", "unreadable"]


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
    # enforces this list, never the hosts a run payload or ascent.toml names.
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


class LocalEnvEntry(BaseModel):
    """One name in a local folder's ``.env`` file, described without its value."""

    name: str
    # True when the name is already saved with a different value.
    clash: bool


class LocalEnvSkipped(BaseModel):
    """One ``.env`` line that the import does not use."""

    line: int
    name: str | None
    reason: str


class LocalEnvPreview(BaseModel):
    """Response of ``GET /secrets/local-env``."""

    status: LocalEnvStatus
    entries: list[LocalEnvEntry] = Field(default_factory=list)
    skipped: list[LocalEnvSkipped] = Field(default_factory=list)


class LocalEnvImportRequest(BaseModel):
    """Body for ``POST /secrets/local-env/import``."""

    path: str = Field(min_length=1, max_length=_FOLDER_PATH_MAX)
    # The clashing names whose saved value the caller wants the .env value to replace.
    replace: list[Annotated[str, Field(pattern=REPO_SECRET_NAME_PATTERN)]] = Field(
        default_factory=list, max_length=MAX_SECRETS_PER_USER
    )


class LocalEnvImportResponse(BaseModel):
    """Response of ``POST /secrets/local-env/import``."""

    status: LocalEnvStatus
    imported: list[str] = Field(default_factory=list)
    replaced: list[str] = Field(default_factory=list)
    kept: list[str] = Field(default_factory=list)
    # Every .env name that is saved now, so a run can reference each by id.
    secrets: list[SavedSecret] = Field(default_factory=list)
    skipped: list[LocalEnvSkipped] = Field(default_factory=list)


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


def _read_local_env(path: str) -> tuple[LocalEnvStatus, EnvFile | None]:
    """Read a local folder's root ``.env`` file and map a failure to a status.

    Args:
        path: Absolute path of the folder.

    Returns:
        The status and, when the file was read, its parsed content.
    """
    try:
        return "found", read_env_file(path)
    except EnvFileError as exc:
        return exc.reason, None  # type: ignore[return-value]


def _skipped(env: EnvFile) -> list[LocalEnvSkipped]:
    """Describe the skipped ``.env`` lines without their values.

    Args:
        env: Parsed ``.env`` file.

    Returns:
        The skipped lines for a response.
    """
    return [LocalEnvSkipped(line=skip.line, name=skip.name, reason=skip.reason) for skip in env.skipped]


def _clashes(store: SavedSecretStore, username: str, env: EnvFile) -> tuple[dict[str, SavedSecretView], set[str]]:
    """Find the ``.env`` names that are already saved with another value.

    Args:
        store: Saved secret store.
        username: Owner of the secrets.
        env: Parsed ``.env`` file.

    Returns:
        The caller's saved secrets by name, and the clashing names.
    """
    saved = {view.name: view for view in store.list(username)}
    clashes: set[str] = set()
    for name, value in env.values.items():
        view = saved.get(name)
        if view is None:
            continue
        try:
            current = store.resolve(username, view.id)
        except DomainError:
            # A value that no longer decrypts cannot be compared, so the
            # caller decides, as for any other clash.
            clashes.add(name)
            continue
        if not hmac.compare_digest(current.encode("utf-8"), value.encode("utf-8")):
            clashes.add(name)
    return saved, clashes


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

    @router.get(
        "/secrets/local-env",
        response_model=LocalEnvPreview,
        include_in_schema=False,
        dependencies=[Depends(require_local_mode)],
    )
    def preview_local_env(
        path: Annotated[str, Query(min_length=1, max_length=_FOLDER_PATH_MAX)],
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> LocalEnvPreview:
        """List the names in a local folder's root ``.env`` file and mark each clash.

        Args:
            path: Absolute path of the folder.
            user: Authenticated caller.

        Returns:
            The names, without values, and the skipped lines.
        """
        status, env = _read_local_env(path)
        if env is None:
            return LocalEnvPreview(status=status)
        _, clashes = _clashes(store, user.username, env)
        entries = [LocalEnvEntry(name=name, clash=name in clashes) for name in env.values]
        return LocalEnvPreview(status=status, entries=entries, skipped=_skipped(env))

    @router.post(
        "/secrets/local-env/import",
        response_model=LocalEnvImportResponse,
        include_in_schema=False,
        dependencies=[Depends(require_local_mode)],
    )
    def import_local_env(
        body: LocalEnvImportRequest,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> LocalEnvImportResponse:
        """Save a local folder's ``.env`` pairs as saved secrets.

        A new name is saved with no host. A name saved with the same value
        stays as it is. A name saved with another value changes only when
        ``replace`` names it, and then it keeps its approved hosts.

        Args:
            body: Folder path and the clashing names to replace.
            user: Authenticated caller.

        Returns:
            What the import saved, replaced and kept, and every ``.env`` name
            that is saved now.

        Raises:
            DomainError: 422 ``secrets.limit_reached`` when the new names do
                not fit under the per-user limit; nothing is saved then.
        """
        status, env = _read_local_env(body.path)
        if env is None:
            return LocalEnvImportResponse(status=status)
        saved, clashes = _clashes(store, user.username, env)
        new = [name for name in env.values if name not in saved]
        if len(saved) + len(new) > MAX_SECRETS_PER_USER:
            raise DomainError("secrets.limit_reached", status=422, limit=MAX_SECRETS_PER_USER)
        replace = set(body.replace)
        imported: list[str] = []
        replaced: list[str] = []
        kept: list[str] = []
        views: list[SavedSecretView] = []
        for name, value in env.values.items():
            view = saved.get(name)
            if view is None:
                views.append(store.save(user.username, name, value))
                imported.append(name)
            elif name in clashes and name in replace:
                views.append(store.save(user.username, name, value, view.allowed_hosts))
                replaced.append(name)
            else:
                if name in clashes:
                    kept.append(name)
                views.append(view)
        return LocalEnvImportResponse(
            status=status,
            imported=imported,
            replaced=replaced,
            kept=kept,
            secrets=[_public(view) for view in views],
            skipped=_skipped(env),
        )

    return router

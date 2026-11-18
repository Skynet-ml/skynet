"""Self-service account data export and irreversible account deletion. [INTERNAL]

Backs the GDPR/CCPA "download my data" and "delete my account" controls in
Settings. Both routes are bearer-authenticated and act only on the caller's own
identity (``user.username`` — the lowercased email every table keys on).
Deletion hard-removes the account's own content and secrets and anonymizes the
financial and audit records retention law requires be kept. Its only outside
call deletes the account's tree packs from the tree bucket; it makes no call to
Stripe or any other provider (see :mod:`core.api.account_data_service`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...service_gateway.agents.attachment_sandbox import stop_chat_runner
from ...storage.models import AgentConversationModel, UserModel
from ...storage.tree_purge import TreePurgeError
from ..account_data_service import delete_account, export_account
from ..account_export_formats import ExportFormat, render_export
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..passwords import verify_password
from .accounts import _has_password

logger = logging.getLogger(__name__)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


# Confirmation payload for irreversible account deletion.
class DeleteAccountRequest(BaseModel):
    password: str = Field(
        default="",
        description="Current password, required to confirm deletion of a local (email/password) account.",
    )


# Row-count outcome returned to the client after a deletion completes.
class AccountDeletionResult(BaseModel):
    deleted_rows: int = Field(description="Rows hard-deleted across the account's data.")
    anonymized_rows: int = Field(
        description="Retained rows (financial, audit, telemetry) whose identity link was severed."
    )


def create_account_data_router(*, job_store) -> APIRouter:
    """Build the account data export + deletion router.

    Args:
        job_store: Job-store instance whose ORM engine backs the routes.

    Returns:
        A FastAPI ``APIRouter`` exposing export + delete for the caller's own
        account.
    """
    router = APIRouter()

    @router.get(
        "/account/export",
        summary="Export all of the caller's account data as a file download",
    )
    def export_my_data(
        user: AuthenticatedUserDep,
        export_format: Annotated[
            ExportFormat,
            Query(
                alias="format",
                description="json or pkl for the whole bundle; csv, parquet or feather for a ZIP "
                "with one file per table; xlsx for one workbook with a sheet per table.",
            ),
        ] = "json",
    ) -> Response:
        """Gather every record the caller owns into a downloadable file.

        Secrets (password hash, 2FA secrets, encrypted provider keys, token
        hashes) are never included — only the non-sensitive fact that each
        exists. The response is served as a file attachment.

        Args:
            user: Authenticated caller; only their own data is gathered.
            export_format: File format to render the bundle in.

        Returns:
            The rendered export as a file attachment.
        """
        with Session(job_store.engine) as session:
            bundle = export_account(session, user.username)
        rendered = render_export(bundle, export_format, datetime.now(UTC).date().isoformat())
        return Response(
            content=rendered.content,
            media_type=rendered.media_type,
            headers={"Content-Disposition": f'attachment; filename="{rendered.filename}"'},
        )

    @router.post(
        "/account/delete",
        response_model=AccountDeletionResult,
        summary="Irreversibly delete the caller's account and all its data",
    )
    def delete_my_account(
        body: DeleteAccountRequest, user: AuthenticatedUserDep
    ) -> AccountDeletionResult:
        """Permanently delete the caller's account and everything it owns.

        Local (email/password) accounts must re-confirm with their current
        password; OAuth accounts have no password on file and are authorized by
        their bearer token alone. Financial and audit rows are anonymized rather
        than deleted, to satisfy record-retention obligations.

        Args:
            body: Confirmation payload carrying the current password.
            user: Authenticated caller whose account is deleted.

        Returns:
            Counts of hard-deleted and anonymized rows.

        Raises:
            DomainError: 401 (``accounts.invalid_credentials``) when a local
                account's confirmation password is missing or does not match.
            DomainError: 503 (``accounts.deletion_storage_unavailable``) when
                the tree bucket does not delete the account's packs. The
                database rows stay, so the account is not deleted. A batch
                delete can remove some packs before the error; a retry is
                safe and deletes the rest.
        """
        with Session(job_store.engine) as session:
            # delete_account locks this row after it deletes passkeys. Locking
            # it here first deadlocks with a passkey sign-in (R782H-1).
            row = session.get(UserModel, user.username)
            if _has_password(row) and not verify_password(body.password, str(row.password_hash)):
                raise DomainError("accounts.invalid_credentials", status=401)
            chats = session.scalars(
                select(AgentConversationModel.id).where(AgentConversationModel.username == user.username)
            ).all()
            try:
                summary = delete_account(session, user.username)
            except TreePurgeError as exc:
                logger.exception("Account deletion stopped: the tree packs of the account were not deleted")
                raise DomainError("accounts.deletion_storage_unavailable", status=503) from exc
            session.commit()
        # A deleted account's chats must not keep a paid sandbox or a Mac workspace with its files.
        for conversation_id in chats:
            stop_chat_runner(conversation_id)
        return AccountDeletionResult(
            deleted_rows=summary.deleted_rows, anonymized_rows=summary.anonymized_rows
        )

    return router

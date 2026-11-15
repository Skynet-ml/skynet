"""Reconcile covered Vercel sessions without recreating, resuming, or extending them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .budgets import BudgetConflictError, BudgetError, BudgetService, OperationSnapshot
from .operation_pricing import json_fingerprint
from .runtime import UsagePendingError
from .vercel_usage import _SCHEMA_URL, _SESSION_FIELDS, vercel_actual_usd, vercel_charge_policy


@dataclass(frozen=True)
class ReconciliationResult:
    """Describe one recovery attempt without concealing retained coverage."""

    operation_id: str
    state: str
    reason: str | None = None


@dataclass(frozen=True)
class ReconciliationPage:
    """Carry a bounded sweep and a cursor that avoids starving later attempts."""

    results: tuple[ReconciliationResult, ...]
    next_cursor: str | None


class VercelSessionUsageClient:
    """Read exact provider sessions with credentials held exclusively by the parent."""

    def __init__(self, token: str, team_id: str, *, client: httpx.Client | None = None) -> None:
        """Bind control-plane access without discovering or creating any resources.

        Args:
            token: Trusted Vercel credential.
            team_id: Team that owns the admitted session.
            client: Optional caller-owned transport, primarily for isolated tests.
        """
        self._token, self._team_id, self._client = token, team_id, client

    def __call__(self, session_id: str) -> Mapping[str, Any]:
        """Fetch one exact session's raw final metrics without SDK field filtering.

        Args:
            session_id: Provider identity saved by the original admitted attempt.

        Returns:
            Whitelisted session usage metadata.

        Raises:
            UsagePendingError: When the provider cannot supply usable usage evidence.
        """
        if self._client is not None:
            return self._read(self._client, session_id)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
            return self._read(client, session_id)

    def _read(self, client: httpx.Client, session_id: str) -> Mapping[str, Any]:
        """Perform one non-retrying authenticated metadata read.

        Args:
            client: HTTP connection scope.
            session_id: Exact admitted identity.

        Returns:
            Provider session fields required for settlement.

        Raises:
            UsagePendingError: On network failure, missing resource, or malformed evidence.
        """
        try:
            response = client.get(
                f"https://vercel.com/api/v2/sandboxes/sessions/{quote(session_id, safe='')}",
                params={"teamId": self._team_id},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=15,
                follow_redirects=False,
            )
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ValueError("Oversized provider receipt")
            payload = response.json()
            session = payload.get("session") if isinstance(payload, dict) else None
            if not isinstance(session, dict) or session.get("id") != session_id:
                raise ValueError("Missing exact provider session")
            return {name: session[name] for name in _SESSION_FIELDS if name in session}
        except (httpx.HTTPError, ValueError) as error:
            # The error text holds the request URL with the team id, so name only the failure kind.
            kind = (
                f"HTTP {int(error.response.status_code)}"
                if isinstance(error, httpx.HTTPStatusError)
                else type(error).__name__
            )
            raise UsagePendingError(
                f"Vercel final usage remains unavailable ({kind}); its reservation is retained."
            ) from error


class VercelUsageReconciler:
    """Settle stopped provider sessions using their original immutable admission."""

    def __init__(self, service: BudgetService, fetch_session: Callable[[str], Mapping[str, Any]]) -> None:
        """Bind the ledger to a trusted exact-session metadata reader.

        Args:
            service: Authoritative shared budget ledger.
            fetch_session: Authenticated control-plane reader; must never resume or create.
        """
        self.service, self.fetch_session = service, fetch_session

    def reconcile(self, operation_id: str, username: str) -> OperationSnapshot:
        """Recover final billing from durable evidence or one exact-session read.

        Args:
            operation_id: Original physical sandbox creation attempt.
            username: Authoritative owner of that attempt.

        Returns:
            Settled operation, including an already-settled idempotent replay.

        Raises:
            UsagePendingError: When identity, prices, final usage, or classification is uncertain.
            BudgetError: When evidence violates the funded bound or ownership.
        """
        record = self.service.get_reconciliation(operation_id, username)
        operation, prices = record.operation, record.price_snapshot
        if operation.cost_kind != "sandbox" or prices.get("provider") != "vercel":
            raise UsagePendingError("The operation is not a covered Vercel session.")
        if operation.state == "settled":
            return operation
        if operation.state not in {"dispatched", "pending"}:
            raise UsagePendingError("Only dispatched Vercel attempts require provider reconciliation.")
        session_id = operation.provider_request_id
        if not session_id:
            raise UsagePendingError("Vercel creation did not return an exact session identity; retain coverage.")
        request = prices.get("request")
        if not isinstance(request, Mapping) or not isinstance(request.get("vcpus"), int):
            raise UsagePendingError("Vercel admission is missing its covered resource allocation.")
        sessions = []
        for document in record.evidence:
            stored = document.get("sessions")
            if isinstance(stored, Mapping) and isinstance(stored.get(session_id), Mapping):
                sessions.append(stored[session_id])
            final = document.get("session")
            if isinstance(final, Mapping) and final.get("id") == session_id:
                sessions.append(final)
        session = None
        for candidate in reversed(sessions):
            try:
                vercel_actual_usd(candidate, session_id=session_id, vcpus=request["vcpus"], price_snapshot=prices)
            except UsagePendingError:
                continue
            session = candidate
            break
        if session is None:
            session = self.fetch_session(session_id)
        sanitized = {name: session[name] for name in _SESSION_FIELDS if name in session}
        pending = {"provider": "vercel", "session_id": session_id, "sessions": {session_id: sanitized}}
        try:
            self.service.mark_pending(operation_id, username, evidence_key=json_fingerprint(pending), evidence=pending)
        except BudgetConflictError:
            current = self.service.get_operation(operation_id, username)
            if current.state == "settled":
                return current
            raise
        usd = vercel_actual_usd(sanitized, session_id=session_id, vcpus=request["vcpus"], price_snapshot=prices)
        charge = vercel_charge_policy(prices).convert(usd)
        return self.service.settle(
            operation_id,
            username,
            evidence_key=f"vercel-stop:{session_id}",
            actual_cents=charge.total,
            actual_wallet_cents=charge.wallet,
            evidence={"provider": "vercel", "source": _SCHEMA_URL, "session": sanitized, "provider_usd": str(usd)},
        )

    def sweep(self, *, limit: int = 100, after_id: str | None = None) -> ReconciliationPage:
        """Visit one bounded page while keeping uncertain attempts covered.

        Args:
            limit: Maximum admitted attempts checked in this page.
            after_id: Prior page's cursor; None starts a new sweep.

        Returns:
            Per-attempt outcomes and the next cursor, if this page was full.
        """
        attempts = self.service.unsettled_operations(
            provider="vercel", cost_kind="sandbox", limit=limit, after_id=after_id
        )
        results = []
        for operation_id, username in attempts:
            try:
                operation = self.reconcile(operation_id, username)
                results.append(ReconciliationResult(operation_id, operation.state))
            except (UsagePendingError, BudgetError) as error:
                results.append(ReconciliationResult(operation_id, "pending", str(error)))
        return ReconciliationPage(tuple(results), attempts[-1][0] if len(attempts) == limit else None)

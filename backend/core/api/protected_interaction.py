"""Execute one caller-funded completed-run interaction in a managed sandbox."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ..billing import ProviderKeyVault, payload_uses_token_source
from ..billing.budget_amounts import MAX_CENTS
from ..billing.budgets import BudgetConflictError, BudgetError, BudgetService, OperationSnapshot
from ..billing.model_gateway import ModelGateway
from ..billing.operation_pricing import OperationQuote, json_fingerprint
from ..billing.protected_credentials import (
    MCP_AUTH_HEADER_FIELD,
    MCP_CREDENTIAL_REF_FIELD,
    MCP_CREDENTIAL_REVISION_FIELD,
    MCP_URL_REF_FIELD,
    MCP_URL_REVISION_FIELD,
    ProtectedCredentialVault,
    endpoint_has_private_components,
    prepare_protected_credentials,
    resolve_execution_credentials,
)
from ..billing.protected_execution import bind_protected_sandbox
from ..billing.runtime import BudgetRuntime, UsagePendingError
from ..config import settings
from ..constants import TOKEN_SOURCE_MANAGED, TOKEN_SOURCE_SUBSCRIPTION
from ..service_gateway.agents.generalist import get_approval_registry
from ..storage.preflights import PreflightStore, WizardPreflightModel, preflight_document
from ..worker.vercel_dspy import run_vercel_dspy
from .auth import AuthenticatedUser
from .errors import DomainError
from .routers.execution_budgets import budget_http_error, budget_response

INTERACTION_LIFETIME_SECONDS = 300
INTERACTION_APPROVAL_SECONDS = 270
_BOUND_TOOL_FIELDS = (
    MCP_CREDENTIAL_REF_FIELD,
    MCP_CREDENTIAL_REVISION_FIELD,
    MCP_URL_REF_FIELD,
    MCP_URL_REVISION_FIELD,
)


class _InteractionRuntime(BudgetRuntime):
    """Namespace physical operations under one idempotent interaction."""

    def __init__(self, service: BudgetService, *, prefix: str, claim_token: str, **kwargs: Any) -> None:
        """Bind operation receipts to the durable interaction attempt.

        Args:
            service: Shared account budget authority.
            prefix: Stable interaction receipt prefix.
            claim_token: Current execution lease token.
            **kwargs: Budget runtime owner and generation fields.
        """
        super().__init__(service, **kwargs)
        self.prefix = prefix
        self.claim_token = claim_token

    def reserve(
        self,
        quote: OperationQuote,
        *,
        operation_key: str,
        cost_kind: str,
        role: str | None = None,
        attempt: int = 0,
    ) -> OperationSnapshot:
        """Reserve one physical operation under the interaction namespace.

        Args:
            quote: Verified maximum charge for the physical request.
            operation_key: Transport-level request identity.
            cost_kind: Model or sandbox attribution.
            role: Optional model role.
            attempt: Physical retry counter.

        Returns:
            Authoritative operation reservation.
        """
        return super().reserve(
            quote,
            operation_key=self.prefix + json_fingerprint({"claim": self.claim_token, "operation": operation_key}),
            cost_kind=cost_kind,
            role=role,
            attempt=attempt,
        )


class _InteractionEvents:
    """Capture the terminal guest result while forwarding live UI events."""

    def __init__(self, on_event: Callable[[dict[str, Any]], None] | None) -> None:
        """Initialize the event collector.

        Args:
            on_event: Optional thread-safe receiver for SSE-shaped events.
        """
        self.on_event = on_event
        self.result: dict[str, Any] | None = None
        self.error: str | None = None

    def put(self, event: dict[str, Any]) -> None:
        """Route one framed sandbox event.

        Args:
            event: Guest event decoded by the trusted supervisor.
        """
        if event.get("type") == "interaction_result":
            self.result = dict(event["result"])
        elif event.get("type") == "error":
            self.error = str(event.get("error") or "The isolated interaction failed.")
        elif event.get("type") == "terminal":
            self.error = str(event.get("outcome", {}).get("message") or "The interaction stopped.")
        elif isinstance(event.get("event"), str) and isinstance(event.get("data"), dict) and self.on_event is not None:
            self.on_event(event)


def _creation_key(kind: str, key: str) -> str:
    """Derive a bounded account-scoped budget creation key.

    Args:
        kind: Serve, evaluation, or chat operation.
        key: Caller-supplied idempotency key.

    Returns:
        Stable opaque budget creation key.
    """
    return "interaction:" + json_fingerprint({"kind": kind, "key": key})


def _existing_document(engine: Engine, budget_id: str, scope: str) -> dict[str, Any] | None:
    """Read a prior interaction result even after its one-request budget closed.

    Args:
        engine: Durable interaction database.
        budget_id: Caller-owned interaction budget.
        scope: Idempotent interaction scope.

    Returns:
        Public interaction document, or None before the first claim.
    """
    with Session(engine) as session:
        row = session.scalar(
            select(WizardPreflightModel).where(
                WizardPreflightModel.budget_id == budget_id,
                WizardPreflightModel.scope == scope,
            )
        )
        if row is None:
            return None
        document = preflight_document(row)
        document["_request_fingerprint"] = row.result.get("_request_fingerprint")
        return document


def _has_bound_tools(payload: dict[str, Any]) -> bool:
    """Detect an owner-bound MCP endpoint or credential reference.

    Args:
        payload: Stored interaction payload.

    Returns:
        True when the tool source cannot be delegated to another account.
    """
    source = payload.get("tool_source")
    if not isinstance(source, dict):
        return False
    endpoint = source.get("mcp_url")
    return (
        any(source.get(field) is not None for field in _BOUND_TOOL_FIELDS)
        or source.get(MCP_AUTH_HEADER_FIELD) is not None
        or (isinstance(endpoint, str) and endpoint_has_private_components(endpoint))
    )


def _resolve_parent_payload(
    payload: dict[str, Any],
    *,
    user: AuthenticatedUser,
    engine: Engine,
    credential_owner: str | None,
    credential_binding_id: str | None,
) -> dict[str, Any]:
    """Resolve only credentials the signed-in caller is authorized to use.

    Args:
        payload: Secret-free stored interaction inputs.
        user: Caller paying for the interaction.
        engine: Credential-vault database.
        credential_owner: Owner of any execution-bound MCP references.
        credential_binding_id: Original run budget binding those references.

    Returns:
        Trusted parent copy with caller model credentials and permitted tool credentials.

    Raises:
        DomainError: When a shared caller would need the run owner's tool credential.
    """
    parent_payload = payload
    if _has_bound_tools(parent_payload):
        if credential_owner != user.username or credential_binding_id is None:
            raise DomainError("serve.caller_tool_connection_required", status=409)
        parent_payload = resolve_execution_credentials(
            parent_payload,
            username=credential_owner,
            binding_id=credential_binding_id,
            vault=ProtectedCredentialVault(engine=engine),
        )
    return prepare_protected_credentials(
        parent_payload,
        username=user.username,
        vault=ProviderKeyVault(engine=engine),
        default_token_source=str(parent_payload.get("token_source") or TOKEN_SOURCE_MANAGED),
    )


def _tool_authorizer(
    on_event: Callable[[dict[str, Any]], None],
) -> Callable[[str, str, dict[str, Any]], bool]:
    """Build the bounded approval bridge for one protected ReAct chat turn.

    Args:
        on_event: Thread-safe SSE event receiver.

    Returns:
        Blocking tool authorization callback used by the trusted MCP relay.
    """
    registry = get_approval_registry()

    def authorize(call_id: str, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Wait for the signed-in caller's explicit tool decision.

        Args:
            call_id: Random tool-call identifier.
            tool_name: Selected tool name.
            arguments: Tool arguments shown before dispatch.

        Returns:
            True only after approval within the sandbox-bound window.
        """
        event = registry.register_blocking(call_id)
        on_event(
            {
                "event": "pending_approval",
                "data": {"id": call_id, "tool": tool_name, "arguments": arguments},
            }
        )
        approved = registry.wait_for_blocking_decision(call_id, event, timeout_seconds=INTERACTION_APPROVAL_SECONDS)
        on_event(
            {
                "event": "approval_resolved",
                "data": {"id": call_id, "tool": tool_name, "approved": approved},
            }
        )
        return approved

    return authorize


def run_protected_interaction(
    payload: dict[str, Any],
    *,
    kind: str,
    max_cost_cents: int | None,
    idempotency_key: str,
    user: AuthenticatedUser,
    job_store: Any,
    credential_owner: str | None = None,
    credential_binding_id: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    require_tool_approval: bool = False,
    stage_payload: Callable[[dict[str, Any]], AbstractContextManager[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run or replay one caller-funded interaction with no reusable allowance.

    Args:
        payload: Secret-free program, evaluator, and request data.
        kind: Serve, evaluation, or chat identity.
        max_cost_cents: Maximum the caller accepted for this request, or ``None``
            to let the request draw on the account balance instead.
        idempotency_key: Transport replay identity.
        user: Authenticated account funding model and sandbox work.
        job_store: Store whose engine owns budgets and credentials.
        credential_owner: Owner of original execution-bound tool credentials.
        credential_binding_id: Original run budget binding those credentials.
        on_event: Optional thread-safe SSE receiver.
        require_tool_approval: Whether every live MCP tool call pauses for approval.
        stage_payload: Optional context that adds parent-only staging, such as a
            fetched repository tree, to the resolved payload. It runs only for a
            claimed attempt and stays out of the request fingerprint, so a replay
            never fetches again.

    Returns:
        Guest result with the closed budget and exact settled charge.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None:
        raise DomainError("budget.invalid", status=503)
    key = idempotency_key.strip()
    if not key:
        raise DomainError("budget.idempotency_required", status=400)
    # An imported local run keeps its subscription model, which only the user's own
    # CLI can run; refuse it here before any budget, sandbox, or model call.
    if not settings.local_mode and payload_uses_token_source(
        payload,
        TOKEN_SOURCE_SUBSCRIPTION,
        default_token_source=str(payload.get("token_source") or TOKEN_SOURCE_MANAGED),
    ):
        raise DomainError("serve.subscription_model_hosted", status=409)
    service = BudgetService(engine=engine)
    creation_key = _creation_key(kind, key)
    scope = json_fingerprint({"kind": kind, "key": key})[:24]
    request_fingerprint = json_fingerprint(payload)
    try:
        if max_cost_cents is None:
            # No caller cap: the budget stays uncapped so only the account balance
            # bounds the request, and the fixed total keeps replays fingerprint-stable.
            budget = service.create(user.username, MAX_CENTS, idempotency_key=creation_key, uncapped=True)
        else:
            budget = service.create(user.username, max_cost_cents, idempotency_key=creation_key)
        document = _existing_document(engine, budget.id, scope)
        if budget.state == "closed":
            if document is None or document.get("_request_fingerprint") != request_fingerprint:
                raise BudgetConflictError("This interaction key already belongs to a different request.")
        else:
            claim = PreflightStore(engine, lease_seconds=INTERACTION_LIFETIME_SECONDS + 60).claim(
                username=user.username,
                budget_id=budget.id,
                revision=budget.revision,
                workflow="interaction",
                scope=scope,
                payload=payload,
                reuse_failed=True,
                exclusive_scope=True,
            )
            document = claim.document
            if claim.token is not None:
                events = _InteractionEvents(on_event)
                gateway = None
                with PreflightStore(engine, lease_seconds=INTERACTION_LIFETIME_SECONDS + 60).heartbeat(
                    document["id"], claim_token=claim.token
                ):
                    try:
                        parent_payload = _resolve_parent_payload(
                            payload,
                            user=user,
                            engine=engine,
                            credential_owner=credential_owner,
                            credential_binding_id=credential_binding_id,
                        )
                        if settings.openrouter_api_key is None and payload_uses_token_source(
                            parent_payload,
                            TOKEN_SOURCE_MANAGED,
                            default_token_source=str(parent_payload.get("token_source") or TOKEN_SOURCE_MANAGED),
                        ):
                            raise ValueError("Managed model routing is not configured.")
                        gateway = ModelGateway(
                            _InteractionRuntime(
                                service,
                                username=user.username,
                                budget_id=budget.id,
                                generation=claim.generation,
                                phase="setup",
                                prefix=f"interaction:{document['id']}:",
                                claim_token=claim.token,
                            )
                        )
                        bind_protected_sandbox(
                            gateway,
                            settings,
                            workflow="dspy",
                            owner_id=document["id"],
                            lifetime_seconds=INTERACTION_LIFETIME_SECONDS,
                        )
                        authorizer = None
                        if require_tool_approval:
                            if on_event is None:
                                raise ValueError("Tool approval requires a live event stream.")
                            authorizer = _tool_authorizer(on_event)
                        staging = (
                            stage_payload(parent_payload) if stage_payload is not None else nullcontext(parent_payload)
                        )
                        with staging as staged_payload:
                            protected = gateway.protect_payload(
                                staged_payload,
                                managed_key=(
                                    settings.openrouter_api_key.get_secret_value()
                                    if settings.openrouter_api_key
                                    else ""
                                ),
                                allow_private_tools=settings.discover_allow_private,
                                authorize_tool=authorizer,
                                on_tool_event=on_event if kind == "react_chat" else None,
                                # Interactions stay unbatched until SKY-283: a synchronous call
                                # hits the 120 s ALB limit long before a batch answers, and a
                                # batch that outlives the box leaves an unsettled hold.
                                allow_batch=False,
                            )
                            run_vercel_dspy(protected, f"interaction-{document['id']}", events, "spawn")
                    except Exception as error:
                        events.error = str(error)
                    finally:
                        if gateway is not None:
                            try:
                                gateway.close()
                            except UsagePendingError:
                                pass
                            except Exception as error:
                                events.error = str(error)
                    result = events.result or {}
                    if events.error is not None:
                        result["error"] = events.error
                    status = "failed" if result.get("error") else "succeeded"
                    if service.get(budget.id, user.username).pending_operations:
                        status = "pending"
                    document = PreflightStore(engine, lease_seconds=INTERACTION_LIFETIME_SECONDS + 60).finish(
                        document["id"],
                        claim_token=claim.token,
                        status=status,
                        result={
                            "checks": [{"key": kind, "status": status}],
                            "interaction_result": result,
                            "_request_fingerprint": request_fingerprint,
                        },
                    )
                    service.stop_admission(
                        budget.id,
                        user.username,
                        reason="interaction_complete" if status == "succeeded" else "interaction_stopped",
                    )
        assert document is not None
        result = dict(document.get("interaction_result") or {})
        current = service.get(budget.id, user.username)
        public_budget = budget_response(current).model_dump(mode="json")
        result.update(
            {
                "cents_charged": public_budget["setup_spent_cents"],
                "budget": public_budget,
                "interaction_status": document["status"],
                "interaction_id": document["id"],
            }
        )
        return result
    except BudgetError as error:
        raise budget_http_error(error) from error

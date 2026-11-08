"""Run legacy debug previews through the same protected setup spending authority."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from ..billing import ProviderKeyVault, payload_uses_token_source
from ..billing.budgets import (
    BudgetConflictError,
    BudgetError,
    BudgetInsufficientError,
    BudgetService,
    OperationSnapshot,
)
from ..billing.dependency_lock import sign_dependency_lock
from ..billing.model_gateway import ModelGateway
from ..billing.operation_pricing import OperationQuote, json_fingerprint
from ..billing.protected_credentials import prepare_protected_credentials
from ..billing.protected_execution import bind_protected_sandbox
from ..billing.runtime import BudgetRuntime, UsagePendingError
from ..billing.service import StripeBillingService
from ..config import settings
from ..constants import TOKEN_SOURCE_MANAGED
from ..service_gateway.optimization.blackbox import package_setup
from ..service_gateway.optimization.blackbox.remote_sandbox import RemoteSandboxRuntime
from ..service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec
from ..service_gateway.optimization.blackbox.sandbox_scorer import _raise_control
from ..storage.models import ExecutionBudgetModel, ExecutionOperationModel, ExecutionUsageEvidenceModel
from ..storage.preflights import PreflightStore
from ..worker.vercel_dspy import run_vercel_dspy
from .auth import AuthenticatedUser
from .errors import DomainError
from .routers.execution_budgets import budget_http_error, budget_response


class _PreviewRuntime(BudgetRuntime):
    """Attribute every physical preview operation without relying on a before/after balance difference."""

    def __init__(self, service: BudgetService, *, prefix: str, claim_token: str, **kwargs: Any) -> None:
        """Bind each attempt to its durable preview receipt prefix.

        Args:
            service: Shared operation admission authority.
            prefix: Preview identity retained across recovered attempts.
            claim_token: Current lease owner, distinguishing fenced physical attempts.
            **kwargs: Authenticated budget runtime context.
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
        """Reserve normally while binding opaque operation keys to this preview's receipts.

        Args:
            quote: Verified price and maximum charge for the exact physical request.
            operation_key: Transport's stable physical-operation identity.
            cost_kind: Model or sandbox attribution.
            role: Optional model role.
            attempt: Physical retry counter supplied by the transport.

        Returns:
            The real ledger reservation, with recovered owners kept distinct.
        """
        return super().reserve(
            quote,
            operation_key=self.prefix + json_fingerprint({"claim": self.claim_token, "operation": operation_key}),
            cost_kind=cost_kind,
            role=role,
            attempt=attempt,
        )


class _PreviewEvents:
    """Retain isolated results and forward actual streamed tokens without replaying execution."""

    def __init__(self, on_token: Callable[[dict[str, str]], None] | None) -> None:
        """Bind the optional stream sink and initialize terminal output.

        Args:
            on_token: Receiver for actual isolated output chunks, or None.
        """
        self.on_token = on_token
        self.result: dict[str, Any] | None = None
        self.error: str | None = None

    def put(self, event: dict[str, Any]) -> None:
        """Accept only preview output, explicit errors, and actual token events.

        Args:
            event: One framed guest event forwarded by the protected supervisor.
        """
        if event.get("type") == "preflight_result":
            self.result = event["result"]["workflow_result"]
        elif event.get("type") == "preview_token" and self.on_token is not None:
            self.on_token({"field": str(event["field"]), "chunk": str(event["chunk"])})
        elif event.get("type") == "error":
            self.error = str(event.get("error") or "The isolated preview failed.")
        elif event.get("type") == "terminal":
            self.error = str(event.get("outcome", {}).get("message") or "The preview stopped at its budget.")


def _run_workflow(
    gateway: ModelGateway, payload: dict[str, Any], identity: str, on_token: Callable[[dict[str, str]], None] | None
) -> dict[str, Any]:
    """Send explicit workflow debug inputs to the selected protected executor.

    Args:
        gateway: Parent model and sandbox authority retained throughout execution.
        payload: Scoped request containing exact user debug values.
        identity: Durable preview identity for runtime attribution.
        on_token: Optional actual output-chunk receiver.

    Returns:
        Guest-produced workflow outputs, traces, errors, and usage.
    """
    events = _PreviewEvents(on_token)
    request = {**payload, "_preflight": {"kind": "workflow_preview", "stream": on_token is not None}}
    run_vercel_dspy(request, f"preview-{identity}", events, "spawn")
    if events.result is None:
        raise RuntimeError(events.error or "The isolated workflow did not return a preview result.")
    return events.result


def _run_dependencies(
    gateway: ModelGateway,
    payload: dict[str, Any],
    identity: str,
    on_token: Callable[[dict[str, str]], None] | None,
) -> dict[str, Any]:
    """Resolve dependency metadata inside a paid offline Vercel sandbox.

    Args:
        gateway: Parent-owned package and spending authority.
        payload: Exact code, requirements, and scoped package capability.
        identity: Durable preview identity.
        on_token: Unused preview adapter argument.

    Returns:
        Signed dependency lock and actual resolution outcome.
    """
    descriptor = payload["_budget_gateway_descriptor"]
    runtime = RemoteSandboxRuntime(descriptor["url"], descriptor["control_token"])
    session = runtime.open(
        SandboxSpec(
            lifetime_seconds=min(300, descriptor["lifetime_seconds"]),
            network_disabled=True,
            operation_key=f"dependencies:{identity}",
        )
    )
    try:
        session.write_files(
            {
                "packages/setup.py": Path(package_setup.__file__).read_text(),
                "packages/request.json": json.dumps(
                    {
                        "action": "resolve",
                        "code": payload["scorer"]["metric_code"],
                        "requirements": payload.get("requirements", []),
                        "candidate": payload.get("seed_candidate"),
                        "route": payload["_skynet_packages_route"],
                    }
                ),
            }
        )
        result = session.run("python3 packages/setup.py packages/request.json", timeout_seconds=300)
        raw = session.read_file("packages/result.json")
        outcome = json.loads(raw) if raw else {}
        _raise_control(outcome)
        if not result.ok or not outcome.get("ok"):
            raise ValueError(
                outcome.get("error")
                or (result.stderr or result.stdout)[-6000:]
                or "Sandbox package resolution did not complete."
            )
        lock = sign_dependency_lock(
            {
                **outcome["result"],
                "image": descriptor["image"],
                "registry_url": payload["registry_url"],
            }
        )
        return {"ok": True, "dependency_lock": lock}
    finally:
        with suppress(UsagePendingError):
            session.close()


def _budget_for_preview(service: BudgetService, engine: Engine, payload: dict, username: str, key: str, kind: str):
    """Recover a shared or automatically created setup envelope without changing its approved total.

    Args:
        service: Authoritative account-owned budget service.
        engine: Shared persistence for automatic-creation replay lookup.
        payload: Debug request with optional budget identity and revision.
        username: Authenticated spending owner.
        key: Stable preview request key.
        kind: Dependency or workflow preview family.

    Returns:
        Current owned setup budget, preserving the original total on replay.
    """
    identity, revision = payload.get("execution_budget_id"), payload.get("execution_budget_revision")
    if identity is not None:
        budget = service.get(identity, username)
        if revision != budget.revision:
            raise BudgetConflictError("The setup budget changed.")
        return budget
    if revision is not None:
        raise BudgetConflictError("An execution budget ID is required with its revision.")
    creation_key = "preview:" + json_fingerprint({"kind": kind, "key": key})
    with Session(engine) as session:
        existing = session.scalar(
            select(ExecutionBudgetModel.id).where(
                ExecutionBudgetModel.username == username, ExecutionBudgetModel.creation_key == creation_key
            )
        )
    if existing is not None:
        return service.get(existing, username)
    available = StripeBillingService(engine=engine).spendable_cents(username)
    if available <= 0:
        raise BudgetInsufficientError("The account has no available funds for a preview.")
    return service.create(username, available, idempotency_key=creation_key)


def _charged(engine: Engine, budget_id: str, identity: str) -> int:
    """Read the cents actually debited for this preview from immutable usage receipts.

    Args:
        engine: Authoritative usage-evidence database.
        budget_id: Owned shared setup budget.
        identity: Durable preview whose operations are being reconciled.

    Returns:
        Sum of confirmed wallet debit deltas, preserving cumulative rounding.
    """
    with Session(engine) as session:
        return int(
            session.scalar(
                select(func.coalesce(func.sum(ExecutionUsageEvidenceModel.billed_cents), 0))
                .join(ExecutionOperationModel, ExecutionOperationModel.id == ExecutionUsageEvidenceModel.operation_id)
                .where(
                    ExecutionOperationModel.budget_id == budget_id,
                    ExecutionOperationModel.operation_key.startswith(f"preview:{identity}:"),
                )
            )
        )


def run_protected_preview(
    payload: dict[str, Any],
    *,
    kind: str,
    user: AuthenticatedUser,
    job_store: Any,
    idempotency_key: str | None = None,
    on_token: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, Any]:
    """Execute and reconcile one legacy preview under account-owned, leased setup authority.

    Args:
        payload: Validated dependency or workflow debug request.
        kind: Dependency or workflow preview family.
        user: Authenticated spending owner.
        job_store: Real database-backed store.
        idempotency_key: Optional transport replay identity; omission requests a fresh preview.
        on_token: Optional receiver for actual workflow output chunks.

    Returns:
        Existing preview fields plus current budget, pending state, and confirmed charged cents.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None:
        raise DomainError("budget.invalid", status=503)
    service = BudgetService(engine=engine)
    store = PreflightStore(engine)
    key = (idempotency_key or "").strip() or str(uuid4())
    scope = json_fingerprint({"kind": kind, "key": key})[:24]
    result_key = "dependency_result" if kind == "dependencies" else "workflow_result"
    started = time.perf_counter()
    try:
        budget = _budget_for_preview(service, engine, payload, user.username, key, kind)
        claim = store.claim(
            username=user.username,
            budget_id=budget.id,
            revision=budget.revision,
            workflow=kind + "_preview",
            scope=scope,
            payload=payload,
            reuse_failed=kind != "dependencies",
            exclusive_scope=True,
        )
        document = claim.document
        if claim.token is not None:
            gateway = None
            result: dict[str, Any] = {}
            error = None
            with store.heartbeat(document["id"], claim_token=claim.token):
                try:
                    parent_payload = prepare_protected_credentials(
                        payload,
                        username=user.username,
                        vault=ProviderKeyVault(engine=engine),
                        default_token_source=str(payload.get("token_source") or TOKEN_SOURCE_MANAGED),
                    )
                    uses_managed_models = payload_uses_token_source(
                        parent_payload,
                        TOKEN_SOURCE_MANAGED,
                        default_token_source=str(parent_payload.get("token_source") or TOKEN_SOURCE_MANAGED),
                    )
                    if settings.openrouter_api_key is None and uses_managed_models and kind != "dependencies":
                        raise ValueError("Managed model routing is not configured.")
                    gateway = ModelGateway(
                        _PreviewRuntime(
                            service,
                            username=user.username,
                            budget_id=budget.id,
                            generation=claim.generation,
                            phase="setup",
                            prefix=f"preview:{document['id']}:",
                            claim_token=claim.token,
                        )
                    )
                    bind_protected_sandbox(
                        gateway,
                        settings,
                        workflow="anything" if kind == "dependencies" else "dspy",
                        owner_id=document["id"],
                    )
                    protected = gateway.protect_payload(
                        parent_payload,
                        managed_key=settings.openrouter_api_key.get_secret_value()
                        if settings.openrouter_api_key
                        else "",
                        allow_private_tools=settings.discover_allow_private,
                        # A debug preview waits for its answer inside one sandbox. A batch
                        # can take hours, outlive that box, and leave an unsettled hold.
                        allow_batch=False,
                    )
                    result = (_run_dependencies if kind == "dependencies" else _run_workflow)(
                        gateway, protected, document["id"], on_token
                    )
                except Exception as caught:
                    error = str(caught)
                finally:
                    if gateway is not None:
                        try:
                            gateway.close()
                        except UsagePendingError:
                            pass
                        except Exception as caught:
                            error = str(caught)
                if error is not None:
                    result["error"] = error
                    if kind == "dependencies":
                        result["ok"] = False
                failed = bool(result.get("error")) or (kind == "dependencies" and not result.get("ok"))
                status = "failed" if failed else "succeeded"
                checks = [{"key": kind, "status": status}]
                if service.get(budget.id, user.username).pending_operations:
                    status = "pending"
                    checks.append({"key": "usage", "status": "pending"})
                document = store.finish(
                    document["id"],
                    claim_token=claim.token,
                    status=status,
                    result={"checks": checks, result_key: result},
                )
        result = dict(document.get(result_key) or {})
        if kind == "dependencies":
            result.setdefault("ok", False)
            result.setdefault("elapsed_ms", (time.perf_counter() - started) * 1000)
        else:
            result.setdefault("model_used", str(payload["model_config"]["name"]))
        if document["status"] == "pending" and not result.get("ok", bool(result.get("outputs"))):
            result.setdefault("error", "The preview is awaiting completion or confirmed usage.")
        return {
            **result,
            "cents_charged": _charged(engine, budget.id, document["id"]),
            "budget": budget_response(service.get(budget.id, user.username)).model_dump(mode="json"),
            "preview_status": document["status"],
            "preflight_id": document["id"],
        }
    except BudgetError as error:
        raise budget_http_error(error) from error

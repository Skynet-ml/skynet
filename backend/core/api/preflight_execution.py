"""Run paid Continue checks under the draft's shared, durable spending authority."""

from __future__ import annotations

import copy
import json
import logging
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, field_validator, model_serializer

from ..billing import ProviderKeyVault, payload_uses_token_source
from ..billing.budgets import (
    AccountFundsError,
    BudgetConflictError,
    BudgetError,
    BudgetInsufficientError,
    BudgetService,
)
from ..billing.model_dispatch import ModelHTTPResult
from ..billing.model_gateway import ModelGateway
from ..billing.protected_credentials import (
    ProtectedCredentialVault,
    prepare_protected_credentials,
    protect_execution_credentials,
    repo_secret_allowed_hosts,
    resolve_execution_credentials,
)
from ..billing.protected_execution import bind_protected_sandbox
from ..billing.runtime import BudgetRuntime, UsagePendingError
from ..billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from ..config import settings
from ..constants import OPTIMIZATION_TYPE_BLACKBOX, TOKEN_SOURCE_MANAGED
from ..models import BlackboxRunRequest
from ..models.blackbox import BLACKBOX_ENGINE_SHINKA_EVOLVE
from ..service_gateway.optimization.blackbox.native_runtime import NATIVE_ENGINES
from ..service_gateway.optimization.blackbox.preflight import judge_lifetime_seconds, preflight_lifetime_seconds
from ..storage.preflights import PreflightStore
from ..worker.repo_gate import RepoGateError, check_repository, sandbox_place
from ..worker.repo_staging import (
    StagedRepository,
    bind_repo_scorer,
    infer_repo_setup,
    is_repo_payload,
    stage_repository,
)
from ..worker.unified_aliases import alias_blackbox_metric_model
from ..worker.vercel_dspy import run_vercel_dspy
from .agent_engine import fill_proposer_host_cli
from .errors import DomainError
from .model_billing import enforce_local_mode_sources, enforce_raw_local_mode_sources, normalize_model_token_sources
from .preflight_progress import report_preflight_phase
from .routers.execution_budgets import ExecutionBudgetResponse, budget_http_error, budget_response

logger = logging.getLogger(__name__)


class WizardPreflightRequest(BaseModel):
    scope: Literal["evaluation", "execution"]
    workflow: Literal["anything"]
    payload: dict[str, Any]
    execution_budget_id: str = Field(min_length=1, max_length=64)
    execution_budget_revision: int = Field(ge=1)
    locale: str | None = Field(default=None, max_length=35)

    @field_validator("payload")
    @classmethod
    def reject_runtime_controls(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Reject public inputs that attempt to supply trusted runtime control fields.

        Args:
            payload: Untrusted public stage inputs before canonicalization or fingerprinting.

        Returns:
            Public inputs containing no parent-owned execution descriptors.

        Raises:
            ValueError: When a top-level private execution key is supplied.
        """
        if any(key.startswith("_") for key in payload):
            raise ValueError("Runtime control fields cannot be supplied in setup inputs.")
        return payload


# ``message`` is worded in the request's locale; when it comes from the
# catalog, ``message_code`` and ``message_params`` let a client word it itself.
class WizardPreflightCheck(BaseModel):
    key: str
    status: Literal["succeeded", "failed", "pending", "skipped"]
    message: str | None = None
    message_code: str | None = None
    message_params: dict[str, str | int] | None = None
    field: str | None = None


class WizardPreflightPendingReason(BaseModel):
    category: Literal["later_stage_dependency", "usage_reconciliation", "setup_incomplete"]
    message: str
    field: str | None = None


class WizardPreflightResponse(BaseModel):
    id: str
    fingerprint: str
    status: Literal["succeeded", "failed", "pending"]
    may_advance: bool
    pending_reason: WizardPreflightPendingReason | None = None
    checks: list[WizardPreflightCheck]
    budget: ExecutionBudgetResponse
    scorer_result: dict[str, Any] | None = None
    workflow_result: dict[str, Any] | None = None
    terminal: dict[str, Any] | None = None
    # A repository run's effective environment: what versions may change,
    # what stays protected, and which network and services each step gets.
    environment: dict[str, Any] | None = None
    # The proposer CLI this check ran on. A submit sends it back so the server
    # does not pick another CLI from changed readiness and refuse the evidence.
    proposer_host_cli: Literal["codex", "claude"] | None = None

    @model_serializer(mode="wrap")
    def _keep_proposer_host_cli(self, handler: SerializerFunctionWrapHandler):
        """Keep the check's proposer CLI in the output even when it is null.

        The routes drop null fields, and a missing key lets the submit pick a
        CLI from the engine as it is now. The null must reach the submit.

        Args:
            handler: Pydantic's default serializer for this model.

        Returns:
            The serialized response, with ``proposer_host_cli`` whenever the check set it.
        """
        data = handler(self)
        if "proposer_host_cli" in self.model_fields_set:
            data["proposer_host_cli"] = self.proposer_host_cli
        return data


def _proposer_host_cli(payload: dict[str, Any]) -> str | None:
    """Read the proposer CLI a setup payload resolved.

    Args:
        payload: A black-box setup payload.

    Returns:
        The proposer's ``host_cli``, or ``None`` when the payload names none.
    """
    proposer = payload.get("proposer")
    return proposer.get("host_cli") if isinstance(proposer, dict) else None


def _canonical_execution_payload(request: WizardPreflightRequest, username: str, job_store: Any) -> dict[str, Any]:
    """Apply the submission schema before hashing complete execution inputs.

    Args:
        request: Full execution preflight request.
        username: Authenticated owner replacing any client-supplied identity.
        job_store: Job store whose engine holds the caller's agent engine setting.

    Returns:
        The same canonical JSON shape the submission endpoint will attach.
    """
    payload = {**request.payload, "username": username}
    typed = BlackboxRunRequest.model_validate(payload)
    requested_source = typed.token_source
    model_configs, _ = normalize_model_token_sources(typed)
    fill_proposer_host_cli(job_store, username, typed)
    enforce_local_mode_sources(typed, requested_source, model_configs)
    return typed.model_dump(mode="json", by_alias=True)


BUDGET_CHECK_KEY = "budget"
# A failed budget check names the balance as what to fix when the account,
# not the spending limit, refused the work: raising the limit cannot help then.
BALANCE_FIELD = "balance"
# A runtime reports a budget stop only by its type name; these rebuild it.
_BUDGET_STOPS = {stop.__name__: stop for stop in (BudgetInsufficientError, AccountFundsError)}

# The browser waits on the check's response, and the connection is cut after
# about five minutes; covered work still unsettled by then reads as pending.
SETUP_WAIT_SECONDS = 90


def _check(key: str, status: str, message: str | None = None, field: str | None = None) -> dict[str, Any]:
    """Build one scoped readiness result without inventing a score."""
    return {
        "key": key,
        "status": status,
        **({"message": message} if message else {}),
        **({"field": field} if field else {}),
    }


def _pending_reason(category: str, message: str, field: str | None = None) -> dict[str, Any]:
    """Describe why incomplete evidence cannot or may safely cross this stage."""
    return {
        "category": category,
        "message": message,
        **({"field": field} if field else {}),
    }


def _mark_usage_pending(result: dict[str, Any], message: str) -> dict[str, Any]:
    """Attach one blocking usage-reconciliation outcome without losing performed checks."""
    checks = list(result.get("checks", []))
    if not any(check.get("key") == "usage" and check.get("status") == "pending" for check in checks):
        checks.append(_check("usage", "pending", message))
    return {
        **result,
        "checks": checks,
        "pending_reason": _pending_reason("usage_reconciliation", message),
    }


def _provider_refusal(response: ModelHTTPResult) -> str:
    """Summarize a provider's error answer for the check report.

    Args:
        response: Non-2xx model response returned through the metered route.

    Returns:
        The HTTP status, followed by the provider's own message when its body carries one.
    """
    try:
        error = json.loads(response.content).get("error")
    except (ValueError, AttributeError):
        error = None
    detail = error.get("message") if isinstance(error, dict) else error if isinstance(error, str) else None
    summary = f"HTTP {response.status}"
    return f"{summary}: {str(detail)[:200]}" if detail else summary


def _verify_model_routes(gateway: ModelGateway, *, native: bool) -> list[dict[str, Any]]:
    """Verify each configured role through its real metered provider path.

    Args:
        gateway: Trusted setup authority retaining provider credentials.
        native: Whether the optimization model uses the native Anthropic protocol.

    Returns:
        Actual successful or failed connection checks for each distinct role route.
    """
    report_preflight_phase("models")
    checks = []
    for route in gateway.model_routes():
        anthropic = native and route["role"] == "optimization"
        body = {"model": route["model"], "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 16}
        response = gateway.dispatch_guest(
            route["token"],
            "/v1/messages" if anthropic else "/v1/chat/completions",
            body,
            {"anthropic-version": "2023-06-01"} if anthropic else {},
        )
        ok = 200 <= response.status < 300
        detail = f"The selected {route['role']} model rejected its setup request ({_provider_refusal(response)})."
        checks.append(
            _check(
                f"model.{route['role']}",
                "succeeded" if ok else "failed",
                None if ok else detail,
                route["role"],
            )
        )
    return checks


def _verify_anything(gateway: ModelGateway, payload: dict[str, Any], *, scope: str, identity: str) -> dict[str, Any]:
    """Run complete Anything readiness in its selected outer sandbox.

    Args:
        gateway: Shared setup model and sandbox authority.
        payload: Canonical partial or full Anything request.
        scope: Evaluation or complete execution readiness.
        identity: Stable preflight evidence identity.

    Returns:
        Real checks and, when a seed exists, its actual scorer preview.
    """
    report_preflight_phase("sandbox")
    events = _Events()
    protected = {
        **payload,
        "_optimization_type": OPTIMIZATION_TYPE_BLACKBOX,
        "_preflight": {"scope": scope, "identity": identity},
    }
    run_vercel_dspy(protected, f"preflight-{identity}", events, "spawn")
    result = events.evidence(gateway)
    optimizer_ready = any(check.get("key") == "optimizer" for check in result["checks"])
    if scope == "execution" and optimizer_ready:
        public = {key: value for key, value in payload.items() if not key.startswith("_")}
        typed = BlackboxRunRequest.model_validate(public)
        # ShinkaEvolve calls its optimization models over chat completions, not
        # through a native harness on the Anthropic protocol.
        native = (
            typed.strategy.mode != "single"
            or typed.strategy.engine in NATIVE_ENGINES - {BLACKBOX_ENGINE_SHINKA_EVOLVE}
            or typed.target.kind == "repo"
        )
        optimizer_index = next(index for index, check in enumerate(result["checks"]) if check.get("key") == "optimizer")
        result["checks"][optimizer_index:optimizer_index] = _verify_model_routes(gateway, native=native)
    return result


class _Events:
    """Collect only terminal preflight results from an isolated runtime."""

    def __init__(self) -> None:
        """Initialize a setup-only event collector."""
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.error_type: str | None = None

    def put(self, event: dict[str, Any]) -> None:
        """Preserve a real result or error without interpreting optimizer logs as checks."""
        if event.get("type") == "preflight_phase":
            report_preflight_phase(str(event.get("phase")))
        elif event.get("type") == "preflight_result":
            self.result = event["result"]
        elif event.get("type") == "error":
            self.error = str(event.get("error") or "Setup execution failed.")
            self.error_type = event.get("error_type")

    def evidence(self, gateway: ModelGateway) -> dict[str, Any]:
        """Return the runtime's setup evidence, or raise the failure it reported.

        Args:
            gateway: The setup's model gateway, which saw any call the limit refused.

        Returns:
            The guest's terminal preflight result.

        Raises:
            BudgetInsufficientError: When the spending limit or the account
                balance stopped the runtime.
            ValueError: For any other runtime failure, or when no result came back.
        """
        # ``run_vercel_dspy`` turns every exception into an error event, so the
        # budget stop has to be rebuilt from its type name to reach the
        # ``budget`` check instead of the generic ``setup`` one. A guest model
        # call the limit refused only reaches the guest as a 402, which its
        # client raises under its own type, so the gateway's record of the
        # refusal decides that case.
        # The guest sees an account-funds refusal as a plain budget stop, so
        # the gateway's record keeps the balance from reading as the limit.
        if self.error and isinstance(gateway.budget_refusal, AccountFundsError):
            raise gateway.budget_refusal
        stop = _BUDGET_STOPS.get(self.error_type or "")
        if stop is not None:
            raise stop(self.error or "The spending limit cannot cover setup.")
        if self.error and gateway.budget_refusal is not None:
            raise gateway.budget_refusal
        if self.error or self.result is None:
            raise ValueError(self.error or "The runtime did not return setup evidence.")
        return self.result


def run_preflight(request: WizardPreflightRequest, user: Any, job_store: Any) -> WizardPreflightResponse:
    """Run canonical setup inputs under a shared authority for wizard and API callers.

    Args:
        request: Canonical current inputs with explicit budget and scope.
        user: Authenticated owner, resolved before dispatch.
        job_store: Authoritative ledger and evidence storage.

    Returns:
        Durable scoped checks and current budget state.
    """
    # Gate before the check claims evidence or spends: a hosted check must not
    # bill a subscription or host-CLI run as managed.
    enforce_raw_local_mode_sources(request.payload)
    return _run_preflight(request, user, job_store)


def _run_preflight(request: WizardPreflightRequest, user: Any, job_store: Any) -> WizardPreflightResponse:
    """Claim, run and record one setup attempt, or return the evidence it duplicates.

    Args:
        request: Canonical current inputs with explicit budget and scope.
        user: Authenticated owner, resolved before dispatch.
        job_store: Authoritative ledger and evidence storage.

    Returns:
        Durable scoped checks and current budget state.
    """
    report_preflight_phase("budget")
    budgets = BudgetService(engine=job_store.engine)
    evidence = PreflightStore(job_store.engine)
    if request.scope == "execution":
        request = request.model_copy(
            update={"payload": _canonical_execution_payload(request, user.username, job_store)}
        )
    host_cli = _proposer_host_cli(request.payload)
    try:
        snapshot = budgets.get(request.execution_budget_id, user.username)
        if snapshot.revision != request.execution_budget_revision:
            raise BudgetConflictError("The setup budget changed.")
        credential_vault = ProtectedCredentialVault(engine=job_store.engine)
        payload = protect_execution_credentials(
            request.payload,
            username=user.username,
            binding_id=request.execution_budget_id,
            vault=credential_vault,
        )
        request = request.model_copy(update={"payload": payload})
        claim = evidence.claim(
            username=user.username,
            budget_id=request.execution_budget_id,
            revision=request.execution_budget_revision,
            workflow=request.workflow,
            scope=request.scope,
            payload=payload,
        )
        snapshot = budgets.get(request.execution_budget_id, user.username)
        if snapshot.generation != claim.generation:
            raise BudgetConflictError("Another setup attempt replaced this execution generation.")
    except BudgetError as error:
        raise budget_http_error(error) from error
    if claim.token is None:
        return WizardPreflightResponse.model_validate(
            {**claim.document, "budget": budget_response(snapshot), "proposer_host_cli": host_cli}
        )
    try:
        with evidence.heartbeat(claim.document["id"], claim_token=claim.token):
            try:
                source = copy.deepcopy(request.payload)
                alias_blackbox_metric_model(source)
                parent_payload = resolve_execution_credentials(
                    source,
                    username=user.username,
                    binding_id=request.execution_budget_id,
                    vault=credential_vault,
                )
                parent_payload = prepare_protected_credentials(
                    parent_payload,
                    username=user.username,
                    vault=ProviderKeyVault(engine=job_store.engine),
                    default_token_source=str(parent_payload.get("token_source") or "managed"),
                )
            except (TypeError, ValueError) as error:
                # Finish the claimed row here: an escaped error would leave it at
                # "running" until its lease ran out, behind a generic wizard error.
                logger.warning("setup check %s has unusable credentials: %s", claim.document["id"], error)
                document = evidence.finish(
                    claim.document["id"],
                    claim_token=claim.token,
                    status="failed",
                    result={"checks": [_check("setup", "failed", str(error))]},
                )
                return WizardPreflightResponse.model_validate(
                    {**document, "budget": budget_response(snapshot), "proposer_host_cli": host_cli}
                )
            parent_request = request.model_copy(update={"payload": parent_payload})
            status, result = _perform_preflight(
                parent_request,
                user,
                budgets,
                snapshot,
                claim.document,
                attempt=claim.attempt,
                engine=job_store.engine,
            )
            snapshot = budgets.get(request.execution_budget_id, user.username)
            if snapshot.pending_operations:
                status = "pending"
                result = _mark_usage_pending(
                    result,
                    "Provider or sandbox usage is still awaiting final confirmation.",
                )
            document = evidence.finish(claim.document["id"], claim_token=claim.token, status=status, result=result)
    except BudgetError as error:
        raise budget_http_error(error) from error
    return WizardPreflightResponse.model_validate(
        {**document, "budget": budget_response(snapshot), "proposer_host_cli": host_cli}
    )


def _perform_preflight(
    request: WizardPreflightRequest,
    user: Any,
    budgets: BudgetService,
    snapshot: Any,
    document: dict[str, Any],
    *,
    attempt: int,
    engine: Any,
) -> tuple[str, dict[str, Any]]:
    """Execute the owned attempt and finalize its transports before publishing evidence.

    Args:
        request: Canonical stage inputs.
        user: Authenticated spending owner.
        budgets: Shared operation ledger.
        snapshot: Generation read after the execution claim.
        document: Durable identity of this setup attempt.
        attempt: Number of this physical run of the setup row.
        engine: Database holding the GitHub connection and saved secrets.

    Returns:
        Actual readiness outcome and preserved results, including pending usage.
    """
    payload = request.payload
    gateway: ModelGateway | None = None
    staged: StagedRepository | None = None
    status = "failed"
    result: dict[str, Any] = {}
    try:
        remote_evaluation = (
            request.scope == "evaluation"
            and payload.get("scorer", {}).get("kind") == "remote"
            and payload.get("target", {}).get("kind") != "agent"
        )
        uses_managed_models = payload_uses_token_source(
            payload,
            TOKEN_SOURCE_MANAGED,
            default_token_source=str(payload.get("token_source") or TOKEN_SOURCE_MANAGED),
        )
        if settings.openrouter_api_key is None and uses_managed_models and not remote_evaluation:
            raise ValueError("Managed model routing is not configured.")
        gateway = ModelGateway(
            BudgetRuntime(
                budgets,
                username=user.username,
                budget_id=snapshot.id,
                generation=snapshot.generation,
                phase="setup",
                wait_timeout=SETUP_WAIT_SECONDS,
            )
        )
        # The box's hold is priced on its whole lifetime, so a check gets the
        # time its own work can take rather than the run's configured ceiling.
        lifetime_seconds = math.ceil(preflight_lifetime_seconds(payload))
        bind_protected_sandbox(
            gateway,
            settings,
            owner_id=document["id"],
            lifetime_seconds=lifetime_seconds,
            parent_hosts=PACKAGE_REGISTRY_HOSTS if is_repo_payload(payload) else (),
            provider=payload.get("sandbox_provider"),
        )
        protected = gateway.protect_payload(
            payload,
            managed_key=settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else "",
            allow_private_tools=settings.discover_allow_private,
            # The setup check must answer in minutes. A batch can take hours,
            # outlive the check's sandbox, and leave an unsettled hold.
            allow_batch=False,
        )
        # The ledger keys the run's sandbox hold by this identity, and a settled
        # or released hold from an earlier run would block a new dispatch under
        # the same key, so every run of the row carries its own number.
        identity = f"{document['id']}-{attempt}"
        if is_repo_payload(protected):
            report_preflight_phase("sandbox")
            # Staging empties the secret list, so read the gate's inputs first.
            secret_bindings = dict((protected["target"].get("repo") or {}).get("secret_bindings") or {})
            secret_allowed_hosts = repo_secret_allowed_hosts(protected)
            protected, staged = stage_repository(
                protected,
                username=user.username,
                binding_id=request.execution_budget_id,
                engine=engine,
                require_constraints=True,
            )
            gate_failures = check_repository(
                staged.snapshot.archive,
                staged.secrets,
                place=sandbox_place(settings.local_mode),
                bindings=secret_bindings,
                allowed_hosts=secret_allowed_hosts,
            )
            if gate_failures:
                raise RepoGateError(gate_failures)
            infer_repo_setup(protected, staged, gateway)
            bind_repo_scorer(
                protected,
                staged,
                gateway,
                owner_id=f"preflight-{identity}",
                managed_key=settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else "",
            )
        result = _verify_anything(gateway, protected, scope=request.scope, identity=identity)
        if is_repo_payload(protected):
            result["environment"] = protected["target"]["repo"]["environment"]
        states = {check["status"] for check in result["checks"]}
        if not states or not states.issubset({"failed", "pending", "succeeded"}):
            raise ValueError("Setup did not return a complete set of readiness checks.")
        status = "failed" if "failed" in states else "pending" if "pending" in states else "succeeded"
        if status == "pending" and "pending_reason" not in result:
            pending_check = next(check for check in result["checks"] if check["status"] == "pending")
            category = "usage_reconciliation" if pending_check.get("key") == "usage" else "setup_incomplete"
            message = pending_check.get("message") or "Setup checks are incomplete."
            result["pending_reason"] = _pending_reason(category, message, pending_check.get("field"))
    except UsagePendingError:
        status, result = (
            "pending",
            _mark_usage_pending(
                {},
                "Previous provider or sandbox usage is awaiting final confirmation.",
            ),
        )
    except BudgetInsufficientError as error:
        field = BALANCE_FIELD if isinstance(error, AccountFundsError) else None
        result = {"checks": [_check(BUDGET_CHECK_KEY, "failed", str(error), field)]}
    except RepoGateError as error:
        result = {
            "checks": [
                {
                    **_check("setup", "failed", failure.reason),
                    "message_code": failure.code,
                    "message_params": failure.params,
                }
                for failure in error.failures
            ]
        }
    except DomainError as error:
        result = {
            "checks": [
                {**_check("setup", "failed", error.detail), "message_code": error.code, "message_params": error.params}
            ]
        }
    except Exception as error:
        result = {"checks": [_check("setup", "failed", str(error))]}
    finally:
        if staged is not None:
            staged.close_scorers()
            staged.cleanup()
        if gateway is not None:
            report_preflight_phase("usage")
            try:
                gateway.close()
            except UsagePendingError:
                status = "pending"
                result = _mark_usage_pending(
                    result,
                    "Provider or sandbox usage is awaiting final confirmation.",
                )
            except Exception as error:
                status = "failed"
                result.setdefault("checks", []).append(_check("runtime", "failed", str(error)))
    return status, result


@contextmanager
def judge_scorer(
    payload: dict[str, Any],
    *,
    username: str,
    budget_id: str,
    generation: int,
    owner_id: str,
    image: str | None,
    engine: Any,
    case_count: int,
    before_scoring: Callable[[dict[str, Any], StagedRepository], None] | None = None,
) -> Iterator[tuple[dict[str, Any], StagedRepository, ModelGateway]]:
    """Stage a judge session's repository and bind its scorer in the parent (SKY-696).

    The steps are those of a setup check and of the worker: the same
    credentials, the same metered gateway and the same repository gate. No
    guest opens. The repository scorer runs each version in its own run box
    and scores it in a separate metric box.

    Args:
        payload: The session's stored run payload, credentials still protected.
        username: Account that pays.
        budget_id: The session's attached budget.
        generation: The budget's current execution generation.
        owner_id: Identity that names this attempt's paid operations.
        image: The sandbox image pinned for the session, or ``None``.
        engine: Database holding the ledger, the vaults and the connections.
        case_count: How many cases the attempt scores, for the box lifetime.
        before_scoring: Called after staging and before any paid box opens;
            it raises to refuse the attempt.

    Yields:
        The protected payload, the staged repository and the gateway.

    Raises:
        RepoGateError: When the repository gate refuses the tree.
    """
    source = copy.deepcopy(payload)
    alias_blackbox_metric_model(source)
    source = resolve_execution_credentials(
        source, username=username, binding_id=budget_id, vault=ProtectedCredentialVault(engine=engine)
    )
    source = prepare_protected_credentials(
        source,
        username=username,
        vault=ProviderKeyVault(engine=engine),
        default_token_source=str(source.get("token_source") or TOKEN_SOURCE_MANAGED),
    )
    uses_managed_models = payload_uses_token_source(
        source,
        TOKEN_SOURCE_MANAGED,
        default_token_source=str(source.get("token_source") or TOKEN_SOURCE_MANAGED),
    )
    if settings.openrouter_api_key is None and uses_managed_models:
        raise ValueError("Managed model routing is not configured.")
    managed_key = settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else ""
    gateway = ModelGateway(
        BudgetRuntime(
            BudgetService(engine=engine),
            username=username,
            budget_id=budget_id,
            generation=generation,
            phase="run",
            wait_timeout=SETUP_WAIT_SECONDS,
        )
    )
    staged: StagedRepository | None = None
    try:
        bind_protected_sandbox(
            gateway,
            settings,
            owner_id=owner_id,
            image=image,
            lifetime_seconds=math.ceil(judge_lifetime_seconds(source, case_count)),
            parent_hosts=PACKAGE_REGISTRY_HOSTS,
            provider=source.get("sandbox_provider"),
        )
        protected = gateway.protect_payload(
            source,
            managed_key=managed_key,
            allow_private_tools=settings.discover_allow_private,
            allow_batch=False,
        )
        # Staging empties the secret list, so read the gate's inputs first.
        secret_bindings = dict((protected["target"].get("repo") or {}).get("secret_bindings") or {})
        secret_allowed_hosts = repo_secret_allowed_hosts(protected)
        protected, staged = stage_repository(
            protected, username=username, binding_id=budget_id, engine=engine, require_constraints=True
        )
        gate_failures = check_repository(
            staged.snapshot.archive,
            staged.secrets,
            place=sandbox_place(settings.local_mode),
            bindings=secret_bindings,
            allowed_hosts=secret_allowed_hosts,
            require_version=staged.require_version,
        )
        if gate_failures:
            raise RepoGateError(gate_failures)
        if before_scoring is not None:
            before_scoring(protected, staged)
        infer_repo_setup(protected, staged, gateway)
        bind_repo_scorer(protected, staged, gateway, owner_id=owner_id, managed_key=managed_key)
        yield protected, staged, gateway
    finally:
        if staged is not None:
            staged.close_scorers()
            staged.cleanup()
        try:
            gateway.close()
        except UsagePendingError:
            # The caller's evidence finish sees the budget's pending operations.
            logger.info("judge attempt %s left usage awaiting confirmation", owner_id)

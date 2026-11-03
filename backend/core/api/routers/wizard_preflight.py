"""Validate wizard inputs before sharing the same paid setup path as API submissions.

Also serves the unified workspace contract check: a static one that reads
the code without running it, and a sandboxed one that runs the starting
version through the code and metric on a few non-held-out samples under
the same budget, evidence and protected sandbox as a setup check.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Iterator
from queue import Empty, Queue
from threading import Thread
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError

from ...billing.byok_bridge import _model_config_dicts
from ...billing.byok_vault import safe_connection_params
from ...i18n import request_locale, t, t_locale
from ...models import BlackboxRunRequest, BlackboxScorer
from ...models.common import ModelConfig
from ...models.unified import (
    ContractCheckItem,
    ContractCheckRequest,
    ContractCheckResponse,
    UnifiedRunRequest,
    resolve_optimizer,
)
from ...service_gateway.optimization.blackbox.check_terminal import CheckTerminalRecorder, recording
from ...service_gateway.optimization.unified.contract import (
    contract_fingerprint,
    in_locale,
    static_contract_check,
    with_message_codes,
)
from ...storage.preflights import setup_seed
from ..agent_engine import fill_proposer_host_cli
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_billing import enforce_local_mode_sources, enforce_raw_local_mode_sources, normalize_model_token_sources
from ..preflight_execution import (
    BUDGET_CHECK_KEY,
    WizardPreflightCheck,
    WizardPreflightRequest,
    WizardPreflightResponse,
    run_preflight,
)
from ..preflight_progress import progress_observer
from ..rate_limit import enforce_submission_rate
from .submissions import _admit_byok_models, _default_unified_seed, _materialize_unified_cases

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
AcceptLanguageDep = Annotated[str | None, Header(include_in_schema=False)]
logger = logging.getLogger(__name__)
CONTRACT_CHECK_KEY = "metric.contract"
# Fixed English texts that setup checks, sandbox images and budget errors emit
# without a catalog code. Stored evidence keeps them as written, so they are
# coded and worded here, at response time, for every caller's locale.
FIXED_MESSAGE_CODES = {
    "Evaluator loads; scoring waits for the first generated candidate.": "submit.preflight.scorer_ready",
    "Endpoint validated; its response will be checked with the first generated candidate.": (
        "submit.preflight.endpoint_ready"
    ),
    "Harness readiness verified; candidate execution waits for the first generated candidate.": (
        "submit.preflight.target_ready"
    ),
    "Sandbox usage is awaiting confirmation.": "submit.preflight.sandbox_usage_pending",
    "Provider or sandbox usage is awaiting final confirmation.": "submit.preflight.usage_pending",
    "Provider or sandbox usage is still awaiting final confirmation.": "submit.preflight.usage_still_pending",
    "Previous provider or sandbox usage is awaiting final confirmation.": "submit.preflight.previous_usage_pending",
    "The spending limit cannot cover setup.": "submit.preflight.budget_setup",
    "The total budget has no remaining allowance.": "submit.preflight.budget_exhausted",
    "The account has no available funds.": "submit.preflight.no_funds",
    "The account has no available funds for additional work.": "submit.preflight.no_funds_more",
    "The account has no funded wallet.": "submit.preflight.no_wallet",
    "The account cannot fund the next operation.": "submit.preflight.cannot_fund",
    "The account has no available funds for a preview.": "submit.preflight.no_funds_preview",
    "Setup checks are incomplete.": "submit.preflight.incomplete",
    "Setup execution failed.": "submit.preflight.execution_failed",
    "The runtime did not return setup evidence.": "submit.preflight.no_evidence",
    "Setup did not return a complete set of readiness checks.": "submit.preflight.incomplete_checks",
}
# The budget texts with amounts in them (``billing.budgets._admit`` and
# ``billing.runtime.BudgetRuntime._wait_cannot_help``); their groups become the
# catalog message's params.
BUDGET_LIMIT_MESSAGE = re.compile(
    r"The next operation needs up to (?P<needed>\$[\d,]+\.\d{2}), but only "
    r"(?P<remaining>\$[\d,]+\.\d{2}) of the (?P<limit>\$[\d,]+\.\d{2}) limit remain\."
)
BUDGET_LIMIT_CODE = "submit.preflight.budget_limit"
BUDGET_SANDBOXES_MESSAGE = re.compile(
    r"The next operation needs up to (?P<needed>\$[\d,]+\.\d{2}), but this check's sandboxes hold "
    r"(?P<held>\$[\d,]+\.\d{2}) of the (?P<remaining>\$[\d,]+\.\d{2}) that remain\."
)
BUDGET_SANDBOXES_CODE = "submit.preflight.budget_sandboxes"


def _message_code(message: str | None) -> tuple[str, dict[str, str]] | None:
    """Find the catalog code and params behind a known English setup-check text.

    Args:
        message: A setup-check message as stored.

    Returns:
        The code and its params, or None when the text is not a known one.
    """
    code = FIXED_MESSAGE_CODES.get(message or "")
    if code is not None:
        return code, {}
    for pattern, pattern_code in (
        (BUDGET_LIMIT_MESSAGE, BUDGET_LIMIT_CODE),
        (BUDGET_SANDBOXES_MESSAGE, BUDGET_SANDBOXES_CODE),
    ):
        match = pattern.fullmatch(message or "")
        if match:
            return pattern_code, match.groupdict()
    return None


def _contract_failure(contract: dict[str, Any], spec: UnifiedRunRequest | None) -> ContractCheckItem | None:
    """Find the coded item behind a failed ``metric.contract`` check.

    Args:
        contract: The contract outcome under the setup check's ``workflow_result``.
        spec: The canonical unified spec, used to recover codes an older
            sandbox image left out; None when it is not known.

    Returns:
        The first failed item carrying a message code, or None.
    """
    try:
        parsed = ContractCheckResponse.model_validate(contract)
    except ValidationError:
        return None
    if spec is not None and any(item.message_code is None for item in parsed.checks):
        parsed = with_message_codes(parsed, static_contract_check(spec))
    return next((item for item in parsed.checks if item.status == "failed" and item.message_code), None)


def _localized_check(
    check: WizardPreflightCheck, code: str, failure: ContractCheckItem, locale: str
) -> WizardPreflightCheck:
    """Word a failed contract check in ``locale`` and attach its message code.

    Args:
        check: The ``metric.contract`` check as the setup check stored it.
        code: The failure's message code.
        failure: The coded contract item the check reports.
        locale: The caller's locale tag.

    Returns:
        The check with its message in ``locale``, keeping the exact error detail.
    """
    message = t_locale(locale, code, **failure.message_params)
    if failure.detail:
        message = f"{message}\n{failure.detail}"
    return check.model_copy(
        update={
            "message": message,
            "message_code": code,
            "message_params": dict(failure.message_params),
        }
    )


def _coded_check(check: WizardPreflightCheck, locale: str) -> WizardPreflightCheck:
    """Word a check whose message is a known fixed English text in ``locale``.

    Args:
        check: One setup check as stored.
        locale: The caller's locale tag.

    Returns:
        The localized check, or the unchanged check when its text is not known
        or its existing code belongs to a non-setup check.
    """
    coded = _message_code(check.message)
    if check.message_code is not None:
        if check.key == "setup":
            return check.model_copy(
                update={"message": t_locale(locale, check.message_code, **(check.message_params or {}))}
            )
        return check
    if coded is None:
        return check
    code, params = coded
    return check.model_copy(
        update={"message": t_locale(locale, code, **params), "message_code": code, "message_params": params}
    )


def _localized_contract(contract: dict[str, Any], spec: UnifiedRunRequest | None, locale: str) -> dict[str, Any]:
    """Word the nested contract outcome's coded items in ``locale``.

    Args:
        contract: The contract outcome under the setup check's ``workflow_result``.
        spec: The canonical unified spec, used to recover codes an older
            sandbox image left out; None when it is not known.
        locale: The caller's locale tag.

    Returns:
        A new contract outcome in ``locale``, or ``contract`` itself when it
        does not parse.
    """
    try:
        parsed = ContractCheckResponse.model_validate(contract)
    except ValidationError:
        return contract
    if spec is not None and any(item.message_code is None for item in parsed.checks):
        parsed = with_message_codes(parsed, static_contract_check(spec))
    return in_locale(parsed, locale).model_dump(mode="json")


def localize_preflight(
    response: WizardPreflightResponse, spec: UnifiedRunRequest | None, locale: str
) -> WizardPreflightResponse:
    """Render a setup check's catalog messages in the caller's locale.

    The contract check words its failure in the Hebrew base wherever it runs
    (often a sandbox with no request locale), other checks store fixed English
    texts, and the stored evidence is replayed to later callers in any
    language, so the wording happens here at response time. The stored
    evidence itself is not changed.

    Args:
        response: The setup check result.
        spec: The canonical unified spec the check ran, if known.
        locale: The caller's locale tag.

    Returns:
        The result with its known messages and nested contract checks worded in ``locale``.
    """
    update: dict[str, Any] = {"checks": [_coded_check(check, locale) for check in response.checks]}
    reason = response.pending_reason
    coded_reason = _message_code(reason.message) if reason is not None else None
    if reason is not None and coded_reason is not None:
        update["pending_reason"] = reason.model_copy(
            update={"message": t_locale(locale, coded_reason[0], **coded_reason[1])}
        )
    workflow = response.workflow_result or {}
    contract = workflow.get("contract")
    if isinstance(contract, dict):
        update["workflow_result"] = {**workflow, "contract": _localized_contract(contract, spec, locale)}
        failure = (
            _contract_failure(contract, spec)
            if any(check.key == CONTRACT_CHECK_KEY and check.status == "failed" for check in response.checks)
            else None
        )
        if failure is not None and failure.message_code is not None:
            update["checks"] = [
                _localized_check(check, failure.message_code, failure, locale)
                if check.key == CONTRACT_CHECK_KEY and check.status == "failed"
                else check
                for check in update["checks"]
            ]
    return response.model_copy(update=update)


def create_wizard_preflight_router(*, job_store: Any) -> APIRouter:
    """Build paid Continue validation using the same ledger as the eventual run."""
    router = APIRouter()

    @router.post("/wizard/preflight", response_model=WizardPreflightResponse, response_model_exclude_none=True)
    def preflight(
        request: WizardPreflightRequest, user: AuthenticatedUserDep, accept_language: AcceptLanguageDep = None
    ) -> WizardPreflightResponse:
        """Validate the current wizard stage once and return durable budget-aware evidence.

        Args:
            request: Current authored inputs and the user's shared budget revision.
            user: Authenticated owner used for data, credentials, and spending.
            accept_language: The browser's languages, used when the body names no locale.

        Returns:
            Scoped checks, reusable evidence identity, and authoritative setup spend,
            worded in the caller's locale.
        """
        try:
            response, spec = _checked_preflight(request, user)
        except DomainError as error:
            if error.code.startswith(("submission.byok_", "billing.byok_")):
                error.detail = t_locale(request_locale(request.locale, accept_language), error.code, **error.params)
            raise
        return localize_preflight(response, spec, request_locale(request.locale, accept_language))

    def _checked_preflight(
        request: WizardPreflightRequest, user: AuthenticatedUser
    ) -> tuple[WizardPreflightResponse, UnifiedRunRequest | None]:
        """Run one setup check, with its messages still in the Hebrew base.

        Args:
            request: Current authored inputs and the user's shared budget revision.
            user: Authenticated owner used for data, credentials, and spending.

        Returns:
            The check result, and the canonical unified spec it ran (None for other workflows).
        """
        enforce_submission_rate(user.username)
        spec: UnifiedRunRequest | None = None
        payload = copy.deepcopy(request.payload)
        payload["username"] = user.username
        payload["execution_budget_id"] = request.execution_budget_id
        payload["execution_budget_revision"] = request.execution_budget_revision
        # A unified run is seeded from its task, not its budget, so the
        # evidence fingerprint matches what POST /optimize later recomputes.
        if payload.get("seed") is None and request.workflow != "unified":
            payload["seed"] = setup_seed(request.execution_budget_id)
        if request.scope == "execution" or request.workflow == "unified":
            model: type[BaseModel] = BlackboxRunRequest if request.workflow == "anything" else UnifiedRunRequest
            try:
                typed = model.model_validate(payload)
            except ValidationError as error:
                raise RequestValidationError(error.errors()) from error
            # The dump below writes host_cli, so fill it first, as the submit route does.
            fill_proposer_host_cli(job_store, user.username, typed)
            requested_source = typed.token_source
            configs, _ = normalize_model_token_sources(typed)
            enforce_local_mode_sources(typed, requested_source, configs)
            if isinstance(typed, UnifiedRunRequest):
                reflection = typed.models.reflection_model
                novelty = bool(typed.optimizer.shinka and typed.optimizer.shinka.novelty)
                _materialize_unified_cases(typed, job_store=job_store, username=user.username)
                _default_unified_seed(typed)
                spec = typed
            else:
                reflection = typed.reflection_model_settings
                novelty = bool(typed.shinka and typed.shinka.novelty)
            _admit_byok_models(job_store, user.username, configs, reflection, novelty)
            payload = typed.model_dump(mode="json", by_alias=True)
        else:
            # The evaluation check skips the full run request, but the sandbox
            # still reads every scorer field, so it gets the run's defaults and limits.
            try:
                scorer = BlackboxScorer.model_validate(payload.get("scorer") or {})
            except ValidationError as error:
                raise RequestValidationError(
                    [{**detail, "loc": ("scorer", *detail["loc"])} for detail in error.errors()]
                ) from error
            enforce_raw_local_mode_sources(payload)
            payload["scorer"] = scorer.model_dump(mode="json", by_alias=True)
            configs = []
            reflection = None
            for raw in _model_config_dicts(payload):
                try:
                    config = ModelConfig.model_validate(raw)
                except ValidationError as error:
                    raise RequestValidationError(error.errors()) from error
                config.token_source = config.token_source or payload.get("token_source", "managed")
                config.base_url = None
                config.extra = safe_connection_params(config.extra)
                configs.append(config)
                if raw is payload.get("reflection_model_config"):
                    reflection = config
            shinka = payload.get("shinka")
            novelty = bool(shinka.get("novelty")) if isinstance(shinka, dict) else False
            _admit_byok_models(job_store, user.username, configs, reflection, novelty)
        return run_preflight(request.model_copy(update={"payload": payload}), user, job_store), spec

    @router.post("/wizard/preflight/stream", include_in_schema=False)
    def stream_preflight(
        request: WizardPreflightRequest, user: AuthenticatedUserDep, accept_language: AcceptLanguageDep = None
    ) -> StreamingResponse:
        """Stream actual setup phases followed by the existing authoritative result.

        Args:
            request: Current wizard inputs and budget revision.
            user: Authenticated owner of the setup request.
            accept_language: The browser's languages, used when the body names no locale.

        Returns:
            An event stream that never dispatches a second validation attempt.
        """
        events: Queue[dict[str, Any] | None] = Queue()
        locale = request_locale(request.locale, accept_language)

        def phase(value: str) -> None:
            """Queue a public execution phase for this request."""
            events.put({"event": "phase", "data": {"phase": value}})

        def execute() -> None:
            """Finish admitted work even if the viewer disconnects, preserving its billing evidence."""
            token = progress_observer.set(phase)
            try:
                result = preflight(request, user, accept_language)
                events.put({"event": "result", "data": result.model_dump(mode="json", exclude_none=True)})
            except DomainError as error:
                events.put(
                    {
                        "event": "error",
                        "data": {
                            "detail": error.detail,
                            "code": error.code,
                            "params": error.params,
                            "status": error.status_code,
                        },
                    }
                )
            except HTTPException as error:
                events.put({"event": "error", "data": {"detail": error.detail}})
            except RequestValidationError:
                events.put({"event": "error", "data": {"detail": t_locale(locale, "submit.preflight.invalid_config")}})
            except Exception:
                # The viewer sees only a generic line, so the cause must reach the log.
                logger.exception("wizard setup check stream failed")
                events.put(
                    {"event": "error", "data": {"detail": t_locale(locale, "submit.preflight.could_not_finish")}}
                )
            finally:
                progress_observer.reset(token)
                events.put(None)

        def stream() -> Iterator[str]:
            """Yield phase events and keep the connection alive during long provider calls."""
            Thread(target=execute, daemon=True, name="wizard-preflight").start()
            while True:
                try:
                    event = events.get(timeout=10)
                except Empty:
                    yield ": waiting\n\n"
                    continue
                if event is None:
                    break
                yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @router.post("/optimizations/contract-check/static", response_model=ContractCheckResponse)
    def contract_check_static(
        request: ContractCheckRequest, user: AuthenticatedUserDep, accept_language: AcceptLanguageDep = None
    ) -> ContractCheckResponse:
        """Check the workspace code's signatures and metric model without running it.

        Args:
            request: The unified spec to check.
            user: Authenticated caller.
            accept_language: The browser's languages, used when the body names no locale.

        Returns:
            The static checks; ``samples`` is always empty.
        """
        enforce_submission_rate(user.username)
        return in_locale(static_contract_check(request.spec), request_locale(request.locale, accept_language))

    def _run_contract_check(
        request: ContractCheckRequest, user: AuthenticatedUser, locale: str
    ) -> ContractCheckResponse:
        """Run the sandboxed contract check shared by the plain and streaming routes.

        Args:
            request: The unified spec and how many samples to score.
            user: Authenticated owner whose budget pays for the check.
            locale: The caller's locale tag.

        Returns:
            Every check item and the scored samples, worded in ``locale``.

        Raises:
            HTTPException: 402 when the spec names no execution budget, or the
                budget errors ``run_preflight`` raises.
        """
        enforce_submission_rate(user.username)
        return in_locale(_sandboxed_check(request, user, locale), locale)

    def _sandboxed_check(request: ContractCheckRequest, user: AuthenticatedUser, locale: str) -> ContractCheckResponse:
        """Run the static check, then the sampled one under the spec's budget.

        Args:
            request: The unified spec and how many samples to score.
            user: Authenticated owner whose budget pays for the check.
            locale: The caller's locale tag, for the 402 detail and the setup error it quotes.

        Returns:
            Every check item and the scored samples, in the Hebrew base.

        Raises:
            HTTPException: 402 when the spec names no execution budget.
        """
        spec = request.spec
        static = static_contract_check(spec)
        if not static.ok:
            return static
        if spec.execution_budget_id is None or spec.execution_budget_revision is None:
            raise HTTPException(status_code=402, detail=t_locale(locale, "submit.preflight.contract_budget_missing"))
        result, _ = _checked_preflight(
            WizardPreflightRequest(
                scope="evaluation",
                workflow="unified",
                payload=spec.model_dump(mode="json", exclude_none=True),
                execution_budget_id=spec.execution_budget_id,
                execution_budget_revision=spec.execution_budget_revision,
                contract_sample_count=request.sample_count,
            ),
            user,
        )
        contract = (result.workflow_result or {}).get("contract")
        if isinstance(contract, dict):
            checked = with_message_codes(ContractCheckResponse.model_validate(contract), static)
            return checked.model_copy(update={"terminal": result.terminal})
        failure = next((check for check in result.checks if check.status != "succeeded"), None)
        error = failure.message if failure is not None and failure.message else result.status
        coded = _message_code(error)
        if coded is not None:
            error = t_locale(locale, coded[0], **coded[1])
        # A spent budget is not a code mismatch; it is fixed by raising the limit.
        code = (
            "submit.unified.contract_budget"
            if failure is not None and failure.key == BUDGET_CHECK_KEY
            else "submit.unified.contract_failed"
        )
        return ContractCheckResponse(
            ok=False,
            optimizer_resolved=resolve_optimizer(spec.optimizer.choice, spec.task.artifact.kind),
            uses_llm=static.uses_llm,
            checks=[
                *static.checks,
                ContractCheckItem(
                    key="workspace.run",
                    status="pending" if result.status == "pending" else "failed",
                    message=t(code, error=error),
                    message_code=code,
                    message_params={"error": error},
                    # The message already carries the error; a detail repeating it
                    # printed the sentence twice under the editor.
                    detail=None,
                ),
            ],
            fingerprint=contract_fingerprint(spec),
            terminal=result.terminal,
        )

    @router.post("/optimizations/contract-check", response_model=ContractCheckResponse)
    def contract_check(
        request: ContractCheckRequest, user: AuthenticatedUserDep, accept_language: AcceptLanguageDep = None
    ) -> ContractCheckResponse:
        """Run the starting version through the code and metric on a few samples, in the sandbox.

        The check runs as an evaluation-scope setup check under the spec's
        execution budget, so it is billed, deduplicated and sandboxed exactly
        like ``/wizard/preflight``. A contract mismatch is a 200 with
        ``ok=false``; only auth, billing and quota failures are 4xx.

        Args:
            request: The unified spec and how many samples to score.
            user: Authenticated owner whose budget pays for the check.
            accept_language: The browser's languages, used when the body names no locale.

        Returns:
            Every check item and the scored samples.
        """
        return _run_contract_check(request, user, request_locale(request.locale, accept_language))

    @router.post("/optimizations/contract-check/stream", include_in_schema=False)
    def contract_check_stream(
        request: ContractCheckRequest, user: AuthenticatedUserDep, accept_language: AcceptLanguageDep = None
    ) -> StreamingResponse:
        """Stream the check's real phases, then the same result the plain route returns.

        Args:
            request: The unified spec and how many samples to score.
            user: Authenticated owner whose budget pays for the check.
            accept_language: The browser's languages, used when the body names no locale.

        Returns:
            An event stream of ``phase`` and ``terminal`` events, then one
            ``result`` or ``error``.
        """
        events: Queue[dict[str, Any] | None] = Queue()
        locale = request_locale(request.locale, accept_language)

        def phase(value: str) -> None:
            """Queue a public execution phase for this request."""
            events.put({"event": "phase", "data": {"phase": value}})

        def terminal(event: dict[str, Any]) -> None:
            """Queue one live, already-redacted line of the check's sandbox transcript."""
            events.put({"event": "terminal", "data": event})

        def execute() -> None:
            """Finish admitted work even if the viewer disconnects, preserving its billing evidence."""
            token = progress_observer.set(phase)
            try:
                with recording(CheckTerminalRecorder(emit=terminal)):
                    result = _run_contract_check(request, user, locale)
                events.put({"event": "result", "data": result.model_dump(mode="json")})
            except HTTPException as error:
                events.put({"event": "error", "data": {"detail": error.detail}})
            except RequestValidationError:
                events.put({"event": "error", "data": {"detail": t_locale(locale, "submit.preflight.invalid_config")}})
            except Exception:
                # The viewer sees only a generic line, so the cause must reach the log.
                logger.exception("workspace contract check stream failed")
                events.put(
                    {
                        "event": "error",
                        "data": {"detail": t_locale(locale, "submit.preflight.contract_could_not_finish")},
                    }
                )
            finally:
                progress_observer.reset(token)
                events.put(None)

        def stream() -> Iterator[str]:
            """Yield phase events and keep the connection alive during long provider calls."""
            Thread(target=execute, daemon=True, name="contract-check").start()
            while True:
                try:
                    event = events.get(timeout=10)
                except Empty:
                    yield ": waiting\n\n"
                    continue
                if event is None:
                    break
                yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    return router

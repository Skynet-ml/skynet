"""Validate wizard inputs before sharing the same paid setup path as API submissions."""

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
from pydantic import ValidationError

from ...billing.byok_bridge import _model_config_dicts
from ...billing.byok_vault import safe_connection_params
from ...i18n import request_locale, t_locale
from ...models import BlackboxRunRequest, BlackboxScorer
from ...models.common import ModelConfig
from ...storage.preflights import setup_seed
from ..agent_engine import fill_proposer_host_cli
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..model_billing import enforce_local_mode_sources, enforce_raw_local_mode_sources, normalize_model_token_sources
from ..preflight_execution import (
    WizardPreflightCheck,
    WizardPreflightRequest,
    WizardPreflightResponse,
    run_preflight,
)
from ..preflight_progress import progress_observer
from ..rate_limit import enforce_submission_rate
from .submissions import (
    _admit_byok_models,
    _materialize_repo_cases,
    staged_repo_cases,
)

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
AcceptLanguageDep = Annotated[str | None, Header(include_in_schema=False)]
logger = logging.getLogger(__name__)
# Fixed English texts that setup checks, sandbox images and budget errors emit
# without a catalog code. Stored evidence keeps them as written, so they are
# coded and worded here, at response time, for every caller's locale.
FIXED_MESSAGE_CODES = {
    "Set the model with llm(model=...) in the metric code.": "submit.preflight.scorer_model",
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


def localize_preflight(response: WizardPreflightResponse, locale: str) -> WizardPreflightResponse:
    """Render a setup check's catalog messages in the caller's locale.

    Checks store fixed English texts, and the stored evidence is replayed to
    later callers in any language, so the wording happens here at response
    time. The stored evidence itself is not changed.

    Args:
        response: The setup check result.
        locale: The caller's locale tag.

    Returns:
        The result with its known messages worded in ``locale``.
    """
    update: dict[str, Any] = {"checks": [_coded_check(check, locale) for check in response.checks]}
    reason = response.pending_reason
    coded_reason = _message_code(reason.message) if reason is not None else None
    if reason is not None and coded_reason is not None:
        update["pending_reason"] = reason.model_copy(
            update={"message": t_locale(locale, coded_reason[0], **coded_reason[1])}
        )
    return response.model_copy(update=update)


def checked_preflight(
    request: WizardPreflightRequest, user: AuthenticatedUser, job_store: Any
) -> WizardPreflightResponse:
    """Run one setup check, with its messages still in the Hebrew base.

    Args:
        request: Current authored inputs and the user's shared budget revision.
        user: Authenticated owner used for data, credentials, and spending.
        job_store: Store that holds staged data, connectors, the budget and the evidence.

    Returns:
        The check result.
    """
    enforce_submission_rate(user.username)
    payload = copy.deepcopy(request.payload)
    payload["username"] = user.username
    payload["execution_budget_id"] = request.execution_budget_id
    payload["execution_budget_revision"] = request.execution_budget_revision
    if payload.get("seed") is None:
        payload["seed"] = setup_seed(request.execution_budget_id)
    if request.scope == "execution":
        try:
            typed = BlackboxRunRequest.model_validate(payload)
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from error
        # The dump below writes host_cli, so fill it first, as the submit route does.
        fill_proposer_host_cli(job_store, user.username, typed)
        requested_source = typed.token_source
        configs, _ = normalize_model_token_sources(typed)
        enforce_local_mode_sources(typed, requested_source, configs)
        _materialize_repo_cases(typed, job_store=job_store, username=user.username)
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
        # The scorer dry run reads the first case, so a staged dataset is
        # inlined here exactly as the execution check inlines it.
        staged_id = payload.pop("staged_dataset_id", None)
        if staged_id and not payload.get("cases"):
            payload["cases"] = staged_repo_cases(
                staged_id,
                input_fields=payload.get("input_fields") or [],
                expected_field=payload.get("expected_field"),
                job_store=job_store,
                username=user.username,
            )
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
    return run_preflight(request.model_copy(update={"payload": payload}), user, job_store)


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
        target = request.payload.get("target")
        repo = target.get("repo") if isinstance(target, dict) else None
        # Same rule as ``/blackbox/run`` (SKY-693): only the MCP draft flow builds
        # an ``mcp`` contract, and that flow calls ``checked_preflight`` directly.
        if isinstance(repo, dict) and repo.get("contract_source") == "mcp":
            repo["contract_source"] = "setup_screen"
        try:
            response = checked_preflight(request, user, job_store)
        except DomainError as error:
            if error.code.startswith(("submission.byok_", "billing.byok_")):
                error.detail = t_locale(request_locale(request.locale, accept_language), error.code, **error.params)
            raise
        return localize_preflight(response, request_locale(request.locale, accept_language))

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

    return router

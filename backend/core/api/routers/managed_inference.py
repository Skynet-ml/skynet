"""Serve authenticated Mac model calls through the hosted managed spending boundary."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ...billing.budgets import MAX_CENTS, BudgetError, BudgetService
from ...billing.data_policy import data_policy_for
from ...billing.model_dispatch import OpenRouterDispatcher
from ...billing.operation_pricing import ChargePolicy, UnpricedOperationError
from ...billing.runtime import BudgetRuntime, UsagePendingError
from ...config import settings
from ..auth import AuthenticatedUser, get_authenticated_user
from ..model_catalog import require_known_model
from .execution_budgets import budget_http_error

UserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def create_managed_inference_router(*, job_store: Any) -> APIRouter:
    """Build the account-authenticated inference endpoint.

    Args:
        job_store: Hosted database owner for the wallet and operation ledger.

    Returns:
        A router accepting the linked account's normal authenticated requests.
    """
    router = APIRouter()

    @router.post("/models/inference/v1/chat/completions", include_in_schema=False)
    def completion(body: dict[str, Any], user: UserDep) -> Response:
        """Reserve and settle one managed model call for the signed-in account.

        Args:
            body: OpenAI-compatible request with a concrete model and output limit.
            user: Account resolved from the session or desktop link, never the body.

        Returns:
            The provider's JSON or SSE response after authoritative settlement.

        Raises:
            HTTPException: When hosted inference, pricing, or account funds are unavailable.
        """
        if settings.local_mode or settings.openrouter_api_key is None:
            raise HTTPException(503, "Managed inference is unavailable on this server.")
        model = str(body.get("model") or "").removeprefix("openrouter/")
        require_known_model(f"openrouter/{model}")
        service = BudgetService(engine=job_store.engine)
        budget = None
        try:
            budget = service.create(user.username, MAX_CENTS, idempotency_key=str(uuid4()), uncapped=True)
            runtime = BudgetRuntime(service, username=user.username, budget_id=budget.id, generation=0, phase="setup")
            with httpx.Client(timeout=settings.lm_request_timeout_seconds, trust_env=False) as client:
                dispatcher = OpenRouterDispatcher(
                    runtime,
                    api_key=settings.openrouter_api_key.get_secret_value(),
                    model=model,
                    role="agent",
                    policy=ChargePolicy("managed_model"),
                    client=client,
                    data_policy=data_policy_for(user.username),
                )
                result = dispatcher.dispatch("/chat/completions", {**body, "model": model})
            return Response(result.body, status_code=result.status, media_type=result.content_type)
        except BudgetError as error:
            raise budget_http_error(error) from error
        except UnpricedOperationError as error:
            raise HTTPException(422, str(error)) from error
        except httpx.HTTPError as error:
            raise HTTPException(502, "The inference provider is unavailable.") from error
        except UsagePendingError as error:
            raise HTTPException(424, "Provider usage is awaiting settlement.") from error
        finally:
            if budget is not None:
                service.stop_admission(budget.id, user.username, reason="inference_finished")

    return router

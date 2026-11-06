"""Dispatch bounded model attempts and reconcile provider-reported dollar usage."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx

from .byok_upstreams import OPENROUTER, ProviderUpstream
from .data_policy import DataPolicy, apply_data_policy
from .model_batch import BatchCollector
from .model_terms import excluded_from_managed, managed_model_refusal
from .openrouter_float import notify_managed_refusal
from .openrouter_quotes import PricedRequest, fetch_endpoint_prices, price_embedding_request, price_text_request
from .operation_pricing import (
    ChargePolicy,
    OperationQuote,
    UnpricedOperationError,
    exact_nonnegative,
    json_fingerprint,
    operation_quote,
)
from .responses_adapter import price_responses_request, responses_failure, responses_receipt
from .runtime import BudgetRuntime, PaidResult
from .usage_tags import header_tags

MODEL_ATTEMPT_HEADER = "x-app-model-attempt-id"

logger = logging.getLogger(__name__)
_BYOK_DEFAULT_MAX_TOKENS = 4000

# How long a route answers a funds refusal locally instead of asking OpenRouter
# again. The SDK retries every failure, and a drained account refuses each
# retry the same way, so the retries are cut off here rather than re-sent.
FUNDS_REFUSAL_HOLD_SECONDS = 60.0

FUNDS_BUSY = "provider_budget_busy"
MANAGED_FUNDS_EXHAUSTED = "managed_funds_exhausted"
BYOK_FUNDS_EXHAUSTED = "byok_funds_exhausted"

# Anthropic caches a prompt prefix only up to an explicit breakpoint; OpenAI,
# Gemini and DeepSeek cache on their own. Optimizers resend the same system
# prompt and demos for every example, so marking them turns repeats into reads
# at a tenth of the input price.
_EPHEMERAL = {"type": "ephemeral"}


@dataclass(frozen=True)
class ModelHTTPResult:
    """Retain the protocol response without putting generated text in the billing ledger."""

    status: int
    content_type: str
    body: bytes


def funds_refusal(managed: bool, retry_after: str | None) -> tuple[bytes, float]:
    """Explain an OpenRouter 402 and say how long to hold it.

    OpenRouter answers 402 for three different causes: an account out of
    credits, a key at its spend limit, and a request held back while in-flight
    spend settles. Only the last carries ``Retry-After``, and only that one
    clears on its own.

    Args:
        managed: Whether the platform's key made the call, rather than the
            user's own OpenRouter key.
        retry_after: The response's ``Retry-After`` header, if any.

    Returns:
        The relay error body and the seconds to answer it locally.
    """
    wait = None
    if retry_after is not None:
        try:
            wait = max(1.0, min(float(retry_after), FUNDS_REFUSAL_HOLD_SECONDS))
        except ValueError:
            wait = None
    if wait is not None:
        code = FUNDS_BUSY
        message = f"The model provider is holding spend while earlier requests settle. Retry in {wait:.0f} seconds."
    elif managed:
        code = MANAGED_FUNDS_EXHAUSTED
        message = (
            "The managed model provider refused this request for lack of funds. It was not charged, and "
            "your balance is untouched. The team has been alerted; try again shortly."
        )
    else:
        code = BYOK_FUNDS_EXHAUSTED
        message = (
            "Your OpenRouter account or key has run out of credits. Add credits on OpenRouter or raise "
            "the key's limit, then retry."
        )
    body = json.dumps({"error": {"type": code, "message": message}}).encode()
    return body, wait if wait is not None else FUNDS_REFUSAL_HOLD_SECONDS


def response_usage(body: bytes, content_type: str) -> tuple[str | None, dict[str, Any] | None]:
    """Extract actual usage from either JSON or the final OpenAI/Anthropic SSE event.

    Args:
        body: Complete provider response bytes from the trusted HTTP client.
        content_type: Provider response content type.

    Returns:
        Generation identity and the most recent usage block, if reported.
    """
    if "text/event-stream" in content_type:
        events = []
        for line in body.decode("utf-8").splitlines():
            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                try:
                    events.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
    else:
        try:
            events = [json.loads(body)]
        except ValueError:
            return None, None
    identity = None
    usage = None
    for event in events:
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if isinstance(message, dict):
            identity = message.get("id") or identity
            if isinstance(message.get("usage"), dict):
                usage = {**(usage or {}), **message["usage"]}
        identity = event.get("id") or identity
        if isinstance(event.get("usage"), dict):
            usage = {**(usage or {}), **event["usage"]}
    return identity if isinstance(identity, str) else None, usage


def generation_charge(record: Mapping[str, Any]) -> Decimal | None:
    """Count an external upstream bill only when the provider identifies a BYOK generation.

    Args:
        record: Exact generation metadata returned by the trusted provider.

    Returns:
        Actual combined provider USD, or None while its billing scope is ambiguous.
    """
    if record.get("total_cost") is None:
        return None
    charge = exact_nonnegative(record["total_cost"])
    byok = record.get("is_byok")
    upstream = record.get("upstream_inference_cost")
    if byok is True:
        return charge + exact_nonnegative(upstream) if upstream is not None else None
    if byok is False or upstream in {None, 0, "0"}:
        return charge
    return None


def usage_charge(usage: Mapping[str, Any]) -> Decimal | None:
    """Use a final usage receipt only when it describes the complete provider charge."""
    if usage.get("cost") is None:
        return None
    details = usage.get("cost_details") or {}
    if not isinstance(details, Mapping):
        return None
    return generation_charge(
        {
            "total_cost": usage["cost"],
            "is_byok": usage.get("is_byok"),
            "upstream_inference_cost": details.get("upstream_inference_cost"),
        }
    )


def response_complete(body: bytes, content_type: str, finish_reason_ends: bool = False) -> bool:
    """Require a terminal protocol marker before treating streamed usage as final.

    Args:
        body: Raw provider response.
        content_type: Provider response content type.
        finish_reason_ends: Also accept a chunk with a ``finish_reason``. BYOK
            providers bill the user directly, and not every OpenAI-compatible
            provider promises a closing ``[DONE]`` line.

    Returns:
        Whether the response reached its end.
    """
    if "text/event-stream" not in content_type:
        return True
    for line in body.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return True
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "message_stop":
            return True
        if (
            finish_reason_ends
            and isinstance(event, dict)
            and isinstance(event.get("choices"), list)
            and any(isinstance(choice, dict) and choice.get("finish_reason") for choice in event["choices"])
        ):
            return True
    return False


def mark_prompt_cache(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Mark the shared prefix of an Anthropic chat request as cacheable.

    Places a breakpoint on the last system message and on the message just
    before the final turn, which covers the instructions and few-shot demos an
    optimizer repeats across examples. Prefixes below the provider minimum are
    silently not cached and cost nothing extra.

    Args:
        path: Protocol path of the request.
        body: Final request body; left untouched unless it is eligible.

    Returns:
        The body, or a copy carrying ``cache_control`` breakpoints.
    """
    messages = body.get("messages")
    if (
        path != "/chat/completions"
        or not str(body.get("model", "")).startswith("anthropic/")
        or not isinstance(messages, list)
        or any(
            isinstance(part, dict) and "cache_control" in part
            for message in messages
            if isinstance(message, dict) and isinstance(message.get("content"), list)
            for part in message["content"]
        )
    ):
        return body
    marked = [dict(message) if isinstance(message, dict) else message for message in messages]
    targets = {
        index for index, message in enumerate(marked) if isinstance(message, dict) and message.get("role") == "system"
    }
    targets = {max(targets)} if targets else set()
    if len(marked) >= 3:
        targets.add(len(marked) - 2)
    for index in targets:
        message = marked[index]
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            message["content"] = [{"type": "text", "text": content, "cache_control": _EPHEMERAL}]
        elif (
            isinstance(content, list)
            and content
            and isinstance(content[-1], dict)
            and content[-1].get("type") == "text"
        ):
            message["content"] = [*content[:-1], {**content[-1], "cache_control": _EPHEMERAL}]
    return {**body, "messages": marked}


def data_policy_refusal(model: str, policy: DataPolicy) -> bytes:
    """Explain a routing refusal in terms of the user's setting, not the platform's OpenRouter account.

    Args:
        model: The model no permitted provider serves.
        policy: The setting that excluded every provider.

    Returns:
        A relay error body the SDK surfaces like a provider error.
    """
    scope = "zero-data-retention" if policy == "zdr" else "no-training, no-retention"
    message = (
        f"No provider for {model} meets your model data privacy setting ({scope} providers only). "
        "Choose another model, or change Model data privacy in Settings."
    )
    return json.dumps({"error": {"type": "data_policy_unavailable", "message": message}}).encode()


def batch_rates(quote: OperationQuote) -> tuple[Decimal, Decimal]:
    """Read the quoted prompt and completion list prices used to share a batch bill.

    Args:
        quote: The request's reservation quote, carrying its endpoint prices.

    Returns:
        Prompt and completion USD per token, zero when the quote lacks them.
    """
    endpoints = quote.price_snapshot.get("endpoints") or [{}]
    pricing = endpoints[0].get("pricing") or {}
    if isinstance(pricing, list):
        pricing = pricing[0] if pricing else {}
    return exact_nonnegative(pricing.get("prompt", "0")), exact_nonnegative(pricing.get("completion", "0"))


class ProviderDispatcher:
    """Keep credentials and the spending ledger outside optimizer-controlled code."""

    def __init__(
        self,
        runtime: BudgetRuntime,
        *,
        api_key: str,
        model: str,
        role: str,
        policy: ChargePolicy,
        client: httpx.Client,
        batch: BatchCollector | None = None,
        data_policy: DataPolicy | None = None,
        embeddings: bool = False,
        upstream: ProviderUpstream = OPENROUTER,
    ) -> None:
        """Bind one model role to a trusted transport and pricing policy.

        Args:
            runtime: Shared setup or run spending authority.
            api_key: Managed or account-owned OpenRouter credential, never exposed to guests.
            model: Exact model slug permitted for this role.
            role: Task, judge, or optimization attribution.
            policy: The approved model charge and external-cost scope.
            client: Non-retrying HTTP client with an enforced request timeout.
            batch: Economy-mode collector; managed chat calls then wait for an
                OpenRouter batch at half price instead of answering directly.
            data_policy: The owner's provider privacy setting, forced onto every
                request; ``None`` leaves BYOK routing to the caller's own account.
            embeddings: Serve only ``/embeddings`` for an embeddings model,
                instead of the chat protocols.
            upstream: Fixed provider endpoint and permitted protocols.
        """
        self.runtime = runtime
        if upstream != OPENROUTER and policy.kind != "byok_model":
            raise UnpricedOperationError("Only BYOK routes can use a non-OpenRouter provider.")
        self._api_key = api_key
        self.model = model
        self.role = role
        self.policy = policy
        self._client = client
        self._attempt_quotes: dict[tuple[str, str, int], tuple[str, PricedRequest]] = {}
        self._refusal: tuple[bytes, float] | None = None
        self._batch = batch if upstream == OPENROUTER else None
        self._data_policy = data_policy if upstream == OPENROUTER else None
        self.embeddings = embeddings
        self.upstream = upstream

    def dispatch(
        self,
        path: str,
        request: Mapping[str, Any],
        *,
        operation_key: str | None = None,
        attempt: int = 0,
        protocol_headers: Mapping[str, str] | None = None,
    ) -> ModelHTTPResult:
        """Send one final request only after its physical attempt is fully reserved.

        Args:
            path: Chat completions, Anthropic messages, Responses, or embeddings protocol path.
            request: Fully resolved SDK body including final token and tool settings.
            operation_key: Optional stable physical-attempt identity for a transport replay.
            attempt: Physical retry number within a stable logical operation.
            protocol_headers: Non-secret Anthropic protocol version or beta headers.

        Returns:
            Provider response after its actual charge is settled or held for reconciliation.
        """
        paths = {"/embeddings"} if self.embeddings and self.upstream.embeddings else set(self.upstream.paths)
        if self.embeddings and not self.upstream.embeddings:
            paths = set()
        if not self.embeddings and self.policy.kind == "managed_model" and self.upstream == OPENROUTER:
            paths.add("/messages")
        if path not in paths or request.get("model") != self.model:
            raise UnpricedOperationError(
                "This scoped route cannot dispatch a different model or API operation.",
                code="submission.byok_route_unsupported",
            )
        if self.policy.kind == "managed_model" and excluded_from_managed(self.model):
            return ModelHTTPResult(403, "application/json", managed_model_refusal(self.model))
        if self._refusal is not None:
            body, until = self._refusal
            if time.monotonic() < until:
                return ModelHTTPResult(402, "application/json", body)
            self._refusal = None
        request_identity = json_fingerprint({"body": dict(request), "headers": dict(protocol_headers or {})})
        attempt_key = (path, operation_key, attempt) if operation_key is not None else None
        cached = self._attempt_quotes.get(attempt_key) if attempt_key is not None else None
        if cached is not None:
            if cached[0] != request_identity:
                raise UnpricedOperationError("A model attempt identity cannot be reused for a different request.")
            priced = cached[1]
        else:
            routed = dict(request) if self._data_policy is None else apply_data_policy(request, self._data_policy)
            if self.upstream.slug == "openrouter":
                routed = mark_prompt_cache(path, routed)
            if self.policy.kind != "byok_model":
                catalog = fetch_endpoint_prices(self.model, client=self._client)
                pricer = {"/responses": price_responses_request, "/embeddings": price_embedding_request}
                priced = pricer.get(path, price_text_request)(routed, catalog, self.policy)
            else:
                if routed.get("n", 1) != 1 or routed.get("best_of", 1) != 1:
                    raise UnpricedOperationError("Multiple completions require independently reserved requests.")
                if path != "/embeddings":
                    if (
                        "max_tokens" in routed
                        and "max_completion_tokens" in routed
                        and routed["max_tokens"] != routed["max_completion_tokens"]
                    ):
                        raise UnpricedOperationError("Conflicting output token limits cannot authorize work.")
                    if path == "/messages" and "max_completion_tokens" in routed:
                        # Anthropic's Messages API rejects unknown top-level fields, so carry the
                        # caller's limit over to the one field it accepts.
                        routed["max_tokens"] = routed.pop("max_completion_tokens")
                    field = (
                        "max_output_tokens"
                        if path == "/responses"
                        else "max_tokens"
                        if path == "/messages"
                        else "max_completion_tokens"
                        if "max_completion_tokens" in routed
                        else "max_tokens"
                    )
                    if path == "/messages" and field not in routed:
                        routed[field] = _BYOK_DEFAULT_MAX_TOKENS
                    limit = routed.get(field)
                    if field in routed and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
                        raise UnpricedOperationError("An explicit positive output token limit is required.")
                priced = PricedRequest(
                    routed,
                    operation_quote(
                        routed, Decimal(0), self.policy, {"provider": self.upstream.slug, "model": self.model}
                    ),
                )
            priced = replace(
                priced,
                quote=replace(
                    priced.quote,
                    price_snapshot={
                        **priced.quote.price_snapshot,
                        "credential_fingerprint": json_fingerprint(self._api_key),
                    },
                ),
            )
            if attempt_key is not None:
                self._attempt_quotes[attempt_key] = (request_identity, priced)
        physical_key = operation_key or str(uuid4())
        tags = header_tags(protocol_headers)
        started = time.monotonic()
        headers = {"Content-Type": "application/json", **dict(self.upstream.extra_headers)}
        if self.upstream.auth == "x-api-key":
            headers["x-api-key"] = self._api_key
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(
            {
                name: value
                for name, value in (protocol_headers or {}).items()
                if self.upstream == OPENROUTER and name.lower() in {"anthropic-version", "anthropic-beta"}
            }
        )
        if self.upstream.slug == "anthropic":
            for name, value in (protocol_headers or {}).items():
                if (
                    name.lower() == "anthropic-beta"
                    and isinstance(value, str)
                    and len(value) <= 1024
                    and re.fullmatch(r"[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*", value)
                ):
                    headers["anthropic-beta"] = value

        # Batches reject streaming, and BYOK batches bill the user's own
        # provider under terms this ledger cannot split per request.
        batched = (
            self._batch is not None
            and self.upstream == OPENROUTER
            and path == "/chat/completions"
            and not priced.body.get("stream")
            and self.policy.kind == "managed_model"
        )

        def send() -> PaidResult[ModelHTTPResult]:
            """Send exactly once and retain trusted usage even for a provider error response."""
            chunks = []
            interrupted = False
            if batched:
                assert self._batch is not None
                answer = self._batch.run(priced.body, batch_rates(priced.quote))
                return settle(answer.status, "application/json", answer.body, answer.interrupted, None)
            with self._client.stream(
                "POST", f"{self.upstream.base_url}{path}", headers=headers, json=priced.body, follow_redirects=False
            ) as response:
                content_type = response.headers.get("content-type", "application/json")
                try:
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)  # noqa: PERF402 - Preserve received chunks if the iterator raises.
                except httpx.HTTPError:
                    interrupted = True
                content = b"".join(chunks)
                status = response.status_code
                retry_after = response.headers.get("retry-after")
            return settle(status, content_type, content, interrupted, retry_after)

        def settle(
            status: int, content_type: str, content: bytes, interrupted: bool, retry_after: str | None
        ) -> PaidResult[ModelHTTPResult]:
            """Turn a provider answer into its measured charge and the relay's response."""
            failure = None
            if path == "/responses":
                identity, usage, complete = responses_receipt(content, content_type)
                interrupted = interrupted or not complete
                failure = responses_failure(content, content_type)
            else:
                interrupted = interrupted or not response_complete(
                    content, content_type, finish_reason_ends=self.policy.kind == "byok_model"
                )
                identity, usage = response_usage(content, content_type)
            evidence: dict[str, Any] = {
                "provider": self.upstream.slug,
                "model": self.model,
                "request_id": identity,
                "status": status,
                "usage": usage,
                "interrupted": interrupted,
            }
            # A batched answer's wait is mostly the batch window and polling, not the model.
            if batched:
                evidence["batched"] = True
            else:
                evidence["latency_ms"] = round((time.monotonic() - started) * 1000)
            if tags:
                evidence["tags"] = tags
            if failure is not None:
                evidence["failure"] = failure
            amount = None
            if (
                self.policy.kind != "byok_model"
                and not interrupted
                and usage is not None
                and usage.get("cost") is not None
            ):
                amount = usage_charge(usage)
            if self.policy.kind != "byok_model" and self.upstream == OPENROUTER and amount is None and identity:
                reconciled = self.reconcile(identity)
                if reconciled is not None:
                    amount, record = reconciled
                    evidence["generation"] = record
            # An error answer that names no generation and reports no usage is the provider
            # refusing the request before anything billable existed (rate limits, moderation,
            # malformed bodies); holding its coverage would wait on a receipt never written.
            refused = status >= 400 and identity is None and usage is None and not interrupted
            result = ModelHTTPResult(status, content_type, content)
            if status == 404 and self._data_policy is not None and b"data policy" in content.lower():
                result = ModelHTTPResult(404, "application/json", data_policy_refusal(self.model, self._data_policy))
            if self.upstream == OPENROUTER and status == 402 and not interrupted:
                managed = self.policy.kind == "managed_model"
                body, hold = funds_refusal(managed, retry_after)
                self._refusal = (body, time.monotonic() + hold)
                result = ModelHTTPResult(402, "application/json", body)
                if managed and retry_after is None:
                    logger.error(
                        "OpenRouter refused a managed %s call with 402: the account or the key is out of "
                        "funds. Check the balance and Auto Top-Up.",
                        self.model,
                    )
                    notify_managed_refusal(self.model)
            if interrupted:
                result = ModelHTTPResult(
                    502,
                    "application/json",
                    json.dumps(
                        {
                            "error": {
                                "type": "provider_interrupted",
                                "message": "The provider response ended before completion.",
                            }
                        }
                    ).encode(),
                )
            if failure is not None and amount is None:
                logger.warning("The provider failed a %s response (%s): %s", self.model, identity, failure)
            return PaidResult(
                value=result,
                provider_usd=amount,
                evidence=evidence,
                provider_request_id=identity,
                refused=refused,
                failure=failure if amount is None and not refused else None,
            )

        return self.runtime.execute(
            priced.quote,
            self.policy,
            send,
            operation_key=physical_key,
            cost_kind="model",
            role=self.role,
            attempt=attempt,
        )

    def stop_batch(self) -> None:
        """Release callers waiting on an economy batch when the run closes."""
        if self._batch is not None:
            self._batch.stop()

    def reconcile(self, generation_id: str) -> tuple[Decimal, dict[str, Any]] | None:
        """Read provider billing evidence without launching or repeating inference.

        Args:
            generation_id: Identity returned by the original paid request.

        Returns:
            Measured USD and usage-only evidence, or None while the bill is unavailable.
        """
        if self.upstream != OPENROUTER:
            return None
        try:
            response = self._client.get(
                "https://openrouter.ai/api/v1/generation",
                params={"id": generation_id},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            record = response.json().get("data")
            if (
                not isinstance(record, dict)
                or record.get("id") != generation_id
                or record.get("model") != self.model
                or record.get("total_cost") is None
            ):
                return None
            safe = {
                key: record[key]
                for key in (
                    "id",
                    "model",
                    "provider_name",
                    "total_cost",
                    "upstream_inference_cost",
                    "native_tokens_prompt",
                    "native_tokens_completion",
                    "native_tokens_reasoning",
                    "num_search_results",
                    "is_byok",
                )
                if key in record
            }
            amount = generation_charge(record)
            return (amount, safe) if amount is not None else None
        except (httpx.HTTPError, ValueError):
            return None


OpenRouterDispatcher = ProviderDispatcher

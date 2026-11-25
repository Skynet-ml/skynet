"""Tests for the ``capacity`` code on a chat turn that the provider could not serve (SKY-705)."""

from __future__ import annotations

from typing import Any

import litellm
import pytest

from core.service_gateway.agents import generalist as generalist_module
from core.service_gateway.agents.generalist import WizardState, _turn_error_payload
from core.service_gateway.agents.pi_bridge import PiBridgeError

PROVIDER_QUOTA = "provider_quota"


def _rate_limit(message: str = "Rate limit reached for requests") -> litellm.RateLimitError:
    """Build litellm's typed 429.

    Args:
        message: The provider text.

    Returns:
        The error.
    """
    return litellm.RateLimitError(message, llm_provider="openai", model="gpt-4o")


def test_rate_limit_error_is_capacity() -> None:
    """litellm's typed 429 gets the capacity code."""
    payload = _turn_error_payload(_rate_limit())
    assert payload["code"] == "capacity"
    assert payload["error"]


def test_service_unavailable_error_is_capacity() -> None:
    """litellm's typed 503 gets the capacity code."""
    exc = litellm.ServiceUnavailableError("upstream busy", llm_provider="openai", model="gpt-4o")
    assert _turn_error_payload(exc)["code"] == "capacity"


def test_anthropic_overloaded_529_is_capacity() -> None:
    """Anthropic's 529 surfaces as a 500-class error; its status code and text classify it."""
    exc = litellm.InternalServerError(
        '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
        llm_provider="anthropic",
        model="claude",
    )
    exc.status_code = 529
    assert _turn_error_payload(exc)["code"] == "capacity"


def test_capacity_error_below_the_pi_bridge_is_found() -> None:
    """The provider error under a Pi bridge failure and a task group still classifies."""
    bridge = PiBridgeError("The model call failed")
    bridge.__cause__ = _rate_limit()
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [bridge])
    assert _turn_error_payload(group)["code"] == "capacity"


def test_spent_quota_429_is_provider_quota() -> None:
    """A 429 for a spent quota does not clear with time, so it gets the provider quota code."""
    exc = _rate_limit("You exceeded your current quota, please check your plan and billing details.")
    assert _turn_error_payload(exc)["code"] == PROVIDER_QUOTA


def test_openai_429_with_a_billing_link_is_capacity() -> None:
    """A rate limit that only links to the billing page still clears with time."""
    exc = _rate_limit(
        "Rate limit reached for gpt-4o in organization org-test on requests per min (RPM): Limit 3, Used 3, "
        "Requested 1. Please try again in 20s. Visit https://platform.openai.com/account/billing to add a "
        "payment method."
    )
    assert _turn_error_payload(exc)["code"] == "capacity"


def test_billing_details_429_is_provider_quota() -> None:
    """A 429 that asks for billing details does not clear with time."""
    exc = _rate_limit("Your request was rejected. Please check your plan and billing details.")
    assert _turn_error_payload(exc)["code"] == PROVIDER_QUOTA


def test_anthropic_spend_cap_429_is_provider_quota() -> None:
    """Anthropic's spend-cap 429 stays until the cap changes, so it gets the provider quota code."""
    exc = _rate_limit(
        "You have reached your specified API usage limits. You will regain access on 2026-12-01 at 00:00 UTC."
    )
    assert _turn_error_payload(exc)["code"] == PROVIDER_QUOTA


def test_empty_byok_key_is_provider_quota() -> None:
    """An own key with no credits gets the provider quota code, not the Skynet wallet's."""
    exc = RuntimeError("Insufficient credits. Add more using https://openrouter.ai/settings/credits")
    assert _turn_error_payload(exc)["code"] == PROVIDER_QUOTA


def test_relay_byok_refusal_below_a_wrapper_is_provider_quota() -> None:
    """The relay's own-key refusal code is found through the exception chain."""
    exc = PiBridgeError("turn failed")
    exc.__cause__ = RuntimeError('{"code": "byok_funds_exhausted"}')
    assert _turn_error_payload(exc)["code"] == PROVIDER_QUOTA


def test_managed_funds_refusal_is_not_a_provider_quota() -> None:
    """Skynet's own provider budget running out is not the user's wallet."""
    exc = RuntimeError('{"code": "managed_funds_exhausted", "message": "Our team is alerted."}')
    assert "code" not in _turn_error_payload(exc)


def test_context_overflow_keeps_its_code() -> None:
    """A classified context overflow keeps ``context_too_long``."""
    exc = litellm.ContextWindowExceededError("prompt is too long", model="gpt-4o", llm_provider="openai")
    assert _turn_error_payload(exc)["code"] == "context_too_long"


def test_plain_error_has_no_code() -> None:
    """An unclassified failure keeps the text-only payload."""
    assert _turn_error_payload(RuntimeError("boom")) == {"error": "RuntimeError: boom"}


async def test_turn_stream_sends_capacity_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that ends on a provider 429 streams an ``error`` event with the capacity code."""

    async def fake_drive(**_kwargs: Any) -> str:
        """Fail as the provider does when it has no capacity.

        Raises:
            litellm.RateLimitError: Always.
        """
        raise _rate_limit()

    monkeypatch.setattr(generalist_module, "_drive_generalist_agent", fake_drive)
    monkeypatch.setattr(generalist_module, "_build_generalist_lm", object)

    events = [
        ev
        async for ev in generalist_module.run_generalist_agent(
            wizard_state=WizardState(), chat_history=[], user_message="go", locale="en"
        )
    ]

    errors = [ev["data"] for ev in events if ev["event"] == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "capacity"
    assert not any(ev["event"] == "done" for ev in events)

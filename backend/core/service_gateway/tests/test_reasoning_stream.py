"""Tests for provider reasoning-token stream normalization and the reply-language lookup."""

from __future__ import annotations

from types import SimpleNamespace

from core.service_gateway.agents.code import _extract_reasoning_token, _reply_language


def test_reply_language_maps_locales_with_hebrew_fallback() -> None:
    """Map UI locales to language names; unknown or missing locales fall back to Hebrew."""
    assert _reply_language("he") == "Hebrew"
    assert _reply_language("en") == "English"
    assert _reply_language("en-GB") == "English"
    assert _reply_language("fr-CA") == "French"
    assert _reply_language("zh-Hans") == "Chinese"
    assert _reply_language("HE") == "Hebrew"
    assert _reply_language(None) == "Hebrew"
    assert _reply_language("") == "Hebrew"
    assert _reply_language("xx") == "Hebrew"


def test_reasoning_token_extracts_streamed_openrouter_summary() -> None:
    """Extract OpenRouter reasoning-summary deltas for the shared agent stream."""
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_details=[
                        {
                            "type": "reasoning.summary",
                            "summary": "I compared the options.",
                        }
                    ]
                )
            )
        ]
    )

    assert _extract_reasoning_token(chunk) == "I compared the options."

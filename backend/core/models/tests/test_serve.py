"""Tests for ServeRequest, ServeResponse, and ServeInfoResponse models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.models.serve import ServeInfoResponse, ServeRequest, ServeResponse


def test_serve_request_accepts_non_empty_inputs() -> None:
    """Verify ServeRequest accepts a non-empty inputs dict."""
    req = ServeRequest(inputs={"question": "What is 2+2?"})

    assert req.inputs == {"question": "What is 2+2?"}


def test_serve_request_rejects_empty_inputs() -> None:
    """Verify ServeRequest validation rejects an empty inputs dict."""
    with pytest.raises(ValidationError, match="At least one input"):
        ServeRequest(inputs={})


def test_serve_request_rejects_model_config_override() -> None:
    """Verify ServeRequest refuses a caller-chosen model."""
    with pytest.raises(ValidationError, match="model settings it was optimized with"):
        ServeRequest.model_validate({"inputs": {"q": "hi"}, "model_config_override": {"name": "gpt-4o"}})


def test_serve_request_multiple_inputs_accepted() -> None:
    """Verify ServeRequest accepts a multi-field inputs dict."""
    req = ServeRequest(inputs={"q": "hi", "context": "some text"})

    assert len(req.inputs) == 2


def test_serve_response_stores_all_fields() -> None:
    """Verify ServeResponse round-trips all fields through construction."""
    resp = ServeResponse(
        optimization_id="abc123",
        outputs={"answer": "4"},
        input_fields=["question"],
        output_fields=["answer"],
        model_used="gpt-4o-mini",
    )

    assert resp.optimization_id == "abc123"
    assert resp.outputs == {"answer": "4"}
    assert resp.model_used == "gpt-4o-mini"


def test_serve_info_response_instructions_default_none() -> None:
    """Verify ServeInfoResponse defaults instructions to None."""
    info = ServeInfoResponse(
        optimization_id="abc123",
        module_name="predict",
        optimizer_name="gepa",
        model_name="gpt-4o-mini",
        input_fields=["question"],
        output_fields=["answer"],
    )

    assert info.instructions is None

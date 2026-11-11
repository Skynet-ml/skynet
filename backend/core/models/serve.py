"""Request/response models for the /serve/* inference endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, model_validator


def reject_model_override(data: Any) -> Any:
    """Refuse a request body that tries to pick the inference model.

    A compiled program's prompts and demos were tuned against one model and its
    settings, so running it on anything else silently serves a different program.

    Args:
        data: Raw request body before field validation.

    Returns:
        The unchanged body.

    Raises:
        ValueError: When the body carries ``model_config_override``.
    """
    if isinstance(data, dict) and "model_config_override" in data:
        raise ValueError(
            "model_config_override is not supported: a program always runs on the model settings it was optimized with."
        )
    return data


class ServeRequest(BaseModel):
    """Request payload for running inference on an optimized program."""

    inputs: dict[str, Any] = Field(..., description="Input field values matching the program's signature.")
    max_cost_cents: int | None = Field(
        validation_alias=AliasChoices("max_cost_cents", "max_cost_credits"),
        default=None,
        ge=1,
        le=1_000_000_000,
        strict=True,
        description="Maximum cents authorized for this one invocation; required for protected runs.",
    )

    @model_validator(mode="before")
    @classmethod
    def _locked_model(cls, data: Any) -> Any:
        """Refuse a caller-chosen model; the program runs on the one it was optimized with.

        Args:
            data: Raw request body.

        Returns:
            The unchanged body.
        """
        return reject_model_override(data)

    @model_validator(mode="after")
    def _ensure_inputs(self) -> ServeRequest:
        """Reject inference requests with no input fields.

        Returns:
            The validated request instance.

        Raises:
            ValueError: When ``inputs`` is empty.
        """
        if not self.inputs:
            raise ValueError("At least one input field is required.")
        return self


class WorkflowNodeTrace(BaseModel):
    """One node's execution record from a workflow inference or dry run."""

    node_id: str
    kind: str
    name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] | None = None
    elapsed_ms: float = 0.0
    error: str | None = None


class ServeResponse(BaseModel):
    """Response payload from program inference.

    ``input_fields`` / ``output_fields`` are lists of signature field *names*.
    They are NOT the same shape as ``ColumnMapping.inputs`` / ``outputs``, which
    are ``{field_name: column_name}`` dicts used at the submission layer. The
    naming differs on purpose: here we are echoing the servable program's
    signature, not binding dataset columns.
    """

    optimization_id: str
    outputs: dict[str, Any]
    input_fields: list[str]
    output_fields: list[str]
    model_used: str
    node_traces: list[WorkflowNodeTrace] | None = Field(
        default=None,
        description="Per-node execution trace, present only for workflow runs.",
    )
    cents_charged: Decimal | None = Field(
        default=None,
        description="Exact settled cents charged for this invocation.",
    )
    budget: dict[str, Any] | None = Field(
        default=None,
        description="Closed one-request execution budget for this invocation.",
    )


class ServeInfoResponse(BaseModel):
    """Metadata about a servable program (no inference call)."""

    optimization_id: str
    module_name: str
    optimizer_name: str
    model_name: str
    input_fields: list[str]
    output_fields: list[str]
    instructions: str | None = None
    demo_count: int = 0
    sample_inputs: dict[str, str] = Field(default_factory=dict)
    serving_enabled: bool = False
    endpoint_url: str | None = None


# Body of PUT /serve/{id}/enabled. Serving is opt-in per run because each call
# runs the best version in a billed sandbox.
class ServingEnabledRequest(BaseModel):
    enabled: bool


# Reply of PUT /serve/{id}/enabled: the stored switch state after the update.
class ServingEnabledResponse(BaseModel):
    optimization_id: str
    serving_enabled: bool

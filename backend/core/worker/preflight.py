"""Run explicit DSPy workflow previews inside the protected guest."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import dspy
from dspy.streaming import StreamListener, StreamResponse

from ..models.common import ModelConfig
from ..models.submissions import ToolSource
from ..models.workflow import WorkflowSpec
from ..service_gateway.language_models import build_language_model, usage_by_model_from_history
from ..service_gateway.optimization.workflow import (
    WorkflowNodeExecutionError,
    build_workflow_program,
    capture_node_traces,
    validate_workflow,
)


def _workflow_result(
    traces: list[dict[str, Any]], model: str, outputs: dict[str, Any] | None, error: Exception | None = None
) -> dict[str, Any]:
    """Serialize actual workflow port values and failure traces for the canvas.

    Args:
        traces: Node records captured during the actual sample prediction.
        model: Configured task model used for the prediction.
        outputs: Actual output anchor values, or None when prediction failed.
        error: Actual node or metric failure, when present.

    Returns:
        A response-safe WorkflowDryRunResponse-shaped dictionary.
    """

    def values(raw: dict[str, Any]) -> dict[str, Any]:
        """Bound authored objects before sending their representations to the canvas."""
        return {
            name: value
            if value is None or isinstance(value, bool | int) or (isinstance(value, float) and math.isfinite(value))
            else str(value)[:2000]
            for name, value in raw.items()
        }

    return {
        "outputs": values(outputs) if outputs is not None else None,
        "node_traces": [
            {
                **trace,
                "inputs": values(trace.get("inputs") or {}),
                "outputs": values(trace["outputs"]) if trace.get("outputs") is not None else None,
            }
            for trace in traces
        ],
        "model_used": model,
        "error": str(error) if error else None,
        "failed_node_id": error.node_id if isinstance(error, WorkflowNodeExecutionError) else None,
    }


def run_workflow_preview(
    payload: dict[str, Any], on_token: Callable[[dict[str, str]], None] | None = None
) -> dict[str, Any]:
    """Execute explicit debug inputs once inside the existing protected guest.

    Args:
        payload: Authored workflow, explicit input values, and scoped model route.
        on_token: Optional receiver for actual output chunks from a streaming call.

    Returns:
        Actual output ports, node traces, errors, and observed model usage.
    """
    workflow = WorkflowSpec.model_validate(payload["workflow"])
    model = ModelConfig.model_validate(payload["model_config"])
    traces: list[dict[str, Any]] = []
    language_model = None
    prediction = None
    error = None
    try:
        validate_workflow(workflow)
        names = workflow.input_field_names()
        missing = [name for name in names if name not in payload["inputs"]]
        if missing:
            raise ValueError(f"Missing workflow inputs: {missing}.")
        inputs = {name: payload["inputs"][name] for name in names}
        program, _hashes = build_workflow_program(
            workflow,
            tool_source=ToolSource.model_validate(payload["tool_source"]) if payload.get("tool_source") else None,
            dataset=None,
        )
        language_model = build_language_model(model, disable_cache=True)
        stream = None
        if on_token is not None:
            try:
                stream = dspy.streamify(
                    program,
                    stream_listeners=[
                        StreamListener(signature_field_name=name) for name in workflow.output_field_names()
                    ],
                    async_streaming=False,
                )
            except Exception:
                # Listener discovery can fail before any execution; replaying after dispatch would spend twice.
                stream = None
        with capture_node_traces() as traces, dspy.context(lm=language_model):
            if stream is None:
                prediction = program(**inputs)
            else:
                for item in stream(**inputs):
                    if isinstance(item, StreamResponse):
                        on_token({"field": item.signature_field_name, "chunk": item.chunk})
                    elif isinstance(item, dspy.Prediction):
                        prediction = item
                if prediction is None:
                    raise RuntimeError("The workflow stream did not return a final prediction.")
    except Exception as caught:
        error = caught
        nested = [caught]
        while nested:
            item = nested.pop()
            if isinstance(item, WorkflowNodeExecutionError):
                error = item
                break
            nested.extend(getattr(item, "exceptions", ()))
            if item.__cause__ is not None:
                nested.append(item.__cause__)
    result = _workflow_result(
        traces,
        model.normalized_identifier(),
        {name: getattr(prediction, name, None) for name in workflow.output_field_names()}
        if prediction is not None
        else None,
        error,
    )
    result["usage_by_model"] = [
        {"model": name, "input_tokens": counts[0], "output_tokens": counts[1]}
        for name, counts in (usage_by_model_from_history(language_model) or {}).items()
    ]
    return result


def run_dspy_preflight(
    payload: dict[str, Any], descriptor: dict[str, Any], on_token: Callable[[dict[str, str]], None] | None = None
) -> dict[str, Any]:
    """Run one explicit workflow preview inside the protected guest.

    Args:
        payload: Authored workflow, explicit input values, and scoped model route.
        descriptor: Check selected by the parent; only ``workflow_preview`` remains.
        on_token: Optional streaming receiver for explicit workflow previews.

    Returns:
        The workflow check and its actual preview result.

    Raises:
        ValueError: When the parent asks for any other check.
    """
    if descriptor.get("kind") != "workflow_preview":
        raise ValueError("Unsupported DSPy setup check.")
    result = run_workflow_preview(payload, on_token if descriptor.get("stream") else None)
    return {
        "checks": [{"key": "workflow", "status": "failed" if result["error"] else "succeeded"}],
        "workflow_result": result,
    }

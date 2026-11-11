"""Run one completed-program interaction inside the protected guest."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import dspy
from dspy.streaming import StreamListener, StreamResponse

from ..api.routers._helpers import _materialize_program, sanitize_node_traces
from ..models.artifacts import ProgramArtifact
from ..models.common import ColumnMapping, ModelConfig
from ..registry.resolvers import ResolverError, resolve_module_factory
from ..service_gateway.language_models import build_language_model
from ..service_gateway.optimization.blackbox.repo_serving import (
    INTERACTION_KIND as REPO_SERVE_KIND,
)
from ..service_gateway.optimization.blackbox.repo_serving import (
    run_repo_serve,
)
from ..service_gateway.optimization.data import load_metric_from_code, load_signature_from_code
from ..service_gateway.optimization.workflow import capture_node_traces


def _response_outputs(prediction: Any, output_fields: list[str], inputs: dict[str, Any]) -> dict[str, Any]:
    """Extract declared response fields from one DSPy prediction.

    Args:
        prediction: Completed DSPy prediction.
        output_fields: Declared output field names.
        inputs: Input values excluded from undeclared prediction output.

    Returns:
        JSON-compatible output mapping.
    """
    if output_fields:
        return {field: getattr(prediction, field, None) for field in output_fields}
    return {key: value for key, value in prediction.toDict().items() if key not in inputs}


def _run_program(
    payload: dict[str, Any], descriptor: dict[str, Any], emit: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    """Materialize and invoke one optimized program inside the sandbox.

    Args:
        payload: Scoped artifact, overview, model route, and caller inputs.
        descriptor: Interaction kind and streaming preference.
        emit: Parent event transport for token and chat patches.

    Returns:
        Completed serve or chat response.
    """
    artifact = ProgramArtifact.model_validate(payload["program_artifact"])
    overview = dict(payload["payload_overview"])
    tool_source = payload.get("tool_source")
    if tool_source is not None:
        overview["tool_source"] = tool_source
        if artifact.react_overlay is not None:
            artifact.react_overlay.tool_source = tool_source
    program = _materialize_program(artifact, overview)
    model = ModelConfig.model_validate(payload["model_config"])
    language_model = build_language_model(model, disable_cache=True)
    input_fields = list(descriptor["input_fields"])
    output_fields = list(descriptor["output_fields"])
    inputs = dict(payload["inputs"])
    prediction = None
    listeners = [StreamListener(signature_field_name=field) for field in output_fields]
    stream = None
    if descriptor.get("stream"):
        try:
            stream = dspy.streamify(program, stream_listeners=listeners, async_streaming=False)
        except Exception:
            stream = None
    with capture_node_traces() as traces, dspy.context(lm=language_model):
        if stream is None:
            prediction = program(**inputs)
        else:
            for item in stream(**inputs):
                if isinstance(item, StreamResponse):
                    emit(
                        {
                            "event": "token",
                            "data": {"field": item.signature_field_name, "chunk": item.chunk},
                        }
                    )
                elif isinstance(item, dspy.Prediction):
                    prediction = item
            if prediction is None:
                raise RuntimeError("The isolated stream did not return a final prediction.")
    outputs = _response_outputs(prediction, output_fields, inputs)
    return {
        "optimization_id": descriptor["optimization_id"],
        "outputs": outputs,
        "input_fields": input_fields,
        "output_fields": output_fields,
        "model_used": model.normalized_identifier(),
        "node_traces": [trace.model_dump(mode="json") for trace in sanitize_node_traces(traces)] or None,
    }


def _baseline_program(payload: dict[str, Any]) -> Any:
    """Construct the original unoptimized program inside the sandbox.

    Args:
        payload: Stored authored signature, module name, and module options.

    Returns:
        Fresh baseline DSPy program.

    Raises:
        ValueError: When the stored module cannot be reconstructed.
    """
    signature = load_signature_from_code(str(payload.get("signature_code") or ""))
    module_name = str(payload.get("module_name") or "predict")
    module_kwargs = dict(payload.get("module_kwargs") or {})
    try:
        factory, automatic_signature = resolve_module_factory(module_name)
    except ResolverError as error:
        raise ValueError(f"The baseline module could not be reconstructed: {error}") from error
    if automatic_signature or "signature" not in module_kwargs:
        module_kwargs["signature"] = signature
    return factory(**module_kwargs)


def _run_evaluation(payload: dict[str, Any], descriptor: dict[str, Any]) -> dict[str, Any]:
    """Evaluate selected persisted rows inside the sandbox.

    Args:
        payload: Stored dataset, metric, program artifact, and scoped model route.
        descriptor: Selected indices and baseline or optimized program identity.

    Returns:
        Per-row predictions and scores.
    """
    mapping = ColumnMapping.model_validate(payload["column_mapping"])
    metric = load_metric_from_code(str(payload["metric_code"]))
    model = ModelConfig.model_validate(payload["model_config"])
    language_model = build_language_model(model, disable_cache=True)
    if descriptor["program_type"] == "baseline":
        program = _baseline_program(payload)
    else:
        artifact = ProgramArtifact.model_validate(payload["program_artifact"])
        overview = dict(payload["payload_overview"])
        tool_source = payload.get("tool_source")
        if tool_source is not None:
            overview["tool_source"] = tool_source
            if artifact.react_overlay is not None:
                artifact.react_overlay.tool_source = tool_source
        program = _materialize_program(artifact, overview)
    dataset = list(payload.get("dataset") or [])
    results = []
    with dspy.context(lm=language_model):
        for index in descriptor["indices"]:
            if index < 0 or index >= len(dataset):
                continue
            row = dataset[index]
            values = {
                field: row.get(column, "")
                for field, column in {**mapping.inputs, **mapping.outputs}.items()
            }
            example = dspy.Example(**values).with_inputs(*mapping.inputs)
            try:
                prediction = program(**{field: values[field] for field in mapping.inputs})
                outputs = {field: getattr(prediction, field, None) for field in mapping.outputs}
                try:
                    raw_score = metric(example, prediction)
                    score = float(raw_score) if isinstance(raw_score, int | float | bool) else 0.0
                    if not math.isfinite(score):
                        score = 0.0
                except Exception:
                    score = 0.0
                results.append({"index": index, "outputs": outputs, "score": score, "pass": score > 0})
            except Exception as error:
                results.append(
                    {"index": index, "outputs": {}, "score": 0.0, "pass": False, "error": str(error)}
                )
    return {"results": results, "program_type": descriptor["program_type"]}


def run_interaction(
    payload: dict[str, Any], descriptor: dict[str, Any], emit: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    """Dispatch one supported completed-run interaction in the guest.

    Args:
        payload: Credential-free interaction payload with scoped parent routes.
        descriptor: Validated interaction operation.
        emit: Parent event transport for streaming output.

    Returns:
        Interaction result returned to the signed-in caller.

    Raises:
        ValueError: When the requested interaction kind is unsupported.
    """
    if descriptor.get("kind") == "serve":
        return _run_program(payload, descriptor, emit)
    if descriptor.get("kind") == "evaluate":
        return _run_evaluation(payload, descriptor)
    if descriptor.get("kind") == REPO_SERVE_KIND:
        return run_repo_serve(payload, descriptor)
    raise ValueError("Unsupported protected interaction kind.")

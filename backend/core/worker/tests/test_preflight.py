"""Verify explicit workflow previews run authored nodes inside the protected guest."""

from __future__ import annotations

from typing import Any

import pytest
from dspy.utils.dummies import DummyLM

from core.worker import preflight


def _workflow_payload(*, fail: bool = False) -> dict[str, Any]:
    """Build an actual authored transform between workflow input and output anchors."""
    payload: dict[str, Any] = {"model_config": {"name": "test/model"}}
    code = "def transform(question):\n    return {'answer': question.upper()}\n"
    if fail:
        code = "def transform(question):\n    raise ValueError('sample node failed')\n"
    payload["workflow"] = {
        "nodes": [
            {"id": "input", "kind": "input", "fields": [{"name": "question"}]},
            {
                "id": "actual_transform",
                "kind": "transform",
                "transform_code": code,
                "input_fields": [{"name": "question"}],
                "output_fields": [{"name": "answer"}],
            },
            {"id": "output", "kind": "output", "fields": [{"name": "answer"}]},
        ],
        "edges": [
            {"source": "input", "source_port": "question", "target": "actual_transform", "target_port": "question"},
            {"source": "actual_transform", "source_port": "answer", "target": "output", "target_port": "answer"},
        ],
    }
    return payload


def test_explicit_workflow_preview_uses_only_debug_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute explicit debug values without inventing a gold answer or evaluating a metric."""
    monkeypatch.setattr(preflight, "build_language_model", lambda *_args, **_kwargs: DummyLM([]))
    payload = _workflow_payload()
    payload["inputs"] = {"question": "explicit debug input"}
    payload["metric_code"] = "raise AssertionError('A debug preview must not load a metric')"
    result = preflight.run_dspy_preflight(payload, {"kind": "workflow_preview"})
    assert result["workflow_result"]["outputs"] == {"answer": "EXPLICIT DEBUG INPUT"}
    assert result["workflow_result"]["node_traces"][1]["inputs"] == {"question": "explicit debug input"}
    assert "sample_score" not in result
    assert "sample_scores" not in result


def test_explicit_workflow_preview_preserves_failed_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return the real failed node and partial traces from an explicit preview."""
    monkeypatch.setattr(preflight, "build_language_model", lambda *_args, **_kwargs: DummyLM([]))
    payload = _workflow_payload(fail=True)
    payload["inputs"] = {"question": "explicit debug input"}
    result = preflight.run_dspy_preflight(payload, {"kind": "workflow_preview"})
    assert result["workflow_result"]["failed_node_id"] == "actual_transform"
    assert "sample node failed" in result["workflow_result"]["error"]
    assert result["workflow_result"]["node_traces"][-1]["node_id"] == "actual_transform"


@pytest.mark.parametrize("node_failure", [False, True])
def test_workflow_preview_does_not_replay_after_stream_dispatch(
    monkeypatch: pytest.MonkeyPatch, node_failure: bool
) -> None:
    """Preserve a stream failure without executing a second blocking prediction."""
    calls = []

    def streamify(*args: Any, **kwargs: Any):
        """Return an execution stream that fails after its physical attempt starts."""

        def stream(**inputs: Any):
            """Report an actual midstream failure without returning a prediction."""
            calls.append(inputs)
            if node_failure:
                raise RuntimeError("Stream ended after provider dispatch") from preflight.WorkflowNodeExecutionError(
                    "actual_transform", "Wrapped node failure"
                )
            raise RuntimeError("Stream ended after provider dispatch")

        return stream

    monkeypatch.setattr(preflight, "build_language_model", lambda *_args, **_kwargs: DummyLM([]))
    monkeypatch.setattr(preflight.dspy, "streamify", streamify)
    payload = _workflow_payload()
    payload["inputs"] = {"question": "explicit"}
    result = preflight.run_dspy_preflight(payload, {"kind": "workflow_preview", "stream": True}, lambda _value: None)
    assert result["workflow_result"]["outputs"] is None
    if node_failure:
        assert result["workflow_result"]["failed_node_id"] == "actual_transform"
        assert "Wrapped node failure" in result["workflow_result"]["error"]
    else:
        assert "provider dispatch" in result["workflow_result"]["error"]
    assert calls == [{"question": "explicit"}]

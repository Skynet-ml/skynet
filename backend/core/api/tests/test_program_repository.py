"""Verify data-free repository packages without evaluating saved programs."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import dspy
import pytest

from ...models import ProgramArtifact
from ...service_gateway.optimization.blackbox.repo_constraints import parse_constraints, scorer_only_paths
from ..routers.optimizations._program_repository import build_program_repository
from .test_program_export import _persisted_workflow_artifact

SIGNATURE = '''import dspy
class Answer(dspy.Signature):
    """Return the answer."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
'''


def _package():
    """Build a synthetic saved program with renamed dataset columns."""
    program = dspy.Predict("question -> answer")
    program.signature = program.signature.with_instructions("Preserve this optimized instruction.")
    state = program.dump_state()
    state["demos"] = [{"question": "SECRET_TRAINING_ROW", "answer": "PRIVATE_ANSWER"}]
    state["traces"] = ["PRIVATE_TRACE"]
    state["train"] = ["PRIVATE_TRAIN"]
    artifact = ProgramArtifact(program_state_json=state)
    payload = {
        "module_name": "predict",
        "signature_code": SIGNATURE,
        "metric_code": "def metric(example, prediction): return example.answer == prediction.answer\n",
        "dataset": [{"q": "SECRET_DATASET_ROW", "gold": "PRIVATE_GOLD"}],
        "column_mapping": {"inputs": {"question": "q"}, "outputs": {"answer": "gold"}},
        "seed": 17,
        "shuffle": False,
        "split_fractions": {"train": 0.6, "val": 0.2, "test": 0.2},
    }
    result = {
        "baseline_test_metric": 0.2,
        "optimized_test_metric": 0.8,
        "split_counts": {"train": 6, "val": 2, "test": 2},
    }
    return artifact, payload, result


def test_repository_keeps_schema_and_prompt_without_training_data():
    """Remove dataset rows, demonstrations and traces while retaining historical scores."""
    artifact, payload, result = _package()
    package = build_program_repository("synthetic-run", artifact, {}, payload, result)
    combined = "\n".join(package.files.values())
    for private in (
        "SECRET_TRAINING_ROW",
        "PRIVATE_ANSWER",
        "PRIVATE_TRACE",
        "PRIVATE_TRAIN",
        "SECRET_DATASET_ROW",
        "PRIVATE_GOLD",
    ):
        assert private not in combined
    assert "Preserve this optimized instruction." in package.files["program.json"]
    assert package.cases == []
    assert package.files["cases.jsonl"] == ""
    assert package.historical_split_counts == {"train": 6, "val": 2, "test": 2}
    schema = json.loads(package.files["data/schema.json"])
    assert schema["properties"]["input"]["properties"] == {"question": {}}
    assert schema["column_mapping"] == payload["column_mapping"]
    history = json.loads(package.files["history.json"])
    assert history["original_baseline_score"] == 0.2
    assert history["starting_version_score"] == 0.8
    assert history["scores_are_historical"]
    assert not history["data_included"]
    assert not history["execution_ready"]
    assert artifact.program_state_json["demos"]
    for name, content in package.files.items():
        if name.endswith(".py"):
            compile(content, name, "exec")
    constraints = parse_constraints(package.files["optimize.toml"].encode())
    assert "cases.jsonl" in scorer_only_paths(constraints)
    assert "[data]" not in package.files["optimize.toml"]


def test_conversion_never_executes_saved_metric():
    """Keep authored metric code out of the API process during packaging."""
    artifact, payload, result = _package()
    payload["metric_code"] = "raise RuntimeError('must not execute')\ndef metric(example, prediction): return 1\n"
    package = build_program_repository("synthetic-run", artifact, {}, payload, result)
    assert "must not execute" in package.files["metric.py"]


def test_tool_free_workflow_keeps_the_complete_graph_without_nested_demos():
    """Preserve workflow nodes while excluding per-predictor training examples."""
    artifact, overview = _persisted_workflow_artifact()
    artifact.program_state_json["nested"] = {"demos": [{"q": "PRIVATE_NESTED_EXAMPLE"}]}
    _, payload, result = _package()
    payload.update(overview)
    package = build_program_repository("synthetic-workflow", artifact, overview, payload, result)
    assert json.loads(package.files["workflow.json"]) == overview["workflow"]
    assert "PRIVATE_NESTED_EXAMPLE" not in "\n".join(package.files.values())
    assert "workflow.json" in package.editable_paths


@pytest.mark.parametrize("missing", ["signature_code", "metric_code"])
def test_missing_saved_program_source_fails(missing: str):
    """Reject incomplete programs instead of inventing their code."""
    artifact, payload, result = _package()
    payload.pop(missing)
    with pytest.raises(ValueError):
        build_program_repository("synthetic-run", artifact, {}, payload, result)


def test_dataset_rows_are_not_required():
    """Build the schema-only package when no original dataset remains."""
    artifact, payload, result = _package()
    payload.pop("dataset")
    assert build_program_repository("synthetic-run", artifact, {}, payload, result).cases == []


def test_placeholder_tools_are_explicit_and_refuse_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep the original tool shape without a live connection or invented observations."""
    artifact, payload, result = _package()
    payload.update(
        {
            "module_name": "workflow",
            "tool_source": {"kind": "live_mcp", "mcp_url": "http://127.0.0.1:8765/mcp"},
            "workflow": {
                "nodes": [
                    {"id": "start", "kind": "input", "fields": [{"name": "question"}]},
                    {
                        "id": "lookup",
                        "kind": "mcp",
                        "tool_name": "search",
                        "input_fields": [{"name": "query"}],
                        "output_field": {"name": "result"},
                    },
                    {"id": "end", "kind": "output", "fields": [{"name": "result"}]},
                ],
                "edges": [
                    {"source": "start", "source_port": "question", "target": "lookup", "target_port": "query"},
                    {"source": "lookup", "source_port": "result", "target": "end", "target_port": "result"},
                ],
            },
        }
    )
    with pytest.raises(ValueError, match="explicitly allow"):
        build_program_repository("synthetic-run", artifact, {}, payload, result)
    package = build_program_repository("synthetic-run", artifact, {}, payload, result, allow_placeholder_tools=True)
    assert package.placeholder_tools == ["search"]
    assert ".mcp.json" not in package.files
    assert "127.0.0.1" not in "\n".join(package.files.values())
    assert json.loads(package.files["history.json"])["placeholder_tools"] == ["search"]
    for name, content in package.files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("synthetic_program_tools", tmp_path / "program_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = module.load_tools()
    assert [tool.name for tool in tools] == ["search"]
    assert list(tools[0].args) == ["query"]
    with pytest.raises(RuntimeError, match="Configure the retrieval tool"):
        tools[0](query="example")
    skynet = types.ModuleType("skynet")
    skynet.llm = lambda **kwargs: pytest.fail("The placeholder must stop before model calls")
    monkeypatch.setitem(sys.modules, "skynet", skynet)
    spec = importlib.util.spec_from_file_location("synthetic_serve", tmp_path / "serve.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    with pytest.raises(RuntimeError, match="Configure the retrieval tool"):
        server.serve({"question": "example"})

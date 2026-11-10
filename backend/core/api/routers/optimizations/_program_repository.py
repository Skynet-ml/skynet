"""Package a saved prompt program as a repository without running user code."""

from __future__ import annotations

import ast
import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

from ....models import ProgramArtifact, WorkflowSpec
from ....models.workflow import workflow_tool_users
from ._program_export import build_program_export_zip


@dataclass(frozen=True)
class ProgramRepository:
    """Hold the files and scoring inputs for a saved program's repository."""

    files: dict[str, str]
    cases: list[dict[str, Any]]
    scorer_code: str
    editable_paths: list[str]
    historical_split_counts: dict[str, int]
    placeholder_tools: list[str]


_SERVE_PY = '''"""Run the saved program through the sandbox's metered model gateway."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import dspy
from skynet import llm


class GatewayLM(dspy.BaseLM):
    """Send DSPy chat messages through the configured sandbox gateway."""

    def forward(self, prompt=None, messages=None, **kwargs):
        """Complete one DSPy request without reading provider credentials."""
        text = llm(messages=messages or [{"role": "user", "content": str(prompt)}])
        message = SimpleNamespace(content=text)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage={}, model=self.model)


def serve(input):
    """Return the saved program's outputs for one input mapping."""
    path = Path(__file__).with_name("load_program.py")
    spec = importlib.util.spec_from_file_location("saved_program_loader", path)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    tools = None
    tools_path = path.with_name("program_tools.py")
    if tools_path.is_file():
        tools_spec = importlib.util.spec_from_file_location("saved_program_tools", tools_path)
        tools_module = importlib.util.module_from_spec(tools_spec)
        tools_spec.loader.exec_module(tools_module)
        tools_module.require_configured()
        tools = tools_module.load_tools()
    with dspy.context(lm=GatewayLM(model="skynet-gateway", cache=False)):
        return loader.load_program(tools=tools)(**input).toDict()
'''

_TOOLS_PY = '''"""Placeholder retrieval tools preserved for repository migration only."""

from typing import Any

import dspy

_TOOL_INPUTS = PLACEHOLDER_INPUTS
_MESSAGE = "Configure the retrieval tool before running this program."


def require_configured():
    """Stop inference until real retrieval tools replace these placeholders."""
    raise RuntimeError(_MESSAGE)


def _unavailable(**kwargs):
    """Refuse a placeholder call without inventing retrieval results."""
    raise RuntimeError(_MESSAGE)


def load_tools():
    """Expose the original tool names and input fields without a live connection."""
    return [dspy.Tool(_unavailable, name=name, desc="Unconfigured retrieval placeholder.",
                     args={field: {} for field in fields}, arg_types={field: Any for field in fields})
            for name, fields in _TOOL_INPUTS.items()]
'''

_SCORER_PY = '''"""Score a repository version with the program's original metric."""

import importlib.util
from pathlib import Path

import dspy

_NAMED = {}


def log_metrics(**values):
    """Keep named scores from the original metric for this case."""
    _NAMED.update(values)


_METRIC_NAMESPACE = {"dspy": dspy, "log_metrics": log_metrics}
exec(compile(METRIC_SOURCE, "metric.py", "exec", dont_inherit=True), _METRIC_NAMESPACE)


def score(repo_path, case):
    """Run one version and score its prediction against the saved expected fields."""
    _NAMED.clear()
    path = Path(repo_path) / "serve.py"
    spec = importlib.util.spec_from_file_location("saved_program_serve", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with dspy.context(lm=module.GatewayLM(model="skynet-gateway", cache=False)):
        prediction = dspy.Prediction(**module.serve(case["input"]))
        example = dspy.Example(**case["input"], **case["expected"]).with_inputs(*case["input"])
        value = _METRIC_NAMESPACE["metric"](example, prediction)
    result = dict(value) if isinstance(value, dict) else {"score": float(value)}
    result.setdefault("feedback", f"Original metric score: {result['score']}.")
    if _NAMED:
        result["scores"] = {name: {"score": float(value), "feedback": f"{name}: {value}."}
                            for name, value in _NAMED.items()}
    return result
'''


def _without_examples(value: Any) -> Any:
    """Remove training rows, demonstrations and traces from saved predictor state.

    Args:
        value: Saved JSON state or one of its nested values.

    Returns:
        A new JSON value containing program structure and instructions only.
    """
    if isinstance(value, dict):
        return {
            key: [] if key in {"demos", "traces", "train"} else _without_examples(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_examples(item) for item in value]
    return value


def _data_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Describe the original input and expected fields without copying dataset rows.

    Args:
        payload: The original request's schema and column mappings.

    Returns:
        A JSON Schema for a future case, with its original column mapping.
    """
    mapping = payload.get("column_mapping") or {}
    return {
        "type": "object",
        "properties": {
            "input": {"type": "object", "properties": {name: {} for name in mapping.get("inputs", {})}},
            "expected": {"type": "object", "properties": {name: {} for name in mapping.get("outputs", {})}},
        },
        "required": ["input", "expected"],
        "column_mapping": mapping,
    }


def build_program_repository(
    optimization_id: str,
    artifact: ProgramArtifact,
    overview: dict[str, Any],
    payload: dict[str, Any],
    result: dict[str, Any],
    *,
    allow_placeholder_tools: bool = False,
) -> ProgramRepository:
    """Build a repository of saved program structure, prompts and metric without training data.

    Original scores remain historical observations. The conversion does not
    execute the program, call a model, or pretend to score a new test split.

    Args:
        optimization_id: The source run identifier.
        artifact: The saved optimized program state.
        overview: Its program recipe and signature or workflow definition.
        payload: The original request; dataset rows are never copied.
        result: The source run's result and recorded scores.
        allow_placeholder_tools: Explicit authorization to preserve unconfigured retrieval tools.

    Returns:
        Repository files, canonical cases, fixed scorer source, and editable paths.

    Raises:
        ValueError: When the saved program cannot be reconstructed faithfully.
    """
    if payload.get("payload_kind") == "unified_v1":
        raise ValueError("This workspace needs its run and metric contracts converted before repository migration.")
    if not isinstance(artifact.program_state_json, dict) or not artifact.program_state_json:
        raise ValueError("This run has no saved program state that can be reconstructed without a pickle.")
    recipe = {**payload, **overview}
    workflow = recipe.get("workflow")
    module = str(recipe.get("module_name") or "").rsplit(".", 1)[-1].lower()
    if artifact.react_overlay is not None or module in {"react", "reactv2"}:
        raise ValueError("This program uses tools. Convert its tool connections before creating a runnable repository.")
    tool_names = []
    tool_inputs: dict[str, list[str]] = {}
    if workflow:
        spec = WorkflowSpec.model_validate(workflow)
        if workflow_tool_users(spec):
            if any(node.kind == "signature" for node in spec.nodes if node.id in workflow_tool_users(spec)):
                raise ValueError("Convert the workflow's agent tool roster before creating a runnable repository.")
            tool_names = sorted({node.tool_name for node in spec.nodes if node.kind == "mcp"})
            for node in spec.nodes:
                if node.kind == "mcp":
                    tool_inputs.setdefault(node.tool_name, [])
                    tool_inputs[node.tool_name] = sorted(
                        set(tool_inputs[node.tool_name]) | {f.name for f in node.input_fields}
                    )
    elif not recipe.get("signature_code"):
        raise ValueError("This run has no saved signature source to reconstruct its program.")
    metric = payload.get("metric_code")
    if not isinstance(metric, str) or not metric.strip():
        raise ValueError("This run needs its original metric source before conversion.")
    tree = ast.parse(metric)
    if not any(isinstance(node, ast.FunctionDef) and node.name == "metric" for node in tree.body):
        raise ValueError("The saved metric must define metric(example, prediction) before conversion.")
    cases: list[dict[str, Any]] = []
    saved_counts = result.get("split_counts") or {}
    historical_split_counts = {
        name: saved_counts[name] for name in ("train", "val", "test") if isinstance(saved_counts.get(name), int)
    }
    artifact = artifact.model_copy(
        update={
            "program_state_json": _without_examples(artifact.program_state_json),
            "program_pickle_base64": None,
            "optimized_prompt": None,
            "path": None,
        }
    )
    archive = build_program_export_zip(optimization_id=optimization_id, artifact=artifact, overview=recipe)
    with zipfile.ZipFile(io.BytesIO(archive)) as packed:
        files = {name: packed.read(name).decode("utf-8") for name in packed.namelist()}
    scorer = _SCORER_PY.replace("METRIC_SOURCE", repr(metric))
    files.update(
        {
            "serve.py": _SERVE_PY,
            "score.py": scorer,
            "metric.py": metric,
            "cases.jsonl": "",
            "data/schema.json": json.dumps(_data_schema(payload), ensure_ascii=False, indent=2),
            "history.json": json.dumps(
                {
                    "source_optimization_id": optimization_id,
                    "starting_version": "saved_program_without_training_examples",
                    "data_included": False,
                    "execution_ready": False,
                    "scores_are_historical": True,
                    "original_baseline_score": result.get("baseline_test_metric"),
                    "starting_version_score": result.get("optimized_test_metric"),
                    "original_split_counts": result.get("split_counts"),
                    "original_seed": payload.get("seed"),
                    "original_shuffle": payload.get("shuffle", True),
                    "original_split_fractions": payload.get("split_fractions"),
                },
                ensure_ascii=False,
                indent=2,
            ),
        }
    )
    if payload.get("tool_source") or tool_names:
        if not allow_placeholder_tools or not tool_names:
            raise ValueError(
                "Configure a saved MCP connection or explicitly allow retrieval placeholders before migration."
            )
        files["program_tools.py"] = _TOOLS_PY.replace("PLACEHOLDER_INPUTS", repr(tool_inputs), 1)
        history = json.loads(files["history.json"])
        history["placeholder_tools"] = tool_names
        history["execution_ready"] = False
        files["history.json"] = json.dumps(history, ensure_ascii=False, indent=2)
    editable = [
        name
        for name in files
        if name in {"program.json", "signature.py", "workflow.json", "optimized_module.py"}
        or name.startswith("optimized_modules/")
    ]
    files["optimize.toml"] = (
        "version = 1\n\n[paths]\n"
        f"editable = {json.dumps(editable)}\n"
        'protected = ["serve.py", "load_program.py", "metadata.json", "requirements.txt", "metric.py", '
        '"score.py", "cases.jsonl", "history.json"]\n\n'
        '[metric]\nfiles = ["metric.py", "score.py", "history.json", "cases.jsonl"]\n'
    )
    files["README.md"] += (
        "\n## Repository optimization\n\n"
        "The starting commit preserves program structure, prompt instructions and the original metric. "
        "Dataset rows, demonstrations and traces are omitted. `cases.jsonl` is empty; `data/schema.json` "
        "describes the expected case fields. Add real cases before optimization. Original scores and split "
        "counts remain historical in `history.json`; they do not measure this data-free repository. "
        "Conversion runs no evaluation and does not claim equivalent predictions after removing examples. "
        "No model keys are stored in this repository.\n"
    )
    if tool_names:
        files["README.md"] += (
            "\n## Retrieval placeholders\n\n"
            "This repository preserves the original retrieval tool names and input fields as placeholders. "
            "No MCP service is configured. Serving and scoring stop before model calls. "
            "Replace the placeholders in `program_tools.py` and update `require_configured` before running it. "
            "The archived scores came from the original run; no score was measured for these placeholders.\n"
        )
    return ProgramRepository(files, cases, scorer, editable, historical_split_counts, tool_names)

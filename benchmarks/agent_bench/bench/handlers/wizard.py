"""Handlers for the submission wizard: state patches, code validation, cards.

``validate_code`` inspects user code with :mod:`ast` only and never executes it,
matching the requirement that a dry run cannot run arbitrary user code.
``update_wizard_state`` returns a ``wizard_state`` patch that :meth:`World.call`
merges into the shared wizard; the request tools append a chat UI card.
"""

from __future__ import annotations

import ast
from typing import Any

from bench.handlers._common import push_card
from bench.world import ToolError, World, tool

WIZARD_KEYS = (
    "job_name", "job_description", "optimizer_name", "module_name", "job_type", "react_config",
    "is_private", "column_roles", "model_config", "reflection_model_config", "generation_models",
    "reflection_models", "use_all_generation_models", "use_all_reflection_models", "split_fractions",
    "split_mode", "seed", "shuffle", "optimizer_kwargs", "target_score", "blackbox_objective",
    "blackbox_seed", "blackbox_scorer_code", "signature_code", "metric_code",
)


@tool("update_wizard_state")
def update_wizard_state(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return a ``wizard_state`` patch of the supplied fields for the world to merge.

    Raises:
        ToolError: 422 when an unknown wizard field is supplied.
    """
    unknown = [k for k in args if k not in WIZARD_KEYS]
    if unknown:
        raise ToolError(422, f"unknown wizard field(s): {', '.join(unknown)}")
    patch = {k: v for k, v in args.items() if k in WIZARD_KEYS}
    return {"ok": True, "wizard_state": patch}


@tool("validate_code_validate_code_post")
def validate_code(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Statically validate a signature and/or metric without executing them.

    Args:
        w: The world.
        args: ``column_mapping`` plus optional ``signature_code``, ``metric_code``,
            ``sample_row``, ``optimizer_name`` and ``module_name``.

    Returns:
        ``{"valid", "errors", "signature", "metric"}`` where ``signature`` reports
        the parsed input/output fields and ``metric`` whether a metric function
        was found. Parsing uses :mod:`ast`; user code is never run.
    """
    errors: list[str] = []
    signature = {"parsed": False, "input_fields": [], "output_fields": []}
    metric = {"parsed": False, "function_name": None}

    sig_code = args.get("signature_code")
    if sig_code:
        try:
            fields = _signature_fields(sig_code)
            signature = {"parsed": True, **fields}
            if not fields["input_fields"]:
                errors.append("signature declares no dspy.InputField")
            if not fields["output_fields"]:
                errors.append("signature declares no dspy.OutputField")
        except SyntaxError as exc:
            errors.append(f"signature_code has a syntax error: {exc.msg} (line {exc.lineno})")

    metric_code = args.get("metric_code")
    if metric_code:
        try:
            name = _metric_function(metric_code)
            metric = {"parsed": True, "function_name": name}
            if name is None:
                errors.append("metric_code defines no top-level function")
        except SyntaxError as exc:
            errors.append(f"metric_code has a syntax error: {exc.msg} (line {exc.lineno})")

    mapping = args.get("column_mapping") or {}
    if not (mapping.get("inputs") and mapping.get("outputs")):
        errors.append("column_mapping must define both inputs and outputs")

    return {"valid": not errors, "errors": errors, "signature": signature, "metric": metric}


@tool("request_code_authoring")
def request_code_authoring(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Signal the chat UI to render an inline code-authoring card."""
    return push_card(w, "code_authoring", {"goal": args.get("goal", "")})


def _signature_fields(code: str) -> dict[str, list[str]]:
    """Extract InputField / OutputField names from a dspy.Signature class.

    Args:
        code: The signature source.

    Returns:
        ``{"input_fields": [...], "output_fields": [...]}``.

    Raises:
        SyntaxError: When ``code`` does not parse.
    """
    tree = ast.parse(code)
    inputs, outputs = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                target, value = _assignment(stmt)
                kind = _field_kind(value)
                if target and kind == "input":
                    inputs.append(target)
                elif target and kind == "output":
                    outputs.append(target)
    return {"input_fields": inputs, "output_fields": outputs}


def _assignment(stmt: ast.stmt) -> tuple[str | None, ast.expr | None]:
    """Return the target name and value expr of an assignment statement."""
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id, stmt.value
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id, stmt.value
    return None, None


def _field_kind(value: ast.expr | None) -> str | None:
    """Return 'input'/'output' when ``value`` is a dspy InputField/OutputField call."""
    if isinstance(value, ast.Call):
        func = value.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name == "InputField":
            return "input"
        if name == "OutputField":
            return "output"
    return None


def _metric_function(code: str) -> str | None:
    """Return the name of the first top-level function in ``code``, or None.

    Raises:
        SyntaxError: When ``code`` does not parse.
    """
    tree = ast.parse(code)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None

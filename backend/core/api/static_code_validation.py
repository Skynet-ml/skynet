"""Inspect authored signature and metric structure without importing or executing code."""

from __future__ import annotations

import ast
from dataclasses import dataclass


class StaticCodeError(ValueError):
    """Describe an authoring syntax or statically visible structural error."""


@dataclass(frozen=True)
class SignatureShape:
    """Carry field names read directly from a declared DSPy signature class."""

    input_fields: list[str]
    output_fields: list[str]


@dataclass(frozen=True)
class MetricShape:
    """Carry a metric's declared positional parameters without evaluating its definition."""

    param_names: list[str]
    accepts_varargs: bool


def _parse(code: str, kind: str) -> ast.Module:
    """Parse source while keeping authored imports, decorators, and expressions inert.

    Args:
        code: User-authored Python source.
        kind: Signature or metric label used in error messages.

    Returns:
        The unevaluated module syntax tree.

    Raises:
        StaticCodeError: When Python syntax is invalid or too deeply nested to parse.
    """
    try:
        return ast.parse(code, filename=f"<{kind}_code>")
    except SyntaxError as error:
        raise StaticCodeError(f"{kind}_code has a syntax error at line {error.lineno}: {error.msg}") from error
    except (ValueError, RecursionError) as error:
        raise StaticCodeError(f"{kind}_code cannot be parsed: {error}") from error


def _dspy_names(tree: ast.Module) -> dict[str, str]:
    """Resolve explicit DSPy import aliases using syntax alone.

    Args:
        tree: Parsed module containing unevaluated import statements.

    Returns:
        Local names mapped to their declared DSPy names.
    """
    names = {"dspy": "dspy"}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name == "dspy":
                    names[alias.asname or alias.name] = "dspy"
        elif isinstance(statement, ast.ImportFrom) and statement.module == "dspy":
            for alias in statement.names:
                if alias.name in {"Signature", "InputField", "OutputField"}:
                    names[alias.asname or alias.name] = "dspy." + alias.name
    return names


def _qualified_name(node: ast.AST, names: dict[str, str]) -> str | None:
    """Read a declared dotted name without resolving any Python object.

    Args:
        node: Name or attribute syntax node.
        names: Explicit import aliases known from this source.

    Returns:
        Resolved declared name, or None for an expression requiring execution.
    """
    if isinstance(node, ast.Name):
        return names.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value, names)
        return f"{parent}.{node.attr}" if parent else None
    return None


def inspect_signature(code: str) -> SignatureShape | None:
    """Read one declared DSPy signature and its field constructors statically.

    Args:
        code: User-authored signature source.

    Returns:
        Declared field names, or None when runtime construction prevents reliable inspection.

    Raises:
        StaticCodeError: When the source has invalid Python syntax.
    """
    tree = _parse(code, "signature")
    names = _dspy_names(tree)
    signatures = []
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef) and any(
            _qualified_name(base, names) == "dspy.Signature" for base in statement.bases
        ):
            signatures.append(statement)
            names[statement.name] = "dspy.Signature"
    if len(signatures) != 1:
        return None
    signature = signatures[0]
    if signature.decorator_list or len(signature.bases) != 1:
        return None
    fields: dict[str, str] = {}
    for statement in signature.body:
        if isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        elif isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.Pass) or (
            isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
        ):
            continue
        else:
            return None
        if not isinstance(value, ast.Call):
            return None
        direction = {"dspy.InputField": "input", "dspy.OutputField": "output"}.get(_qualified_name(value.func, names))
        if direction is None:
            return None
        for target in targets:
            if not isinstance(target, ast.Name):
                return None
            fields[target.id] = direction
    return SignatureShape(
        input_fields=[name for name, direction in fields.items() if direction == "input"],
        output_fields=[name for name, direction in fields.items() if direction == "output"],
    )


# ``metric`` is the retired DSPy entry point; the workspace metric and a
# repository run's scorer both define ``score``, often beside helpers.
METRIC_ENTRY_NAMES = frozenset({"metric", "score"})


def inspect_metric(code: str) -> MetricShape:
    """Read the declared metric's positional interface without creating a callable.

    Args:
        code: User-authored metric source.

    Returns:
        Positional parameter names and whether additional positional arguments are accepted.

    Raises:
        StaticCodeError: When a synchronous metric function cannot be identified.
    """
    tree = _parse(code, "metric")
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
    candidates = [node for node in functions if node.name in METRIC_ENTRY_NAMES]
    candidates.extend(
        statement.value
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.value, ast.Lambda)
        and any(isinstance(target, ast.Name) and target.id in METRIC_ENTRY_NAMES for target in statement.targets)
    )
    if not candidates and len(functions) == 1:
        candidates = functions
    if len(candidates) > 1:
        # Older metrics define ``metric`` beside a helper named ``score``.
        named_metric = [node for node in functions if node.name == "metric"]
        if len(named_metric) == 1:
            candidates = named_metric
    if len(candidates) != 1:
        raise StaticCodeError("metric_code must declare a callable named 'metric' or 'score'.")
    function = candidates[0]
    if isinstance(function, ast.AsyncFunctionDef):
        raise StaticCodeError("The metric must be synchronous; async functions cannot supply a score directly.")
    args = function.args
    return MetricShape(
        param_names=[parameter.arg for parameter in (*args.posonlyargs, *args.args)],
        accepts_varargs=args.vararg is not None,
    )

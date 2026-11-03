"""Resolve the metric's model from its source before billing and routing."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from ....billing.byok_bridge import byok_prefix_routable, provider_slug_for_model
from ....billing.byok_vault import byok_provider_for_litellm
from ....config import settings
from ....local.cli_catalog import parse_subscription_model_id
from ....models.common import ModelConfig
from ....models.unified import UnifiedModels


@dataclass
class MetricLLMScan:
    """Model declarations and helper use found in a metric."""

    uses_llm: bool = False
    names: set[str] = field(default_factory=set)
    non_literal: bool = False
    omitted: bool = False


def scan_metric_llm(code: str) -> MetricLLMScan:
    """Extract literal model declarations without executing metric code.

    Args:
        code: Python metric source.

    Returns:
        Helper usage, declared model names, omissions, and unsafe uses.
    """
    result = MetricLLMScan()
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return result
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    scopes = (
        ast.Module,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.Lambda,
        ast.ClassDef,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )
    annotations: set[ast.AST] = set()
    for node in ast.walk(tree):
        annotation = (
            node.annotation
            if isinstance(node, (ast.arg, ast.AnnAssign))
            else node.returns
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else None
        )
        if annotation is not None:
            annotations.update(ast.walk(annotation))
    bindings_by_scope: dict[ast.AST, set[str]] = {}
    imported_by_scope: dict[ast.AST, set[str]] = {}
    modules_by_scope: dict[ast.AST, set[str]] = {}
    globals_by_scope: dict[ast.AST, set[str]] = {}

    def scope_of(node: ast.AST) -> ast.AST:
        """Return the lexical scope containing a node."""
        parent = parents.get(node)
        while parent is not None and not isinstance(parent, scopes):
            parent = parents.get(parent)
        return parent or tree

    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            globals_by_scope.setdefault(scope_of(node), set()).update(node.names)

    for node in ast.walk(tree):
        scope = scope_of(node)
        bindings = bindings_by_scope.setdefault(scope, set())
        imported = imported_by_scope.setdefault(scope, set())
        modules = modules_by_scope.setdefault(scope, set())
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings.add(node.id)
        elif isinstance(node, ast.arg):
            bindings.add(node.arg)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler, ast.MatchAs, ast.MatchStar)
        ):
            if node.name:
                bindings.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bindings.add(node.rest)
        if isinstance(node, ast.ImportFrom) and node.module == "skynet" and node.level == 0:
            for alias in node.names:
                if alias.name == "llm":
                    imported.add(alias.asname or alias.name)
                    result.uses_llm = True
                elif alias.name == "*":
                    imported.add("llm")
                else:
                    bindings.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bindings.update(alias.asname or alias.name.split(".")[0] for alias in node.names if alias.name != "*")
            if isinstance(node, ast.Import):
                modules.update(alias.asname or alias.name for alias in node.names if alias.name == "skynet")

    for scope, names in globals_by_scope.items():
        for mapping in (bindings_by_scope, imported_by_scope, modules_by_scope):
            moved = mapping.get(scope, set()) & names
            mapping.setdefault(tree, set()).update(moved)
            mapping.setdefault(scope, set()).difference_update(moved)
    injected_rebound = any(
        isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign))
        and (scope_of(node) is tree or "llm" in globals_by_scope.get(scope_of(node), set()))
        and any(
            isinstance(child, ast.Name) and child.id == "llm" and isinstance(child.ctx, ast.Store)
            for child in ast.walk(node)
        )
        and node.value is not None
        and any(
            isinstance(child, ast.Name) and child.id == "llm" and isinstance(child.ctx, ast.Load)
            for child in ast.walk(node.value)
        )
        for node in ast.walk(tree)
    )
    if (
        any(imported_by_scope.get(scope, set()) & names for scope, names in bindings_by_scope.items())
        or injected_rebound
    ):
        result.uses_llm = True
        result.non_literal = True

    def resolves(node: ast.AST, name: str, *, module: bool = False) -> bool:
        """Resolve a helper or module binding through enclosing scopes."""
        scope = scope_of(node)
        while True:
            if name in globals_by_scope.get(scope, set()):
                scope = tree
            found = modules_by_scope.get(scope, set()) if module else imported_by_scope.get(scope, set())
            if name in found:
                return True
            if name in bindings_by_scope.get(scope, set()):
                return False
            if scope is tree:
                return not module and name == "llm"
            scope = scope_of(scope)
            if isinstance(scope, ast.ClassDef):
                scope = scope_of(scope)

    def is_helper(node: ast.AST) -> bool:
        """Recognize injected or imported helper names and module attributes."""
        return (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and resolves(node, node.id)) or (
            isinstance(node, ast.Attribute)
            and node.attr == "llm"
            and isinstance(node.value, ast.Name)
            and resolves(node.value, node.value.id, module=True)
        )

    def is_namespace(node: ast.AST) -> bool:
        """Recognize helper-bearing module and namespace expressions."""
        if isinstance(node, ast.Name):
            return node.id in {"builtins", "__builtins__"} or resolves(node, node.id, module=True)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id in {"globals", "vars"}
        return isinstance(node, ast.Attribute) and node.attr == "__dict__" and is_namespace(node.value)

    for node in ast.walk(tree):
        if node in annotations:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and resolves(node, node.id, module=True):
            parent = parents.get(node)
            if not isinstance(parent, ast.Attribute) or parent.value is not node:
                result.uses_llm = result.non_literal = True
        if (
            isinstance(node, ast.Subscript)
            and is_namespace(node.value)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "llm"
        ) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and is_namespace(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "llm"
        ):
            result.uses_llm = result.non_literal = True
        if is_helper(node):
            result.uses_llm = True
            parent = parents.get(node)
            if not isinstance(parent, ast.Call) or parent.func is not node:
                result.non_literal = True
        if not isinstance(node, ast.Call):
            continue
        if not is_helper(node.func):
            continue
        result.omitted |= not any(keyword.arg == "model" for keyword in node.keywords)
        for keyword in node.keywords:
            if keyword.arg is None:
                result.non_literal = True
            elif keyword.arg == "model":
                if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                    result.omitted = True
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    source = ast.get_source_segment(code, keyword.value) or ""
                    if re.search(r"\\N\{", source) and "\\N{" not in keyword.value.value:
                        result.non_literal = True
                    else:
                        result.names.add(keyword.value.value)
                else:
                    result.non_literal = True
    return result


def metric_declaration_unsafe(scan: MetricLLMScan, reflection_name: str | None) -> bool:
    """Determine whether helper calls can select more than one model."""
    if scan.non_literal or len(scan.names) > 1:
        return True
    return bool(scan.omitted and scan.names and (reflection_name is None or scan.names != {reflection_name}))


def resolve_metric_model(models: UnifiedModels, metric_code: str) -> str | None:
    """Derive and set the metric model on the supplied models.

    Args:
        models: Run models, mutated in place at ``metric_model``.
        metric_code: Python metric source.

    Returns:
        ``contract.metric_model_declaration`` for an unsafe declaration, else None.
    """
    scan = scan_metric_llm(metric_code)
    models.metric_model = None
    if metric_declaration_unsafe(scan, models.reflection_model.name):
        return "contract.metric_model_declaration"
    if not scan.uses_llm:
        return None
    reflection = models.reflection_model
    if not scan.names:
        models.metric_model = reflection.model_copy(deep=True)
        return None
    name = next(iter(scan.names), reflection.name)
    for role in (models.task_model, reflection, *models.extra_reflection_models):
        if role is not None and name == role.name:
            models.metric_model = role.model_copy(deep=True)
            return None
    kwargs: dict[str, Any] = {
        "name": name,
        "token_source": "managed" if reflection.token_source == "subscription" else reflection.token_source,
    }
    subscription = parse_subscription_model_id(name.strip("/")) if settings.local_mode else None
    if subscription is not None:
        kwargs.update(token_source="subscription", cli_provider=subscription[0])
    elif reflection.token_source == "byok":
        prefix = provider_slug_for_model(reflection.name)
        provider = reflection.byok_provider or (byok_provider_for_litellm(prefix) if prefix else None)
        if provider:
            custom_base = None
            if prefix == "openai" and byok_provider_for_litellm(prefix) != provider:
                custom_base = custom_base or "selected-endpoint"
            if byok_prefix_routable(provider_slug_for_model(name), provider, api_base=custom_base):
                kwargs["byok_provider"] = provider
    models.metric_model = ModelConfig(**kwargs)
    return None


def resolve_metric_model_dict(payload: dict[str, Any], *, preserve_metric_model: bool = False) -> str | None:
    """Resolve a raw unified payload before legacy aliases are made.

    Args:
        payload: Unified payload whose metric model is mutated in place.
        preserve_metric_model: Keep a stored model when no model is declared.

    Returns:
        The declaration error message key, or None.
    """
    models = payload.get("models")
    workspace = payload.get("workspace")
    if not isinstance(models, dict) or not isinstance(workspace, dict):
        return None
    code = str(workspace.get("metric_code") or "")
    if preserve_metric_model and isinstance(models.get("metric_model"), dict):
        # A stored legacy selection already determined this run's budget route.
        scan = scan_metric_llm(code)
        if not scan.names and not scan.non_literal:
            return None
    typed = UnifiedModels.model_validate({**models, "metric_model": None})
    if typed.reflection_model.token_source is None:
        typed.reflection_model.token_source = payload.get("token_source") or "managed"
    error = resolve_metric_model(typed, code)
    models["metric_model"] = typed.metric_model.model_dump(mode="json") if typed.metric_model else None
    return error

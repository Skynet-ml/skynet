"""Resolve the metric's model from its source before billing and routing."""

from __future__ import annotations

import ast
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


def scan_metric_llm(code: str) -> MetricLLMScan:
    """Extract literal model declarations without executing metric code."""
    result = MetricLLMScan()
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return result
    bindings: set[str] = set()
    imported: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
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

    injected_rebound = any(
        isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign))
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
    aliases = set(imported)
    if "llm" not in bindings or injected_rebound:
        aliases.add("llm")
    if imported & bindings or injected_rebound:
        result.uses_llm = True
        result.non_literal = True

    def is_helper(node: ast.AST) -> bool:
        """Recognize injected or imported helper names and module attributes."""
        return (isinstance(node, ast.Name) and node.id in aliases and isinstance(node.ctx, ast.Load)) or (
            isinstance(node, ast.Attribute)
            and node.attr == "llm"
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        )

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if is_helper(node):
            result.uses_llm = True
            parent = parents.get(node)
            if not isinstance(parent, ast.Call) or parent.func is not node:
                result.non_literal = True
        if not isinstance(node, ast.Call):
            continue
        if not is_helper(node.func):
            continue
        for keyword in node.keywords:
            if keyword.arg is None:
                result.non_literal = True
            elif keyword.arg == "model":
                if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    result.names.add(keyword.value.value)
                else:
                    result.non_literal = True
    return result


def resolve_metric_model(models: UnifiedModels, metric_code: str) -> str | None:
    """Derive the metric model, returning a message key for an unsafe declaration."""
    scan = scan_metric_llm(metric_code)
    models.metric_model = None
    if scan.non_literal or len(scan.names) > 1:
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
    """Resolve a raw unified payload in place before legacy aliases are made."""
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

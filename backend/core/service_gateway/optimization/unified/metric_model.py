"""Resolve the metric's model from its source before billing and routing."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from typing import Any

from ....billing.byok_bridge import byok_prefix_routable, provider_slug_for_model
from ....billing.byok_vault import byok_provider_for_litellm
from ....config import settings
from ....local.cli_catalog import parse_subscription_model_id
from ....models.common import ModelConfig
from ....models.unified import UnifiedModels

# Type parameter nodes exist only on Python 3.12+; the local backend still supports 3.11.
_TYPE_PARAM_NODES = tuple(getattr(ast, name) for name in ("TypeVar", "ParamSpec", "TypeVarTuple") if hasattr(ast, name))


@dataclass
class MetricLLMScan:
    """Model declarations and helper use found in a metric."""

    uses_llm: bool = False
    names: set[str] = field(default_factory=set)
    non_literal: bool = False
    omitted: bool = False
    empty_model: bool = False


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
    aliases = {"llm"}
    modules: set[str] = set()
    allowed_fallbacks: set[ast.AST] = set()
    for statement in tree.body:
        if not isinstance(statement, ast.Try) or len(statement.body) != 1 or len(statement.handlers) != 1:
            continue
        imported, handler = statement.body[0], statement.handlers[0]
        if not (
            isinstance(imported, ast.ImportFrom)
            and imported.module == "skynet"
            and imported.level == 0
            and len(imported.names) == 1
            and imported.names[0].name == "llm"
            and imported.names[0].asname is None
            and handler.name is None
            and not statement.orelse
            and not statement.finalbody
            and len(handler.body) == 1
        ):
            continue
        types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        if not any(isinstance(t, ast.Name) and t.id == "ImportError" for t in types):
            continue
        assignment = handler.body[0]
        if (
            isinstance(assignment, ast.Assign)
            and len(assignment.targets) == 1
            and isinstance(assignment.targets[0], ast.Name)
            and assignment.targets[0].id == "llm"
            and isinstance(assignment.value, ast.Constant)
            and assignment.value.value is None
        ):
            allowed_fallbacks.add(assignment.targets[0])

    if len(allowed_fallbacks) > 1:
        allowed_fallbacks.clear()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "skynet" and node.level == 0:
            for alias in node.names:
                if alias.name == "llm":
                    aliases.add(alias.asname or alias.name)
                    result.uses_llm = True
                elif alias.name == "*":
                    aliases.add("llm")
        elif isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "skynet")

    def is_helper(node: ast.AST) -> bool:
        """Recognize a direct helper name or module attribute."""
        return (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases) or (
            isinstance(node, ast.Attribute)
            and node.attr == "llm"
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        )

    def is_namespace(node: ast.AST) -> bool:
        """Recognize helper-bearing module and namespace expressions."""
        if isinstance(node, ast.Name):
            return node.id in {"builtins", "__builtins__"} | modules
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id in {"globals", "vars"}
        return isinstance(node, ast.Attribute) and node.attr == "__dict__" and is_namespace(node.value)

    for node in ast.walk(tree):
        bound: set[str] = set()
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node not in allowed_fallbacks:
            bound.add(node.id)
        elif isinstance(node, _TYPE_PARAM_NODES):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler, ast.MatchAs, ast.MatchStar)
        ):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if isinstance(node, ast.Import) and alias.name == "skynet":
                    continue
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "skynet"
                    and node.level == 0
                    and alias.name in {"llm", "*"}
                ):
                    continue
                if alias.name != "*":
                    bound.add(alias.asname or alias.name.split(".")[0])
        if bound & (aliases | modules):
            result.uses_llm = result.non_literal = True

        if isinstance(node, ast.ClassDef):
            for statement in node.body:
                for item in ast.walk(statement):
                    if (
                        (isinstance(item, ast.Name) and item.id in aliases)
                        or (isinstance(item, ast.Attribute) and item.attr == "llm")
                        or (isinstance(item, ast.arg) and item.arg in aliases)
                        or (isinstance(item, ast.keyword) and item.arg in aliases)
                        or (isinstance(item, ast.alias) and (item.name in aliases or item.asname in aliases))
                    ):
                        result.uses_llm = result.non_literal = True

        if isinstance(node, ast.Attribute) and node.attr == "llm" and isinstance(node.value, (ast.Call, ast.Subscript)):
            result.uses_llm = result.non_literal = True
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
            and node.attr not in {"llm", "log", "Image", "observation", "service_cost", "reward"}
        ):
            result.uses_llm = result.non_literal = True
        if (
            (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "skynet"
                and (
                    (isinstance(node.func, ast.Name) and node.func.id in {"__import__", "import_module"})
                    or (
                        isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "importlib"
                        and node.func.attr == "import_module"
                    )
                )
            )
            or (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "sys"
                and node.value.attr == "modules"
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "skynet"
            )
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and is_namespace(node.func.value)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "llm"
            )
        ):
            result.uses_llm = result.non_literal = True
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in modules:
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
        if not isinstance(node, ast.Call) or not is_helper(node.func):
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
                    if not keyword.value.value.strip():
                        result.empty_model = result.non_literal = True
                    elif any(
                        token.type == tokenize.STRING
                        and not re.match(r"(?i)r", token.string)
                        and re.search(r"(^|[^\\])(?:\\\\)*\\N\{", token.string)
                        for token in tokenize.generate_tokens(io.StringIO(source).readline)
                    ):
                        result.non_literal = True
                    else:
                        result.names.add(keyword.value.value)
                else:
                    result.non_literal = True
    return result


def metric_declaration_unsafe(scan: MetricLLMScan, reflection_name: str | None) -> bool:
    """Determine whether helper calls can select more than one model.

    Args:
        scan: The static scan of the metric's ``llm`` calls.
        reflection_name: The model a call without ``model=`` uses: a stored
            metric model, else the reflection model. ``None`` means no such
            model is known, so no call may omit ``model=`` next to a named one.

    Returns:
        True when the calls can reach more than one model: a non-literal
        declaration, two or more named models, or a call that omits
        ``model=`` next to a call that names a model other than
        ``reflection_name``.
    """
    if scan.non_literal or len(scan.names) > 1:
        return True
    return bool(scan.omitted and scan.names and (reflection_name is None or scan.names != {reflection_name}))


def resolve_metric_model(models: UnifiedModels, metric_code: str) -> str | None:
    """Derive and set the metric model on the supplied models.

    Args:
        models: Run models, mutated in place at ``metric_model``.
        metric_code: Python metric source.

    Returns:
        ``contract.metric_model_declaration`` or another contract message key
        for an invalid declaration, else None.
    """
    scan = scan_metric_llm(metric_code)
    if scan.empty_model:
        return "contract.metric_model_empty"
    legacy = models.metric_model
    if scan.non_literal or len(scan.names) > 1:
        return "contract.metric_model_declaration"
    if legacy and scan.names and scan.names != {legacy.name}:
        return "contract.metric_model_conflict"
    if metric_declaration_unsafe(scan, legacy.name if legacy else models.reflection_model.name):
        return "contract.metric_model_declaration"
    if legacy is not None:
        return None
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


def resolve_metric_model_dict(payload: dict[str, Any]) -> str | None:
    """Resolve a raw unified payload before legacy aliases are made.

    Args:
        payload: Unified payload whose metric model is mutated in place.

    Returns:
        The declaration error message key, or None.
    """
    models = payload.get("models")
    workspace = payload.get("workspace")
    if not isinstance(models, dict) or not isinstance(workspace, dict):
        return None
    code = str(workspace.get("metric_code") or "")
    stored = models.get("metric_model")
    typed = UnifiedModels.model_validate(models)
    if typed.reflection_model.token_source is None:
        typed.reflection_model.token_source = payload.get("token_source") or "managed"
    error = resolve_metric_model(typed, code)
    if error is None and not isinstance(stored, dict):
        models["metric_model"] = typed.metric_model.model_dump(mode="json") if typed.metric_model else None
    return error


def resolve_blackbox_metric_model_dict(payload: dict[str, Any]) -> str | None:
    """Resolve a Python scorer model in a raw black-box payload.

    Args:
        payload: Black-box request whose scorer model is mutated in place.

    Returns:
        The declaration error message key, or None.
    """
    scorer = payload.get("scorer")
    if not isinstance(scorer, dict) or scorer.get("kind", "python") != "python":
        return None
    code = str(scorer.get("metric_code") or "")
    reflection = payload.get("reflection_model_config")
    if not isinstance(reflection, dict):
        scan = scan_metric_llm(code)
        if scan.empty_model:
            return "contract.metric_model_empty"
        if scan.non_literal or len(scan.names) > 1 or (scan.omitted and scan.names):
            return "contract.metric_model_declaration"
        return None
    models = {
        "reflection_model": reflection,
        "metric_model": scorer.get("model"),
        "task_model": payload.get("task_model_config"),
        "extra_reflection_models": payload.get("extra_reflection_model_configs") or [],
    }
    raw = {"models": models, "workspace": {"metric_code": code}, "token_source": payload.get("token_source")}
    error = resolve_metric_model_dict(raw)
    if not error and models.get("metric_model") is not None:
        scorer["model"] = models["metric_model"]
    return error

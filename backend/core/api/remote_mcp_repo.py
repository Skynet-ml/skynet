"""Step-by-step repository run setup for the remote MCP server (SKY-320).

An MCP client builds a repository run the way the run wizard does, one step
at a time: repository, data, scorer, models, engine and budget. Each step
saves its part of a server-side draft and answers with feedback: what it
saved, the problems it found, and the next tool to call. ``repo_draft_review``
checks the whole draft and prices its smallest spending limit.
``repo_draft_submit`` starts the run only with ``confirm=true`` and a
``max_cost_cents`` the user agreed to.

The submission goes through the same path as ``POST /blackbox/run``
(:func:`core.api.routers.submissions.submit_repo_payload`), so the run gets
the same admission, balance check, protected budget and replay protection.
No step runs user code or spends money before the submit.
"""

from __future__ import annotations

import ast
import json
import math
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..billing.budgets import BudgetConflictError, BudgetError, BudgetService
from ..billing.operation_pricing import json_fingerprint
from ..billing.service import StripeBillingService
from ..config import settings
from ..connectors import github
from ..connectors.vault import ConnectorVault
from ..exceptions import ServiceError
from ..local.cli_catalog import parse_subscription_model_id
from ..models.blackbox import (
    BLACKBOX_ENGINE_SHINKA_EVOLVE,
    BLACKBOX_ITERATION_LIMIT_ENGINES,
    BLACKBOX_MAX_EXTRA_REFLECTION_MODELS,
    BlackboxBudget,
    BlackboxRepoSource,
    BlackboxRunRequest,
)
from ..service_gateway.optimization.blackbox.service import engine_catalog, validate_job
from ..storage.models import McpRunDraftModel
from .auth import AuthenticatedUser
from .errors import DomainError
from .mcp_oauth import SCOPE_READ, SCOPE_WRITE
from .remote_mcp import _domain_tool_error, _user
from .routers.connectors import connector_secret
from .routers.models import box_hold_cents, call_hold_cents
from .routers.submissions import (
    _BLACKBOX_WORKFLOW,
    staged_repo_cases,
    submit_repo_payload,
)
from .submission_idempotency import resolve_submission_replay_keys

STEPS = ("repository", "data", "scorer", "models", "engine", "budget")
_REQUIRED_STEPS = ("repository", "scorer", "models", "engine")
_STEP_TOOLS = {step: f"repo_draft_set_{step}" for step in STEPS}
_CASE_KEYS = frozenset({"input", "expected"})
# A draft is a scratch setup, not a saved object; old ones go so a client
# that never submits cannot grow the table without limit.
_MAX_DRAFTS_PER_ACCOUNT = 20
_DRAFT_MAX_AGE = timedelta(days=7)
# Matches the wizard: a model call reserves at least one dollar, and the
# default limit is at least five dollars.
_MIN_CALL_HOLD_CENTS = 100
_MIN_SUGGESTED_LIMIT_CENTS = 500
_PENDING_RETRY_SECONDS = 5
_TREE_SAMPLE = 60
_SCORER_NAMES = ("score", "metric")

TokenSource = Literal["managed", "byok", "subscription"]
StepCheck = Callable[[dict[str, Any], str], tuple[list[str], list[str], dict[str, Any]]]


def _refuse_lower_limit(job_store: Any, username: str, budget_key: str, max_cost_cents: int) -> None:
    """Refuse a retry that asks for less than the budget its key already made.

    The submission reuses the budget a known key made, so the run would keep
    the higher limit without a word. A retry that asks for more is allowed:
    the submission lowers a first limit to what the balance can back. A retry
    with the first limit is allowed too, also after the user raised the
    total in the app: ``BudgetService.create`` returns the existing budget
    unchanged when ``max_cost_cents`` equals its creation total. Known limit:
    the budget stores only a hash of the creation total. If the balance cap
    lowered the first limit and the user then raised the total, the original
    request gets this refusal, which names the run.

    Args:
        job_store: Store whose engine holds the budgets.
        username: Account that asks.
        budget_key: Budget creation key of the agent's idempotency key.
        max_cost_cents: Spending limit of this call.

    Raises:
        ToolError: When the key's budget total is above ``max_cost_cents``.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None:
        return
    service = BudgetService(engine=engine)
    try:
        existing = service.find_by_creation_key(username, budget_key)
        if existing is None or max_cost_cents >= existing.total_cents:
            return
        service.create(username, max_cost_cents, idempotency_key=budget_key, uncapped=settings.local_mode)
    except BudgetConflictError as error:
        detail = f"This idempotency_key already has a limit of {existing.total_cents} cents. "
        payload: dict[str, Any] = {"code": "budget.conflict", "status": 409}
        if existing.job_id:
            payload["optimization_id"] = existing.job_id
            detail += "Use get_run or stop_run on optimization_id, or a new key for a run with a lower limit."
        else:
            detail += "Use a new key for a run with a lower limit."
        payload["detail"] = detail
        raise ToolError(json.dumps(payload)) from error
    except BudgetError:
        # The submission reads the same wallet next and reports this with its own code.
        return


def _now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def _load_draft(session: Session, draft_id: str, username: str, *, lock: bool = False) -> McpRunDraftModel:
    """Load one of the account's drafts that is not expired.

    Args:
        session: Open database session.
        draft_id: The draft.
        username: Account that owns it.
        lock: Lock the row until the session commits, for a read-modify-write.

    Returns:
        The draft row.

    Raises:
        ToolError: When the account has no draft with this id, or the draft expired.
    """
    row = session.get(McpRunDraftModel, draft_id, with_for_update=lock)
    if row is None or row.username != username:
        raise ToolError("Unknown draft_id. Call repo_draft_start for a new draft.")
    # SQLite gives back naive datetimes; the column stores UTC.
    updated = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=UTC)
    if updated < _now() - _DRAFT_MAX_AGE:
        raise ToolError("This draft expired after 7 days without changes. Call repo_draft_start for a new draft.")
    return row


def _remaining(spec: dict[str, Any]) -> list[str]:
    """List the required steps the draft does not have yet."""
    return [step for step in _REQUIRED_STEPS if step not in spec]


def _feedback(
    draft_id: str, spec: dict[str, Any], step: str | None, issues: list[str], notes: list[str], **extra: Any
) -> dict[str, Any]:
    """Build the answer every step gives: what it saved and what comes next.

    Args:
        draft_id: The draft.
        spec: The whole draft after this step.
        step: The step that ran, if any.
        issues: Problems that block the run until fixed.
        notes: Facts the agent should know or tell the user.
        **extra: More fields for this step's answer.

    Returns:
        The step's feedback.
    """
    remaining = _remaining(spec)
    if issues and step is not None:
        next_tool = _STEP_TOOLS[step]
    elif remaining:
        next_tool = _STEP_TOOLS[remaining[0]]
    else:
        next_tool = "repo_draft_review"
    return {
        "draft_id": draft_id,
        "step": step,
        "saved": spec.get(step) if step else None,
        "issues": issues,
        "notes": notes,
        "remaining_steps": remaining,
        "next_tool": next_tool,
        **extra,
    }


def _github_token(job_store: Any, username: str) -> str | None:
    """Return the account's GitHub token, or ``None`` when GitHub is not connected.

    Args:
        job_store: Store whose engine holds the connector vault.
        username: The account.

    Returns:
        The access token, or ``None`` for an anonymous read of a public repository.
    """
    try:
        return connector_secret(ConnectorVault(job_store.engine), username, github.PROVIDER).access_token
    except DomainError:
        return None


def repository_issues(job_store: Any, username: str, repo: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Check that the repository, its ref and its editable paths exist on GitHub.

    Args:
        job_store: Store whose engine holds the connector vault.
        username: The account.
        repo: The draft's repository part.

    Returns:
        The issues, the notes, and a sample of the repository's top-level paths.
    """
    token = _github_token(job_store, username)
    notes: list[str] = []
    if token is None:
        notes.append("GitHub is not connected to this account, so only a public repository can be read.")
    ref = repo.get("commit") or repo.get("branch") or ""
    try:
        tree = github.tree_entries(token, repo["repository"], ref)
    except DomainError as error:
        where = f" at '{ref}'" if ref else ""
        issue = f"Cannot read {repo['repository']}{where} on GitHub ({error.code})."
        if token is None:
            issue += " For a private repository, connect GitHub in Skynet first."
        return [issue], notes, []
    paths = {entry["path"] for entry in tree["entries"]}
    top_level = sorted({path.split("/", 1)[0] for path in paths})[:_TREE_SAMPLE]
    missing = [path for path in repo["editable_paths"] if path != "." and path not in paths]
    if missing and tree["truncated"]:
        notes.append("The repository is too large to list whole, so the editable paths were not all checked.")
        missing = []
    issues = [f"Editable path '{path}' does not exist in the repository." for path in missing]
    return issues, notes, top_level


def case_shape_issues(cases: list[dict[str, Any]]) -> list[str]:
    """Check that each inline case has the shape staged cases get from ``rows_to_cases`` (E42).

    Args:
        cases: The inline cases of the data step.

    Returns:
        One issue that names the first bad case and the count, or no issue.
    """
    bad = [number for number, case in enumerate(cases, start=1) if "input" not in case or set(case) - _CASE_KEYS]
    if not bad:
        return []
    keys = ", ".join(sorted(cases[bad[0] - 1])) or "no keys"
    return [
        f'Each inline case must be {{"input": ..., "expected": ...}}; "expected" is optional. '
        f"Case {bad[0]} has {keys}, and {len(bad)} of {len(cases)} cases have another shape."
    ]


def scorer_issues(metric_code: str, has_cases: bool) -> tuple[list[str], list[str]]:
    """Check scorer code statically: it parses and defines the scorer function.

    The code never runs here. The run's setup check runs it in the sandbox.

    Args:
        metric_code: The scorer source.
        has_cases: Whether the draft has data, so the scorer can get a case.

    Returns:
        The issues and the notes.
    """
    try:
        tree = ast.parse(metric_code)
    except SyntaxError as error:
        return [f"The scorer has a syntax error on line {error.lineno}: {error.msg}."], []
    except (ValueError, MemoryError, RecursionError):
        # Very deep nesting makes the parser give up without a SyntaxError.
        return ["Skynet cannot parse the scorer. Make it simpler."], []
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
    bound, named = _scorer_binding(tree)
    if bound and named is None:
        # The runner takes any callable bound to the name, such as an import,
        # an instance or a decorated function, so only the run's setup check
        # can check it.
        return [], ["Skynet cannot check an imported, assigned or decorated scorer before the run's setup check."]
    scorer = named or (functions[0] if len(functions) == 1 else None)
    if scorer is None:
        return [
            "The scorer must define score(repo_path), score(repo_path, case) or score(repo_path, case, output=None)."
        ], []
    args = scorer.args
    positional = len(args.posonlyargs) + len(args.args)
    if args.vararg is None and positional < 1:
        return [f"The scorer function {scorer.name} takes no arguments. Use score(repo_path)."], []
    # The runner calls the scorer with at most (repo_path, case, output) and
    # without await, so a coroutine or a generator result stops the run. The
    # output is what the [run] entry returned for the case (E39). The runner
    # calls fn(repo_path, case) today and passes no output when the file has no
    # [run] entry, so output must default to None.
    if isinstance(scorer, ast.AsyncFunctionDef):
        return [f"The scorer function {scorer.name} is async. Use a plain def."], []
    if any(isinstance(node, ast.Yield | ast.YieldFrom) for node in _own_nodes(scorer)):
        return [f"The scorer function {scorer.name} uses yield. Return the score instead."], []
    required = positional - len(args.defaults)
    if required > 3 or any(default is None for default in args.kw_defaults):
        return [f"The scorer function {scorer.name} needs more arguments than (repo_path, case, output)."], []
    if required == 3:
        return [
            f"The scorer function {scorer.name} needs output. Write score(repo_path, case, output=None), because"
            " a run with no [run] entry passes no output."
        ], []
    issues: list[str] = []
    notes: list[str] = []
    if _returns_bare_number(scorer):
        issues.append(
            f"The scorer function {scorer.name} returns a bare number. Return (score, feedback) "
            "or {'score': ..., 'feedback': ...}, because the engines learn from the feedback."
        )
    if has_cases and args.vararg is None and positional < 2:
        # The runner calls a one-argument scorer without the case, so this runs.
        notes.append(f"The scorer function {scorer.name} does not take the case, so every case gets the same score.")
    return issues, notes


def _scorer_binding(tree: ast.Module) -> tuple[bool, ast.FunctionDef | ast.AsyncFunctionDef | None]:
    """Find the top-level binding the runner uses as the scorer.

    Like the runner, ``score`` wins over ``metric``, and the last top-level
    binding of the name wins.

    Args:
        tree: The parsed scorer module.

    Returns:
        Whether a scorer name is bound, and the function when that binding is
        a plain ``def`` without decorators.
    """
    for name in _SCORER_NAMES:
        bound = False
        function: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names = [node.name]
            elif isinstance(node, ast.Import | ast.ImportFrom):
                names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.Assign):
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            elif isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            if name in names:
                bound = True
                plain = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.decorator_list
                function = node if plain else None
        if bound:
            return True, function
    return False, None


def _returns_bare_number(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Tell whether ``function`` has a ``return`` of a number literal.

    The first bare-number score stops the whole run, also on an error path,
    so this catches the mistake before the paid run. Nested functions are
    not checked.

    Args:
        function: The scorer function node.

    Returns:
        True when one of its own ``return`` statements gives a number literal.
    """
    for node in _own_nodes(function):
        if isinstance(node, ast.Return):
            value = node.value
            if isinstance(value, ast.UnaryOp):
                value = value.operand
            # The runner refuses bools too: they are ints.
            if isinstance(value, ast.Constant) and isinstance(value.value, int | float):
                return True
    return False


def _own_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """Yield the nodes of ``function``'s body, without nested functions or classes.

    Args:
        function: The scorer function node.

    Yields:
        Each node that runs as part of ``function`` itself.
    """
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _model_entries(models: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """List the ``(name, token_source, cli_provider)`` of every model of the draft's models part.

    Like ``normalize_model_token_sources``, the Mac app reads a
    ``subscription/<cli>/<slug>`` name as a subscription model on that CLI,
    whatever source the step names.

    Args:
        models: The draft's models part.

    Returns:
        One entry for each model; ``cli_provider`` is ``None`` unless the
        name is a subscription model id.
    """
    names = [models["reflection_model"], *models.get("extra_models", [])]
    if models.get("scorer_model"):
        names.append(models["scorer_model"])
    entries: list[tuple[str, str, str | None]] = []
    for name in names:
        parsed = parse_subscription_model_id(name.strip("/"))
        source = "subscription" if parsed is not None and settings.local_mode else models["token_source"]
        entries.append((name, source, parsed[0] if parsed is not None and source == "subscription" else None))
    return entries


def _model_config(entry: tuple[str, str, str | None]) -> dict[str, Any]:
    """Build the request model config of one ``_model_entries`` entry."""
    name, source, cli = entry
    return {"name": name, "token_source": source, **({"cli_provider": cli} if cli else {})}


def model_issues(models: dict[str, Any], http_client: Callable[[], httpx.Client]) -> tuple[list[str], list[str]]:
    """Check that this server can run the token source, and that managed models have a verified price.

    The source rules mirror ``enforce_local_mode_sources``, so a draft that
    review calls ready is not refused at submit for its token source.

    Args:
        models: The draft's models part.
        http_client: Factory for the model catalog client.

    Returns:
        The issues and the notes.
    """
    entries = _model_entries(models)
    sources = {source for _, source, _ in entries}
    if settings.local_mode and "managed" in sources:
        return ["The Mac app cannot use managed models. Use token_source 'subscription' or 'byok'."], []
    if not settings.local_mode and "subscription" in sources:
        return ["A subscription model runs only in the Mac app. Use token_source 'managed' or 'byok'."], []
    issues = [
        f"Name the subscription model '{name}' as subscription/<codex|claude>/<model>, so Skynet knows the CLI."
        for name, source, cli in entries
        if source == "subscription" and cli is None
    ]
    if "managed" not in sources:
        return issues, ["Skynet cannot check a model on your own key or subscription before the run starts."]
    with http_client() as client:
        for name, source, _cli in entries:
            if call_hold_cents(name, source, client=client) is None:
                issues.append(f"Skynet has no verified price for model '{name}'. Choose another model.")
    return issues, []


def engine_issues(spec: dict[str, Any]) -> list[str]:
    """Check the engine against the repository catalog and the other steps.

    Args:
        spec: The whole draft.

    Returns:
        The issues.
    """
    engine = spec["engine"]["engine"]
    catalog = {entry.id: entry for entry in engine_catalog("repo").engines}
    entry = catalog.get(engine)
    if entry is None:
        return [f"Unknown engine '{engine}'. Use one of: {', '.join(sorted(catalog))}."]
    issues: list[str] = []
    if not entry.available:
        issues.append(f"Engine '{engine}' cannot run a repository here: {entry.unavailable_reason}")
    if spec.get("models", {}).get("extra_models") and engine != BLACKBOX_ENGINE_SHINKA_EVOLVE:
        issues.append(f"Extra optimization models apply only to {BLACKBOX_ENGINE_SHINKA_EVOLVE}. Remove them.")
    if spec.get("budget", {}).get("max_iterations") is not None and engine not in BLACKBOX_ITERATION_LIMIT_ENGINES:
        issues.append("An iteration limit applies only to Meta-Harness, AutoSaddler or ShinkaEvolve.")
    return issues


def engine_choices() -> list[dict[str, Any]]:
    """List the repository engines with their availability, for the agent to offer."""
    return [
        {"id": entry.id, "label": entry.label, "description": entry.description, "available": entry.available}
        for entry in engine_catalog("repo").engines
    ]


def build_request(spec: dict[str, Any]) -> BlackboxRunRequest:
    """Build the repository run request from a complete draft.

    Args:
        spec: The whole draft.

    Returns:
        The run request, without a spending limit.

    Raises:
        ValidationError: When the parts do not form a valid request.
    """
    data = spec.get("data") or {}
    models = spec["models"]
    scorer = spec["scorer"]
    entries = _model_entries(models)
    extra_count = len(models.get("extra_models", []))
    body: dict[str, Any] = {
        "name": spec.get("name"),
        "description": spec.get("description"),
        "objective": spec.get("objective"),
        "recipe": "repo",
        "scorer": {
            "kind": "python",
            "metric_code": scorer["metric_code"],
            "timeout_seconds": scorer["timeout_seconds"],
            **({"model": _model_config(entries[-1])} if models.get("scorer_model") else {}),
        },
        "cases": data.get("cases"),
        "staged_dataset_id": data.get("staged_dataset_id"),
        "input_fields": data.get("input_fields") or [],
        "expected_field": data.get("expected_field"),
        "budget": spec.get("budget") or {},
        "strategy": {"mode": "single", "engine": spec["engine"]["engine"]},
        "target": {
            "kind": "repo",
            "setup_command": spec["repository"].get("setup_command"),
            "repo": {key: spec["repository"].get(key) for key in ("repository", "branch", "commit", "editable_paths")},
        },
        "task_model_config": None,
        "reflection_model_config": _model_config(entries[0]),
        "extra_reflection_model_configs": [_model_config(entry) for entry in entries[1 : 1 + extra_count]],
        "token_source": models["token_source"],
    }
    return BlackboxRunRequest.model_validate(body)


def _validation_issues(error: ValidationError) -> list[str]:
    """Turn a request validation error into short issue lines."""
    return [str(item.get("msg", "")).removeprefix("Value error, ") for item in error.errors()]


def repo_floor_cents(spec: dict[str, Any], http_client: Callable[[], httpx.Client]) -> tuple[int, list[str]]:
    """Estimate the smallest spending limit a repository draft can start with.

    It mirrors the wizard and ``check_run``: the boxes' hold plus one call of
    the dearest model. A run on the user's subscription needs only the boxes.

    Args:
        spec: The whole draft.
        http_client: Factory for the model catalog client.

    Returns:
        The floor in cents, and the models the catalog has no price for.
    """
    entries = _model_entries(spec["models"])
    boxes = math.ceil(box_hold_cents())
    unpriced: list[str] = []
    if all(source == "subscription" for _, source, _cli in entries):
        return boxes, unpriced
    holds: list[Decimal] = []
    with http_client() as client:
        for name, source, _cli in entries:
            hold = call_hold_cents(name, source, client=client)
            if hold is None:
                unpriced.append(name)
            else:
                holds.append(hold)
    return boxes + max(_MIN_CALL_HOLD_CENTS, math.ceil(max(holds, default=Decimal(0)))), unpriced


def review_draft(
    spec: dict[str, Any], user: AuthenticatedUser, *, job_store: Any, http_client: Callable[[], httpx.Client]
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Check a whole draft again, and price it when it is complete.

    Args:
        spec: The whole draft.
        user: Account that owns it.
        job_store: Store that holds staged data, connectors and the wallet.
        http_client: Factory for the model catalog client.

    Returns:
        The issues, the notes, and the price fields (empty while steps are missing).
    """
    missing = _remaining(spec)
    if missing:
        return [f"Step '{step}' is not done. Call {_STEP_TOOLS[step]}." for step in missing], [], {}
    repo_problems, notes, _top = repository_issues(job_store, user.username, spec["repository"])
    issues = list(repo_problems)
    data = spec.get("data") or {}
    issues += case_shape_issues(data.get("cases") or [])
    if data.get("staged_dataset_id"):
        try:
            rows = staged_repo_cases(
                data["staged_dataset_id"],
                input_fields=data.get("input_fields") or [],
                expected_field=data.get("expected_field"),
                job_store=job_store,
                username=user.username,
            )
            notes.append(f"The staged dataset has {len(rows)} cases.")
        except DomainError as error:
            issues.append(f"The staged dataset cannot be read ({error.code}). Stage it again and set the data step.")
    scorer_found, scorer_notes = scorer_issues(spec["scorer"]["metric_code"], bool(data))
    issues += scorer_found
    notes += scorer_notes
    issues += model_issues(spec["models"], http_client)[0]
    issues += engine_issues(spec)
    try:
        request = build_request(spec)
    except ValidationError as error:
        return issues + _validation_issues(error), notes, {}
    floor, unpriced = repo_floor_cents(spec, http_client)
    request.max_cost_cents = max(floor, 1)
    try:
        validate_job(request)
    except ServiceError as error:
        issues.append(str(error))
    price = {
        "min_max_cost_cents": floor,
        "suggested_max_cost_cents": max(_MIN_SUGGESTED_LIMIT_CENTS, floor),
        "unpriced_models": unpriced,
        "spendable_cents": StripeBillingService(engine=job_store.engine).spendable_cents(user.username),
    }
    return issues, notes, price


def submit_draft(
    spec: dict[str, Any],
    draft_id: str,
    max_cost_cents: int,
    idempotency_key: str | None,
    user: AuthenticatedUser,
    *,
    service: Any,
    job_store: Any,
    http_client: Callable[[], httpx.Client],
) -> dict[str, Any]:
    """Start the draft's run under a new execution budget of ``max_cost_cents``.

    Without a key, the key is the draft id plus a fingerprint of the draft.
    A retry of the same submit returns the first run, and a changed draft
    starts a new one.

    Args:
        spec: The whole draft.
        draft_id: The draft.
        max_cost_cents: Spending limit the user agreed to.
        idempotency_key: Optional key; a repeat returns the original run.
        user: Account that runs it.
        service: Optimization service handed to the worker.
        job_store: Store that persists the run.
        http_client: Factory for the model catalog client.

    Returns:
        The submission, or ``status: in_progress`` while other usage on the
        account settles.

    Raises:
        ToolError: When the draft has issues, or the limit is below its floor.
    """
    issues, _notes, price = review_draft(spec, user, job_store=job_store, http_client=http_client)
    if issues:
        raise ToolError(json.dumps({"code": "draft.not_ready", "issues": issues, "next_tool": "repo_draft_review"}))
    floor = price["min_max_cost_cents"]
    if max_cost_cents < floor:
        raise ToolError(
            json.dumps(
                {
                    "code": "budget.below_minimum",
                    "detail": f"max_cost_cents {max_cost_cents} is below this run's minimum of {floor} cents.",
                    "min_max_cost_cents": floor,
                }
            )
        )
    request = build_request(spec)
    request.max_cost_cents = max_cost_cents
    key = idempotency_key or f"mcp-draft-{draft_id}-{json_fingerprint(spec)[:16]}"
    replay_keys = resolve_submission_replay_keys(
        request, username=user.username, workflow=_BLACKBOX_WORKFLOW, supplied_key=key
    )
    if replay_keys.budget is not None:
        _refuse_lower_limit(job_store, user.username, replay_keys.budget, max_cost_cents)
    try:
        result = submit_repo_payload(
            request, service=service, job_store=job_store, current_user=user, idempotency_key=key
        )
    except DomainError as error:
        if error.code != "budget.pending":
            raise
        return {
            "status": "in_progress",
            "retry_after_seconds": _PENDING_RETRY_SECONDS,
            "detail": "Other usage on this account must settle first. Call repo_draft_submit again with the same arguments.",
        }
    return {**result.model_dump(mode="json"), "next_tool": "get_run"}


def register_repo_tools(mcp: FastMCP, http_client: Callable[[], httpx.Client], *, service: Any, job_store: Any) -> None:
    """Add the repository draft tools to the remote MCP server.

    Args:
        mcp: The remote MCP server.
        http_client: Factory for the model catalog client.
        service: Optimization service handed to the worker.
        job_store: Store that holds drafts, runs, budgets and connectors.
    """
    writes = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def guarded(work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run a tool body and turn service-layer errors into tool errors.

        Args:
            work: The tool body.

        Returns:
            The tool result.

        Raises:
            ToolError: For any service-layer error.
        """
        try:
            return work()
        except DomainError as error:
            raise _domain_tool_error(error) from error

    def save_step(draft_id: str, step: str, part: dict[str, Any] | None, check: StepCheck) -> dict[str, Any]:
        """Save one step's part of a draft, then check it and answer with feedback.

        Args:
            draft_id: The draft.
            step: The step name.
            part: The step's part; ``None`` removes it.
            check: Returns the issues, notes and extra answer fields for the saved draft.

        Returns:
            The step's feedback.
        """
        user = _user(SCOPE_WRITE)
        with Session(job_store.engine) as session:
            row = _load_draft(session, draft_id, user.username, lock=True)
            spec = dict(row.spec or {})
            if part is None:
                spec.pop(step, None)
            else:
                spec[step] = part
            row.spec = spec
            row.updated_at = _now()
            session.commit()
        try:
            issues, notes, extra = check(spec, user.username)
        except DomainError as error:
            raise _domain_tool_error(error) from error
        return _feedback(draft_id, spec, step, issues, notes, **extra)

    @mcp.tool(name="repo_draft_start", annotations=writes)
    def repo_draft_start(
        objective: Annotated[str, Field(min_length=1, max_length=4000, description="What the run should improve.")],
        name: Annotated[str | None, Field(max_length=200)] = None,
        description: Annotated[str | None, Field(max_length=280)] = None,
    ) -> dict[str, Any]:
        """Start a repository run draft. This is the first step of a repository optimization.

        The steps follow the run wizard: repository, data (optional), scorer,
        models, engine, budget (optional), then review and submit. Every step
        answers with the issues it found and the next tool to call.
        """
        user = _user(SCOPE_WRITE)
        cutoff = _now() - _DRAFT_MAX_AGE
        spec = {"objective": objective.strip(), "name": name, "description": description}
        with Session(job_store.engine) as session:
            session.execute(
                delete(McpRunDraftModel).where(
                    McpRunDraftModel.username == user.username, McpRunDraftModel.updated_at < cutoff
                )
            )
            count = session.scalar(
                select(func.count()).select_from(McpRunDraftModel).where(McpRunDraftModel.username == user.username)
            )
            if (count or 0) >= _MAX_DRAFTS_PER_ACCOUNT:
                oldest = session.scalars(
                    select(McpRunDraftModel)
                    .where(McpRunDraftModel.username == user.username)
                    .order_by(McpRunDraftModel.updated_at)
                    .limit(int(count) - _MAX_DRAFTS_PER_ACCOUNT + 1)
                ).all()
                for row in oldest:
                    session.delete(row)
            row = McpRunDraftModel(username=user.username, spec=spec, updated_at=_now())
            session.add(row)
            session.commit()
            draft_id = row.id
        return _feedback(draft_id, spec, None, [], [], engines=engine_choices())

    @mcp.tool(name="repo_draft_set_repository", annotations=writes)
    def repo_draft_set_repository(
        draft_id: Annotated[str, Field(max_length=64)],
        repository: Annotated[str, Field(description="GitHub repository as owner/name.")],
        editable_paths: Annotated[
            list[str],
            Field(min_length=1, max_length=200, description="Files and folders the run may change; '.' is all."),
        ],
        branch: Annotated[
            str | None, Field(max_length=255, description="Branch; empty for the default branch.")
        ] = None,
        commit: Annotated[str | None, Field(description="Full 40-character commit to start from.")] = None,
        setup_command: Annotated[
            str | None, Field(max_length=2000, description="Command that prepares the checkout; empty to infer it.")
        ] = None,
    ) -> dict[str, Any]:
        """Set the GitHub repository, its branch or commit, and the paths the run may change.

        Skynet checks that the repository and every editable path exist.
        """
        try:
            source = BlackboxRepoSource(
                repository=repository, branch=branch, commit=commit, editable_paths=editable_paths
            )
        except ValidationError as error:
            raise ToolError("; ".join(_validation_issues(error))) from error
        part = {
            **source.model_dump(include={"repository", "branch", "commit", "editable_paths"}),
            "setup_command": setup_command,
        }

        def check(spec: dict[str, Any], username: str) -> tuple[list[str], list[str], dict[str, Any]]:
            """Check the repository on GitHub."""
            issues, notes, top_level = repository_issues(job_store, username, spec["repository"])
            return issues, notes, {"top_level_paths": top_level}

        return save_step(draft_id, "repository", part, check)

    @mcp.tool(name="repo_draft_set_data", annotations=writes)
    def repo_draft_set_data(
        draft_id: Annotated[str, Field(max_length=64)],
        cases: Annotated[
            list[dict[str, Any]] | None,
            Field(
                max_length=2000,
                description='Small inline cases, each {"input": ..., "expected": ...}; "expected" is optional.'
                " The candidate gets only input; the scorer gets the whole case.",
            ),
        ] = None,
        staged_dataset_id: Annotated[
            str | None, Field(max_length=64, description="A dataset staged before, for more cases.")
        ] = None,
        input_fields: Annotated[list[str] | None, Field(description="Staged columns that form a case.")] = None,
        expected_field: Annotated[str | None, Field(description="Staged column with the expected answer.")] = None,
    ) -> dict[str, Any]:
        """Set the cases the scorer runs on. Optional: skip it when the scorer needs no cases.

        Send no cases and no staged_dataset_id to remove the data step.
        """
        if cases and staged_dataset_id:
            raise ToolError("Send cases or staged_dataset_id, not both.")
        part = (
            {
                "cases": cases or None,
                "staged_dataset_id": staged_dataset_id,
                "input_fields": input_fields or [],
                "expected_field": expected_field,
            }
            if cases or staged_dataset_id
            else None
        )

        def check(spec: dict[str, Any], username: str) -> tuple[list[str], list[str], dict[str, Any]]:
            """Count the cases and check that a staged dataset exists."""
            data = spec.get("data")
            if not data:
                return [], ["The run has no cases. The scorer runs once on the whole repository."], {}
            if data.get("cases"):
                return case_shape_issues(data["cases"]), [f"The run has {len(data['cases'])} inline cases."], {}
            try:
                rows = staged_repo_cases(
                    data["staged_dataset_id"],
                    input_fields=data["input_fields"],
                    expected_field=data["expected_field"],
                    job_store=job_store,
                    username=username,
                )
            except DomainError as error:
                return [f"The staged dataset cannot be read ({error.code})."], [], {}
            return [], [f"The staged dataset has {len(rows)} cases."], {"case_example": rows[0] if rows else None}

        return save_step(draft_id, "data", part, check)

    @mcp.tool(name="repo_draft_set_scorer", annotations=writes)
    def repo_draft_set_scorer(
        draft_id: Annotated[str, Field(max_length=64)],
        metric_code: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200_000,
                description="Python that defines score(repo_path), score(repo_path, case) or "
                "score(repo_path, case, output=None), where output is what the optimize.toml [run] entry "
                "returned for the case and None when nothing ran. It returns a "
                "(score, feedback) pair or a {'score': float, 'feedback': str} dict. A bare number fails the run.",
            ),
        ],
        timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 60,
    ) -> dict[str, Any]:
        """Set the Python scorer that grades each version of the repository.

        Skynet checks the code statically. It does not run it before the run starts.
        """
        part = {"metric_code": metric_code, "timeout_seconds": timeout_seconds}

        def check(spec: dict[str, Any], _username: str) -> tuple[list[str], list[str], dict[str, Any]]:
            """Check the scorer code without running it."""
            issues, notes = scorer_issues(metric_code, bool(spec.get("data")))
            return issues, notes, {}

        return save_step(draft_id, "scorer", part, check)

    @mcp.tool(name="repo_draft_set_models", annotations=writes)
    def repo_draft_set_models(
        draft_id: Annotated[str, Field(max_length=64)],
        reflection_model: Annotated[
            str, Field(min_length=1, max_length=200, description="Model of the coding agent that writes each version.")
        ],
        token_source: Annotated[
            TokenSource,
            Field(description="managed: Skynet pays and charges you; byok: your key; subscription: your CLI."),
        ] = "managed",
        extra_models: Annotated[
            list[str],
            Field(max_length=BLACKBOX_MAX_EXTRA_REFLECTION_MODELS, description="More models for shinka_evolve only."),
        ] = [],  # noqa: B006 - FastMCP copies tool defaults per call.
        scorer_model: Annotated[
            str | None, Field(max_length=200, description="Model the scorer's llm() helper calls, if it uses one.")
        ] = None,
    ) -> dict[str, Any]:
        """Set the models the run uses and who pays for their calls."""
        part = {
            "reflection_model": reflection_model.strip(),
            "token_source": token_source,
            "extra_models": [name.strip() for name in extra_models if name.strip()],
            "scorer_model": (scorer_model or "").strip() or None,
        }

        def check(spec: dict[str, Any], _username: str) -> tuple[list[str], list[str], dict[str, Any]]:
            """Check that each platform-paid model has a price."""
            issues, notes = model_issues(spec["models"], http_client)
            return issues, notes, {}

        return save_step(draft_id, "models", part, check)

    @mcp.tool(name="repo_draft_set_engine", annotations=writes)
    def repo_draft_set_engine(
        draft_id: Annotated[str, Field(max_length=64)],
        engine: Annotated[str, Field(max_length=64, description="An engine id from repo_draft_start's engines list.")],
    ) -> dict[str, Any]:
        """Set the optimization engine. A repository run uses exactly one engine."""

        def check(spec: dict[str, Any], _username: str) -> tuple[list[str], list[str], dict[str, Any]]:
            """Check the engine against the repository catalog."""
            return engine_issues(spec), [], {}

        return save_step(draft_id, "engine", {"engine": engine.strip()}, check)

    @mcp.tool(name="repo_draft_set_budget", annotations=writes)
    def repo_draft_set_budget(
        draft_id: Annotated[str, Field(max_length=64)],
        max_scorer_runs: Annotated[
            int, Field(ge=1, le=100_000, description="Most scorer runs in the whole run.")
        ] = 200,
        max_iterations: Annotated[int | None, Field(ge=1, le=1000)] = None,
        stop_at_score: Annotated[float | None, Field(description="Stop early at this score.")] = None,
    ) -> dict[str, Any]:
        """Set how long the run searches. Optional: the default is 200 scorer runs.

        The money limit is not set here. repo_draft_submit takes it.
        """
        try:
            budget = BlackboxBudget(
                max_scorer_runs=max_scorer_runs, max_iterations=max_iterations, stop_at_score=stop_at_score
            )
        except ValidationError as error:
            raise ToolError("; ".join(_validation_issues(error))) from error

        def check(spec: dict[str, Any], _username: str) -> tuple[list[str], list[str], dict[str, Any]]:
            """Check the iteration limit against the engine."""
            return (engine_issues(spec) if "engine" in spec else []), [], {}

        return save_step(draft_id, "budget", budget.model_dump(exclude_none=True), check)

    @mcp.tool(name="repo_draft_review", annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def repo_draft_review(draft_id: Annotated[str, Field(max_length=64)]) -> dict[str, Any]:
        """Check the whole draft again and price its smallest spending limit. It spends nothing.

        Show the user the limits and the balance, and ask them to agree to a
        max_cost_cents before repo_draft_submit.
        """
        user = _user(SCOPE_READ)
        with Session(job_store.engine) as session:
            spec = dict(_load_draft(session, draft_id, user.username).spec or {})
        try:
            issues, notes, price = review_draft(spec, user, job_store=job_store, http_client=http_client)
        except DomainError as error:
            raise _domain_tool_error(error) from error
        answer = _feedback(draft_id, spec, None, issues, notes, ready=not issues, **price)
        answer["draft"] = {key: value for key, value in spec.items() if key != "scorer"}
        if not issues:
            answer["next_tool"] = "repo_draft_submit"
            answer["note"] = "A run stops when it reaches max_cost_cents. Unused budget is released when the run ends."
        return answer

    @mcp.tool(
        name="repo_draft_submit",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    def repo_draft_submit(
        draft_id: Annotated[str, Field(max_length=64)],
        max_cost_cents: Annotated[int, Field(ge=1, le=1_000_000, description="Spending limit of this run, in cents.")],
        confirm: Annotated[bool, Field(description="True only after the user agreed to max_cost_cents.")],
        idempotency_key: Annotated[
            str | None, Field(max_length=128, description="Send the same key to retry without a second run.")
        ] = None,
    ) -> dict[str, Any]:
        """Start the repository run. It spends at most max_cost_cents from the account balance.

        Call repo_draft_review first and show the user its price. Send
        confirm=true only after the user agreed. If the result says
        in_progress, call again with the same arguments.
        """
        if not confirm:
            raise ToolError(
                "Show the user max_cost_cents and the balance from repo_draft_review. Send confirm=true after they agree."
            )
        user = _user(SCOPE_WRITE)
        with Session(job_store.engine) as session:
            spec = dict(_load_draft(session, draft_id, user.username).spec or {})
        return guarded(
            lambda: submit_draft(
                spec,
                draft_id,
                max_cost_cents,
                idempotency_key,
                user,
                service=service,
                job_store=job_store,
                http_client=http_client,
            )
        )

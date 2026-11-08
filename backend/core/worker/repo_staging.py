"""Stage a repository target's tree and secrets in the trusted parent before its sandbox opens."""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api.model_catalog import agent_model_id, catalog_snapshot, lowest_effort_from_low
from ..billing.model_gateway import ROUTE_KEY
from ..billing.operation_pricing import ChargePolicy
from ..billing.protected_credentials import ProtectedCredentialVault, resolve_repo_secrets
from ..billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from ..brand import APP_NAME
from ..config import settings
from ..connectors.github_publish import (
    BEST_BRANCH,
    HardeningSummary,
    RepoPublishError,
    open_pull_request,
    push_candidates,
    push_version,
)
from ..connectors.github_repo import RepoFetchError, RepoSnapshot, fetch_snapshot, github_token, resolve_commit
from ..connectors.saved_secrets import SavedSecretStore
from ..exceptions import ServiceError
from ..models.common import ModelConfig
from ..models.hardening import HardeningJob, HardeningJobModel
from ..service_gateway.optimization.blackbox.protocol import is_metric_error
from ..service_gateway.optimization.blackbox.repo_constraints import (
    ConstraintsError,
    MetricSpec,
    ServiceSpec,
    TargetStage,
    effective_environment,
    instructions_brief,
    read_constraints,
)
from ..service_gateway.optimization.blackbox.repo_scorer import RepoScorer
from ..service_gateway.optimization.blackbox.repo_services import plan_roles
from ..service_gateway.optimization.blackbox.repo_setup import infer_setup_command
from ..service_gateway.optimization.blackbox.repo_tree import (
    EXPLOITS_DIR,
    REPO_SNAPSHOT_KEY,
    drop_paths,
    patch_paths,
    patch_violations,
    strip_archive,
)
from ..service_gateway.optimization.blackbox.repo_workspace import RepoWorkspace
from ..service_gateway.optimization.blackbox.rule_judge import (
    JUDGE_ANSWER_TOKENS,
    JUDGE_REASONING_TOKENS,
    RuleJudge,
    describe_model_call,
    run_log_emitter,
)
from ..service_gateway.optimization.blackbox.sandbox import SandboxSpec
from ..service_gateway.optimization.blackbox.sandbox_scorer import ScorerGateway, scorer_gateway
from .repo_gate import RepoGateError, check_hidden, sandbox_place

logger = logging.getLogger(__name__)

PULL_REQUEST_DETAIL = "pull_request"
CANDIDATE_BRANCHES_DETAIL = "candidate_branches"
# A long run can record hundreds of versions; only the top scorers get a branch,
# so a created repository does not fill with branches nobody reads.
MAX_CANDIDATE_BRANCHES = 10
ENVIRONMENT_KEY = "environment"
# Rules the run is held to; a resumed run must arrive at exactly these again.
_ENFORCED_FIELDS = ("commit", "editable_paths", "protected_paths", "readonly_paths", "services", "rules")
# Environments recorded before a field existed hold its empty value.
_ENFORCED_DEFAULTS: dict[str, Any] = {"rules": []}
_SETUP_ANSWER_TOKENS = 1_024
_JUDGE_ROLE = "judge"
# The per-run watch hook setting: ``{"enabled", "model", "threshold"}``, each
# optional; a missing key takes the server default (SKY-349).
RULE_WATCH_KEY = "rule_watch"
# Only a job that ended on its own terms ships its suite; a failed, rejected or
# stale job may hold rounds the user never finished reviewing (SKY-356).
_FINISHED_HARDENING = frozenset({"clean_stop", "budget_stop", "done"})
# Exploit ids are uuid4 hex; anything else could name a path outside the suite.
_EXPLOIT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class StagedRepository:
    """A fetched tree plus the secrets only the parent's scorer box receives."""

    snapshot: RepoSnapshot
    secrets: dict[str, str]
    workdir: Path
    scorers: list[RepoScorer] = field(default_factory=list)
    protected_paths: tuple[str, ...] = ()
    constrained: bool = False
    rules: tuple[str, ...] = ()
    intent: str | None = None
    hidden_paths: tuple[str, ...] = ()
    targets: Mapping[str, TargetStage] = field(default_factory=dict)
    services: Mapping[str, ServiceSpec] = field(default_factory=dict)
    # The services each role lists, by role name: [run] for the version's entry,
    # [metric] for the scorer. Each runs in its own process with its own relay (E37).
    service_roles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # The [run] entry, which runs before the metric with the metric files out of the tree (E38).
    run_entry: str | tuple[str, ...] | None = None
    run_timeout: float = 60.0
    agent_archive: Path | None = None
    exploits_path: str = EXPLOITS_DIR

    def close_scorers(self) -> None:
        """Close the parent's scoring boxes; the gateway still stops any that fail to close."""
        scorers, self.scorers[:] = list(self.scorers), []
        for scorer in scorers:
            with contextlib.suppress(Exception):
                scorer.close()

    def cleanup(self) -> None:
        """Delete the fetched tree from the parent's disk."""
        shutil.rmtree(self.workdir, ignore_errors=True)


def is_repo_payload(payload: dict[str, Any]) -> bool:
    """Report whether a black-box payload optimizes a repository.

    Args:
        payload: Black-box run payload.

    Returns:
        Whether its target is a repository.
    """
    target = payload.get("target")
    return isinstance(target, dict) and target.get("kind") == "repo" and isinstance(target.get("repo"), dict)


def stage_repository(
    payload: dict[str, Any],
    *,
    username: str,
    binding_id: str,
    engine: Any,
) -> tuple[dict[str, Any], StagedRepository]:
    """Fetch the pinned tree and decrypt the run's secrets in the parent.

    Args:
        payload: Parent payload whose secrets are still vault references.
        username: Owner of the run and of the GitHub connection.
        binding_id: Execution budget the vaulted secrets are bound to.
        engine: SQLAlchemy engine for the vaults.

    Returns:
        A payload copy that pins the commit, lists no secret entries, names
        the packed tree and carries the effective environment under
        ``target.repo.environment``, plus the staged tree and secrets for the parent.

    Raises:
        ConstraintsError: When the repository's ``optimize.toml`` is invalid,
            asks for more than the run approved, or a resumed run's rules
            changed since they were first recorded.
        RepoGateError: When the agent's tree still shows a metric file.
    """
    result = copy.deepcopy(payload)
    repo = result["target"]["repo"]
    secrets = resolve_repo_secrets(
        result,
        username=username,
        binding_id=binding_id,
        vault=ProtectedCredentialVault(engine=engine),
        saved=SavedSecretStore(engine),
    )
    token = github_token(engine, username)
    commit = repo.get("commit") or resolve_commit(token, repo["repository"], repo.get("branch"))
    workdir = Path(tempfile.mkdtemp(prefix="app-repo-"))
    try:
        snapshot = fetch_snapshot(repo["repository"], commit, token, workdir)
        environment = effective_environment(
            read_constraints(snapshot.archive),
            approved_editable=repo["editable_paths"],
            approved_setup=result["target"].get("setup_command"),
        )
        record = environment.record(
            repository=repo["repository"],
            commit=commit,
            readonly_paths=snapshot.readonly_paths,
            secret_names=secrets,
        )
        if environment.constraints is not None:
            record["roles"] = plan_roles(environment.constraints)
        _ensure_unchanged(repo.get(ENVIRONMENT_KEY), record)
        # The guest's workspace (Vercel or local Docker) unpacks this copy; the
        # parent's scorer box keeps the full tree, so it scores with the base
        # commit's metric (SKY-353).
        metric = environment.constraints.metric if environment.constraints is not None else MetricSpec()
        hidden = (*metric.files, metric.exploits)
        agent_archive = strip_archive(snapshot.archive, hidden, workdir / "agent-tree.tgz")
        leaks = check_hidden(
            snapshot.archive,
            agent_archive,
            hidden,
            place=sandbox_place(settings.local_mode),
            optional_paths=(metric.exploits,),
        )
        if leaks:
            raise RepoGateError(leaks)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    repo["commit"] = commit
    repo["editable_paths"] = list(environment.editable_paths)
    repo[ENVIRONMENT_KEY] = record
    result["target"]["setup_command"] = environment.setup_command
    if environment.constraints is not None:
        result["background"] = "\n\n".join(
            part for part in ((result.get("background") or "").strip(), instructions_brief(environment)) if part
        )
    repo["secrets"] = []
    result[REPO_SNAPSHOT_KEY] = {
        "archive": str(agent_archive),
        "commit": commit,
        "readonly_paths": list(snapshot.readonly_paths),
        "secret_names": sorted(secrets),
        "constrained": environment.constraints is not None,
    }
    return result, StagedRepository(
        snapshot=snapshot,
        secrets=secrets,
        workdir=workdir,
        protected_paths=environment.protected_paths,
        constrained=environment.constraints is not None,
        rules=environment.rules,
        intent=environment.constraints.intent if environment.constraints is not None else None,
        hidden_paths=hidden,
        targets=environment.constraints.targets if environment.constraints is not None else {},
        services=environment.constraints.services if environment.constraints is not None else {},
        service_roles={
            "metric": environment.constraints.metric_role.services,
            "run": environment.constraints.run_role.services,
        }
        if environment.constraints is not None
        else {},
        run_entry=environment.constraints.run_entry if environment.constraints is not None else None,
        run_timeout=environment.constraints.run_timeout if environment.constraints is not None else 60.0,
        agent_archive=agent_archive,
        exploits_path=metric.exploits,
    )


def _ensure_unchanged(previous: Any, current: dict[str, Any]) -> None:
    """Refuse to resume a run under rules other than the ones first recorded for it.

    Args:
        previous: The environment saved on the run by an earlier attempt, if any.
        current: The environment this attempt worked out.

    Raises:
        ConstraintsError: When an enforced rule differs from the saved one.
    """
    if not isinstance(previous, dict):
        return
    changed = [
        name
        for name in _ENFORCED_FIELDS
        if previous.get(name, _ENFORCED_DEFAULTS.get(name)) != current.get(name, _ENFORCED_DEFAULTS.get(name))
    ]
    if changed:
        raise ConstraintsError(
            f"The run's environment changed since it was recorded ({', '.join(changed)}); clone the run to review it."
        )


def _reply_text(body: bytes) -> str | None:
    """Return the text of a chat completion that finished on its own.

    Args:
        body: The provider's JSON response body.

    Returns:
        The reply text, or ``None`` when it is missing or the provider cut
        it off at the length limit.
    """
    choice = json.loads(body)["choices"][0]
    # A cut-off judge reply can end before its own answer, leaving only a
    # verdict it quoted from the diff to be read as the answer.
    if choice.get("finish_reason") == "length":
        return None
    content = choice["message"].get("content")
    return content if isinstance(content, str) else None


def _setup_asker(payload: dict[str, Any], gateway: Any) -> Callable[[str], str | None] | None:
    """Return a metered one-shot call to the run's optimization model, if it has one.

    Args:
        payload: Protected payload whose model roles are already registered.
        gateway: The run's model gateway.

    Returns:
        A function sending one prompt and returning the reply text, or
        ``None`` when the run has no optimization model to ask.
    """
    # An economy run's managed chat calls wait for a half-price batch, which
    # can take far longer than staging should.
    if payload.get("economy_mode"):
        return None
    route = next((route for route in gateway.model_routes() if route["role"] == "optimization"), None)
    if route is None or gateway.batches(route["token"]):
        return None

    def ask(prompt: str) -> str | None:
        """Send one prompt through the run's metered optimization route.

        Args:
            prompt: The question.

        Returns:
            The model's reply, or ``None`` when the provider refused it or
            cut it off at the length limit.
        """
        body = {
            "model": route["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": _SETUP_ANSWER_TOKENS,
        }
        response = gateway.dispatch_guest(route["token"], "/v1/chat/completions", body, {})
        if not 200 <= response.status < 300:
            return None
        return _reply_text(response.body)

    return ask


def _judge_limits(model: str) -> dict[str, Any]:
    """Return the length limit and reasoning control for one judge model's requests.

    Args:
        model: The judge's route model, which the gateway gives without the
            ``openrouter/`` prefix that catalog ids keep.

    Returns:
        ``max_tokens`` with room for reasoning, plus the lowest effort from
        ``low`` up when the model reasons; only the answer limit for a model
        the catalog says does not reason. A model missing from the catalog,
        or one whose reasoning data is unknown, gets the room without an effort.
    """
    snapshot = catalog_snapshot()
    slug = model.removeprefix("openrouter/")
    entry = (
        next((m for m in snapshot.models if m.value.removeprefix("openrouter/") == slug), None)
        if snapshot is not None
        else None
    )
    roomy = {"max_tokens": JUDGE_ANSWER_TOKENS + JUDGE_REASONING_TOKENS}
    # ``None`` reasoning data means the provider did not say, so the judge
    # may still reason; only a known non-reasoner gets the tight limit.
    if entry is None or entry.reasoning_efforts is None:
        return roomy
    if not (entry.reasoning_efforts or entry.reasoning_default_enabled or entry.reasoning_mandatory):
        return {"max_tokens": JUDGE_ANSWER_TOKENS}
    effort = lowest_effort_from_low(entry)
    return {**roomy, "reasoning": {"effort": effort}} if effort else roomy


def _route_asker(gateway: Any, route: dict[str, str]) -> Callable[[str], str | None]:
    """Return a metered one-shot chat call through one registered model route.

    Args:
        gateway: The run's model gateway.
        route: A route the parent registered.

    Returns:
        A function sending one prompt and returning the reply text, or
        ``None`` when the provider refused it.
    """
    limits = _judge_limits(route["model"])

    def ask(prompt: str) -> str | None:
        """Send one prompt through the route.

        Args:
            prompt: The question.

        Returns:
            The model's reply, or ``None`` when the provider refused it or
            cut it off at the length limit.
        """
        body = {"model": route["model"], "messages": [{"role": "user", "content": prompt}], **limits}
        response = gateway.dispatch_guest(route["token"], "/v1/chat/completions", body, {})
        if not 200 <= response.status < 300:
            return None
        return _reply_text(response.body)

    return ask


def _rule_watch_setting(payload: dict[str, Any]) -> tuple[bool, str, float]:
    """Read the run's watch hook setting, with the server defaults for what it leaves out.

    Args:
        payload: Protected run payload.

    Returns:
        Whether the hook is on, its model, and the confidence at which it warns.

    Raises:
        ConstraintsError: When the setting is not an object of the expected types.
    """
    # JSON null means "not set", for the whole setting and for the model. Any
    # other falsy value is a mistake, so it must not fall back to the defaults.
    setting = payload.get(RULE_WATCH_KEY)
    if setting is None:
        setting = {}
    if not isinstance(setting, dict):
        setting = {"enabled": None}
    enabled = setting.get("enabled", settings.rule_watch_enabled)
    model = setting.get("model")
    model_ok = model is None or (isinstance(model, str) and bool(model.strip()))
    if model is None:
        model = settings.rule_watch_model
    threshold = setting.get("threshold", settings.rule_watch_threshold)
    if (
        not isinstance(enabled, bool)
        or not model_ok
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= 1
    ):
        raise ConstraintsError(
            f"'{RULE_WATCH_KEY}' must be an object with a boolean 'enabled', a model name and a 'threshold' from 0 to 1."
        )
    return enabled, model, float(threshold)


def _rule_judge(
    payload: dict[str, Any], staged: StagedRepository, gateway: Any, *, owner_id: str, managed_key: str
) -> RuleJudge | None:
    """Build the constraints-file enforcement, with the watch hook when the run turned it on.

    The watch hook model runs on the managed key through a route only the
    parent holds, so its cost counts in the run's budget and the guest cannot
    call it. With the hook off, no model is asked.

    Args:
        payload: Protected run payload, which holds the watch hook setting.
        staged: The fetched tree and its constraints.
        gateway: The run's model gateway.
        owner_id: Job identity that owns the rule events.
        managed_key: The platform's OpenRouter key, or ``""`` when not configured.

    Returns:
        The enforcement, or ``None`` when the repository has no ``optimize.toml``.

    Raises:
        ConstraintsError: When the watch hook setting is invalid, or the hook
            is on but its model cannot be reached.
    """
    if not staged.constrained:
        return None
    enabled, model, threshold = _rule_watch_setting(payload)
    emit = run_log_emitter(owner_id)
    if not enabled or not (staged.rules or staged.intent):
        return RuleJudge(staged.rules, intent=staged.intent, ask=None, emit=emit)
    if not managed_key:
        raise ConstraintsError("The run turned the rule watch on, but its model is not configured on this server.")
    route = gateway.register(
        model=agent_model_id(model),
        api_key=managed_key,
        role=_JUDGE_ROLE,
        policy=ChargePolicy("managed_model"),
        parent_only=True,
    )
    judge = RuleJudge(
        staged.rules, intent=staged.intent, ask=_route_asker(gateway, route), emit=emit, threshold=threshold
    )
    gateway.observe_role("optimization", lambda body: judge.record_action(describe_model_call(body)))
    return judge


def infer_repo_setup(payload: dict[str, Any], staged: StagedRepository, gateway: Any) -> str | None:
    """Fill in the target's setup command from the fetched tree when the client sent none.

    Args:
        payload: Protected payload, updated in place.
        staged: The fetched tree.
        gateway: The run's model gateway, whose optimization model is asked.

    Returns:
        The inferred command, or ``None`` when the client named one or the
        repository needs no setup.
    """
    target = payload["target"]
    if (target.get("setup_command") or "").strip():
        return None
    command = infer_setup_command(staged.snapshot.archive, ask=_setup_asker(payload, gateway))
    target["setup_command"] = command
    return command


def bind_repo_scorer(
    payload: dict[str, Any], staged: StagedRepository, gateway: Any, *, owner_id: str, managed_key: str = ""
) -> None:
    """Score the run's versions in a parent-owned box and hand the guest only its route.

    The box is opened in-process through the gateway's parent-only runtime,
    never over the guest's control route, so it is billed and cleaned up like
    the guest's boxes while the guest never holds its secrets, its filesystem
    or a way to open a box with package-registry access. When the target has
    a setup command the box opens reaching only package registries, runs
    setup once, and goes offline before any version is laid out.

    Args:
        payload: Protected payload, updated in place with the evaluator route.
        staged: The fetched tree and decrypted secrets.
        gateway: The run's model gateway, already bound to its sandbox broker.
        owner_id: Job identity naming the box's operations.
        managed_key: The platform's OpenRouter key, which pays for the
            watch hook under the run's budget; ``""`` when not configured.

    Raises:
        ConstraintsError: When the run turned the watch hook on but it cannot run.
        ServiceError: When the scorer has a model but no scoped route from the trusted parent.
    """
    model_gateway = _repo_model_gateway(payload["scorer"])
    rule_judge = _rule_judge(payload, staged, gateway, owner_id=owner_id, managed_key=managed_key)
    descriptor = payload["_budget_gateway_descriptor"]
    target = payload["target"]
    scorer_spec = payload["scorer"]
    hosts = PACKAGE_REGISTRY_HOSTS if (target.get("setup_command") or "").strip() else ()
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=gateway.parent_sandbox_runtime(),
            spec=SandboxSpec(
                lifetime_seconds=descriptor["lifetime_seconds"],
                image=descriptor["image"],
                network_disabled=not hosts,
                allowed_hosts=hosts,
                operation_key=f"repo-scorer:{owner_id}",
            ),
            archive=staged.snapshot.archive,
            editable_paths=target["repo"]["editable_paths"],
            readonly_paths=staged.snapshot.readonly_paths,
            setup_command=target.get("setup_command"),
            secrets=staged.secrets,
            protected_paths=staged.protected_paths,
            hidden_paths=staged.hidden_paths,
        ),
        str(scorer_spec["metric_code"]),
        timeout_seconds=float(scorer_spec["timeout_seconds"]),
        rule_judge=rule_judge,
        gateway=model_gateway,
        stages=staged.targets,
        stage_runtime=gateway.parent_sandbox_runtime() if staged.targets else None,
        stage_archive=staged.agent_archive,
        hidden_paths=staged.hidden_paths,
        max_concurrency=int(payload.get("max_concurrency") or 1),
        services=staged.services,
        service_roles=staged.service_roles,
        run_entry=staged.run_entry,
        run_timeout=staged.run_timeout,
    )
    environment = target["repo"].get(ENVIRONMENT_KEY)
    if isinstance(environment, dict):
        environment["setup_command"] = target.get("setup_command")
        environment["network"] = {"setup": list(hosts), "versions": []}
    staged.scorers.append(scorer)
    # Setup and the network cutoff finish here, in the parent, before the
    # guest that writes versions exists; either failing fails the run.
    scorer.prepare()
    payload["_skynet_evaluator_route"] = gateway.bind_evaluator(scorer)


def _repo_model_gateway(scorer_spec: dict[str, Any]) -> ScorerGateway | None:
    """Resolve the scoped model route the repository box may call.

    Only the trusted parent's opaque route may reach the box. The other
    gateways ``scorer_gateway`` can return carry a provider key or need
    network the offline box does not have, so they are refused.

    Args:
        scorer_spec: The payload's scorer, after the gateway scoped its model.

    Returns:
        The route, or ``None`` when the scorer names no model.

    Raises:
        ServiceError: When the scorer has a model but no scoped route.
    """
    model = scorer_spec.get("model")
    if not isinstance(model, dict):
        return None
    if not isinstance((model.get("extra") or {}).get(ROUTE_KEY), dict):
        raise ServiceError("Repository scorers reach a model only through the trusted parent's scoped route.")
    return scorer_gateway(ModelConfig.model_validate(model), settings)


def _version_score(result: dict[str, Any], candidate: str) -> float | None:
    """Return the score the run ranked one version by.

    Args:
        result: Finished black-box result.
        candidate: The version's patch.

    Returns:
        Its recorded score, or ``None`` when the run never scored it or the
        metric crashed on it.
    """
    for version in result.get("versions") or []:
        if isinstance(version, dict) and version.get("candidate") == candidate:
            # SKY-350: a crash has no score, even if an older result stored a number.
            if is_metric_error(version.get("side_info") or {}):
                return None
            score = version.get("score")
            return float(score) if isinstance(score, int | float) else None
    return None


def _ranking_score(result: dict[str, Any], candidate: str) -> float:
    """Return a version's score for ranking, with unscored versions last.

    Args:
        result: Finished black-box result.
        candidate: The version's patch.

    Returns:
        The recorded score, or negative infinity when the run never scored it.
    """
    score = _version_score(result, candidate)
    return float("-inf") if score is None else score


def _version_metric_error(result: dict[str, Any], candidate: str) -> bool:
    """Return whether the metric or scorer crashed on one version during the run.

    Args:
        result: Finished black-box result.
        candidate: The version's patch.

    Returns:
        ``True`` when the version's recorded side info has the metric-error status.
    """
    return any(
        isinstance(version, dict)
        and version.get("candidate") == candidate
        and is_metric_error(version.get("side_info") or {})
        for version in result.get("versions") or []
    )


def _scored_candidate(candidate: Any, hidden_paths: tuple[str, ...]) -> Any:
    """Drop the hidden-path edits from one recorded candidate.

    Args:
        candidate: A patch, a mapping of patches, or anything else.
        hidden_paths: Metric files and the exploit folder.

    Returns:
        The candidate with every patch reduced to the scored change.
    """
    if isinstance(candidate, str):
        return drop_paths(candidate, hidden_paths)
    if isinstance(candidate, dict):
        return {key: _scored_candidate(value, hidden_paths) for key, value in candidate.items()}
    return candidate


def drop_hidden_edits(result: dict[str, Any], hidden_paths: tuple[str, ...]) -> None:
    """Replace every version a finished result records with the change the scorer scored.

    Serving, download and publishing all read these fields, so a version's
    edits to a metric file or the exploit folder never leave the run (SKY-353).

    Args:
        result: Finished black-box result, updated in place.
        hidden_paths: Metric files and the exploit folder.
    """
    if not hidden_paths:
        return
    for key in ("seed_candidate", "best_candidate"):
        if key in result:
            result[key] = _scored_candidate(result[key], hidden_paths)
    for key in ("versions", "candidate_tree", "lanes"):
        for entry in result.get(key) or []:
            if isinstance(entry, dict) and "candidate" in entry:
                entry["candidate"] = _scored_candidate(entry["candidate"], hidden_paths)


def improved_patch(result: dict[str, Any]) -> str | None:
    """Return the best version when it beat the starting code, else ``None``.

    The final run's fresh scores decide; a run stopped before its final run
    compares the versions' own scores instead.

    Args:
        result: Finished black-box result.

    Returns:
        The best version's patch, or ``None`` when it is empty, no better,
        or a version the metric crashed on.
    """
    best = result.get("best_candidate")
    if not isinstance(best, str) or not best.strip() or result.get("regression_guard_applied"):
        return None
    if _version_metric_error(result, best):
        return None
    baseline, optimized = result.get("baseline_score"), result.get("best_score")
    if baseline is None or optimized is None:
        baseline, optimized = _version_score(result, result.get("seed_candidate") or ""), _version_score(result, best)
    if baseline is None or optimized is None:
        return None
    return best if optimized > baseline else None


def _description(result: dict[str, Any], patch: str, run_url: str) -> str:
    """Write the pull request description.

    Args:
        result: Finished black-box result.
        patch: The published version.
        run_url: Link to the run in the web app.

    Returns:
        Markdown body.
    """
    baseline, optimized = result.get("baseline_score"), result.get("best_score")
    if baseline is None or optimized is None:
        baseline, optimized = _version_score(result, result.get("seed_candidate") or ""), _version_score(result, patch)
    files = sorted(set(patch_paths(patch)))
    listed = "\n".join(f"- `{path}`" for path in files[:50])
    more = f"\n- and {len(files) - 50} more" if len(files) > 50 else ""
    return (
        f"{APP_NAME} found this version while optimizing the repository with "
        f"{result.get('engine_used', 'an agent')}.\n\n"
        f"Score went from {baseline:.4g} to {optimized:.4g}.\n\n"
        f"Changed files:\n{listed}{more}\n\n"
        f"Review the run: {run_url}\n"
    )


def publish_improvement(
    result: dict[str, Any],
    payload: dict[str, Any],
    staged: StagedRepository,
    *,
    username: str,
    engine: Any,
    optimization_id: str,
    app_url: str,
) -> dict[str, Any] | None:
    """Open a draft pull request with the run's best version when it beat the start.

    A repository Skynet created for the run has no one else's code to review
    against, so it gets every version on its own branch instead (see
    :func:`publish_candidates`).

    Never raises: the run already finished, so a failure is recorded on the
    result for its owner to read instead.

    Args:
        result: Finished black-box result, updated in place.
        payload: The staged payload, which pins the commit.
        staged: The fetched tree, whose read-only paths still apply.
        username: Owner of the run and of the GitHub connection.
        engine: SQLAlchemy engine holding the connector vault.
        optimization_id: The run, named in the branch.
        app_url: Public origin of the web app.

    Returns:
        What was recorded under ``details.pull_request`` (or
        ``details.candidate_branches`` for a created repository), or ``None``
        when the run did not improve an existing repository.
    """
    drop_hidden_edits(result, staged.hidden_paths)
    if payload["target"]["repo"].get("created_by_skynet"):
        return publish_candidates(
            result, payload, staged, username=username, engine=engine, optimization_id=optimization_id
        )
    patch = improved_patch(result)
    if patch is None:
        return None
    repo = payload["target"]["repo"]
    outcome: dict[str, Any]
    # The patch came back from the guest, so the editing rules are checked
    # again before anything reaches the user's repository.
    problems = patch_violations(
        patch,
        repo["editable_paths"],
        staged.snapshot.readonly_paths,
        staged.protected_paths,
        exploit_paths=(staged.exploits_path,),
    )
    if problems:
        outcome = {"error": f"The best version breaks the editing rules: {problems[0]}"}
    else:
        # Users already have branches under this prefix in their repositories,
        # so publishing keeps it rather than starting a second naming scheme.
        branch = f"skynet/optimize-{optimization_id[:12]}"
        run_url = f"{app_url.rstrip('/')}/optimizations/{optimization_id}"
        suite, hardening = _hardening_suite(staged, engine, username, payload.get("hardening_job_id"), repo)
        try:
            token = github_token(engine, username)
            push_version(
                repo["repository"],
                repo["commit"],
                _joined(patch, suite),
                branch,
                f"Apply the best version from {APP_NAME} run {optimization_id[:12]}",
                token,
            )
            change = open_pull_request(
                token,
                repo["repository"],
                head=branch,
                base_branch=repo.get("branch"),
                title=f"{APP_NAME}: optimized version from run {optimization_id[:12]}",
                body=_description(result, patch, run_url),
                hardening=hardening,
            )
            outcome = {"url": change.url, "number": change.number, "branch": change.branch, "draft": change.draft}
        except (RepoFetchError, RepoPublishError) as error:
            outcome = {"error": str(error)}
        except Exception:  # isolation boundary: a finished run must keep its result
            logger.exception("Publishing the best version of %s failed", optimization_id)
            outcome = {"error": "The pull request could not be opened."}
    details = result.setdefault("details", {})
    details[PULL_REQUEST_DETAIL] = outcome
    return outcome


def _hardening_suite(
    staged: StagedRepository, engine: Any, username: str, job_id: Any, repo: dict[str, Any]
) -> tuple[str, HardeningSummary | None]:
    """Build the exploit-suite patch and PR counts of the run's finished hardening job.

    Never raises: a run without a usable job publishes as it did before
    hardening existed, with no suite and no counts.

    Args:
        staged: The fetched tree, whose base suite the patch builds on.
        engine: SQLAlchemy engine holding the hardening jobs.
        username: Owner of the run; only this user's job counts.
        job_id: The payload's ``hardening_job_id``, if any.
        repo: The payload's repository source.

    Returns:
        The suite patch (empty when nothing is new) and the counts, or
        ``("", None)`` when the run has no finished job of its own.
    """
    if not job_id:
        return "", None
    try:
        job = _finished_hardening_job(engine, username, str(job_id), repo["repository"])
        if job is None:
            return "", None
        fixed = [
            e for r in job.rounds_detail if r.status == "fixed" for e in r.exploits if e.user_verdict == "confirmed"
        ]
        if any(not _EXPLOIT_ID.fullmatch(e.id) for e in fixed):
            logger.warning("Hardening job %s has an exploit id that is not a safe folder name.", job_id)
            return "", None
        files = {
            f"{staged.exploits_path}/{e.id}/{name}": text
            for e in fixed
            for name, text in (("patch.diff", e.patch_diff), ("why.md", e.why_md))
        }
        return _suite_patch(Path(staged.snapshot.archive), files, staged.workdir), _hardening_summary(job)
    except Exception as error:  # isolation boundary: the improvement still publishes without the suite
        logger.warning("The exploit suite of hardening job %s was left out: %s.", job_id, type(error).__name__)
        return "", None


def _finished_hardening_job(engine: Any, username: str, job_id: str, repository: str) -> HardeningJob | None:
    """Load the user's finished hardening job, if it may ship with this repository's PR.

    Args:
        engine: SQLAlchemy engine holding the hardening jobs.
        username: Owner of the run.
        job_id: The job to load.
        repository: The run's ``owner/name`` repository.

    Returns:
        The job, or ``None`` (with the reason logged) when it is missing, of
        another user, not finished, names no repository, or names another one.
    """
    with Session(engine) as session:
        row = session.scalars(
            select(HardeningJobModel).where(HardeningJobModel.id == job_id, HardeningJobModel.username == username)
        ).one_or_none()
        if row is None:
            # A job of another user reads as missing, so the log does not
            # tell which jobs exist.
            logger.info("Hardening job %s was not found for the run's owner; no suite ships.", job_id)
            return None
        if row.status not in _FINISHED_HARDENING:
            logger.info("Hardening job %s is %s, not finished; no suite ships.", job_id, row.status)
            return None
        target = row.job.get("target")
        source = target.get("repo") if isinstance(target, dict) else None
        named = source.get("repository") if isinstance(source, dict) else None
        # Fail closed: a job that names no repository could come from another
        # repository, and its exploits must never ship in this repository's PR.
        if not isinstance(named, str):
            logger.info("Hardening job %s names no repository; no suite ships.", job_id)
            return None
        if named.casefold() != repository.casefold():
            logger.info("Hardening job %s hardened another repository; no suite ships.", job_id)
            return None
        return HardeningJob.model_validate(row.job)


def _hardening_summary(job: HardeningJob) -> HardeningSummary:
    """Count the attacks the metric survived and the exploits its fixes closed.

    The count matches the setup page's "survived" number: every attack the
    metric held against, plus each exploit a fixed round closed.

    Args:
        job: A finished hardening job.

    Returns:
        The PR body counts.
    """
    attacks = fixed = 0
    for hardening_round in job.rounds_detail:
        for exploit in hardening_round.exploits:
            closed = exploit.user_verdict == "confirmed" and hardening_round.status == "fixed"
            held = exploit.user_verdict == "rejected" or (exploit.label == "valid" and exploit.user_verdict is None)
            attacks += closed or held
            fixed += closed
    return HardeningSummary(attacks=attacks, exploits_fixed=fixed)


def _suite_patch(archive: Path, files: dict[str, str], workdir: Path) -> str:
    """Diff the job's suite files against the base commit's versions of them.

    A base commit may already hold part of the suite (a merged earlier PR), so
    the patch is taken against those files and stays empty for unchanged ones.

    Args:
        archive: The full base-commit tree.
        files: Suite path to its text, as the job wrote it.
        workdir: The run's scratch folder.

    Returns:
        A ``git diff --binary`` patch, empty when nothing is new.
    """
    if not files:
        return ""
    root = Path(tempfile.mkdtemp(prefix="suite-", dir=workdir))
    try:
        with tarfile.open(archive) as tree:
            for member in tree:
                path = posixpath.normpath(member.name).lstrip("/")
                if path in files and member.isfile() and (data := tree.extractfile(member)) is not None:
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    (root / path).write_bytes(data.read())
        _git(root, "init", "--quiet")
        _git(root, "add", "--all")
        base = _git(root, "write-tree").strip()
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text, encoding="utf-8")
        _git(root, "add", "--all")
        return _git(root, "diff", "--cached", "--binary", base)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _git(cwd: Path, *arguments: str) -> str:
    """Run git in a scratch repository with no user or system config.

    Args:
        cwd: Repository root.
        *arguments: Arguments after ``git``.

    Returns:
        Standard output.
    """
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd)},
    ).stdout


def _joined(patch: str, suite: str) -> str:
    """Append the suite patch to the improvement patch.

    Args:
        patch: The improvement; it never touches the suite (``patch_violations``).
        suite: The suite patch, possibly empty.

    Returns:
        One patch that ``git apply`` takes in one pass.
    """
    if not suite:
        return patch
    return patch + ("" if patch.endswith("\n") else "\n") + suite


def _candidate_versions(
    result: dict[str, Any], repo: dict[str, Any], staged: StagedRepository, run: str
) -> tuple[list[tuple[str, str]], int]:
    """Number the run's versions that may reach the repository.

    Args:
        result: Finished black-box result.
        repo: The payload's repository source.
        staged: The fetched tree, whose read-only paths still apply.
        run: Short run id that starts every version id.

    Returns:
        ``(id, patch)`` pairs in first-seen order, ids ``<run>-1`` upward, and
        how many versions were left out.
    """
    versions: list[tuple[str, str]] = []
    seen: set[str] = set()
    skipped = 0
    for index, version in enumerate(result.get("versions") or [], start=1):
        patch = version.get("candidate") if isinstance(version, dict) else None
        if not isinstance(patch, str) or patch in seen:
            continue
        seen.add(patch)
        # The patches came back from the guest, so the editing rules are
        # checked again before any of them reaches the repository.
        if _version_metric_error(result, patch) or patch_violations(
            patch,
            repo["editable_paths"],
            staged.snapshot.readonly_paths,
            staged.protected_paths,
            exploit_paths=(staged.exploits_path,),
        ):
            skipped += 1
            continue
        versions.append((f"{run}-{index}", patch))
    return versions, skipped


def publish_candidates(
    result: dict[str, Any],
    payload: dict[str, Any],
    staged: StagedRepository,
    *,
    username: str,
    engine: Any,
    optimization_id: str,
) -> dict[str, Any]:
    """Push the top versions of a run on a created repository to their own branches.

    At most ``MAX_CANDIDATE_BRANCHES`` versions get a branch, the best one
    always among them. ``skynet/best`` moves to the improved version, or to the starting code
    when nothing beat it. Never raises: a failure is recorded on the result.

    Args:
        result: Finished black-box result, updated in place.
        payload: The staged payload, which pins the commit.
        staged: The fetched tree, whose read-only paths still apply.
        username: Owner of the run and of the GitHub connection.
        engine: SQLAlchemy engine holding the connector vault.
        optimization_id: The run, named in every version id.

    Returns:
        What was recorded under ``details.candidate_branches``.
    """
    repo = payload["target"]["repo"]
    # A later run on the same repository (a retry, clone or scheduled cycle)
    # makes different commits for the same version number. Branch names carry
    # the run id so those pushes never need to rewrite an earlier run's branch.
    run = optimization_id[:12]
    versions, skipped = _candidate_versions(result, repo, staged, run)
    best_patch = improved_patch(result) or ""
    best_id = next((version_id for version_id, patch in versions if patch == best_patch), None)
    left_out = 0
    if len(versions) > MAX_CANDIDATE_BRANCHES:
        ranked = sorted(
            versions,
            key=lambda pair: (pair[0] == best_id, _ranking_score(result, pair[1])),
            reverse=True,
        )
        kept = {version_id for version_id, _ in ranked[:MAX_CANDIDATE_BRANCHES]}
        left_out = len(versions) - len(kept)
        versions = [pair for pair in versions if pair[0] in kept]
    if best_id is None:
        best_id = f"{run}-start"
        versions.append((best_id, ""))
    outcome: dict[str, Any]
    try:
        pushed = push_candidates(repo["repository"], repo["commit"], versions, best_id, github_token(engine, username))
        outcome = {
            "repository": repo["repository"],
            "best_branch": BEST_BRANCH,
            "best_version": best_id,
            "branches": pushed,
            "skipped": skipped,
            "left_out": left_out,
        }
    except (RepoFetchError, RepoPublishError) as error:
        outcome = {"error": str(error)}
    except Exception:  # isolation boundary: a finished run must keep its result
        logger.exception("Pushing the candidate branches of %s failed", repo["repository"])
        outcome = {"error": "The candidate branches could not be pushed."}
    result.setdefault("details", {})[CANDIDATE_BRANCHES_DETAIL] = outcome
    return outcome

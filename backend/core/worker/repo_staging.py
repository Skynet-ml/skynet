"""Stage a repository target's tree and secrets in the trusted parent before its sandbox opens."""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..api.model_catalog import agent_model_id, catalog_snapshot, lowest_effort_from_low, strongest_model_id
from ..billing.operation_pricing import ChargePolicy
from ..billing.protected_credentials import ProtectedCredentialVault, resolve_repo_secrets
from ..billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from ..brand import APP_NAME
from ..config import settings
from ..connectors.github_publish import RepoPublishError, open_pull_request, push_version
from ..connectors.github_repo import RepoFetchError, RepoSnapshot, fetch_snapshot, github_token, resolve_commit
from ..connectors.saved_secrets import SavedSecretStore
from ..service_gateway.optimization.blackbox.repo_constraints import (
    ConstraintsError,
    effective_environment,
    instructions_brief,
    read_constraints,
)
from ..service_gateway.optimization.blackbox.repo_scorer import RepoScorer
from ..service_gateway.optimization.blackbox.repo_setup import infer_setup_command
from ..service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY, patch_paths, patch_violations
from ..service_gateway.optimization.blackbox.repo_workspace import RepoWorkspace
from ..service_gateway.optimization.blackbox.rule_judge import (
    ILLEGAL_APPROACH_KEY,
    JUDGE_ANSWER_TOKENS,
    JUDGE_REASONING_TOKENS,
    RuleJudge,
    describe_model_call,
    run_log_emitter,
)
from ..service_gateway.optimization.blackbox.sandbox import SandboxSpec

logger = logging.getLogger(__name__)

PULL_REQUEST_DETAIL = "pull_request"
ENVIRONMENT_KEY = "environment"
# Rules the run is held to; a resumed run must arrive at exactly these again.
_ENFORCED_FIELDS = ("commit", "editable_paths", "protected_paths", "readonly_paths", "services", "rules")
# Environments recorded before a field existed hold its empty value.
_ENFORCED_DEFAULTS: dict[str, Any] = {"rules": []}
_SETUP_ANSWER_TOKENS = 1_024
_JUDGE_ROLE = "judge"


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
        _ensure_unchanged(repo.get(ENVIRONMENT_KEY), record)
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
        "archive": str(snapshot.archive),
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


def _rule_judge(staged: StagedRepository, gateway: Any, *, owner_id: str, managed_key: str) -> RuleJudge | None:
    """Build the judge that enforces the repository's constraints file, when it has one.

    Both judge models run on the managed key through routes only the parent
    holds, so their cost counts in the run's budget and the guest cannot call them.

    Args:
        staged: The fetched tree and its constraints.
        gateway: The run's model gateway.
        owner_id: Job identity that owns the rule events.
        managed_key: The platform's OpenRouter key, or ``""`` when not configured.

    Returns:
        The judge, or ``None`` when the repository has no ``optimize.toml``.

    Raises:
        ConstraintsError: When the file has plain-language rules but no judge model can be reached.
    """
    if not staged.constrained:
        return None
    if not staged.rules:
        return RuleJudge(
            (), ask_fast=lambda _prompt: None, ask_strong=lambda _prompt: None, emit=run_log_emitter(owner_id)
        )
    if not managed_key:
        raise ConstraintsError(
            "optimize.toml has [rules], but the judge models that check them are not configured on this server."
        )
    policy = ChargePolicy("managed_model")
    fast = gateway.register(
        model=agent_model_id(settings.rule_watch_model),
        api_key=managed_key,
        role=_JUDGE_ROLE,
        policy=policy,
        parent_only=True,
    )
    strong = gateway.register(
        model=strongest_model_id(settings.rule_judge_model),
        api_key=managed_key,
        role=_JUDGE_ROLE,
        policy=policy,
        parent_only=True,
    )
    judge = RuleJudge(
        staged.rules,
        ask_fast=_route_asker(gateway, fast),
        ask_strong=_route_asker(gateway, strong),
        emit=run_log_emitter(owner_id),
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
        managed_key: The platform's OpenRouter key, which pays for the rule
            judges under the run's budget; ``""`` when not configured.

    Raises:
        ConstraintsError: When the repository's plain-language rules cannot be judged.
    """
    rule_judge = _rule_judge(staged, gateway, owner_id=owner_id, managed_key=managed_key)
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
        ),
        str(scorer_spec["metric_code"]),
        timeout_seconds=float(scorer_spec["timeout_seconds"]),
        rule_judge=rule_judge,
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


def _version_score(result: dict[str, Any], candidate: str) -> float | None:
    """Return the score the run ranked one version by.

    Args:
        result: Finished black-box result.
        candidate: The version's patch.

    Returns:
        Its recorded score, or ``None`` when the run never scored it.
    """
    for version in result.get("versions") or []:
        if isinstance(version, dict) and version.get("candidate") == candidate:
            score = version.get("score")
            return float(score) if isinstance(score, int | float) else None
    return None


def _version_illegal(result: dict[str, Any], candidate: str) -> bool:
    """Return whether the rule judge rejected one version during the run.

    Args:
        result: Finished black-box result.
        candidate: The version's patch.

    Returns:
        ``True`` when the version's recorded side info carries the judge's rejection.
    """
    return any(
        isinstance(version, dict)
        and version.get("candidate") == candidate
        and ILLEGAL_APPROACH_KEY in (version.get("side_info") or {})
        for version in result.get("versions") or []
    )


def improved_patch(result: dict[str, Any]) -> str | None:
    """Return the best version when it beat the starting code, else ``None``.

    The final run's fresh scores decide; a run stopped before its final run
    compares the versions' own scores instead.

    Args:
        result: Finished black-box result.

    Returns:
        The best version's patch, or ``None`` when it is empty, no better, or
        an approach the rule judge rejected.
    """
    best = result.get("best_candidate")
    if not isinstance(best, str) or not best.strip() or result.get("regression_guard_applied"):
        return None
    if _version_illegal(result, best):
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
        What was recorded under ``details.pull_request``, or ``None`` when the
        run did not improve.
    """
    patch = improved_patch(result)
    if patch is None:
        return None
    repo = payload["target"]["repo"]
    outcome: dict[str, Any]
    # The patch came back from the guest, so the editing rules are checked
    # again before anything reaches the user's repository.
    problems = patch_violations(patch, repo["editable_paths"], staged.snapshot.readonly_paths, staged.protected_paths)
    if problems:
        outcome = {"error": f"The best version breaks the editing rules: {problems[0]}"}
    else:
        # Users already have branches under this prefix in their repositories,
        # so publishing keeps it rather than starting a second naming scheme.
        branch = f"skynet/optimize-{optimization_id[:12]}"
        run_url = f"{app_url.rstrip('/')}/optimizations/{optimization_id}"
        try:
            token = github_token(engine, username)
            push_version(
                repo["repository"],
                repo["commit"],
                patch,
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

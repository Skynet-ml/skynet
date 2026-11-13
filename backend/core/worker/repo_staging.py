"""Stage a repository target's tree and secrets in the trusted parent before its sandbox opens."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
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

from .. import run_log
from ..api.model_catalog import agent_model_id, catalog_snapshot, lowest_effort_from_low
from ..api.routers.package_registry import package_registry_for
from ..billing.host_agent import HOST_AGENT_TOKEN_KEY, _proposer_model_extra
from ..billing.model_gateway import EMBEDDING_ROUTE_KEY, MCP_RELAY_ROUTE_KEY, ROUTE_KEY
from ..billing.operation_pricing import ChargePolicy
from ..billing.protected_credentials import (
    ProtectedCredentialVault,
    repo_secret_allowed_hosts,
    resolve_repo_secrets,
)
from ..billing.vercel_usage import PACKAGE_REGISTRY_HOSTS, proposer_hosts
from ..brand import APP_NAME
from ..config import settings
from ..connectors import repo_hosts
from ..connectors.github_publish import (
    BEST_BRANCH,
    HardeningSummary,
    RepoPublishError,
    open_pull_request,
    push_candidates,
    push_version,
)
from ..connectors.github_repo import (
    RepoFetchError,
    RepoRateLimitError,
    RepoSnapshot,
    fetch_snapshot,
    github_token,
    resolve_commit,
)
from ..connectors.repo_drafts import DraftFileError, apply_to_archive
from ..connectors.saved_secrets import SavedSecretStore
from ..connectors.transport import label as host_label
from ..exceptions import ServiceError
from ..models.common import ModelConfig
from ..models.hardening import HardeningJob, HardeningJobModel
from ..sandbox.services import ComposeService
from ..service_gateway.optimization.blackbox import runner, sandbox_log
from ..service_gateway.optimization.blackbox.harness_bridge import hidden_hashes
from ..service_gateway.optimization.blackbox.protocol import is_metric_error
from ..service_gateway.optimization.blackbox.repo_constraints import (
    CONSTRAINTS_FILE,
    ConstraintsError,
    MetricSpec,
    RepoConstraints,
    RoleEnvironment,
    ServiceSpec,
    TargetStage,
    effective_environment,
    instructions_brief,
    read_constraints,
    scorer_only_paths,
)
from ..service_gateway.optimization.blackbox.repo_scorer import RepoScorer
from ..service_gateway.optimization.blackbox.repo_services import box_mcp_config, plan_roles
from ..service_gateway.optimization.blackbox.repo_setup import infer_setup_command
from ..service_gateway.optimization.blackbox.repo_tree import (
    EXPLOITS_DIR,
    REPO_SNAPSHOT_KEY,
    drop_paths,
    inside,
    linked_hidden_paths,
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
from ..service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec
from ..service_gateway.optimization.blackbox.sandbox_scorer import ScorerGateway, scorer_gateway
from ..service_gateway.optimization.blackbox.service import CASE_SPLIT_KEY
from .repo_gate import RepoGateError, check_draft_base, check_hidden, metric_import_warnings, sandbox_place
from .scoped_relay import McpRelay

logger = logging.getLogger(__name__)

PULL_REQUEST_DETAIL = "pull_request"
CANDIDATE_BRANCHES_DETAIL = "candidate_branches"
# A long run can record hundreds of versions; only the top scorers get a branch,
# so a created repository does not fill with branches nobody reads.
MAX_CANDIDATE_BRANCHES = 10
ENVIRONMENT_KEY = "environment"
UPLOADED_CASES_REFUSED = "This repo defines its cases in optimize.toml. Remove the uploaded cases."
# Rules the run is held to; a resumed run must arrive at exactly these again.
_ENFORCED_FIELDS = ("commit", "editable_paths", "protected_paths", "readonly_paths", "services", "rules", "outcome")
# Environments recorded before a field existed hold its empty value.
_ENFORCED_DEFAULTS: dict[str, Any] = {"rules": [], "outcome": {"target": None, "require": {}}}
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
    # [metric] for the scorer. Each runs in its own box with its own relay (E37, SKY-398).
    service_roles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Each role with the servers of its own MCP file, ``target.<name>`` for each
    # stage: the relay checks each MCP request against the calling role only
    # (E49, E55, SKY-611).
    mcp_roles: Mapping[str, RoleEnvironment] = field(default_factory=dict)
    # The proposer, whose box reaches the servers of its own MCP file and the
    # services it lists only through the parent's relay, behind its own token
    # (SKY-610, SKY-612).
    proposer_role: RoleEnvironment | None = None
    # The [run] entry, which runs in the candidate box; the metric files are only in the metric box (E38, SKY-398).
    run_entry: str | tuple[str, ...] | None = None
    run_timeout: float = 60.0
    # Each role's own box: [run] sizes and walls off the candidate box, [metric]
    # the metric box. No hosts keeps the box offline; None keeps the default size.
    run_hosts: tuple[str, ...] = ()
    run_vcpus: int | None = None
    metric_hosts: tuple[str, ...] = ()
    metric_vcpus: int | None = None
    # Each repository module a metric file imports; the metric box cannot import it (SKY-398).
    metric_warnings: tuple[str, ...] = ()
    agent_archive: Path | None = None
    # The scorer box's tree: the full tree with every role's MCP file replaced
    # by loopback urls, so the box never holds a real upstream url (E49, E53).
    scorer_archive: Path | None = None
    exploits_path: str = EXPLOITS_DIR
    # The relay fills a service's ${NAME} headers only for the secret's own
    # host and the run's allowed hosts, and meters a Skynet key (E49-E53).
    secret_bindings: Mapping[str, str] = field(default_factory=dict)
    secret_hosts: Mapping[str, str] = field(default_factory=dict)
    secret_allowed_hosts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # The [sandbox] compose services start only next to the scorer box, read
    # from the base commit, so a version cannot change what scores it.
    compose_services: tuple[ComposeService, ...] = ()
    # The setup draft's files, kept in the parent only: the guest gets them
    # inside the packed tree, never as a list, as a draft can be a hidden path.
    drafts: Mapping[str, str] = field(default_factory=dict)
    # Whether staging refused an optimize.toml with no version. Every later read
    # of this tree uses the same rule, so one pass never reads the file two ways.
    require_version: bool = True
    # The run record's hashes and metric home, taken from the staged tree the
    # run reads, so the run end never reads the repository again (SKY-694).
    record_facts: Mapping[str, Any] = field(default_factory=dict)

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


def saved_index_url(engine: Any, username: str) -> str:
    """Read the package index the account saved, or the PyPI default.

    Args:
        engine: SQLAlchemy engine for the account settings.
        username: Owner of the run.

    Returns:
        The index URL the proposer's box may install from.
    """
    with Session(engine) as session:
        return package_registry_for(session, username).index_url


def hidden_repo_paths(constraints: RepoConstraints | None) -> tuple[str, ...]:
    """Return every repository path that only the parent's scorer may read (E61, E62).

    The optimization agent's tree, the candidate's box, every tool and every
    published version leave these out. A path that a visible role also names,
    such as one ``.mcp.json`` for ``[run]`` and ``[metric]``, stays visible,
    because hiding it would break that role.

    Args:
        constraints: The repository's validated constraints, or ``None`` without a file.

    Returns:
        The paths from ``scorer_only_paths``, or only the default exploit
        folder when the repository has no constraints file.
    """
    if constraints is None:
        return (MetricSpec().exploits,)
    return scorer_only_paths(constraints)


def hidden_path_labels(constraints: RepoConstraints | None) -> dict[str, str]:
    """Name the ``optimize.toml`` setting that hides each scoring-side path (SKY-446).

    A leak report then names the setting a user must fix, not always
    ``[metric] files``. A path that two settings name keeps the first label.

    Args:
        constraints: The repository's validated constraints, or ``None`` without a file.

    Returns:
        Each hidden path from :func:`hidden_repo_paths`, mapped to its setting.
    """
    if constraints is None:
        return {MetricSpec().exploits: "[metric] exploits"}
    metric = constraints.metric
    sources = [
        (CONSTRAINTS_FILE, CONSTRAINTS_FILE),
        *((path, "[metric] files") for path in metric.files),
        (metric.exploits, "[metric] exploits"),
        (metric.reference, "[metric] reference"),
        (constraints.cases_file, "[data] cases"),
        (constraints.metric_role.illegal_file, "[metric] illegal"),
        (constraints.metric_role.mcp, "[metric] mcp"),
    ]
    labels: dict[str, str] = {}
    for path, label in sources:
        if path is not None:
            labels.setdefault(path, label)
    hidden = hidden_repo_paths(constraints)
    return {path: label for path, label in labels.items() if path in hidden}


def _owner_token(engine: Any, username: str, provider: str) -> str | None:
    """Return the owner's token for a repository host, or ``None`` without a usable connection.

    A public repository needs no connection (SKY-568), so a missing or expired
    one falls back to an anonymous read instead of stopping the run.

    Args:
        engine: SQLAlchemy engine holding the connector vault.
        username: Owner of the run and of the connection.
        provider: The repository host.

    Returns:
        The token, or ``None``.
    """
    try:
        if provider == repo_hosts.DEFAULT_PROVIDER:
            return github_token(engine, username)
        return repo_hosts.host_token(engine, username, provider)
    except RepoFetchError:
        return None


def _anonymous_fetch_error(error: RepoFetchError, provider: str) -> RepoFetchError:
    """Explain that an anonymous read reaches public repositories only.

    Args:
        error: Why the anonymous lookup or clone failed.
        provider: The repository host.

    Returns:
        The error with the advice to connect the host.
    """
    host = host_label(provider)
    return RepoFetchError(
        f"{error} Without a {host} connection, only a public repository can be read."
        f" Connect {host} to optimize a private repository."
    )


def stage_repository(
    payload: dict[str, Any],
    *,
    username: str,
    binding_id: str,
    engine: Any,
    started: bool = False,
) -> tuple[dict[str, Any], StagedRepository]:
    """Fetch the pinned tree and decrypt the run's secrets in the parent.

    A run with a recorded environment, or a ``started`` job, already passed
    its first staging, maybe before ``version`` was required (SKY-690). Its
    commit's ``optimize.toml`` then reads a missing ``version`` as 1.

    Args:
        payload: Parent payload whose secrets are still vault references.
        username: Owner of the run and of the GitHub connection.
        binding_id: Execution budget the vaulted secrets are bound to.
        engine: SQLAlchemy engine for the vaults.
        started: Whether the caller's job already started; a hardening job
            records no environment.

    Returns:
        A payload copy that pins the commit, lists no secret entries and no
        draft files or draft base (the staged tree keeps the files), names
        the packed tree and carries the effective environment under
        ``target.repo.environment``, plus the staged tree and secrets for the parent.

    Raises:
        ConstraintsError: When the repository's ``optimize.toml`` is invalid,
            asks for more than the run approved, or a resumed run's rules
            changed since they were first recorded, or when the request
            uploads cases and the repository defines its own in ``[data] cases``,
            or when a hidden path is, holds or lies inside a link, or when a
            draft file cannot be written over the tree.
        RepoGateError: When the agent's tree still shows a hidden path from ``hidden_repo_paths``,
            or when the draft files were written on another commit than the one the run starts from.
        RepoFetchError: When the repository cannot be read; without a host
            connection, the message says to connect the host.
    """
    result = copy.deepcopy(payload)
    repo = result["target"]["repo"]
    require_version = not started and not isinstance(repo.get(ENVIRONMENT_KEY), dict)
    # The guest receives this payload, and a draft (often optimize.toml) can be
    # a hidden scoring path, so only the parent keeps the drafts.
    drafts = dict(repo.pop("draft_files", None) or {})
    draft_base = repo.pop("draft_base", None)
    # Resolving the secrets empties their list, so read the relay's inputs first.
    secret_bindings = dict(repo.get("secret_bindings") or {})
    secret_allowed_hosts = repo_secret_allowed_hosts(payload)
    secrets = resolve_repo_secrets(
        result,
        username=username,
        binding_id=binding_id,
        vault=ProtectedCredentialVault(engine=engine),
        saved=SavedSecretStore(engine),
    )
    provider = repo_hosts.provider_of(repo)
    token = _owner_token(engine, username, provider)
    try:
        if provider == repo_hosts.DEFAULT_PROVIDER:
            commit = repo.get("commit") or resolve_commit(token, repo["repository"], repo.get("branch"))
        else:
            commit = repo.get("commit") or repo_hosts.resolve_commit(
                provider, token, repo["repository"], repo.get("branch")
            )
    except RepoFetchError as error:
        # A spent rate limit says nothing about whether the repository is public.
        if token is not None or isinstance(error, RepoRateLimitError):
            raise
        raise _anonymous_fetch_error(error, provider) from error
    if moved := check_draft_base(bool(drafts), draft_base, commit, place=sandbox_place(settings.local_mode)):
        raise RepoGateError(moved)
    workdir = Path(tempfile.mkdtemp(prefix="app-repo-"))
    try:
        try:
            snapshot = fetch_snapshot(repo["repository"], commit, token, workdir, **repo_hosts.git_kwargs(provider))
        except RepoFetchError as error:
            if token is not None:
                raise
            raise _anonymous_fetch_error(error, provider) from error
        # The setup draft's files replace the commit's, so every box and the
        # draft's optimize.toml see the tree the user set up.
        try:
            apply_to_archive(snapshot.archive, drafts, snapshot.readonly_paths)
        except DraftFileError as error:
            raise ConstraintsError(str(error)) from None
        environment = effective_environment(
            read_constraints(snapshot.archive, require_version=require_version),
            approved_editable=repo["editable_paths"],
            approved_setup=result["target"].get("setup_command"),
        )
        record = environment.record(
            repository=repo["repository"],
            commit=commit,
            readonly_paths=snapshot.readonly_paths,
            secret_names=secrets,
        )
        hosts = proposer_hosts(
            environment.constraints.proposer.network if environment.constraints is not None else None,
            saved_index_url(engine, username),
        )
        if environment.constraints is not None:
            record["roles"] = plan_roles(environment.constraints, proposer_hosts=hosts)
        _ensure_unchanged(repo.get(ENVIRONMENT_KEY), record)
        # The guest's workspace (Vercel or local Docker) unpacks this copy; the
        # parent's scorer box keeps the full tree, so it scores with the base
        # commit's metric (SKY-353).
        metric = environment.constraints.metric if environment.constraints is not None else MetricSpec()
        cases_file = environment.constraints.cases_file if environment.constraints is not None else None
        # Two sources would leave it unclear which cases the run scores on (E58).
        if cases_file is not None and result.get("cases"):
            raise ConstraintsError(UPLOADED_CASES_REFUSED)
        hidden = hidden_repo_paths(environment.constraints)
        if linked := linked_hidden_paths(snapshot.archive, hidden):
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: the hidden scoring path '{linked[0]}' is, holds or lies inside a link."
                " Make the [metric] and [data] cases paths real files and folders, with no symlinks or hard links."
            )
        agent_archive = strip_archive(snapshot.archive, hidden, workdir / "agent-stripped.tgz")
        if environment.constraints is not None:
            # Every role's MCP file gets the same relay urls as in the scorer's
            # tree, so the protected files match and never enter a patch (SKY-610).
            agent_archive = relay_mcp_archive(
                agent_archive, tuple(environment.constraints.mcp_roles.values()), workdir / "agent-tree.tgz"
            )
        # Nothing checks that [metric] reference names a file, so a missing one is not a leak.
        leaks = check_hidden(
            snapshot.archive,
            agent_archive,
            hidden,
            place=sandbox_place(settings.local_mode),
            optional_paths=tuple(path for path in (metric.exploits, metric.reference) if path is not None),
            labels=hidden_path_labels(environment.constraints),
        )
        if leaks:
            raise RepoGateError(leaks)
        warnings = tuple(metric_import_warnings(snapshot.archive, metric.files))
        record_facts = (
            _record_facts(
                snapshot.archive,
                contract_sha256=environment.constraints.sha256,
                metric_files=metric.files,
                metric_code=str((result.get("scorer") or {}).get("metric_code") or ""),
                drafts=drafts,
                cases_file=cases_file,
                cases=result.get("cases"),
            )
            if environment.constraints is not None
            else {}
        )
        scorer_archive = (
            relay_mcp_archive(
                snapshot.archive,
                tuple(environment.constraints.mcp_roles.values()),
                workdir / "scorer-tree.tgz",
            )
            if environment.constraints is not None
            else None
        )
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    repo["commit"] = commit
    repo["editable_paths"] = list(environment.editable_paths)
    repo[ENVIRONMENT_KEY] = record
    result["target"]["setup_command"] = environment.setup_command
    if environment.constraints is not None:
        result["background"] = "\n\n".join(
            part for part in ((result.get("background") or "").strip(), instructions_brief(environment, hidden)) if part
        )
    repo["secrets"] = []
    if cases_file is not None:
        result["cases"] = list(environment.constraints.cases)
    result[REPO_SNAPSHOT_KEY] = {
        "archive": str(agent_archive),
        "commit": commit,
        "readonly_paths": list(snapshot.readonly_paths),
        "secret_names": sorted(secrets),
        "constrained": environment.constraints is not None,
        "proposer_hosts": list(hosts),
        # Hashes only: the guest's tool guard needs them, and the agent must never read a hidden name (E61).
        "hidden_hashes": hidden_hashes(hidden),
        "outcome": record["outcome"],
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
        mcp_roles={
            "metric": environment.constraints.metric_role,
            "run": environment.constraints.run_role,
            **{f"target.{name}": stage for name, stage in environment.constraints.targets.items()},
        }
        if environment.constraints is not None
        else {},
        proposer_role=environment.constraints.proposer if environment.constraints is not None else None,
        run_entry=environment.constraints.run_entry if environment.constraints is not None else None,
        run_timeout=environment.constraints.run_timeout if environment.constraints is not None else 60.0,
        run_hosts=_role_hosts(environment.constraints.run_role if environment.constraints is not None else None),
        run_vcpus=environment.constraints.run_vcpus if environment.constraints is not None else None,
        metric_hosts=_role_hosts(environment.constraints.metric_role if environment.constraints is not None else None),
        metric_vcpus=environment.constraints.metric_vcpus if environment.constraints is not None else None,
        metric_warnings=warnings,
        agent_archive=agent_archive,
        scorer_archive=scorer_archive,
        compose_services=environment.constraints.compose_services if environment.constraints is not None else (),
        exploits_path=metric.exploits,
        secret_bindings=secret_bindings,
        secret_hosts=environment.constraints.secret_hosts if environment.constraints is not None else {},
        secret_allowed_hosts=secret_allowed_hosts,
        drafts=drafts,
        require_version=require_version,
        record_facts=record_facts,
    )


def _role_hosts(role: RoleEnvironment | None) -> tuple[str, ...]:
    """Return the hosts a ``[run]`` or ``[metric]`` box may reach besides the relay.

    Args:
        role: The role's parsed environment, or ``None`` without a constraints file.

    Returns:
        The role's ``network`` hosts; empty keeps the box offline.
    """
    return tuple(role.network or ()) if role is not None else ()


def _warn_metric_imports(owner_id: str, warnings: tuple[str, ...]) -> None:
    """Write each metric import warning into the run's log at setup.

    Args:
        owner_id: The run the warnings belong to.
        warnings: The messages from :func:`metric_import_warnings`.
    """
    for warning in warnings:
        sandbox_log.stream_logger.warning(
            "%s",
            warning,
            extra=run_log.event_extra(
                source="host", event="metric.imports_repo", fields={"warning": warning}, sandbox_owner=owner_id
            ),
        )


def relay_mcp_archive(archive: Path, roles: tuple[RoleEnvironment, ...], destination: Path) -> Path:
    """Pack a copy of a tree with each role's MCP file pointed at the box's relay.

    The protected check compares the box's files with this archive, so the
    rewrite happens here and not in the box. Each entry gets the loopback url
    of the runner's relay; the parent keeps the real url and headers (E49, E53).

    Args:
        archive: Packed full tree.
        roles: Every role that may name an MCP file; [run], [metric] and
            [proposer] call through the relay, and the other files get
            loopback urls too. Both the scorer's tree and the proposer's
            tree get this rewrite (SKY-610).
        destination: Where to write the copy.

    Returns:
        ``destination``.
    """
    relay = f"http://127.0.0.1:{runner.RELAY_PORT}"
    files = {
        posixpath.normpath(role.mcp): (json.dumps(box_mcp_config(role, relay), indent=2) + "\n").encode()
        for role in roles
        if role.mcp is not None
    }
    with tarfile.open(archive) as source, tarfile.open(destination, "w:gz") as target:
        for member in source:
            body = files.get(posixpath.normpath(member.name)) if member.isreg() else None
            if body is not None:
                member = copy.copy(member)
                member.size = len(body)
                target.addfile(member, io.BytesIO(body))
            else:
                target.addfile(member, source.extractfile(member) if member.isreg() else None)
    return destination


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
    a setup command the setup box reaches only package registries, and its
    state is saved; each version then runs in a new candidate box and is
    scored in a new metric box, each with its own role's hosts (SKY-398). When the
    proposer's MCP file lists servers or ``[proposer] services`` lists
    services, the proposer's route also gets the token, server names and
    service names of a relay that reaches only those.

    Args:
        payload: Protected payload, updated in place with the evaluator route.
        staged: The fetched tree and decrypted secrets.
        gateway: The run's model gateway, already bound to its sandbox broker.
        owner_id: Job identity naming the box's operations.
        managed_key: The platform's OpenRouter key, which pays for the
            watch hook under the run's budget; ``""`` when not configured.

    Raises:
        ConstraintsError: When the run turned the watch hook on but it cannot run.
        ServiceError: When the scorer has a model but no scoped route from the trusted
            parent, or the proposer has MCP servers or services but no model route.
    """
    model_gateway = _repo_model_gateway(payload["scorer"])
    rule_judge = _rule_judge(payload, staged, gateway, owner_id=owner_id, managed_key=managed_key)
    descriptor = payload["_budget_gateway_descriptor"]
    target = payload["target"]
    scorer_spec = payload["scorer"]
    hosts = PACKAGE_REGISTRY_HOSTS if (target.get("setup_command") or "").strip() else ()
    parent_runtime = gateway.parent_sandbox_runtime()
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=parent_runtime,
            spec=SandboxSpec(
                # The descriptor's lifetime is the guest's, which a per-run deadline
                # shortens (SKY-572). This box opens before the guest and must outlive
                # it, so it asks for the parent profile maximum.
                lifetime_seconds=parent_runtime.max_lifetime_seconds,
                image=descriptor["image"],
                network_disabled=not hosts,
                allowed_hosts=hosts,
                operation_key=f"repo-scorer:{owner_id}",
                services=staged.compose_services,
            ),
            archive=staged.scorer_archive or staged.snapshot.archive,
            editable_paths=target["repo"]["editable_paths"],
            readonly_paths=staged.snapshot.readonly_paths,
            setup_command=target.get("setup_command"),
            secrets=staged.secrets,
            protected_paths=staged.protected_paths,
            hidden_paths=staged.hidden_paths,
            run_hosts=staged.run_hosts,
            run_vcpus=staged.run_vcpus,
            metric_hosts=staged.metric_hosts,
            metric_vcpus=staged.metric_vcpus,
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
        mcp_roles=staged.mcp_roles,
        run_entry=staged.run_entry,
        run_timeout=staged.run_timeout,
        secret_bindings=staged.secret_bindings,
        secret_hosts=staged.secret_hosts,
        secret_allowed_hosts=staged.secret_allowed_hosts,
        budget=getattr(gateway, "runtime", None),
        recorder=getattr(gateway, "recorder", None),
    )
    environment = target["repo"].get(ENVIRONMENT_KEY)
    if isinstance(environment, dict):
        environment["setup_command"] = target.get("setup_command")
        environment["network"] = {
            "setup": list(hosts),
            "versions": list(staged.run_hosts),
            "metric": list(staged.metric_hosts),
        }
    staged.scorers.append(scorer)
    _warn_metric_imports(owner_id, staged.metric_warnings)
    # Setup finishes here, in the parent, before the guest that writes
    # versions exists; a failure fails the run.
    scorer.prepare()
    payload["_skynet_evaluator_route"] = gateway.bind_evaluator(scorer)
    proposer = staged.proposer_role
    if proposer is not None and (proposer.mcp_servers or proposer.services):
        proposer_route = _proposer_model_extra(payload).get(ROUTE_KEY)
        if not isinstance(proposer_route, dict):
            raise ServiceError("The [proposer] mcp servers and services need the proposer's model route.")
        relay = McpRelay(
            "[proposer]",
            proposer,
            secrets=staged.secrets,
            bindings=staged.secret_bindings,
            secret_hosts=staged.secret_hosts,
            allowed_hosts=staged.secret_allowed_hosts,
            budget=getattr(gateway, "runtime", None),
            services=staged.services,
            # Box code can put any token it holds into a call (SKY-640).
            record_secrets=lambda: _box_route_tokens(payload, proposer_route),
        )
        # Beside the proposer's model route, as the host proposer's token is:
        # the guest's harness launch reads only that route.
        proposer_route[MCP_RELAY_ROUTE_KEY] = {
            "token": gateway.bind_mcp_relay(relay)["token"],
            "servers": [server.name for server in proposer.mcp_servers],
            "services": list(proposer.services),
        }


def _box_route_tokens(payload: Mapping[str, Any], route: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every token the proposer box reads from its model routes.

    The worker attaches the host agent token after the relay is made, so
    the relay calls this for each record, not once.

    Args:
        payload: The run payload, whose extra reflection models a ShinkaEvolve
            box also receives, one ``APP_SHINKA_KEY_<n>`` each.
        route: The proposer's model route, which later steps change in place.

    Returns:
        The model, host agent, embedding, MCP relay and extra reflection
        model tokens that are set.
    """
    embedding = route.get(EMBEDDING_ROUTE_KEY)
    mcp = route.get(MCP_RELAY_ROUTE_KEY)
    tokens = [
        route.get("token"),
        route.get(HOST_AGENT_TOKEN_KEY),
        embedding.get("token") if isinstance(embedding, dict) else None,
        mcp.get("token") if isinstance(mcp, dict) else None,
    ]
    # The same configs the blackbox service's ``_shinka_models`` turns into
    # the box's ShinkaEvolve keys; one with no route reuses the proposer's token.
    for config in payload.get("extra_reflection_model_configs") or ():
        extra = config.get("extra") if isinstance(config, dict) else None
        extra_route = extra.get(ROUTE_KEY) if isinstance(extra, dict) else None
        if isinstance(extra_route, dict):
            tokens.append(extra_route.get("token"))
    return tuple(str(token) for token in tokens if token)


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
        hidden_paths: The hidden list from ``hidden_repo_paths``.

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
    edit to a hidden path, ``optimize.toml`` included, never leaves the run
    (SKY-353, E62).

    Args:
        result: Finished black-box result, updated in place.
        hidden_paths: The hidden list from ``hidden_repo_paths``.
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


def _framed(path: str, data: bytes) -> bytes:
    """Frame one file for the metric hash as ``<path>\\0<byte length>\\0<bytes>``.

    Args:
        path: Repository-relative path, encoded as UTF-8.
        data: The file's bytes.

    Returns:
        The framed part.
    """
    return path.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data


def _record_facts(
    archive: Path,
    *,
    contract_sha256: str,
    metric_files: tuple[str, ...],
    metric_code: str,
    drafts: Mapping[str, str],
    cases_file: str | None,
    cases: list[Any] | None,
) -> dict[str, Any]:
    """Hash the contract, the metric and the cases the run reads from the staged tree (SKY-694).

    ``metric_sha256`` is one SHA-256 over the framed entry code (path
    ``metric.py``, the UTF-8 bytes of ``metric_code``) and then each regular
    file under the ``[metric] files`` paths, in sorted path order; see
    :func:`_framed`. ``metric_home`` describes the ``[metric] files`` only: it
    is ``repository`` when the file sets ``[metric] files`` and every one comes
    from the pinned commit, and ``judge`` when there are none or a draft file
    replaces or adds one. ``cases_sha256`` hashes the ``[data] cases`` file
    bytes (``cases_source`` ``file``), or else the compact JSON of the
    request's cases with sorted keys (``cases_source`` ``request``).

    Args:
        archive: The staged tree, after the draft files apply.
        contract_sha256: SHA-256 of the ``optimize.toml`` bytes the run read.
        metric_files: The ``[metric] files`` paths, files or folders.
        metric_code: The scorer's entry code.
        drafts: The run's draft files by path.
        cases_file: The ``[data] cases`` path, or ``None``.
        cases: The request's cases, used when there is no cases file.

    Returns:
        ``contract_sha256``, ``metric_sha256``, ``metric_home``,
        ``cases_sha256`` and ``cases_source``; the last two are ``None`` with no cases.
    """
    roots = tuple(metric_files) + ((cases_file,) if cases_file is not None else ())
    files: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as packed:
        for member in packed.getmembers():
            path = posixpath.normpath(member.name).lstrip("/")
            if member.isfile() and inside(path, roots):
                handle = packed.extractfile(member)
                files[path] = handle.read() if handle is not None else b""
    metric = hashlib.sha256(_framed("metric.py", metric_code.encode("utf-8")))
    for path in sorted(path for path in files if inside(path, metric_files)):
        metric.update(_framed(path, files[path]))
    from_commit = bool(metric_files) and not any(inside(path, metric_files) for path in drafts)
    if cases_file is not None:
        cases_sha256, cases_source = hashlib.sha256(files.get(cases_file, b"")).hexdigest(), "file"
    elif cases:
        canonical = json.dumps(cases, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        cases_sha256, cases_source = hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "request"
    else:
        cases_sha256, cases_source = None, None
    return {
        "contract_sha256": contract_sha256,
        "metric_sha256": metric.hexdigest(),
        "metric_home": "repository" if from_commit else "judge",
        "cases_sha256": cases_sha256,
        "cases_source": cases_source,
    }


def add_run_record(
    result: dict[str, Any], payload: dict[str, Any], staged: StagedRepository, *, image: str | None
) -> None:
    """Write the run record into ``result["details"]["record"]`` at the run end (SKY-694).

    The record lets another person reproduce the score: the hashes and the
    metric home from staging (:func:`_record_facts`), the seed, the split the
    guest made, the pinned sandbox image, and the ``[outcome]`` verdict keys
    with the same absent-key rule as ``details["outcome"]``. ``contract_met``
    is true when no rule failed, also when the file sets no target and no
    minimum. A run with no ``optimize.toml``, a result with no ``details``, or
    a run with cases whose guest wrote no split positions (an older image)
    gets no record. The record holds no secret, no case content and no metric code.

    Args:
        result: The run's final result; changed in place.
        payload: The staged run payload, with the cases the guest received.
        staged: The staged tree, which keeps the record facts.
        image: The sandbox image pinned for the run, or ``None``.
    """
    details = result.get("details")
    if not staged.constrained or not staged.record_facts or not isinstance(details, dict):
        return
    split = details.get(CASE_SPLIT_KEY) if payload.get("cases") else None
    if payload.get("cases") and not isinstance(split, dict):
        return
    outcome = details.get("outcome") or {}
    verdict = {key: outcome[key] for key in ("target_reached", "minimums_passed", "failed_minimums") if key in outcome}
    details["record"] = {
        **staged.record_facts,
        "seed": payload.get("seed"),
        "split": split,
        "image": image,
        **verdict,
        "contract_met": verdict.get("target_reached", True) is True and verdict.get("minimums_passed", True) is True,
    }


def _record_block(result: dict[str, Any]) -> str:
    """Write the pull request's run record block, or nothing for a run with no record.

    Args:
        result: Finished black-box result.

    Returns:
        Markdown that starts with one plain line on the contract, then the
        record fields; an empty string when the result has no record.
    """
    record = (result.get("details") or {}).get("record")
    if not isinstance(record, dict):
        return ""
    if "target_reached" not in record and "minimums_passed" not in record:
        verdict = f"The {CONSTRAINTS_FILE} file sets no target and no minimum."
    elif record.get("contract_met"):
        verdict = "This run met the contract."
    else:
        failed = []
        if record.get("target_reached") is False:
            target = ((result.get("details") or {}).get("outcome") or {}).get("target")
            score = result.get("best_score")
            reached = f"{score:.12g}" if isinstance(score, int | float) else "no score"
            failed.append(f"The final score {reached} did not reach the target {target:.12g}.")
        failed.extend(str(entry.get("reason")) for entry in record.get("failed_minimums") or [])
        verdict = "This run did not meet the contract. " + " ".join(failed)
    split = record.get("split")
    if split is None:
        split_line = "none, the run has no cases"
    else:
        parts = {name: split.get(name) or [] for name in ("search", "validation", "held_out")}
        if sum(len(positions) for positions in parts.values()) > 30:
            counts = ", ".join(f"{len(positions)} {name}" for name, positions in parts.items())
            split_line = (
                f"`{split.get('rule')}`, seed {split.get('seed')}: {counts} cases (`get_run` lists the positions)"
            )
        else:
            listed = "; ".join(f"{name} {positions}" for name, positions in parts.items())
            split_line = f"`{split.get('rule')}`, seed {split.get('seed')}: {listed}"
    fields = [
        f"- Contract SHA-256: `{record.get('contract_sha256')}`",
        f"- Metric SHA-256: `{record.get('metric_sha256')}` (metric home: {record.get('metric_home')})",
        f"- Cases SHA-256: `{record.get('cases_sha256')}` (source: {record.get('cases_source')})",
        f"- Seed: {record.get('seed')}",
        f"- Split: {split_line}",
        f"- Sandbox image: `{record.get('image')}`",
    ]
    return f"{verdict}\n\nRun record:\n" + "\n".join(fields) + "\n\n"


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
        f"{_record_block(result)}"
        f"Review the run: {run_url}\n"
    )


def _drafts_kwargs(staged: StagedRepository) -> dict[str, Any]:
    """Pass the run's draft files and read-only paths to a push only when it has drafts.

    Args:
        staged: The staged tree, which keeps the drafts that staging took off the payload.

    Returns:
        ``{"drafts": ..., "readonly_paths": ...}``, or an empty dict for a run without draft files.
    """
    if not staged.drafts:
        return {}
    return {"drafts": dict(staged.drafts), "readonly_paths": tuple(staged.snapshot.readonly_paths)}


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
            provider = repo_hosts.provider_of(repo)
            github_host = provider == repo_hosts.DEFAULT_PROVIDER
            token = github_token(engine, username) if github_host else repo_hosts.host_token(engine, username, provider)
            push_version(
                repo["repository"],
                repo["commit"],
                _joined(patch, suite),
                branch,
                f"Apply the best version from {APP_NAME} run {optimization_id[:12]}",
                token,
                **repo_hosts.git_kwargs(provider),
                **_drafts_kwargs(staged),
            )
            change_fields = {
                "head": branch,
                "base_branch": repo.get("branch"),
                "title": f"{APP_NAME}: optimized version from run {optimization_id[:12]}",
                "body": _description(result, patch, run_url),
                "hardening": hardening,
            }
            if github_host:
                change = open_pull_request(token, repo["repository"], **change_fields)
            else:
                change = repo_hosts.open_change(provider, token, repo["repository"], **change_fields)
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
        pushed = push_candidates(
            repo["repository"],
            repo["commit"],
            versions,
            best_id,
            github_token(engine, username),
            **_drafts_kwargs(staged),
        )
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

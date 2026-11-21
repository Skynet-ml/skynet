"""Stage a repository target's tree and secrets in the trusted parent before its sandbox opens."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import logging
import math
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import run_log
from ..api.model_catalog import agent_model_id, catalog_snapshot, lowest_effort_from_low
from ..api.routers.package_registry import package_registry_for
from ..billing.host_agent import HOST_AGENT_TOKEN_KEY, _proposer_model_extra
from ..billing.model_gateway import EMBEDDING_ROUTE_KEY, MCP_RELAY_ROUTE_KEY, ROUTE_KEY
from ..billing.operation_pricing import ChargePolicy, image_build_usd, operation_quote
from ..billing.protected_credentials import (
    ProtectedCredentialVault,
    repo_secret_allowed_hosts,
    resolve_repo_secrets,
)
from ..billing.runtime import PaidResult
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
from ..models.blackbox import (
    BLACKBOX_ENGINE_AUTORESEARCH,
    BLACKBOX_ENGINE_META_HARNESS,
    BLACKBOX_ENGINE_SHINKA_EVOLVE,
    BLACKBOX_HARNESS_CUSTOM,
    HOST_CLI_AUTO_KEY,
    BlackboxProposer,
)
from ..models.common import ModelConfig
from ..models.hardening import HardeningJob, HardeningJobModel
from ..sandbox.image_build import (
    CODEBUILD_PLATFORM,
    BuildLimits,
    BuildRequest,
    BuildResult,
    CodeBuildImageBuilder,
    ImageBuilder,
    ImageBuildError,
    LocalDockerImageBuilder,
    compute_input_hash,
)
from ..sandbox.image_registry import ImageRegistry, ImageRegistryError
from ..sandbox.services import ComposeService
from ..service_gateway.optimization.blackbox import loop_tools, runner, sandbox_log
from ..service_gateway.optimization.blackbox.harness import mcp_unsupported
from ..service_gateway.optimization.blackbox.harness_bridge import hidden_hashes
from ..service_gateway.optimization.blackbox.protocol import RUN_FAILED_STATUS, STATUS_KEY, is_metric_error
from ..service_gateway.optimization.blackbox.registry import ENGINES
from ..service_gateway.optimization.blackbox.repo_constraints import (
    CONSTRAINTS_FILE,
    CONSTRAINTS_REQUIRED,
    DEFAULT_METRIC_ENTRY,
    MINIMIZE,
    ConstraintsError,
    MetricSpec,
    RepoConstraints,
    RepoEnvironment,
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
    SAFE_GIT,
    drop_paths,
    extract_folder,
    inside,
    linked_hidden_paths,
    patch_paths,
    patch_violations,
    strip_archive,
)
from ..service_gateway.optimization.blackbox.repo_workspace import RepoSetupError, RepoWorkspace
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
UPLOADED_CASES_REFUSED = "This repo defines its cases in ascent.toml. Remove the uploaded cases."
# Rules the run is held to; a resumed run must arrive at exactly these again (only the compose file may join or move).
_ENFORCED_FIELDS = ("commit", "editable_paths", "protected_paths", "readonly_paths", "services", "rules", "outcome")
# Environments recorded before a field existed hold its empty value.
_ENFORCED_DEFAULTS: dict[str, Any] = {"rules": [], "outcome": {"target": None, "require": {}, "direction": "maximize"}}
_SETUP_ANSWER_TOKENS = 1_024
_JUDGE_ROLE = "judge"
# The per-run watch hook setting: ``{"enabled", "model", "threshold"}``, each
# optional; a missing key takes the server default (SKY-349).
RULE_WATCH_KEY = "rule_watch"
# Only a job that ended on its own terms ships its suite; a failed, rejected or
# stale job may hold rounds the user never finished reviewing (SKY-356).
_FINISHED_HARDENING = frozenset({"clean_stop", "budget_stop", "done"})
METRIC_CODE_NEEDED = (
    "The [metric] entry {entry} names a Python function, and the run has no metric code."
    ' Set the metric code, or make [metric] entry a command such as entry = ["./metric.sh"].'
)
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
    # The [metric] entry: a Python function in the request's metric code, or a command (SKY-738).
    metric_entry: str | tuple[str, ...] = DEFAULT_METRIC_ENTRY
    # Each role's own box: [run] sizes and walls off the candidate box, [metric]
    # the metric box. No hosts keeps the box offline; None keeps the default size.
    run_hosts: tuple[str, ...] = ()
    run_vcpus: int | None = None
    metric_hosts: tuple[str, ...] = ()
    metric_vcpus: int | None = None
    # The [metric] setup command, run once in the metric setup box (SKY-740).
    metric_setup: str | None = None
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
    # Whether staging refused an ascent.toml with no version. Every later read
    # of this tree uses the same rule, so one pass never reads the file two ways.
    require_version: bool = True
    # The run record's hashes and metric source, taken from the staged tree the
    # run reads, so the run end never reads the repository again (SKY-694).
    record_facts: Mapping[str, Any] = field(default_factory=dict)
    # The inputs of :func:`_record_facts`, so a metric image can join the metric hash after its build (SKY-737).
    record_inputs: Mapping[str, Any] = field(default_factory=dict)
    # Each box's own image in ascent.toml, by role: ("image", reference) or ("dockerfile", path) (SKY-737).
    box_sources: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    # Each box's checked image, by role, from :func:`prepare_box_images`; empty for Skynet's image.
    box_images: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

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
    """Name the ``ascent.toml`` setting that hides each scoring-side path (SKY-446).

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
    require_constraints: bool = False,
    started: bool = False,
) -> tuple[dict[str, Any], StagedRepository]:
    """Fetch the pinned tree and decrypt the run's secrets in the parent.

    A run with a recorded environment, or a ``started`` job, already passed
    its first staging, maybe before ``version`` was required (SKY-690). Its
    commit's ``ascent.toml`` then reads a missing ``version`` as 1.

    Args:
        payload: Parent payload whose secrets are still vault references.
        username: Owner of the run and of the GitHub connection.
        binding_id: Execution budget the vaulted secrets are bound to.
        engine: SQLAlchemy engine for the vaults.
        require_constraints: Refuse a new run whose tree and drafts have no
            ``ascent.toml``. A run that recorded its environment before keeps going.
        started: Whether the caller's job already started; a hardening job
            records no environment.

    Returns:
        A payload copy that pins the commit, lists no secret entries and no
        draft files or draft base (the staged tree keeps the files), names
        the packed tree and carries the effective environment under
        ``target.repo.environment``, plus the staged tree and secrets for the parent.

    Raises:
        ConstraintsError: When the repository's ``ascent.toml`` is invalid or
            missing for a new run that requires it, asks for more than the run approved, or a resumed run's rules
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
    # The guest receives this payload, and a draft (often ascent.toml) can be
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
        # draft's ascent.toml see the tree the user set up.
        try:
            apply_to_archive(snapshot.archive, drafts, snapshot.readonly_paths)
        except DraftFileError as error:
            raise ConstraintsError(str(error)) from None
        # A started run passed its first staging under Skynet's older reader, so it re-reads in its forms (SKY-760).
        constraints = read_constraints(
            snapshot.archive, require_version=require_version, legacy_forms=not require_version
        )
        # A started run (resume, recovery) already recorded the rules it runs under, so only a new run stops here.
        if require_constraints and constraints is None and not isinstance(repo.get(ENVIRONMENT_KEY), dict):
            raise ConstraintsError(CONSTRAINTS_REQUIRED)
        environment = effective_environment(
            constraints,
            approved_editable=repo["editable_paths"],
            approved_setup=result["target"].get("setup_command"),
            approved_proposer=_names_proposer(result),
        )
        # A custom command runs without the relay's MCP servers, so it would run without the file's tools.
        if environment.proposer_entry is not None and environment.constraints.proposer.mcp_servers:
            raise ConstraintsError(mcp_unsupported(BLACKBOX_HARNESS_CUSTOM), section="proposer", key="mcp")
        if environment.proposer_entry is not None:
            _refuse_other_runs(result.get("strategy") or {})
            _refuse_engine_loop_settings(environment, result)
        else:
            _refuse_unsupported_proposer(environment, result)
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
        _ensure_unchanged(
            repo.get(ENVIRONMENT_KEY),
            record,
            compose_file=environment.constraints.compose_file if environment.constraints is not None else None,
        )
        # The guest's workspace (Vercel or local Docker) unpacks this copy; the
        # parent's scorer box keeps the full tree, so it scores with the base
        # commit's metric (SKY-353).
        metric = environment.constraints.metric if environment.constraints is not None else MetricSpec()
        # A Python scorer may skip the metric code only for a command entry (SKY-738).
        scorer = result.get("scorer") or {}
        code_needed = scorer.get("kind") == "python" and isinstance(metric.entry, str)
        if code_needed and not str(scorer.get("metric_code") or "").strip():
            raise ConstraintsError(METRIC_CODE_NEEDED.format(entry=metric.entry), section="metric", key="entry")
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
        record_inputs = (
            {
                "contract_sha256": environment.constraints.sha256,
                "metric_files": metric.files,
                "metric_entry": metric.entry,
                "metric_setup": metric.setup,
                "metric_code": str((result.get("scorer") or {}).get("metric_code") or ""),
                "drafts": drafts,
                "cases_file": cases_file,
                "cases": result.get("cases"),
            }
            if environment.constraints is not None
            else {}
        )
        record_facts = _record_facts(snapshot.archive, **record_inputs) if record_inputs else {}
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
    if environment.proposer_entry is not None:
        result["proposer"] = {
            **(result.get("proposer") or {}),
            "harness": BLACKBOX_HARNESS_CUSTOM,
            "run_command": shlex.join(environment.proposer_entry),
            # The guest runs the file's setup in its checkout (``proposer_setup`` below), not in the bootstrap.
            "install_command": None,
            # Only a filled-in host CLI reaches here, and it would replace the file's command.
            "host_cli": None,
        }
        result["proposer"].pop(HOST_CLI_AUTO_KEY, None)
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
        "file_proposer": environment.proposer_entry is not None,
        "proposer_setup": list(environment.proposer_setup or ()),
        # Names, addresses and a has-headers flag only: an engine that calls the servers from a box grants their
        # hosts, and the provider edge adds the headers, so their secrets stay out of the run's options (SKY-830).
        "proposer_mcp": [
            {"name": server.name, "url": server.url, "has_headers": bool(server.headers)}
            for server in (environment.constraints.proposer.mcp_servers if environment.constraints is not None else ())
        ],
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
        metric_entry=environment.constraints.metric.entry
        if environment.constraints is not None
        else DEFAULT_METRIC_ENTRY,
        run_hosts=_role_hosts(environment.constraints.run_role if environment.constraints is not None else None),
        run_vcpus=environment.constraints.run_vcpus if environment.constraints is not None else None,
        metric_hosts=_role_hosts(environment.constraints.metric_role if environment.constraints is not None else None),
        metric_vcpus=environment.constraints.metric_vcpus if environment.constraints is not None else None,
        metric_setup=metric.setup,
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
        record_inputs=record_inputs,
        box_sources=_box_sources(environment.constraints),
    )


# The engines that run [proposer] entry in the worker engine loop (SKY-816).
_ENGINE_LOOP_ENGINES = frozenset({BLACKBOX_ENGINE_AUTORESEARCH, BLACKBOX_ENGINE_META_HARNESS})


def runs_proposer_entry(strategy: Mapping[str, Any]) -> bool:
    """Tell whether a run is a single AutoResearch or Meta-Harness run, which runs the file's command.

    Every run is on the engine loop since the cutover (SKY-728), but only these single runs give the
    command to the engine's tool loop.

    Args:
        strategy: The stored run request's ``strategy``.

    Returns:
        True when the engine loop runs the file's command for this run (SKY-816).
    """
    return strategy.get("mode", "auto") == "single" and strategy.get("engine") in _ENGINE_LOOP_ENGINES


def _refuse_engine_loop_settings(environment: RepoEnvironment, payload: Mapping[str, Any]) -> None:
    """Refuse the settings that the engine loop cannot give ``[proposer] entry``, before the engine starts.

    AutoResearch and Meta-Harness run the file's command in the engine loop (SKY-816). That loop gives it no
    relay for ``[proposer] services`` and no reasoning effort, so the engine would fail at start.

    Args:
        environment: The run's rules, with the file's proposer command.
        payload: The stored run request.

    Raises:
        ConstraintsError: When the run has one of the settings.
    """
    fix = "Remove it, or remove [proposer] entry."
    if environment.constraints is not None and environment.constraints.proposer.services:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: AutoResearch and Meta-Harness cannot give [proposer] services to the"
            f" [proposer] entry command. {fix}",
            section="proposer",
            key="services",
        )
    # The stored payload is saved by alias, so the model settings are under ``reflection_model_config``.
    effort = ((payload.get("reflection_model_config") or {}).get("extra") or {}).get("reasoning_effort")
    if effort:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: AutoResearch and Meta-Harness cannot give the reasoning_effort setting to the"
            f" [proposer] entry command. {fix}",
            section="proposer",
            key="entry",
        )


def _refuse_other_runs(strategy: Mapping[str, Any]) -> None:
    """Refuse ``[proposer] entry`` for every run but a single AutoResearch or Meta-Harness run.

    Only those two loop engines give the file's command to their tool loop (SKY-816). GEPA, Best-of-N and
    AutoSaddler ran it in the native box, which the cutover removed (SKY-728); on the loop they would use the
    built-in proposer in silence. ShinkaEvolve never runs it (SKY-753), and an Auto run would keep only lanes
    that ignore it. The check refuses the run here, before a paid box starts (SKY-815). SKY-828 lifts it.

    Args:
        strategy: The stored run request's ``strategy``.

    Raises:
        ConstraintsError: When the run is not a single AutoResearch or Meta-Harness run.
    """
    if not runs_proposer_entry(strategy):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: Only a single AutoResearch or Meta-Harness run can use the [proposer] entry"
            " command yet. Remove [proposer] entry, or choose one of those engines.",
            section="proposer",
            key="entry",
        )


def _refuse_unsupported_proposer(environment: RepoEnvironment, payload: Mapping[str, Any]) -> None:
    """Refuse the proposer settings that the run's engine does not run on the engine loop, before paid work.

    Every engine runs on the worker engine loop since the cutover (SKY-728), and the built-in proposer of most
    engines reads few of the proposer settings, so the run would drop the others in silence. The rules are the
    engines' own (``loop_tools.unsupported_proposer_settings``). A host CLI that local mode filled in is not the
    user's choice, so it is not refused here.

    Args:
        environment: The run's rules, with no ``[proposer] entry`` command.
        payload: The stored run request.

    Raises:
        ConstraintsError: When the run names a proposer setting that its engine does not run, or gives a
            ShinkaEvolve run a [proposer] mcp server with headers.
    """
    strategy = payload.get("strategy") or {}
    engine = strategy.get("engine") if strategy.get("mode", "auto") == "single" else None
    stored = dict(payload.get("proposer") or {})
    if stored.get(HOST_CLI_AUTO_KEY):
        stored["host_cli"] = None
    proposer_role = environment.constraints.proposer if environment.constraints is not None else None
    # The stored payload is saved by alias, so the model settings are under ``reflection_model_config``.
    effort = ((payload.get("reflection_model_config") or {}).get("extra") or {}).get("reasoning_effort")
    refused = loop_tools.unsupported_proposer_settings(
        BlackboxProposer.model_validate(stored),
        supports=loop_tools.proposer_support(engine, (payload.get("shinka") or {}).get("editor")),
        mcp_servers=bool(proposer_role is not None and proposer_role.mcp_servers),
        services=bool(proposer_role is not None and proposer_role.services),
        reasoning_effort=bool(effort),
    )
    if refused:
        name = ENGINES[engine].label if engine in ENGINES else "Auto"
        raise ConstraintsError(
            f"{name} runs only its built-in proposer, so it cannot apply these proposer settings: {', '.join(refused)}."
            " Remove them, or choose another engine.",
            section="proposer",
            key=refused[0],
        )
    # A protected box cannot add the headers, and the loop has no MCP relay yet, so the run would fail at its first
    # MCP call after paid work starts. An Auto run already refuses every [proposer] mcp server. SKY-840 (an outbound
    # proxy that adds the headers) lifts this refusal.
    headed = [server.name for server in proposer_role.mcp_servers if server.headers] if proposer_role else []
    if engine == BLACKBOX_ENGINE_SHINKA_EVOLVE and headed:
        raise ConstraintsError(
            "ShinkaEvolve does not support [proposer] mcp servers with headers yet: "
            f"{', '.join(headed)}. Remove these servers or their headers."
            " MCP servers with no headers still work.",
            section="proposer",
            key="mcp",
        )


def _names_proposer(payload: Mapping[str, Any]) -> bool:
    """Tell whether a run request names its own proposer.

    The stored request keeps ``proposer.harness`` only when the client sent
    it (SKY-753), so a default does not replace ``[proposer] entry``. A host
    CLI, a run command and an install command are the user's own choice too,
    but not a host CLI that local mode filled in.

    Args:
        payload: The stored run request.

    Returns:
        True when the request sends ``proposer.harness``, a host CLI, a run
        command or an install command.
    """
    proposer = payload.get("proposer")
    return isinstance(proposer, dict) and (
        "harness" in proposer
        or (proposer.get("host_cli") is not None and not proposer.get(HOST_CLI_AUTO_KEY))
        or bool(proposer.get("run_command") or proposer.get("install_command"))
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


def _ensure_unchanged(previous: Any, current: dict[str, Any], *, compose_file: str | None = None) -> None:
    """Refuse to resume a run under rules other than the ones first recorded for it.

    Args:
        previous: The environment saved on the run by an earlier attempt, if any.
        current: The environment this attempt worked out.
        compose_file: The contract's ``[sandbox] compose`` file, which the current protected paths may add.

    Raises:
        ConstraintsError: When an enforced rule differs from the saved one.
    """
    if not isinstance(previous, dict):
        return

    def enforced(environment: dict[str, Any], name: str) -> Any:
        """Read one enforced field, with the value it had before the field or its key existed.

        Args:
            environment: A recorded or a current environment.
            name: One of :data:`_ENFORCED_FIELDS`.

        Returns:
            The field's value; an ``outcome`` recorded before SKY-750 maximizes.
        """
        value = environment.get(name, _ENFORCED_DEFAULTS.get(name))
        if name == "outcome" and isinstance(value, dict):
            return {**_ENFORCED_DEFAULTS["outcome"], **value}
        return value

    def differs(name: str) -> bool:
        """Tell whether one enforced field changed since it was recorded.

        Args:
            name: One of :data:`_ENFORCED_FIELDS`.

        Returns:
            Whether the run would resume under another rule.
        """
        before, after = enforced(previous, name), enforced(current, name)
        # The package reader also protects the [sandbox] compose file, which Skynet's older reader did not, so a run
        # recorded before SKY-760 resumes with that one path added. More protection keeps the run safe. The package
        # also moves the file ahead of [paths] protected, so it is left out of both lists before the compare.
        if (
            name == "protected_paths"
            and compose_file is not None
            and isinstance(before, list)
            and isinstance(after, list)
        ):
            return [path for path in before if path != compose_file] != [path for path in after if path != compose_file]
        return before != after

    changed = [name for name in _ENFORCED_FIELDS if differs(name)]
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
        The enforcement, or ``None`` when the repository has no ``ascent.toml``.

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


# The role each box's image check runs as, by the ascent.toml section that names the image.
_CHECK_ROLES = {"run": "candidate", "metric": "metric"}
# ``ascent check --image`` ends in about 24 seconds; the rest covers the box start.
_CHECK_TIMEOUT_SECONDS = 120.0
_CHECK_LIFETIME_SECONDS = 600.0
_CHECK_OUTPUT_CHARS = 4_000
USER_IMAGES_UNSUPPORTED = (
    "This deployment cannot start a box from a user image yet. Remove image and dockerfile"
    " from [run] and [metric] in ascent.toml to use the default image."
)
DOCKERFILE_UNSUPPORTED = (
    "This deployment cannot build a Dockerfile yet. Set [{role}] image to an image pinned by its digest instead."
)


class BoxImageError(RepoSetupError):
    """A user box image did not build or failed its check, so no candidate runs (SKY-737)."""


def _box_sources(constraints: RepoConstraints | None) -> dict[str, tuple[str, str]]:
    """Read the user image of the ``[run]`` and ``[metric]`` boxes from ascent.toml.

    Args:
        constraints: The parsed file, or ``None`` for a repository without one.

    Returns:
        ``("image", reference)`` or ``("dockerfile", path)`` by role, only for a role that sets one.
    """
    if constraints is None:
        return {}
    roles = {"run": constraints.run_role, "metric": constraints.metric_role}
    return {
        name: ("image", role.image) if role.image else ("dockerfile", role.dockerfile)
        for name, role in roles.items()
        if role.image or role.dockerfile
    }


def image_builder(config: Any = settings) -> ImageBuilder | None:
    """Return the builder of a run's Dockerfiles on this deployment.

    Args:
        config: The app settings.

    Returns:
        Local Docker in local mode; ``None`` on a cloud deployment, which has
        no CodeBuild project setting yet, so a Dockerfile there fails clearly.
    """
    return LocalDockerImageBuilder() if config.local_mode else None


def _image_owner(username: str) -> str:
    """Name the registry repository of one user's images.

    Args:
        username: The run's owner.

    Returns:
        A short lowercase name that the registry accepts and that hides the username.
    """
    return "u" + hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]


def _digest(image: str) -> str:
    """Return the digest of a pinned reference or a local image id.

    Args:
        image: ``name@sha256:<hex>`` or ``sha256:<hex>``.

    Returns:
        The ``sha256:<hex>`` part.
    """
    return image.rsplit("@", 1)[-1]


def prepare_box_images(
    staged: StagedRepository,
    *,
    owner_id: str,
    username: str,
    runtime: Any,
    builder: ImageBuilder | None,
    store: Any,
    budget: Any = None,
    registry: ImageRegistry | None = None,
    pinned: Mapping[str, Mapping[str, Any]] | None = None,
) -> StagedRepository:
    """Build or pin each user box image once, then check it before the first candidate (SKY-737).

    A ``[run]`` or ``[metric]`` box with ``dockerfile`` builds from the
    staged start commit, so a candidate edit never reaches its image. A
    build of the same user's same inputs is reused by its input hash. Each
    image must pass ``ascent check --image`` in an offline box; the metric
    check gives the trusted Python path that the scorer runs. A resume uses
    the images the run pinned at its start and builds and checks nothing.

    Args:
        staged: The staged tree, with the box sources from ascent.toml.
        owner_id: Job identity naming the build and check operations.
        username: The run's owner, whose builds can be reused.
        runtime: The parent's metered sandbox runtime, which opens the check boxes.
        builder: The Dockerfile builder, or ``None`` when this deployment has none.
        store: The job store, which keeps each build and its log.
        budget: The run's budget runtime, which reserves and settles the build; ``None`` runs it free.
        registry: The registry that must still hold a reused build, or ``None`` to skip that check.
        pinned: The images the run pinned at its start, for a resume.

    Returns:
        ``staged`` with ``box_images`` and, for a metric image, record facts that cover its digest.

    Raises:
        BoxImageError: When the runtime cannot start a user image, a build fails, or a check fails.
    """
    if not staged.box_sources:
        return staged
    if pinned:
        images = {role: dict(record) for role, record in pinned.items()}
    else:
        # A runtime that ignores the spec's image would run Skynet's image in its place, with no sign.
        if not getattr(runtime, "accepts_user_images", False):
            raise BoxImageError(USER_IMAGES_UNSUPPORTED)
        images = {}
        for role, (kind, value) in staged.box_sources.items():
            if kind == "image":
                record: dict[str, Any] = {
                    "image": value,
                    "digest": _digest(value),
                    "source": "image",
                    "input_hash": None,
                    "build_log": None,
                }
            else:
                record = _dockerfile_image(
                    staged,
                    role,
                    value,
                    owner_id=owner_id,
                    username=username,
                    builder=builder,
                    store=store,
                    budget=budget,
                    registry=registry,
                )
            python = _check_box_image(runtime, record["image"], role, owner_id=owner_id)
            if python is not None:
                record["python"] = python
            images[role] = record
    facts = staged.record_facts
    if "metric" in images and staged.record_inputs:
        facts = _record_facts(staged.snapshot.archive, **staged.record_inputs, metric_image=images["metric"]["digest"])
    return replace(staged, box_images=images, record_facts=facts)


def _dockerfile_image(
    staged: StagedRepository,
    role: str,
    dockerfile: str,
    *,
    owner_id: str,
    username: str,
    builder: ImageBuilder | None,
    store: Any,
    budget: Any,
    registry: ImageRegistry | None,
) -> dict[str, Any]:
    """Reuse or build the image of one box's Dockerfile from the staged start commit.

    Args:
        staged: The staged tree.
        role: ``run`` or ``metric``.
        dockerfile: The Dockerfile's repository path; its folder is the build context.
        owner_id: Job identity naming the build operation.
        username: The run's owner.
        builder: The Dockerfile builder, or ``None``.
        store: The job store.
        budget: The run's budget runtime, or ``None``.
        registry: The registry that must still hold a reused build, or ``None``.

    Returns:
        The box's image record: image, digest, source, input hash and build log reference.

    Raises:
        BoxImageError: When there is no builder, the context is invalid, or the build fails.
    """
    if builder is None:
        raise BoxImageError(DOCKERFILE_UNSUPPORTED.format(role=role))
    archive = staged.snapshot.archive
    if role == "run":
        # The candidate image must never hold the hidden scoring files, so its
        # context comes from the tree with them stripped, as the candidate's own.
        if linked := linked_hidden_paths(archive, staged.hidden_paths):
            raise BoxImageError(
                f"[run] dockerfile '{dockerfile}': the hidden scoring path '{linked[0]}' is, holds or lies inside"
                " a link."
            )
        archive = strip_archive(archive, staged.hidden_paths, staged.workdir / "image-context-run.tgz")
    context = extract_folder(archive, posixpath.dirname(dockerfile) or ".", staged.workdir / f"image-context-{role}")
    request = BuildRequest(context_dir=context, owner=_image_owner(username), dockerfile=posixpath.basename(dockerfile))
    kind = builder.kind
    try:
        input_hash = compute_input_hash(
            request, builder=kind, platform=CODEBUILD_PLATFORM if kind == CodeBuildImageBuilder.kind else ""
        )
    except ImageBuildError as error:
        raise BoxImageError(f"[{role}] dockerfile '{dockerfile}': {error}") from error
    record = {"source": "dockerfile", "input_hash": input_hash}
    found = store.find_image_build(username, input_hash)
    if found is not None and _still_held(registry, request.owner, found["image"]):
        return {
            "image": found["image"],
            "digest": _digest(found["image"]),
            **record,
            "build_log": f"image-build:{found['id']}",
        }
    limits = BuildLimits.from_settings()

    def build() -> PaidResult[BuildResult | ImageBuildError]:
        """Run the build once and charge its started minutes, also for a failed build."""
        started = time.monotonic()
        try:
            value: BuildResult | ImageBuildError = builder.build(request)
        except ImageBuildError as error:
            value = error
        seconds = time.monotonic() - started
        return PaidResult(
            value=value,
            provider_usd=image_build_usd(kind, seconds),
            evidence={"builder": kind, "input_hash": input_hash, "seconds": round(seconds, 3)},
        )

    if budget is None:
        outcome = build().value
    else:
        policy = ChargePolicy("sandbox")
        quote = operation_quote(
            {"builder": kind, "input_hash": input_hash},
            image_build_usd(kind, limits.timeout_seconds),
            policy,
            {"provider": kind, "timeout_seconds": limits.timeout_seconds},
        )
        outcome = budget.execute(
            quote, policy, build, operation_key=f"image-build:{owner_id}:{role}", cost_kind="sandbox"
        )
    if isinstance(outcome, ImageBuildError):
        store.save_image_build(username, input_hash, builder=kind, image=None, size_bytes=None, log=outcome.log)
        tail = outcome.log_tail.strip()
        raise BoxImageError(
            f"[{role}] dockerfile '{dockerfile}' did not build: {outcome}"
            + (f"\n\nBuild log tail:\n{tail}" if tail else "")
        )
    build_id = store.save_image_build(
        username, input_hash, builder=kind, image=outcome.image, size_bytes=outcome.size_bytes, log=outcome.log
    )
    return {"image": outcome.image, "digest": outcome.digest, **record, "build_log": f"image-build:{build_id}"}


def _still_held(registry: ImageRegistry | None, owner: str, image: str) -> bool:
    """Check that the registry still holds a reused build.

    Args:
        registry: The user image registry, or ``None`` for a local image.
        owner: The owner of the registry repository.
        image: The reused build's image.

    Returns:
        True with no registry, or when the registry holds the digest.
    """
    if registry is None:
        return True
    try:
        registry.check_digest(owner, _digest(image))
    except ImageRegistryError:
        return False
    return True


def _check_box_image(runtime: Any, image: str, role: str, *, owner_id: str) -> str | None:
    """Run ``ascent check --image`` in an offline box of the image.

    Args:
        runtime: The parent's metered sandbox runtime.
        image: The box image to check.
        role: ``run`` or ``metric``.
        owner_id: Job identity naming the check operation.

    Returns:
        The trusted absolute Python path for the metric role; ``None`` for the run role.

    Raises:
        BoxImageError: When the check fails or reports no usable result.
    """
    check_role = _CHECK_ROLES[role]
    session = runtime.open(
        SandboxSpec(
            lifetime_seconds=_CHECK_LIFETIME_SECONDS,
            image=image,
            network_disabled=True,
            operation_key=f"image-check:{owner_id}:{role}",
            private=role == "metric",
        )
    )
    try:
        result = session.run(f"ascent check --image --role {check_role}", timeout_seconds=_CHECK_TIMEOUT_SECONDS)
    finally:
        with contextlib.suppress(Exception):
            session.close()
    report = _check_report(result.stdout)
    python = report.get("python") if report is not None else None
    passed = result.ok and report is not None and report.get("ok") is True
    if passed and (check_role != "metric" or (isinstance(python, str) and python.startswith("/"))):
        return python if check_role == "metric" else None
    failed = [
        f"- {check.get('name')}: {check.get('message')}"
        for check in (report or {}).get("checks") or []
        if isinstance(check, dict) and check.get("ok") is not True
    ]
    output = "\n".join(failed) or (result.stdout + result.stderr).strip()[-_CHECK_OUTPUT_CHARS:]
    raise BoxImageError(
        f"The [{role}] image '{image}' failed `ascent check --image --role {check_role}`"
        f" (exit code {result.exit_code}).\n\n{output or 'The check printed nothing.'}"
    )


def _check_report(stdout: str) -> dict[str, Any] | None:
    """Read the JSON report that ``ascent check --image`` prints last.

    Args:
        stdout: The check's standard output.

    Returns:
        The report, or ``None`` when the last line is not a JSON object.
    """
    lines = stdout.strip().splitlines()
    try:
        report = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    return report if isinstance(report, dict) else None


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
            metric_setup=staged.metric_setup,
            run_image=(staged.box_images.get("run") or {}).get("image"),
            metric_image=(staged.box_images.get("metric") or {}).get("image"),
            metric_python=(staged.box_images.get("metric") or {}).get("python"),
        ),
        str(scorer_spec.get("metric_code") or ""),
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
        metric_entry=staged.metric_entry,
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


def _has_no_score(side_info: dict[str, Any]) -> bool:
    """Return whether a version's recorded side info marks it as having no score.

    The repository scorer drops a metric's own ``run_failed`` status, so on a
    repository run that status always means a failed ``[run]`` entry.

    Args:
        side_info: The version's recorded side info.

    Returns:
        ``True`` after a metric error (SKY-350) or a failed ``[run]`` entry (SKY-751).
    """
    return is_metric_error(side_info) or side_info.get(STATUS_KEY) == RUN_FAILED_STATUS


def _version_score(result: dict[str, Any], candidate: str) -> float | None:
    """Return the score the run ranked one version by.

    Args:
        result: Finished black-box result.
        candidate: The version's patch.

    Returns:
        Its recorded score, or ``None`` when the run never scored it, the
        metric crashed on it, or its ``[run]`` entry failed.
    """
    for version in result.get("versions") or []:
        if isinstance(version, dict) and version.get("candidate") == candidate:
            # SKY-350, SKY-751: a crash has no score, even if an older result stored a number.
            if _has_no_score(version.get("side_info") or {}):
                return None
            score = version.get("score")
            return float(score) if isinstance(score, int | float) else None
    return None


def _minimizes(result: dict[str, Any]) -> bool:
    """Return whether the run wants the lowest score; a result stored before SKY-750 maximizes.

    Args:
        result: Finished black-box result.

    Returns:
        ``True`` for ``[metric] direction = "minimize"``.
    """
    return result.get("direction") == MINIMIZE


def _ranking_score(result: dict[str, Any], candidate: str) -> float:
    """Return a version's score for ranking, higher is better, with unscored versions last.

    Args:
        result: Finished black-box result.
        candidate: The version's patch.

    Returns:
        The recorded score (negated for a minimize run), or negative infinity
        when the run never scored it.
    """
    score = _version_score(result, candidate)
    if score is None:
        return float("-inf")
    return -score if _minimizes(result) else score


def _version_has_no_score(result: dict[str, Any], candidate: str) -> bool:
    """Return whether one version has no score in the run's version list.

    Args:
        result: Finished black-box result.
        candidate: The version's patch.

    Returns:
        ``True`` when the metric or scorer crashed on the version, or its
        ``[run]`` entry failed, on any case.
    """
    return any(
        isinstance(version, dict)
        and version.get("candidate") == candidate
        and _has_no_score(version.get("side_info") or {})
        for version in result.get("versions") or []
    )


def _seed_run_failed(result: dict[str, Any]) -> bool:
    """Return whether the starting code has no score because its own ``[run]`` entry failed.

    Args:
        result: Finished black-box result.

    Returns:
        ``True`` when the run's version list marks the starting code with the
        ``run_failed`` status; ``False`` after a metric error or when the run
        never recorded the starting code.
    """
    seed = result.get("seed_candidate") or ""
    return any(
        isinstance(version, dict)
        and version.get("candidate") == seed
        and (version.get("side_info") or {}).get(STATUS_KEY) == RUN_FAILED_STATUS
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
    edit to a hidden path, ``ascent.toml`` included, never leaves the run
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


def _final_run_finished(result: dict[str, Any]) -> bool:
    """Return whether the final run finished for both the starting code and the best version.

    A final run where every version failed a ``[run]`` case leaves both
    scores empty, so the boolean ``details.baseline_run_failed`` flag marks
    a finished final run; the service sets it only when both runs finished.
    A result without the flag finished only when it has both scores.

    Args:
        result: Finished black-box result.

    Returns:
        ``True`` when the final run's scores decide, ``False`` when the
        versions' own scores must stand in.
    """
    if isinstance((result.get("details") or {}).get("baseline_run_failed"), bool):
        return True
    return result.get("baseline_score") is not None and result.get("best_score") is not None


def improved_patch(result: dict[str, Any]) -> str | None:
    """Return the best version when it beat the starting code, else ``None``.

    The final run's fresh scores decide, also when both are empty; a run
    stopped before its final run finished for both versions compares the
    versions' own scores instead. A scored version beats a starting version
    with no score only when the starting code's own ``[run]`` entry failed
    in the run that gave that missing score: the final run's
    ``details.baseline_run_failed`` flag, or the version list for a stopped
    run (SKY-751).

    Args:
        result: Finished black-box result.

    Returns:
        The best version's patch, or ``None`` when it is empty, no better,
        or a version with no score.
    """
    best = result.get("best_candidate")
    if not isinstance(best, str) or not best.strip() or result.get("regression_guard_applied"):
        return None
    if _version_has_no_score(result, best):
        return None
    baseline, optimized = result.get("baseline_score"), result.get("best_score")
    run_failed_flag = (result.get("details") or {}).get("baseline_run_failed")
    final_run = _final_run_finished(result)
    if not final_run:
        baseline, optimized = _version_score(result, result.get("seed_candidate") or ""), _version_score(result, best)
    if optimized is None:
        return None
    if baseline is None:
        # A metric error on the starting code is the metric's fault, not the
        # code's, so a scored version proves no improvement over it. The
        # search may have scored the starting code while its final run
        # failed, so the final run's own flag decides when there is one.
        seed_run_failed = run_failed_flag is True if final_run else _seed_run_failed(result)
        return best if seed_run_failed else None
    better = optimized < baseline if _minimizes(result) else optimized > baseline
    return best if better else None


# The [metric] setup part's key: archive paths lose their leading "/", so no file can take it (SKY-740).
_METRIC_SETUP_PART = "/[metric] setup"
# The same rule keeps the [metric] image part's key free (SKY-737).
_METRIC_IMAGE_PART = "/[metric] image"
# The record hashes stream each member in parts of this size, so a large
# metric file never sits in the worker's memory (SKY-694).
_HASH_CHUNK = 1 << 20


def _framed(path: str, size: int, digest: str) -> bytes:
    """Frame one file for the metric hash as ``<path>\\0<byte length>\\0<SHA-256 hex>``.

    The part holds the file's hex digest, not its bytes, so a person can
    rebuild it from ``sha256sum`` output and the worker never holds a whole file.

    Args:
        path: Repository-relative path, encoded as UTF-8.
        size: The file's byte length.
        digest: The SHA-256 of the file's bytes, as 64 lowercase hex characters.

    Returns:
        The framed part.
    """
    return path.encode("utf-8") + b"\0" + str(size).encode("ascii") + b"\0" + digest.encode("ascii")


def _stream_digest(handle: Any) -> tuple[int, str]:
    """Hash one archive member in fixed-size parts.

    Args:
        handle: The member's file object from ``TarFile.extractfile``.

    Returns:
        The byte length and the SHA-256 hex digest of the member's bytes.
    """
    digest, size = hashlib.sha256(), 0
    while chunk := handle.read(_HASH_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _record_facts(
    archive: Path,
    *,
    contract_sha256: str,
    metric_files: tuple[str, ...],
    metric_entry: str | tuple[str, ...],
    metric_code: str,
    drafts: Mapping[str, str],
    cases_file: str | None,
    cases: list[Any] | None,
    metric_setup: str | None = None,
    metric_image: str | None = None,
) -> dict[str, Any]:
    """Hash the contract, the metric and the cases the run reads from the staged tree (SKY-694).

    ``metric_sha256`` is one SHA-256 over framed parts, one for each regular
    file under the ``[metric] files`` paths, in sorted path order; see
    :func:`_framed`. The rule is the same for a Python and a command
    ``[metric] entry`` (SKY-738). For a Python entry, ``metric_code`` counts as
    one file at the entry's path and takes the place of a repository file
    there. The entry value is not a part: ``contract_sha256`` covers it. A
    ``[metric] setup`` command is one more part at the path ``/[metric] setup``,
    which no archive path can take, framed over the command's UTF-8 bytes
    (SKY-740); the record then also holds the command as ``metric_setup``.
    A ``[metric]`` user image is one more part at the path ``/[metric] image``,
    framed over the UTF-8 bytes of its digest, so the hash also pins the box
    that runs the metric (SKY-737).
    ``metric_source`` describes the ``[metric] files`` only: it is
    ``repository`` when the file sets ``[metric] files`` and every one comes
    from the pinned commit, and ``request`` when there are none or a draft
    file replaces or adds one.
    ``cases_sha256`` hashes the ``[data] cases`` file bytes (``cases_source``
    ``file``), or else the compact JSON of the request's cases with sorted
    keys (``cases_source`` ``request``).

    The archive is read once, as a stream, and each member is hashed in
    parts, so memory stays constant for any file size. When a path appears
    twice, the later member wins, as extraction writes it last.

    Args:
        archive: The staged tree, after the draft files apply.
        contract_sha256: SHA-256 of the ``ascent.toml`` bytes the run read.
        metric_files: The ``[metric] files`` paths, files or folders.
        metric_entry: The ``[metric] entry``: ``"path.py:function"`` or a command list.
        metric_code: The request's metric code; a command entry does not use it.
        drafts: The run's draft files by path.
        cases_file: The ``[data] cases`` path, or ``None``.
        cases: The request's cases, used when there is no cases file.
        metric_setup: The ``[metric] setup`` command, or ``None``.
        metric_image: The digest of the ``[metric]`` user image, or ``None`` for Skynet's image.

    Returns:
        ``contract_sha256``, ``metric_sha256``, ``metric_source``,
        ``cases_sha256`` and ``cases_source``; the last two are ``None`` with
        no cases. ``metric_setup`` is added only when the file sets one, so a
        record without it keeps its old keys.
    """
    roots = tuple(metric_files) + ((cases_file,) if cases_file is not None else ())
    digests: dict[str, tuple[int, str]] = {}
    # Stream mode refuses a backward seek, which in a gzip tar would decompress the archive again.
    with tarfile.open(archive, "r|gz") as packed:
        for member in packed:
            path = posixpath.normpath(member.name).lstrip("/")
            if member.isfile() and inside(path, roots):
                digests[path] = _stream_digest(packed.extractfile(member))
    parts = {path: digest for path, digest in digests.items() if inside(path, metric_files)}
    if isinstance(metric_entry, str) and metric_code:
        code = metric_code.encode("utf-8")
        parts[posixpath.normpath(metric_entry.rsplit(":", 1)[0])] = (len(code), hashlib.sha256(code).hexdigest())
    if metric_setup is not None:
        command = metric_setup.encode("utf-8")
        parts[_METRIC_SETUP_PART] = (len(command), hashlib.sha256(command).hexdigest())
    if metric_image is not None:
        digest = metric_image.encode("utf-8")
        parts[_METRIC_IMAGE_PART] = (len(digest), hashlib.sha256(digest).hexdigest())
    metric = hashlib.sha256()
    for path in sorted(parts):
        metric.update(_framed(path, *parts[path]))
    from_commit = bool(metric_files) and not any(inside(path, metric_files) for path in drafts)
    if cases_file is not None:
        cases_sha256 = digests[cases_file][1] if cases_file in digests else hashlib.sha256(b"").hexdigest()
        cases_source = "file"
    elif cases:
        canonical = json.dumps(cases, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        cases_sha256, cases_source = hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "request"
    else:
        cases_sha256, cases_source = None, None
    facts: dict[str, Any] = {
        "contract_sha256": contract_sha256,
        "metric_sha256": metric.hexdigest(),
        "metric_source": "repository" if from_commit else "request",
        "cases_sha256": cases_sha256,
        "cases_source": cases_source,
    }
    if metric_setup is not None:
        facts["metric_setup"] = metric_setup
    return facts


def _finite_number(value: Any) -> bool:
    """Check that a guest value is a number the record block can format.

    A ``bool`` is not a number here. An ``int`` past the float range fails,
    as its ``g`` format raises ``OverflowError``.

    Args:
        value: Any value from the guest's ``details``.

    Returns:
        True for a finite ``float``, or an ``int`` inside the float range.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return abs(value) <= sys.float_info.max if isinstance(value, int) else math.isfinite(value)


def _guest_record_parts_ok(split: Any, outcome: Any) -> bool:
    """Check the split and verdict shapes the record and its pull request block read.

    The guest writes both into ``details``, so a value of another shape must
    skip the record instead of failing a finished run. The rule follows the
    shapes Skynet's own guest writes: the split has a ``str`` rule, an ``int``
    seed and ``int`` positions; a verdict with ``target_reached`` has a finite
    ``target``; each failed requirement has a ``str`` name and reason, a
    ``direction`` of ``"maximize"`` or ``"minimize"``, a finite limit, and a
    finite or ``None`` value. A number never means a ``bool``.

    Args:
        split: ``details["case_split"]``, or ``None`` for a run with no cases.
        outcome: ``details["outcome"]``, or an empty dict when it is absent.

    Returns:
        True when :func:`add_run_record` and :func:`_record_block` can read both.
    """
    if split is not None and not (
        isinstance(split, dict)
        and isinstance(split.get("rule"), str)
        and isinstance(split.get("seed"), int)
        and not isinstance(split.get("seed"), bool)
        and all(
            isinstance(split.get(name), list)
            and all(isinstance(position, int) and not isinstance(position, bool) for position in split[name])
            for name in ("search", "validation", "held_out")
        )
    ):
        return False
    if not isinstance(outcome, dict):
        return False
    if any(key in outcome and not isinstance(outcome[key], bool) for key in ("target_reached", "requirements_passed")):
        return False
    if "target_reached" in outcome and not _finite_number(outcome.get("target")):
        return False
    failed = outcome.get("failed_requirements", [])
    return isinstance(failed, list) and all(
        isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("reason"), str)
        and entry.get("direction") in ("maximize", "minimize")
        and _finite_number(entry.get("limit"))
        and (entry.get("value") is None or _finite_number(entry.get("value")))
        for entry in failed
    )


def add_run_record(
    result: dict[str, Any], payload: dict[str, Any], staged: StagedRepository, *, image: str | None
) -> None:
    """Write the run record into ``result["details"]["record"]`` at the run end (SKY-694).

    The record lets another person check the score: ``record_version``, the
    pinned commit, the SHA-256 of the reported patch (null when that patch is
    empty), the baseline and final scores at full precision (null when the
    run has none), the hashes and the metric source from staging
    (:func:`_record_facts`), the seed, the split the guest made, the pinned
    sandbox image, and the ``[outcome]`` verdict keys
    with the same absent-key rule as ``details["outcome"]``. ``contract_met``
    is true when no rule failed, also when the file sets no target and no
    limit. A run with user box images also gets ``boxes``, each box's digest,
    source, input hash and build log reference, and ``record_version`` 2
    (SKY-737). A run with no ``ascent.toml``, a result with no ``details``, a
    run with cases whose guest wrote no split positions (an older image), or a
    guest split or verdict of another shape gets no record. A verdict whose
    keys do not match the file's own rules in the staged snapshot also gets
    no record: ``target_reached`` exactly when the file sets a target, and
    ``requirements_passed`` exactly when it sets limits, with
    ``failed_requirements`` only then. A snapshot with no ``outcome`` (an older
    payload) sets no rule. A ``record`` the guest wrote is removed on every
    path. The record holds no secret, no case content and no metric code.

    Args:
        result: The run's final result; changed in place.
        payload: The staged run payload, with the cases the guest received.
        staged: The staged tree, which keeps the record facts.
        image: The sandbox image pinned for the run, or ``None``.
    """
    details = result.get("details")
    if not isinstance(details, dict):
        return
    # Only the worker writes the record; a guest copy would pass for Skynet's own.
    details.pop("record", None)
    if not staged.constrained or not staged.record_facts:
        return
    split = details.get(CASE_SPLIT_KEY) if payload.get("cases") else None
    if payload.get("cases") and not isinstance(split, dict):
        return
    outcome = details.get("outcome") or {}
    if not _guest_record_parts_ok(split, outcome):
        return
    # The verdict crosses the guest's result file, so its keys must match the file's own rules.
    rules = (payload.get(REPO_SNAPSHOT_KEY) or {}).get("outcome") or {}
    has_target, has_limits = rules.get("target") is not None, bool(rules.get("require"))
    if (
        ("target_reached" in outcome) != has_target
        or ("requirements_passed" in outcome) != has_limits
        or ("failed_requirements" in outcome and not has_limits)
    ):
        return
    verdict = {
        key: outcome[key] for key in ("target_reached", "requirements_passed", "failed_requirements") if key in outcome
    }
    best = result.get("best_candidate")
    # Hash the change the run serves and publishes, which never holds a hidden-path edit (SKY-764).
    change = drop_paths(best, staged.hidden_paths) if isinstance(best, str) else ""
    baseline, final = result.get("baseline_score"), result.get("best_score")
    details["record"] = {
        # A record with user box images has the boxes key, which version 1 does not have (SKY-737).
        "record_version": 2 if staged.box_images else 1,
        "base_commit": payload["target"]["repo"]["commit"],
        "change_sha256": hashlib.sha256(change.encode("utf-8")).hexdigest() if change.strip() else None,
        "baseline_score": baseline if _finite_number(baseline) else None,
        "final_score": final if _finite_number(final) else None,
        **staged.record_facts,
        "seed": payload.get("seed"),
        "split": split,
        "image": image,
        **(
            {
                "boxes": {
                    role: {key: box.get(key) for key in ("digest", "source", "input_hash", "build_log")}
                    for role, box in sorted(staged.box_images.items())
                }
            }
            if staged.box_images
            else {}
        ),
        **verdict,
        "contract_met": verdict.get("target_reached", True) is True
        and verdict.get("requirements_passed", True) is True,
    }


# GitHub refuses a pull request body over 65,536 characters, and the split
# holds one position for each case, so a longer record stays out of the text.
_RECORD_JSON_LIMIT = 20_000


def _record_block(result: dict[str, Any]) -> str:
    """Write the pull request's run record block, or nothing for a run with no record.

    Args:
        result: Finished black-box result.

    Returns:
        Markdown that starts with one plain line on the contract, then the
        record fields, then the whole record as one JSON block in a collapsed
        "Record as JSON" section, or one line that points to ``get_run`` when
        the JSON is too long; an empty string when the result has no record.
    """
    record = (result.get("details") or {}).get("record")
    if not isinstance(record, dict):
        return ""
    if "target_reached" not in record and "requirements_passed" not in record:
        verdict = f"The {CONSTRAINTS_FILE} file sets no target and no limit."
    elif record.get("contract_met"):
        verdict = "This run met the contract."
    else:
        failed = []
        if record.get("target_reached") is False:
            target = ((result.get("details") or {}).get("outcome") or {}).get("target")
            score = result.get("best_score")
            reached = f"{score:.12g}" if _finite_number(score) else "no score"
            if _minimizes(result):
                failed.append(f"The final score {reached} is not at or below the target {target:.12g}.")
            else:
                failed.append(f"The final score {reached} did not reach the target {target:.12g}.")
        failed.extend(str(entry.get("reason")) for entry in record.get("failed_requirements") or [])
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
    setup = record.get("metric_setup")
    # The JSON escape keeps a backtick in the command inside the code span (SKY-740).
    setup_text = json.dumps(setup).replace("`", "\\u0060") if isinstance(setup, str) else None
    change = record.get("change_sha256")
    change_line = f"`{change}`" if change else "none, the reported patch is empty"
    fields = [
        f"- Record version: {record.get('record_version')}",
        f"- Base commit: `{record.get('base_commit')}`",
        f"- Change SHA-256: {change_line}",
        *(
            f"- {label} score: {'none' if record.get(key) is None else repr(record.get(key))}"
            for label, key in (("Baseline", "baseline_score"), ("Final", "final_score"))
        ),
        f"- Contract SHA-256: `{record.get('contract_sha256')}`",
        f"- Metric SHA-256: `{record.get('metric_sha256')}` (metric source: {record.get('metric_source')})",
        *([f"- Metric setup: `{setup_text}`"] if setup_text is not None else []),
        f"- Cases SHA-256: `{record.get('cases_sha256')}` (source: {record.get('cases_source')})",
        f"- Seed: {record.get('seed')}",
        f"- Split: {split_line}",
        f"- Sandbox image: `{record.get('image')}`",
        *(
            f"- [{role}] box image: `{box.get('digest')}` (source: {box.get('source')})"
            for role, box in (record.get("boxes") or {}).items()
            if isinstance(box, dict)
        ),
    ]
    # A guest reason can hold a code fence; the JSON escape keeps it inside the block and parses back the same.
    as_json = json.dumps(record, indent=2, ensure_ascii=False).replace("`", "\\u0060")
    if len(as_json) > _RECORD_JSON_LIMIT:
        collapsed = "The record is too long to print as JSON here. `get_run` returns it in `details.record`.\n\n"
    else:
        collapsed = f"<details>\n<summary>Record as JSON</summary>\n\n```json\n{as_json}\n```\n\n</details>\n\n"
    return f"{verdict}\n\nRun record:\n" + "\n".join(fields) + "\n\n" + collapsed


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
    if not _final_run_finished(result):
        baseline, optimized = _version_score(result, result.get("seed_candidate") or ""), _version_score(result, patch)
    lower = " (lower is better)" if _minimizes(result) else ""
    went = (
        f"Score went from {baseline:.4g} to {optimized:.4g}{lower}."
        if baseline is not None
        else f"The starting code has no score: it failed on at least one case. This version scored {optimized:.4g}{lower}."
    )
    files = sorted(set(patch_paths(patch)))
    listed = "\n".join(f"- `{path}`" for path in files[:50])
    more = f"\n- and {len(files) - 50} more" if len(files) > 50 else ""
    return (
        f"{APP_NAME} found this version while optimizing the repository with "
        f"{result.get('engine_used', 'an agent')}.\n\n"
        f"{went}\n\n"
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


# The scratch root holds files from the job's suite, so git must read no
# config, attributes or ignore files under it: a planted ``.gitconfig`` or
# ``.config/git/`` file would run code on the worker host through an alias,
# hook or fsmonitor (SKY-812). The calls make no commit, so git needs no
# identity from a config file.
_SCRATCH_GIT_ENV = {
    "HOME": "/nonexistent",
    "XDG_CONFIG_HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
}


def _git(cwd: Path, *arguments: str) -> str:
    """Run git in a scratch repository with no user or system config.

    Args:
        cwd: Repository root.
        *arguments: Arguments after ``git``.

    Returns:
        Standard output.
    """
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *SAFE_GIT, *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), **_SCRATCH_GIT_ENV},
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
        if _version_has_no_score(result, patch) or patch_violations(
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

"""Stage a repository target's tree and secrets in the trusted parent before its sandbox opens."""

from __future__ import annotations

import ast
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
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import run_log
from ..api.model_catalog import agent_model_id, catalog_snapshot, lowest_effort_from_low
from ..api.routers.package_registry import package_registry_for
from ..billing.budgets import BudgetError
from ..billing.host_agent import HOST_AGENT_TOKEN_KEY, _proposer_model_extra
from ..billing.model_gateway import EMBEDDING_ROUTE_KEY, MCP_RELAY_ROUTE_KEY, ROUTE_KEY
from ..billing.operation_pricing import ChargePolicy, UnpricedOperationError, image_build_usd, operation_quote
from ..billing.protected_credentials import (
    ProtectedCredentialVault,
    repo_secret_allowed_hosts,
    resolve_repo_secrets,
)
from ..billing.runtime import PaidResult, UsagePendingError
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
from ..exceptions import InfrastructureInterruptionError, ServiceError
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
from ..sandbox.errors import SandboxInfraError, SandboxTransientError, sandbox_api_error
from ..sandbox.image_build import (
    CODEBUILD_PLATFORM,
    BuildLimits,
    BuildRequest,
    BuildResult,
    CodeBuildImageBuilder,
    ImageBuildCancelledError,
    ImageBuilder,
    ImageBuildError,
    LocalDockerImageBuilder,
    codebuild_builder,
    compute_input_hash,
)
from ..sandbox.image_registry import ImageRegistry, ImageRegistryError, RegistryCredentials, image_owner, image_registry
from ..sandbox.providers.docker import in_repository, registry_host
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
from ..service_gateway.optimization.blackbox.repo_scorer import _SUPERVISOR_BOOT, RepoScorer
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
from ..service_gateway.optimization.blackbox.repo_workspace import (
    _HASH_PREPARED,
    _HASH_PROTECTED,
    _WRITE_ANSWER,
    BASE_PYTHON,
    RepoSetupError,
    RepoWorkspace,
)
from ..service_gateway.optimization.blackbox.rule_judge import (
    JUDGE_ANSWER_TOKENS,
    JUDGE_REASONING_TOKENS,
    RuleJudge,
    describe_model_call,
    run_log_emitter,
)
from ..service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec, UserImage
from ..service_gateway.optimization.blackbox.sandbox_scorer import RUNNER_SOURCE, ScorerGateway, scorer_gateway
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
    # Each box's start access to its image, by role: the reference, the owner's
    # repository and fresh pull credentials. Never pinned, as it holds a token source (SKY-891).
    box_user_images: Mapping[str, UserImage] = field(default_factory=dict)

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
# ``docker image inspect`` reads only the local image store.
_INSPECT_TIMEOUT_SECONDS = 10.0
_CHECK_LIFETIME_SECONDS = 600.0
_CHECK_OUTPUT_CHARS = 4_000
USER_IMAGES_UNSUPPORTED = (
    "This deployment cannot start a box from a user image yet. Remove image and dockerfile"
    " from [run] and [metric] in ascent.toml to use the default image."
)
DOCKERFILE_UNSUPPORTED = (
    "This deployment cannot build a Dockerfile yet. Set [{role}] image to an image pinned by its digest instead."
)
OTHER_REPOSITORY_IMAGE = (
    "The [{role}] image '{image}' is in {app}'s registry, but not in your own image repository."
    " Use an image that {app} built from your Dockerfile, or an image from another registry."
)
COPIED_IMAGE_OUTSIDE_REPOSITORY = (
    "The [{role}] image '{image}' is not in your own image repository. This deployment starts each box"
    " from a copy of an image that {app} built from your Dockerfile, so it cannot start a public image or"
    " an image from another registry. Use dockerfile in [{role}] in ascent.toml."
)
ROOT_BOX_USER = (
    "The [{role}] image '{image}' runs the box as root (uid 0), or its check reported no box uid. A root box user"
    " could replace the Python and the standard library that {app} checks the protected files with. Set a non-root USER in the image."
)
ROOT_IMAGE_USER = (
    "The [{role}] image '{image}' sets no USER, or sets root (uid 0) as its USER, so its boxes run as root."
    " A root box user could replace the Python and the standard library that {app} checks the protected files with."
    " Set a non-root USER in the image."
)
NAMED_IMAGE_USER = (
    "The [{role}] image '{image}' sets its USER by name. {app} cannot confirm outside the box that a name"
    " is not root (uid 0), because the image's own /etc/passwd maps the name to a uid."
    " Set a numeric non-root USER in the image, for example `USER 1000` or `USER 1000:1000`."
)
UNREADABLE_IMAGE_USER = (
    "{app} could not read the USER of the [{role}] image '{image}', so it cannot confirm that its boxes do not run"
    " as root. Try again, or use an image that {app} built from your Dockerfile."
)


def _top_level_imports(sources: tuple[str, ...]) -> list[str]:
    """List the absolute modules that the module body of each source imports.

    Args:
        sources: Python sources that root runs in a candidate box.

    Returns:
        The sorted module names, without ``__future__``.
    """
    names = set()
    for source in sources:
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module != "__future__":
                names.add(node.module)
    return sorted(names)


# The candidate box runs root scripts with BASE_PYTHON and never falls back
# to PATH in a user image, so the box user must not be able to change any
# file that the interpreter loads. The check asks the interpreter itself:
# it runs it as root runs it, with ``-I -S``, imports the modules that the
# root scripts import, and prints a tagged line for each path that it uses:
# ``path`` for a ``sys.path`` entry, ``file`` for each file mapped into it or
# into its shell (``/proc/<pid>/maps``, so libpython, libc and any preloaded
# library wherever they are) and for the ELF loader that it, root's programs
# and root's tools name, ``opt`` for a loader file that may be missing, ``lib``
# for each loader folder (the default ones, those in ``/etc/ld.so.cache`` or
# ``/etc/ld-musl-*.path``, and the RPATH and RUNPATH of each mapped file and
# root program), ``absent`` for a path that must not exist
# (``/etc/ld.so.preload``, and the CPython build and venv landmarks that
# ``getpath`` looks for next to the interpreter), and ``dir`` for its prefix.
# Every regular file that the listing names must be readable by others: a
# root-only file could name a path, such as a preloaded library, that the
# probe never sees. Each library in a loader folder must be root's and
# readable by others for the same reason. The hash scripts start it with
# ``env -i OPENSSL_CONF=/dev/null``, so an image OpenSSL config loads no
# engine into root, and the supervisor with the box's own environment, so it
# runs both ways. ``w`` walks one path from ``/``: it
# prints ``stat`` (owner uid, octal mode, type, name) of ``/`` and of each
# part, follows each link hop by hop with plain ``readlink``, as ``namei`` and
# ``readlink -f`` hide the hops, and prints ``link <target>`` after it, or
# ``none <path>`` for the first part that does not exist. The probe runs as
# the box user, and a folder that it cannot search hides what is in it from
# ``[ -e ]``, but not from root, so ``w`` fails the probe instead of printing
# ``none`` below such a folder. ``_trusted_python``
# judges the listing. Below each ``sys.path`` folder, except
# ``site-packages`` and ``dist-packages``, which ``-S`` keeps out of
# ``sys.path``, ``find`` fails the probe on any entry that is not root's, that
# group or others can write, or that is a link. In the locale trees a
# root-owned link may stay, as Debian links ``coreutils.mo`` files there; the
# interpreter prints a ``file`` line for each one, so its target is checked. A ``pyvenv.cfg`` or ``._pth``
# file next to the interpreter would move ``sys.path``, so it fails the probe
# too. ``env`` lines name the variables of each run, without their values,
# for :func:`_root_variables`. A newline in a name or target could forge a listing line, so the probe
# stops at any byte outside printable ASCII. ``$(...)`` drops trailing
# newlines, so the ``x`` keeps them and the probe sees the target the kernel follows.
_MAX_LINK_HOPS = 40
# The programs that root runs by absolute path in a box: the Vercel root
# wrapper (``/bin/mkdir``, ``/usr/bin/timeout``, ``/bin/bash``), the hash
# scripts (``/usr/bin/env``), any shell that a root step starts (``/bin/sh``)
# and the upload's stage removal (``/bin/rm``). ``/usr/bin/setpriv`` starts
# every box-user command with no new privileges: a box user who could replace
# it would run the next one free to use the image's sudo (SKY-902, SKY-907).
_ROOT_PROGRAMS = (
    "/usr/bin/env",
    "/bin/bash",
    "/bin/sh",
    "/bin/mkdir",
    "/usr/bin/timeout",
    "/bin/rm",
    "/usr/bin/setpriv",
)
# Other paths that root uses in a box. The supervisor reads ``/etc/passwd``
# itself; ``/etc/nsswitch.conf`` names the NSS modules that glibc loads for
# any other lookup. ``/tmp`` holds the supervisor's run folder and
# the Vercel root wrapper's output, so it must be root's, sticky and 1777.
# Root runs ``/usr/bin/find`` in the upload's stage removal and the model
# mailbox cleanup when it exists, and falls back without it.
_ROOT_TMP = "/tmp"
_ROOT_FIND = "/usr/bin/find"
_OPTIONAL_ROOT_FILES = frozenset({"/etc/nsswitch.conf", _ROOT_FIND})
_ROOT_PATHS = (*_ROOT_PROGRAMS, "/etc/passwd", "/etc/nsswitch.conf", _ROOT_FIND, _ROOT_TMP)
# The PATH of every root script on Vercel (``vercel.ROOT_PATH``, which a test
# keeps equal). Its root output poll runs ``_ROOT_TOOLS`` by name every 0.5 s,
# so each folder must be out of the box user's reach. Root's lookup is not the
# box user's ``command -v``: root takes the first file with any x bit, and a
# dangling link can get a target later. So every ``folder/tool`` entry that
# exists or is a link must resolve to a root-owned regular file, and at least
# one folder must hold each tool. A missing folder may be missing. The probe
# prints ``tool`` or ``notool`` for each pair, so no pair goes unchecked, and
# each present folder must let others search it, or the box user's view of
# it would hide entries that root runs.
_ROOT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_ROOT_TOOLS = ("tail", "head", "base64", "tr")
# Vercel's init starts every ``sudo: true`` command as a bare ``sudo`` (SKY-912,
# real box). The command's own PATH did not change which sudo ran, and a sudo
# in /usr/local/bin did not win over /usr/bin/sudo. So Vercel runs a fixed path
# or looks sudo up on the image PATH. A box user who could replace that sudo
# would run every later root step, and get the run's variables. So the probe
# walks each ``sudo`` in a folder of ``_ROOT_PATH``, and each folder of the box
# PATH up to and with its first ``sudo``, which must be root's. Sudo reads
# these files and every entry in these folders as root, and its PAM steps read
# the PAM stack. Linux-PAM does not check who owns its files, so they must be
# root's too. Others must be able to list each folder so that no entry goes
# unchecked. Sudo itself checks the owner of any other file that sudoers
# includes, but not the folders on its way. Linux-PAM reads /etc/pam.conf only
# when /etc/pam.d is missing. Debian's Linux-PAM reads a service or @include
# file from /usr/lib/pam.d when /etc/pam.d does not have it, so an image could
# remove /etc/pam.d/sudo and put a box-owned one there (SKY-912 review).
# Its libpam also names /usr/lib/security, a module folder outside the loader
# folders; stock images do not have it, so it must be missing or root's.
# /etc/security may hold one level of folders, such as limits.d. Ubuntu's sudo
# PAM stack reads /etc/default/locale into the root environment. A box user
# who could change the account files could add itself to the sudo group or set
# a root password. Shadow files are often 0640 root:shadow or 0000, so the
# check does not need others to read them.
_SUDO_FILES = (
    "/etc/sudo.conf",
    "/etc/sudoers",
    "/etc/environment",
    "/etc/default/locale",
    "/etc/pam.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/shadow",
    "/etc/gshadow",
)
_SUDO_FOLDERS = ("/etc/sudoers.d", "/etc/pam.d", "/usr/lib/pam.d", "/usr/lib/security", "/etc/security")
_DEEP_FOLDER = "/etc/security"
_SUDO_DONE = "sudo checked"
_ROOT_SCRIPTS = (_HASH_PROTECTED, _HASH_PREPARED, _WRITE_ANSWER, _SUPERVISOR_BOOT, RUNNER_SOURCE)
_PATH_SCRIPT = (
    r"""import os
names = [n for n in os.environ if not (n == "OPENSSL_CONF" and os.environ[n] == "/dev/null")]
os.environ["OPENSSL_CONF"] = "/dev/null"
import """
    + ", ".join(_top_level_imports(_ROOT_SCRIPTS))
    + r"""
import locale
PAM_ENV_CONF = "/etc/security/pam_env.conf"
PAM_ENV_FILES = ("/etc/environment", "/etc/default/locale", PAM_ENV_CONF)
# Newer Linux-PAM builds also read drop-in folders and vendor copies. The check does not parse them, so it refuses them.
PAM_ENV_EXTRA = (
    "/etc/security/pam_env.conf.d",
    "/usr/etc/security/pam_env.conf",
    "/usr/etc/security/pam_env.conf.d",
    "/usr/etc/environment",
    "/usr/lib/security/pam_env.conf",
    "/usr/lib/security/pam_env.conf.d",
    "/usr/lib/environment",
    "/etc/environment.d",
    "/usr/etc/environment.d",
    "/usr/lib/environment.d",
)
PAM_STACKS = ("/etc/pam.d", "/usr/lib/pam.d")
PAM_PATHS = PAM_ENV_FILES + ("/run/motd.dynamic",)
PAM_RUNNERS = ("pam_exec", "pam_script", "pam_python")
if sys.prefix != sys.base_prefix:
    sys.exit("the interpreter runs in a virtual environment")
for name in names:
    if not (name.isascii() and name.isprintable()):
        sys.exit("an environment variable name is not printable ASCII")
    print("env", name)
    if name in ("LANG", "LANGUAGE") or name.startswith("LC_"):
        value = os.environ[name]
        if not (value.isascii() and value.isprintable()):
            sys.exit("a locale value is not printable ASCII")
        print("locale", name + "=" + value)
for extra in PAM_ENV_EXTRA:
    try:
        os.lstat(extra)
    except (FileNotFoundError, NotADirectoryError):
        continue
    except OSError:
        # A folder that the box user cannot search could hide a file that root's PAM reads.
        sys.exit("the check cannot look for " + extra)
    sys.exit("the image has " + extra + ", which the check does not read")
for file in PAM_ENV_FILES:
    try:
        with open(file, encoding="latin-1") as text:
            rows = text.read().split("\n")
    except FileNotFoundError:
        continue
    for row in rows:
        row = row.strip()
        if not row or row.startswith("#"):
            continue
        if row.endswith("\\"):
            # pam_env joins a line that ends in a backslash with the next line, so a name could span two rows.
            sys.exit("a PAM environment file continues a line")
        if file == PAM_ENV_CONF:
            # Its values can expand other variables, so a locale set there counts as unsafe.
            name, value = row.split()[0], "?"
        else:
            name, _, value = (row[7:] if row.startswith("export ") else row).partition("=")
            name, value = name.strip(), value.replace('"', "").replace("'", "")
        if not (name and all(c == "_" or (c.isascii() and c.isalnum()) for c in name)):
            sys.exit("a PAM environment variable name is not letters, digits and underscores")
        print("env", name)
        if name in ("LANG", "LANGUAGE") or name.startswith("LC_"):
            if not (value.isascii() and value.isprintable()):
                sys.exit("a PAM locale value is not printable ASCII")
            print("locale", name + "=" + value)
pam_configs = ["/etc/pam.conf"]
for stack in PAM_STACKS:
    try:
        pam_configs += [os.path.join(stack, service) for service in sorted(os.listdir(stack))]
    except FileNotFoundError:
        continue
for config in pam_configs:
    try:
        with open(config, encoding="latin-1") as text:
            rows = text.read().replace("\\\n", " ").split("\n")
    except (FileNotFoundError, IsADirectoryError):
        continue
    for row in rows:
        for word in row.split("#")[0].split():
            # Linux-PAM drops the brackets around an argument with spaces, so the check compares the text inside.
            bare = word.strip("[]")
            key, _, value = bare.partition("=")
            # These modules run a program that an argument names, and a relative name resolves in sudo's folder.
            if any(runner in word for runner in PAM_RUNNERS):
                sys.exit("the PAM configuration " + config + " runs " + word)
            # The check does not read another envfile or conffile; user_readenv reads the box user's own file.
            # Another module name can load pam_env, so the check reads these keys on every line.
            if (
                (key == "envfile" and value not in PAM_ENV_FILES)
                or (key == "conffile" and value != PAM_ENV_CONF)
                or (key == "user_readenv" and value != "0")
            ):
                sys.exit("the PAM stack " + config + " gives pam_env " + word)
            # Linux-PAM opens a module, include or argument path as written, so only stock paths may appear.
            if "/" in bare and (bare if bare.startswith("/") else value) not in PAM_PATHS:
                sys.exit("the PAM configuration " + config + " names " + word)
for entry in sys.path:
    print("path", entry)
for tree in ("/usr/lib/locale", "/usr/share/locale"):
    print("path", tree)
    if not os.path.lexists(tree):
        continue
    for folder, subfolders, files in os.walk(tree, onerror=lambda error: sys.exit(str(error))):
        for name in subfolders + files:
            link = os.path.join(folder, name)
            if not (link.isascii() and link.isprintable()):
                sys.exit("a locale file name is not printable ASCII")
            if os.path.islink(link):
                print("file", link)
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    sys.exit("the image locale does not load")
seen = set()
for pid in ("self", str(os.getppid())):
    with open(f"/proc/{pid}/maps") as maps:
        for line in maps:
            name = line.rstrip("\n").split(None, 5)[5:]
            if name and name[0].startswith("/") and name[0] not in seen:
                seen.add(name[0])
                print("file", name[0])
with open(sys.executable, "rb") as elf:
    data = elf.read()
if data[:6] != b"\x7fELF\x02\x01":
    sys.exit("the interpreter is not a 64-bit little-endian ELF file")
start, size, count = (int.from_bytes(data[a:b], "little") for a, b in ((32, 40), (54, 56), (56, 58)))
for at in range(start, start + size * count, size):
    if int.from_bytes(data[at : at + 4], "little") == 3:
        offset, length = (int.from_bytes(data[a:b], "little") for a, b in ((at + 8, at + 16), (at + 32, at + 40)))
        print("file", data[offset : offset + length].rstrip(b"\0").decode())
try:
    with open("/etc/passwd", encoding="utf-8") as accounts:
        known = any(
            len(f) == 7 and f[0] and f[2] == str(os.getuid()) for f in (line.rstrip("\n").split(":") for line in accounts)
        )
except (OSError, UnicodeDecodeError):
    known = False
print("account", "ok" if known else "none", os.getuid())
print("absent", "/etc/ld.so.preload")
folders = {"/lib", "/usr/lib", "/lib64", "/usr/lib64", "/usr/local/lib"}
print("opt", "/etc/ld.so.cache")
if os.path.lexists("/etc/ld.so.cache"):
    with open("/etc/ld.so.cache", "rb") as cache:
        data = cache.read()
    at = data.find(b"glibc-ld.so.cache1.1")
    if at < 0:
        sys.exit("the loader cache has no glibc-ld.so.cache1.1 table")
    for entry in range(at + 48, at + 48 + 24 * int.from_bytes(data[at + 20 : at + 24], "little"), 24):
        name = at + int.from_bytes(data[entry + 8 : entry + 12], "little")
        folders.add(os.path.dirname(data[name : data.index(b"\0", name)].decode("ascii")))
for name in os.listdir("/etc"):
    if name.startswith("ld-musl-"):
        print("opt", "/etc/" + name)
        with open("/etc/" + name, encoding="ascii") as paths:
            folders.update(paths.read().replace(":", "\n").split())
"""
    + f"programs = {[*_ROOT_PROGRAMS, _ROOT_FIND, *(f'{b}/{t}' for b in _ROOT_PATH.split(':') for t in (*_ROOT_TOOLS, 'sudo'))]!r}"
    + r"""
for folder in os.environ.get("PATH", "").split(":"):
    if folder.startswith("/") and os.path.lexists(os.path.join(folder, "sudo")):
        programs.append(os.path.join(folder, "sudo"))
        break
sudos = {os.path.realpath(name) for name in programs if name.endswith("/sudo") and os.path.exists(name)}
for name in sorted(seen | {os.path.realpath(name) for name in programs if os.path.exists(name)}):
    with open(name, "rb") as elf:
        data = elf.read()
    if data[:4] != b"\x7fELF":
        # A script sudo names its own interpreter, which the scan would never walk.
        if name in sudos:
            sys.exit("a sudo is not an ELF file")
        continue
    if data[4:6] != b"\x02\x01":
        sys.exit("a mapped file is not a 64-bit little-endian ELF file")
    u = lambda a, b: int.from_bytes(data[a:b], "little")
    start, size, count = u(32, 40), u(54, 56), u(56, 58)
    heads = [at for at in range(start, start + size * count, size)]
    for at in heads:
        if u(at, at + 4) == 3:
            print("file", data[u(at + 8, at + 16) : u(at + 8, at + 16) + u(at + 32, at + 40)].rstrip(b"\0").decode())
    loads = [(u(at + 16, at + 24), u(at + 8, at + 16), u(at + 32, at + 40)) for at in heads if u(at, at + 4) == 1]
    strtab, found = None, []
    for at in heads:
        if u(at, at + 4) == 2:
            for item in range(u(at + 8, at + 16), u(at + 8, at + 16) + u(at + 32, at + 40), 16):
                tag = u(item, item + 8)
                if tag == 0:
                    break
                if tag == 5:
                    strtab = u(item + 8, item + 16)
                if tag in (15, 29):
                    found.append(u(item + 8, item + 16))
    if found:
        base = next(offset + strtab - address for address, offset, length in loads if address <= strtab < address + length)
        for value in found:
            for folder in data[base + value : data.index(b"\0", base + value)].decode("ascii").split(":"):
                folder = folder.replace("${ORIGIN}", "$ORIGIN").replace("$ORIGIN", os.path.dirname(name))
                if "$" in folder or not folder.startswith("/"):
                    sys.exit("a library search path is not an absolute folder")
                folders.add(folder)
def scan(folder, depth):
    for item in os.scandir(folder):
        info = item.stat(follow_symlinks=False)
        if item.is_symlink():
            if ".so" in item.name:
                if not os.path.exists(item.path):
                    sys.exit("a library link in a loader folder is dangling")
                links.append(item.path)
        elif item.is_dir(follow_symlinks=False):
            if item.name in ("site-packages", "dist-packages"):
                continue
            if info.st_uid or info.st_mode & 0o022 or info.st_mode & 0o005 != 0o005:
                sys.exit("a loader folder is not root's, or others cannot read it")
            if depth < 4:
                scan(item.path, depth + 1)
        elif ".so" in item.name:
            if info.st_uid or info.st_mode & 0o022 or not info.st_mode & 0o004:
                sys.exit("a library in a loader folder is not root's, or others cannot read it")
            checked.add(item.path)
if not all(folder.startswith("/") for folder in folders):
    sys.exit("a loader folder is not absolute")
links, checked = [], set()
for folder in sorted({os.path.realpath(folder) for folder in folders if os.path.isdir(folder)}):
    scan(folder, 1)
for target in sorted({os.path.realpath(link) for link in links} - checked):
    print("file", target)
for folder in sorted(folders):
    print("lib", folder)
version = f"python{sys.version_info[0]}.{sys.version_info[1]}" + ("t" if "t" in sys.abiflags else "")
packed = f"python{sys.version_info[0]}{sys.version_info[1]}" + ("t" if "t" in sys.abiflags else "") + ".zip"
for start in sorted({os.path.dirname(sys.executable), os.path.dirname(os.path.realpath(sys.executable))}):
    for name in ("pybuilddir.txt", "Modules/Setup.local", "pyvenv.cfg"):
        print("absent", start + "/" + name)
    print("absent", os.path.dirname(start).rstrip("/") + "/pyvenv.cfg")
    for landmarks, top in (
        ((f"{sys.platlibdir}/{version}/os.py", f"{sys.platlibdir}/{version}/os.pyc"), sys.prefix),
        ((f"{sys.platlibdir}/{packed}",), sys.prefix),
        ((f"{sys.platlibdir}/{version}/lib-dynload",), sys.exec_prefix),
    ):
        here = start
        while not (here == top and any(os.path.lexists(here.rstrip("/") + "/" + name) for name in landmarks)):
            for name in landmarks:
                print("absent", here.rstrip("/") + "/" + name)
            if here == "/":
                break
            here = os.path.dirname(here)
print("dir", sys.prefix)
print("dir", sys.exec_prefix)
"""
)
_TRUSTED_PYTHON_PROBE = (
    # The box run gets the image's own LC_ALL back, so it maps the locale files that root's runs map.
    f"g=${{LC_ALL+1}}; h=${{LC_ALL-}}; export LC_ALL=C; f='%u %a %F %n'; s={shlex.quote(BASE_PYTHON)}; y={shlex.quote(_PATH_SCRIPT)}\n"
    "w() {\n"
    "  a=; r=${1#/}; n=0; z=\n"
    '  stat -c "$f" / || exit 1\n'
    '  while [ -n "$r" ]; do\n'
    "    c=${r%%/*}\n"
    "    case $r in */*) r=${r#*/};; *) r=;; esac\n"
    "    case $c in ''|.) continue;; ..) a=${a%/*}; continue;; esac\n"
    '    if [ ! -e "$a/$c" ] && [ ! -h "$a/$c" ]; then [ -x "${a:-/}" ] || exit 1; printf \'none %s\\n\' "$a/$c"; z=1; return 0; fi\n'
    '    stat -c "$f" "$a/$c" || exit 1\n'
    '    if [ -h "$a/$c" ]; then\n'
    f"      n=$((n+1)); [ $n -le {_MAX_LINK_HOPS} ] || exit 1\n"
    '      l=$(readlink "$a/$c" && echo x) || exit 1\n'
    "      l=${l%?x}\n"
    "      case $l in *[![:print:]]*|'') exit 1;; esac\n"
    "      printf 'link %s\\n' \"$l\"\n"
    '      case $l in /*) a=; r=${l#/}${r:+/$r}; stat -c "$f" / || exit 1;; *) r=$l${r:+/$r};; esac\n'
    "    else a=$a/$c; fi\n"
    "  done\n"
    "}\n"
    'w "$s"\n'
    '[ -z "$z" ] || exit 1\n'
    'for d in "${s%/*}" "${a%/*}"; do\n'
    '  [ -r "$d" ] && [ -x "$d" ] || exit 1\n'
    '  for e in "$d/pyvenv.cfg" "${d%/*}/pyvenv.cfg" "$d"/*._pth; do\n'
    '    if [ -e "$e" ] || [ -h "$e" ]; then exit 1; fi\n'
    "  done\n"
    "done\n"
    f'for b in {" ".join(_ROOT_PATHS)}; do printf \'file %s\\n\' "$b"; w "$b"; done\n'
    f'for b in {_ROOT_PATH.replace(":", " ")}; do printf \'bin %s\\n\' "$b"; w "$b"; if [ -e "$b" ] && [ ! -x "$b" ]; then exit 1; fi; done\n'
    f"for b in {_ROOT_PATH.replace(':', ' ')}; do for t in {' '.join(_ROOT_TOOLS)}; do\n"
    '  if [ -e "$b/$t" ] || [ -h "$b/$t" ]; then printf \'tool %s\\n\' "$b/$t"; w "$b/$t"; else printf \'notool %s\\n\' "$b/$t"; fi\n'
    "done; done\n"
    f"for b in {_ROOT_PATH.replace(':', ' ')}; do\n"
    '  if [ -e "$b/sudo" ] || [ -h "$b/sudo" ]; then printf \'sudo %s\\n\' "$b/sudo"; w "$b/sudo"; else printf \'nosudo %s\\n\' "$b/sudo"; fi\n'
    "done\n"
    # Vercel may look sudo up on the image PATH, which the box commands inherit.
    "p=$PATH:\n"
    'while [ -n "$p" ]; do\n'
    "  b=${p%%:*}; p=${p#*:}\n"
    "  case $b in /*) ;; *) exit 1;; esac\n"
    "  case $b in *[![:print:]]*) exit 1;; esac\n"
    '  printf \'pathdir %s\\n\' "$b"; w "$b"\n'
    '  if [ -e "$b" ] && [ ! -x "$b" ]; then exit 1; fi\n'
    # The mode alone does not show that the box user can run it: a group entry or an ACL may stop the box user.
    '  if [ -e "$b/sudo" ] || [ -h "$b/sudo" ]; then printf \'sudo %s\\n\' "$b/sudo"; w "$b/sudo"; [ -x "$b/sudo" ] || exit 1; break; fi\n'
    "done\n"
    "k() {\n"
    '  [ -r "$1" ] && [ -x "$1" ] || exit 1\n'
    '  for e in "$1"/* "$1"/.[!.]* "$1"/..?*; do\n'
    '    if [ -e "$e" ] || [ -h "$e" ]; then\n'
    "      case $e in *[![:print:]]*) exit 1;; esac\n"
    '      printf \'entry %s\\n\' "$e"; w "$e"\n'
    f'      if [ "$1" = {_DEEP_FOLDER} ] && [ -d "$e" ]; then k "$e"; fi\n'
    "    fi\n"
    "  done\n"
    "}\n"
    f'for b in {" ".join(_SUDO_FILES)}; do printf \'conf %s\\n\' "$b"; w "$b"; done\n'
    f'for b in {" ".join(_SUDO_FOLDERS)}; do printf \'conf %s\\n\' "$b"; w "$b"; if [ -d "$b" ]; then k "$b"; fi; done\n'
    f"printf '%s\\n' '{_SUDO_DONE}'\n"
    "for m in clean box; do\n"
    "  printf 'run %s\\n' $m\n"
    '  if [ $m = clean ]; then o=$(/usr/bin/env -i OPENSSL_CONF=/dev/null "$s" -I -S -c "$y") || exit 1\n'
    '  elif [ -n "$g" ]; then o=$(LC_ALL="$h" "$s" -I -S -c "$y") || exit 1\n'
    '  else o=$(/usr/bin/env -u LC_ALL "$s" -I -S -c "$y") || exit 1; fi\n'
    "  printf '%s\\n' \"$o\" | while IFS= read -r v; do\n"
    "    case $v in *[![:print:]]*) exit 1;; 'env '?*|'locale '?*|'account '?*|'path /'*|'file /'*|'dir /'*|'opt /'*|'lib /'*|'absent /'*) ;; *) exit 1;; esac\n"
    '    printf \'%s\\n\' "$v"; t=${v%% *}; case $t in env|locale|account) continue;; esac; w "${v#* }"\n'
    '    if [ "$t" = path ] && [ -z "$z" ] && [ -d "$a" ] && [ ! -h "$a" ]; then\n'
    "      case ${v#* } in /usr/lib/locale|/usr/share/locale)\n"
    '        q=$(find "$a" \\( ! -user 0 -o ! -type l \\( -perm -020 -o -perm -002 \\) \\) -print) || exit 1;;\n'
    '      *) q=$(find "$a" \\( -path "$a/site-packages" -o -path "$a/dist-packages" \\) -prune'
    " -o \\( ! -user 0 -o -perm -020 -o -perm -002 -o -type l \\) -print) || exit 1;; esac\n"
    '      [ -z "$q" ] || exit 1\n'
    "    fi\n"
    "  done || exit 1\n"
    "done"
)
# An absolute path of printable ASCII only, as the probe allows.
_PRINTABLE_PATH = re.compile(r"/[\x20-\x7e]*")
_ACCOUNT_OK = re.compile(r"ok [0-9]+")
_ACCOUNT_NONE = re.compile(r"account none ([0-9]+)")
# Only ASCII digits: ``str.isdigit`` would also take digits such as "²".
_BOX_UID = re.compile(r"account ok ([0-9]+)")
# An image USER as ``<uid>`` or ``<uid>:<gid>``, in ASCII digits only.
_NUMERIC_USER = re.compile(r"[0-9]+(?::[0-9]+)?")
# The metric role runs no trusted-Python probe, so this gives its box uid (SKY-891).
_BOX_UID_PROBE = 'import os; print("account ok", os.getuid())'
# Each role's boxes call these by name on the box user's PATH, to unpack, save
# and score each candidate, to copy, start and stop its Compose services, and
# to install and save the metric files (SKY-903).
_BOX_TOOLS = {
    "run": (
        "env", "git", "tar", "gzip", "split", "base64", "cat", "ls", "cp", "mv", "rm", "mkdir", "find",
        "grep", "sed", "mktemp", "chmod", "bash", "setsid", "nohup", "seq", "sleep",
    ),
    "metric": ("tar", "gzip", "split", "base64", "cat", "ls", "rm", "mkdir", "find", "xargs", "touch"),
}  # fmt: skip
_BOX_TOOLS_PURPOSE = {
    "run": "to unpack, save and score each version, and to copy, start and stop its Compose services",
    "metric": "to install and save the metric files",
}
_TOOL_MISSING = 'command -v "$tool" >/dev/null 2>&1 || printf \'%s\\n\' "$tool"'
# Every Vercel file upload runs these by absolute path, as root or as the box
# user, in both roles (SKY-902). A PATH lookup that finds ``cat`` only in
# /usr/bin would pass an image whose every upload then exits 127 (SKY-907).
# The upload ignores a failed ``/bin/rmdir``, so it is not here.
#
# This probe checks only that each one is a file the box user can run. The
# ownership rule of ``_ROOT_PROGRAMS`` applies only to the run role. The metric
# box user runs only the owner's metric, and root in the offline metric box
# reaches nothing more than that user: the user already writes the score and
# makes the metered model calls. So a box-writable setpriv risks only that box.
#
# On Vercel, setpriv, timeout and bash also start every box-user command, so a
# missing one fails the first check command before this probe runs. Vercel
# refuses the command when setpriv is missing, and setpriv exits 127 when
# timeout or bash is missing. Docker and Apple Container do not use these
# paths, so there the probe names the missing one. So an image whose six
# paths pass locally also has them on Vercel. Its sudo rule shows only there.
_UPLOAD_PROGRAMS = (
    "/usr/bin/setpriv",
    "/usr/bin/timeout",
    "/bin/bash",
    "/bin/cat",
    "/bin/rm",
    "/bin/mkdir",
)
# The last line proves all three loops ran to their end.
_BOX_TOOLS_DONE = "tools checked"
# Vercel reads a command's output as it runs with ``_ROOT_TOOLS``, found only on ``_ROOT_PATH``.
_BOX_TOOLS_PROBE = {
    role: f"for tool in {' '.join(tools)}; do {_TOOL_MISSING}; done;"
    f" (PATH={_ROOT_PATH}; for tool in {' '.join(_ROOT_TOOLS)}; do {_TOOL_MISSING}; done);"
    f' for tool in {" ".join(_UPLOAD_PROGRAMS)}; do [ -f "$tool" ] && [ -x "$tool" ] || printf \'%s\\n\' "$tool"; done;'
    f" echo {shlex.quote(_BOX_TOOLS_DONE)}"
    for role, tools in _BOX_TOOLS.items()
}
# Root commands start through the provider: on Vercel through the image's sudo.
# Docker and Apple Container reach root with no sudo, so there the probe proves less. A
# run's supervisor gets the run's variables as root, so the run probe checks that one
# stays. The metric box runs only the upload's root steps, which pass none (SKY-902),
# so its probe checks only that the command ran as root.
_ROOT_VARIABLE = "APP_ROOT_PROBE"
# Bash builtins only, so the probe needs no program that a real run does not use.
_ROOT_PROBE = f'printf \'%s\\n\' "$EUID" "${_ROOT_VARIABLE}"'
_ROOT_PROBE_OUTPUT = ["0", "kept"]
# These say nothing about the image, so the root probe lets them stop the run.
_NOT_IMAGE_ERRORS = (
    BudgetError,
    UsagePendingError,
    UnpricedOperationError,
    SandboxInfraError,
    SandboxTransientError,
    InfrastructureInterruptionError,
)
# Both boxes keep their own folders in the working folder, as the box user.
_WORKDIR_DONE = "working folder writable"
_WORKDIR_PROBE = f'pwd; d=".app-write-check-$$"; mkdir "$d" && rm -r "$d" && echo {shlex.quote(_WORKDIR_DONE)}'
_FILE_TYPES = frozenset({"directory", "symbolic link", "regular file"})
# The file types that each tagged path may have; ``path``, ``opt`` and ``lib`` may also be missing.
# An ``absent`` path is a CPython build or venv landmark, and it must be missing.
_ENTRY_KINDS = {
    "path": frozenset({"directory", "regular file"}),
    "file": frozenset({"regular file"}),
    "opt": frozenset({"regular file"}),
    "dir": frozenset({"directory"}),
    "lib": frozenset({"directory"}),
    "absent": frozenset(),
}
_MAY_BE_MISSING = frozenset({"path", "opt", "lib", "absent"})
_VARIABLE_NAME = re.compile(r"[\x20-\x7e]+")
# Root runs ``/usr/bin/env``, ``/bin/bash --noprofile --norc -c`` and the
# supervisor with the box's environment, which holds the image ENV. The
# loader, the shell and libc read these names before ``env -i`` can clear
# anything, as no root process here is setuid (SKY-737). Root steps that start
# through the image's sudo also get the variables that pam_env reads from
# /etc/environment, /etc/default/locale and /etc/security/pam_env.conf
# (Ubuntu's sudo PAM stack), so the probe lists those names too (SKY-912).
# Each group:
# - ``LD_*``: the glibc and musl loader search paths, preloads and audit
#   libraries. An empty ``LD_LIBRARY_PATH`` element means the work folder.
# - ``GCONV_PATH``, ``LOCPATH``, ``GLIBC_TUNABLES``: iconv modules, locale
#   files, and how the loader picks libraries and how malloc works.
# - ``OPENSSL_*``: the root supervisor imports ``ssl``, and OpenSSL loads
#   its config, providers and engines from these paths.
# - ``BASH_ENV``, ``ENV``: a file that the shell sources at its start.
# - ``BASH_FUNC_*``: exported functions (``BASH_FUNC_name%%``), which win
#   over builtins such as ``cd``.
# - ``SHELLOPTS``, ``BASHOPTS``, ``POSIXLY_CORRECT``, ``BASH_COMPAT``: shell
#   options, for example ``xtrace``, which expands ``PS4``.
# - ``PS4``, ``BASH_XTRACEFD``: the trace prompt runs command substitutions,
#   and the trace goes to any open file.
# - ``CDPATH``, ``GLOBIGNORE``, ``EXECIGNORE``, ``IFS``,
#   ``BASH_LOADABLES_PATH``: which folder ``cd`` picks, which files a glob
#   or a command search takes, how words split, and where ``enable -f``
#   loads builtins from. The root ``cd`` targets are absolute, so
#   ``CDPATH`` is refused only to be safe.
# - ``TMPDIR``, ``TEMP``, ``TMP``: Python's ``tempfile`` reads them even
#   under ``-I``, so they could put a root folder where the box user writes.
# - ``LANG``, ``LANGUAGE``, ``LC_*``: refused only when the value is not C,
#   POSIX or a UTF-8 locale. Another codeset makes glibc load ``gconv``
#   modules, and a value with ``/`` names a locale folder (``_locale_ok``).
# ``MALLOC_*``, ``TZDIR`` and ``HOSTALIASES`` load no code, and the
# interactive-only names (``PROMPT_COMMAND``, ``INPUTRC``, ``HISTFILE``) are
# never read by ``bash -c``, so they stay allowed.
_ROOT_PREFIXES = ("LD_", "OPENSSL_", "BASH_FUNC_")
_ROOT_VARIABLES = frozenset(
    {
        "GCONV_PATH",
        "LOCPATH",
        "GLIBC_TUNABLES",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "POSIXLY_CORRECT",
        "BASH_COMPAT",
        "PS4",
        "BASH_XTRACEFD",
        "CDPATH",
        "GLOBIGNORE",
        "EXECIGNORE",
        "IFS",
        "BASH_LOADABLES_PATH",
        "TMPDIR",
        "TEMP",
        "TMP",
    }
)
# A UTF-8 locale name, with no ``/``, so glibc reads it from its own locale files.
_UTF8_LOCALE = re.compile(r"[A-Za-z][A-Za-z0-9_]*\.(?:utf-8|utf8)", re.IGNORECASE)
ROOT_ENVIRONMENT = (
    "The [run] image '{image}' sets {names} in its environment, /etc/environment, /etc/default/locale or"
    " /etc/security/pam_env.conf. {app} runs its root steps in the box with the image environment and, through"
    " sudo, with those files. These variables change which code the shell, the system loader or the C library"
    " runs, or which files and folders they use. Remove them from the image ENV and from those files: every"
    " LD_*, OPENSSL_* and BASH_FUNC_* variable, and {variables}. LANG, LANGUAGE and LC_* may stay when they"
    " name C, POSIX or a UTF-8 locale, such as C.UTF-8 or en_US.UTF-8. Images that need LD_LIBRARY_PATH, such"
    " as CUDA images, are not supported yet."
)
_RUN_MODES = ("clean", "box")
_MISSING = "missing"
UNTRUSTED_PYTHON = (
    "The [run] image '{image}' has no trusted Python at {python}. {app} hashes the protected files with it."
    " The image must hold it as an executable regular file, or as a chain of links to one, outside a"
    " virtual environment. Every program that root, or the start of a box-user command, runs by"
    " path ({programs}), /etc/passwd, plus /etc/nsswitch.conf and /usr/bin/find when present, every tail,"
    " head, base64 and tr in a folder of root's PATH ({path}), each folder of that PATH that exists, every"
    " file that Python loads, the locale trees, each folder on its import path and every file and folder"
    " below that path (except site-packages), and every folder from / down to each of them, must be owned by"
    " root and not writable by group or others. Others must be able to read each of those files,"
    " /etc/ld.so.cache and every library in the loader folders. /tmp must be a root-owned folder with mode 1777."
    " The same rule covers sudo: every sudo in a folder of root's PATH, the first sudo on the image PATH, each"
    " folder of the image PATH up to and with the folder that holds that sudo, /etc/sudo.conf, /etc/sudoers,"
    " /etc/environment, /etc/default/locale, /etc/pam.conf, /etc/passwd, /etc/group, /etc/shadow and"
    " /etc/gshadow when present, and /etc/sudoers.d, /etc/pam.d, /usr/lib/pam.d, /usr/lib/security and"
    " /etc/security when present, with every entry in them. Others must be able to read each sudo and search"
    " each of those image PATH folders. Others must be able to run the first sudo on the image PATH, and each"
    " sudo must resolve to an ELF file. Others must be able to list each of those five folders,"
    " for example with chmod 755 /etc/sudoers.d. Each entry in them must be a regular file, except one level of"
    " folders in /etc/security."
    " Links count only below /, outside the import path, and in the locale trees when they resolve to such a"
    " file. The image must have no /etc/ld.so.preload, and no pybuilddir.txt, Modules/Setup.local or stray"
    " lib/pythonX.Y/os.py next to the Python. Each of those four tools must be in at least one"
    " of the folders, and no entry may be a dangling link."
)
NO_ACCOUNT = (
    "The [run] image '{image}' has no /etc/passwd line for the box user (uid {uid})."
    " {app}'s root supervisor finds the box user's name and home there. Add a line with seven fields for that uid."
)

MISSING_TOOLS = (
    "The [{role}] image '{image}' has no {missing}. {app} runs these programs by name on the box user's PATH"
    " {purpose}: {tools}. It also runs {poll} from {path} to read a command's output, and {upload} by these"
    " exact paths to start box commands and write files into the box. Install them in the image."
)
NO_ROOT = {
    "run": (
        "The [run] image '{image}' did not run a command as root with the run's variables. {app} runs every"
        " file upload's first and last steps, and the run's supervisor, as root, through the image's sudo on a"
        " hosted run. Install sudo, give the box user a NOPASSWD rule for all commands, keep the caller's"
        " variables, and set its secure_path to {path}."
    ),
    "metric": (
        "The [metric] image '{image}' did not run a command as root. {app} runs every file upload's first and"
        " last steps as root, through the image's sudo on a hosted run. Install sudo and give the box user a"
        " NOPASSWD rule for all commands."
    ),
}
# On Vercel, every box-user command starts with /usr/bin/setpriv, so its
# refusal can only mean that setpriv is missing; a missing ascent exits 127.
NO_SETPRIV = (
    "The [{role}] image '{image}' has no /usr/bin/setpriv, so the box could not start"
    " `ascent check --image --role {check_role}`. {app} starts every box command and every file upload with"
    " these exact paths, so the image must have them: {upload}."
)
# On Vercel, setpriv exits 127 when timeout or bash is missing.
MISSING_UPLOAD_HINT = (
    "\n\nThe image has no {missing}. Also check that the image has `ascent` (the openascent package)."
    " {app} starts every box command and every file upload with these exact paths, so the image must have"
    " them: {upload}."
)
NOT_FOUND_HINT = (
    "\n\nThe box found no program to run. Check that the image has `ascent`"
    " (the openascent package). {app} also starts every box command and every file upload with these"
    " exact paths, so the image must have them: {upload}."
)
WORKDIR_NOT_WRITABLE = (
    "The [{role}] image '{image}' starts in {folder}, and the box user cannot make a folder there. {app} keeps"
    " its files in the working folder. Set WORKDIR to a folder that the box user owns, for example"
    " `WORKDIR /home/box` after `USER`."
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
        Local Docker in local mode; on a cloud deployment the CodeBuild
        builder when its settings and the user image registry are set, else
        ``None``, so a Dockerfile there fails clearly (SKY-891).
    """
    if config.local_mode:
        return LocalDockerImageBuilder()
    return codebuild_builder(image_registry(config), config)


# A box start can take minutes after it reads the token, so a token with less left is renewed.
_CREDENTIALS_MARGIN = timedelta(minutes=15)


def _pull_access(registry: ImageRegistry, owner: str) -> Callable[[], tuple[str, str]]:
    """Return a callable that gives one token for the owner's repository while it has time left (SKY-891).

    The callable reuses one token while it has more than
    ``_CREDENTIALS_MARGIN`` left and then asks the registry for a new one,
    so the boxes of a run do not ask STS for a new token at each start.

    Args:
        registry: The user image registry.
        owner: The registry repository's owner.

    Returns:
        The callable, which returns the token's (username, password).
    """
    lock = threading.Lock()
    cached: list[RegistryCredentials] = []

    def credentials() -> tuple[str, str]:
        """Return a token that pulls only from the owner's repository and has time left."""
        # Boxes open on several threads, so the lock makes them share one token.
        with lock:
            token = cached[0] if cached else None
            if token is None or token.expires_at is None or token.expires_at - datetime.now(UTC) <= _CREDENTIALS_MARGIN:
                token = registry.pull_credentials(owner)
                cached[:] = [token]
            return token.username, token.password

    return credentials


def _user_image(
    registry: ImageRegistry | None,
    username: str,
    role: str,
    image: str,
    *,
    copies: bool = False,
    credentials: Callable[[], tuple[str, str]] | None = None,
) -> UserImage:
    """Give one box the access its start needs to its user image (SKY-891).

    Pull credentials reach only the owner's own repository: an image there
    gets a :func:`_pull_access` callable. An image from another registry
    gets none, and a provider that copies images cannot start it, because
    its copy needs the owner's repository.

    Args:
        registry: The user image registry, or ``None`` when local Docker keeps the images.
        username: The run's owner.
        role: ``run`` or ``metric``.
        image: The box image reference.
        copies: Whether the runtime starts each box from a copy of the image (Vercel).
        credentials: The run's shared pull access to the owner's repository, so each box
            of the run uses one token; ``None`` makes new access for this box only.

    Returns:
        The reference, with the owner's repository and its pull credentials for an image there.

    Raises:
        BoxImageError: The image is in the user image registry, but in another repository, or
            the runtime copies images and the image is not in the owner's repository.
    """
    outside = BoxImageError(COPIED_IMAGE_OUTSIDE_REPOSITORY.format(role=role, image=image, app=APP_NAME))
    if registry is None:
        if copies:
            raise outside
        return UserImage(image)
    owner = image_owner(username)
    repository = registry.repository_uri(owner)
    if not in_repository(image, repository):
        if registry_host(image) == registry_host(repository):
            raise BoxImageError(OTHER_REPOSITORY_IMAGE.format(role=role, image=image, app=APP_NAME))
        if copies:
            raise outside
        return UserImage(image)
    return UserImage(image, repository=repository, credentials=credentials or _pull_access(registry, owner))


def _run_pull_access(
    staged: StagedRepository, registry: ImageRegistry | None, username: str
) -> Callable[[], tuple[str, str]] | None:
    """Return the one pull access that every box of the run shares (SKY-891).

    Each role and each prepare of one run share one token, so the run asks
    STS for one token and not one for each role. A second prepare of the run (after a parallel start pinned first) keeps
    the access of the first, but only when it is for the same owner's repository.

    Args:
        staged: The staged tree, with the box access of an earlier prepare.
        registry: The user image registry, or ``None`` when local Docker keeps the images.
        username: The run's owner.

    Returns:
        The shared access, or ``None`` when there is no registry.
    """
    if registry is None:
        return None
    owner = image_owner(username)
    repository = registry.repository_uri(owner)
    for user_image in staged.box_user_images.values():
        if user_image.repository == repository and user_image.credentials is not None:
            return user_image.credentials
    return _pull_access(registry, owner)


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
    cancelled: Callable[[], bool] | None = None,
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
        registry: The user image registry: it must still hold a reused build, and it gives each box in
            the owner's repository its pull access. ``None`` skips the check and pulls with no credentials.
        pinned: The images the run pinned at its start, for a resume.
        cancelled: Tells whether the run was cancelled, so a running build stops before it pushes.

    Returns:
        ``staged`` with ``box_images``, the start access of each box in ``box_user_images``, and, for a
        metric image, record facts that cover its digest.

    Raises:
        BoxImageError: When the runtime cannot start a user image, an image is in another user's
            repository, a copying runtime gets an image outside the owner's repository, a build
            fails, or a check fails.
        ImageBuildCancelledError: When ``cancelled`` stopped a build.
    """
    if not staged.box_sources:
        return staged
    # Vercel copies each image into its own registry, keyed by the owner's repository, so it
    # starts no public image and no image from another registry (SKY-891).
    copies = bool(getattr(runtime, "copies_user_images", False))
    access = _run_pull_access(staged, registry, username)
    if pinned:
        images = {role: dict(record) for role, record in pinned.items()}
        # A resume checks nothing again, but its boxes still need the pull access of today's registry.
        user_images = {
            role: _user_image(registry, username, role, record["image"], copies=copies, credentials=access)
            for role, record in images.items()
        }
    else:
        # A runtime that ignores the spec's image would run Skynet's image in its place, with no sign.
        # A runtime that sets ``accepts_user_images`` starts the box as the image's USER, so
        # :func:`_check_box_image` refuses a root USER in each role (SKY-891).
        if not getattr(runtime, "accepts_user_images", False):
            raise BoxImageError(USER_IMAGES_UNSUPPORTED)
        images = {}
        # A refused image reference must stop the run before any paid build or check box.
        user_images = {
            role: _user_image(registry, username, role, value, copies=copies, credentials=access)
            for role, (kind, value) in staged.box_sources.items()
            if kind == "image"
        }
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
                    cancelled=cancelled,
                )
            if role not in user_images:
                user_images[role] = _user_image(
                    registry, username, role, record["image"], copies=copies, credentials=access
                )
            python = _check_box_image(
                runtime, user_images[role], role, owner_id=owner_id, registry=registry, username=username
            )
            if python is not None:
                record["python"] = python
            images[role] = record
    facts = staged.record_facts
    if "metric" in images and staged.record_inputs:
        facts = _record_facts(staged.snapshot.archive, **staged.record_inputs, metric_image=images["metric"]["digest"])
    return replace(staged, box_images=images, box_user_images=user_images, record_facts=facts)


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
    cancelled: Callable[[], bool] | None = None,
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
        cancelled: Tells whether the run was cancelled, or ``None``.

    Returns:
        The box's image record: image, digest, source, input hash and build log reference.

    Raises:
        BoxImageError: When there is no builder, the context is invalid, or the build fails.
        ImageBuildCancelledError: When ``cancelled`` stopped the build. If CodeBuild started the
            build, it keeps only the started record with its CodeBuild id, which account deletion
            checks (SKY-896).
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
    request = BuildRequest(context_dir=context, owner=image_owner(username), dockerfile=posixpath.basename(dockerfile))
    kind = builder.kind
    try:
        input_hash = compute_input_hash(
            request, builder=kind, platform=CODEBUILD_PLATFORM if kind == CodeBuildImageBuilder.kind else ""
        )
    except ImageBuildError as error:
        raise BoxImageError(f"[{role}] dockerfile '{dockerfile}': {error}") from error
    record = {"source": "dockerfile", "input_hash": input_hash}
    found = store.find_image_build(username, input_hash)
    if found is not None and _still_held(registry, kind, request.owner, found["image"]):
        return {
            "image": found["image"],
            "digest": _digest(found["image"]),
            **record,
            "build_log": f"image-build:{found['id']}",
        }
    limits = BuildLimits.from_settings()
    pending: list[str] = []

    def record_start(codebuild_id: str) -> None:
        """Store the started build's id before the wait, so account deletion can stop it (SKY-896).

        Args:
            codebuild_id: The CodeBuild build id.
        """
        pending.append(store.start_image_build(username, input_hash, builder=kind, codebuild_id=codebuild_id))

    def build() -> PaidResult[BuildResult | ImageBuildError]:
        """Run the build once and charge its started minutes, also for a failed build.

        Returns:
            The build result or its error, with the charge of its started minutes up
            to the reserved limit. A build that a cancel stopped before it started
            costs nothing.
        """
        started = time.monotonic()
        try:
            value: BuildResult | ImageBuildError = builder.build(request, cancelled=cancelled, on_start=record_start)
        except ImageBuildError as error:
            value = error
        seconds = time.monotonic() - started
        # The measured time also holds the upload, the queue, the log wait and a cancel's stop
        # wait, so it can pass the reserved limit; the settlement must stay inside the reservation.
        charged = min(seconds, limits.timeout_seconds)
        if isinstance(value, ImageBuildCancelledError) and not value.started:
            charged = 0.0
        return PaidResult(
            value=value,
            provider_usd=image_build_usd(kind, charged),
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
    # A cancelled build keeps its started row and id: the stop wait can end before the build does.
    if isinstance(outcome, ImageBuildCancelledError):
        raise outcome
    row_id = pending[-1] if pending else None
    if isinstance(outcome, ImageBuildError):
        store.save_image_build(
            username, input_hash, builder=kind, image=None, size_bytes=None, log=outcome.log, row_id=row_id
        )
        tail = outcome.log_tail.strip()
        raise BoxImageError(
            f"[{role}] dockerfile '{dockerfile}' did not build: {outcome}"
            + (f"\n\nBuild log tail:\n{tail}" if tail else "")
        )
    build_id = store.save_image_build(
        username,
        input_hash,
        builder=kind,
        image=outcome.image,
        size_bytes=outcome.size_bytes,
        log=outcome.log,
        row_id=row_id,
    )
    return {"image": outcome.image, "digest": outcome.digest, **record, "build_log": f"image-build:{build_id}"}


def _still_held(registry: ImageRegistry | None, builder: str, owner: str, image: str) -> bool:
    """Check that a reused build's image still exists where the builder puts it.

    Args:
        registry: The user image registry, or ``None`` when the builder keeps its images.
        builder: The kind of the builder in use.
        owner: The owner of the registry repository.
        image: The reused build's image.

    Returns:
        True when the registry holds the digest, or with no registry when
        local Docker still holds the image id. False for any other builder
        with no registry, because nothing can confirm its image.
    """
    if registry is not None:
        try:
            registry.check_digest(owner, _digest(image))
        except ImageRegistryError:
            return False
        return True
    # ``docker image prune`` removes a local build, and a run that reused
    # its id would then fail at every box start.
    return builder == LocalDockerImageBuilder.kind and _local_image_exists(image)


def _local_image_exists(image: str) -> bool:
    """Ask local Docker whether it still holds an image.

    Args:
        image: The image id or reference.

    Returns:
        Whether ``docker image inspect`` finds it.
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=_INSPECT_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _check_box_image(
    runtime: Any,
    user_image: UserImage,
    role: str,
    *,
    owner_id: str,
    registry: ImageRegistry | None,
    username: str,
) -> str | None:
    """Run ``ascent check --image`` in an offline box of the image.

    The run role also needs a trusted interpreter and standard library. Both
    roles need a box user that is not root, on every runtime: a root box user
    could change the checked files after the check (SKY-891). The image's
    own USER, read outside the box, must be a numeric non-root uid: the box
    runs the image's own interpreter, so the uid that the box reports is only
    a second check.

    Args:
        runtime: The parent's metered sandbox runtime.
        user_image: The box image to check, with the access its start needs.
        role: ``run`` or ``metric``.
        owner_id: Job identity naming the check operation.
        registry: The user image registry, which reads the USER of an image in the owner's repository.
        username: The run's owner.

    Returns:
        The trusted absolute Python path for the metric role; ``None`` for the run role.

    Raises:
        BoxImageError: When the check fails or reports no usable result.
    """
    image = user_image.reference
    check_role = _CHECK_ROLES[role]
    session = runtime.open(
        SandboxSpec(
            lifetime_seconds=_CHECK_LIFETIME_SECONDS,
            image=image,
            network_disabled=True,
            operation_key=f"image-check:{owner_id}:{role}",
            private=role == "metric",
            user_image=user_image,
        )
    )
    try:
        try:
            result = session.run(f"ascent check --image --role {check_role}", timeout_seconds=_CHECK_TIMEOUT_SECONDS)
        except _NOT_IMAGE_ERRORS:
            raise
        except Exception as error:
            if not _program_refused(error):
                raise
            raise BoxImageError(
                NO_SETPRIV.format(
                    role=role, image=image, check_role=check_role, app=APP_NAME, upload=", ".join(_UPLOAD_PROGRAMS)
                )
            ) from error
        probe = session.run(_TRUSTED_PYTHON_PROBE, timeout_seconds=_CHECK_TIMEOUT_SECONDS) if role == "run" else None
        # No timeout: on Vercel the wrapper then starts no /usr/bin/timeout, so the
        # probe can name it. The probe runs only shell builtins, and the box lifetime bounds it.
        tools = session.run(_BOX_TOOLS_PROBE[role])
        root = _run_root_probe(session, role)
        workdir = session.run(_WORKDIR_PROBE, timeout_seconds=_CHECK_TIMEOUT_SECONDS)
        report = _check_report(result.stdout)
        python = report.get("python") if report is not None else None
        absolute = isinstance(python, str) and python.startswith("/")
        # The image's own python reports the uid, so an image can lie here; only the
        # numeric USER read outside the box below protects against a root box user.
        uid_probe = (
            session.run(
                f"{shlex.quote(python)} -I -S -c {shlex.quote(_BOX_UID_PROBE)}", timeout_seconds=_CHECK_TIMEOUT_SECONDS
            )
            if check_role == "metric" and absolute
            else probe
        )
    finally:
        with contextlib.suppress(Exception):
            session.close()
    passed = result.ok and report is not None and report.get("ok") is True
    if passed and (check_role != "metric" or absolute):
        names = _root_variables(probe) if probe is not None else []
        if names:
            raise BoxImageError(
                ROOT_ENVIRONMENT.format(
                    image=image, names=", ".join(names), app=APP_NAME, variables=", ".join(sorted(_ROOT_VARIABLES))
                )
            )
        uid = _missing_account(probe) if probe is not None else None
        if uid is not None:
            raise BoxImageError(NO_ACCOUNT.format(image=image, uid=uid, app=APP_NAME))
        missing = _missing_tools(tools, role)
        if missing:
            raise BoxImageError(
                MISSING_TOOLS.format(
                    role=role,
                    image=image,
                    missing=", ".join(missing),
                    app=APP_NAME,
                    purpose=_BOX_TOOLS_PURPOSE[role],
                    tools=", ".join(_BOX_TOOLS[role]),
                    poll=", ".join(_ROOT_TOOLS),
                    path=_ROOT_PATH,
                    upload=", ".join(_UPLOAD_PROGRAMS),
                )
            )
        if probe is not None and not _trusted_python(probe):
            raise BoxImageError(
                UNTRUSTED_PYTHON.format(
                    image=image,
                    python=BASE_PYTHON,
                    app=APP_NAME,
                    programs=", ".join(_ROOT_PROGRAMS),
                    path=_ROOT_PATH,
                )
            )
        expected = _ROOT_PROBE_OUTPUT if role == "run" else _ROOT_PROBE_OUTPUT[:1]
        if root is None or root.exit_code != 0 or root.stdout.split() != expected:
            raise BoxImageError(NO_ROOT[role].format(image=image, app=APP_NAME, path=_ROOT_PATH))
        folder = _unwritable_workdir(workdir)
        if folder is not None:
            raise BoxImageError(WORKDIR_NOT_WRITABLE.format(role=role, image=image, folder=folder, app=APP_NAME))
        if uid_probe is None or _root_box_user(uid_probe):
            raise BoxImageError(ROOT_BOX_USER.format(role=role, image=image, app=APP_NAME))
        _check_image_user(registry, user_image, role, username)
        return python if check_role == "metric" else None
    failed = [
        f"- {check.get('name')}: {check.get('message')}"
        for check in (report or {}).get("checks") or []
        if isinstance(check, dict) and check.get("ok") is not True
    ]
    output = "\n".join(failed) or (result.stdout + result.stderr).strip()[-_CHECK_OUTPUT_CHARS:]
    hint = ""
    if result.exit_code == 127:
        missing = _missing_upload_programs(tools)
        hint = (
            MISSING_UPLOAD_HINT.format(missing=", ".join(missing), app=APP_NAME, upload=", ".join(_UPLOAD_PROGRAMS))
            if missing
            else NOT_FOUND_HINT.format(app=APP_NAME, upload=", ".join(_UPLOAD_PROGRAMS))
        )
    raise BoxImageError(
        f"The [{role}] image '{image}' failed `ascent check --image --role {check_role}`"
        f" (exit code {result.exit_code}).\n\n{output or 'The check printed nothing.'}{hint}"
    )


def _run_root_probe(session: Any, role: str) -> Any:
    """Run :data:`_ROOT_PROBE` as root, with one run variable in the run role.

    A provider can refuse a root command that the image cannot start, for
    example when Vercel finds no sudo. That counts as a failed probe.

    Args:
        session: The image check box.
        role: ``run`` or ``metric``.

    Returns:
        The probe's result, or ``None`` when the provider refused the root command.

    Raises:
        Exception: Any other error, for example a budget error, a stopped box, a
            failed connection or another provider answer; it is not an image fault.
    """
    try:
        return session.run(
            _ROOT_PROBE,
            env={_ROOT_VARIABLE: _ROOT_PROBE_OUTPUT[1]} if role == "run" else None,
            timeout_seconds=_CHECK_TIMEOUT_SECONDS,
            sudo=True,
        )
    except _NOT_IMAGE_ERRORS:
        raise
    except Exception as error:
        # Only a refusal of the command itself says the image has no usable root.
        if not _program_refused(error):
            raise
        logger.info("The image check box could not start a root command.", exc_info=True)
        return None


def _program_refused(error: BaseException) -> bool:
    """Tell whether the provider refused a command because the image lacks its program.

    Vercel answers with a 400 ``executable_not_found`` error when the image has
    no sudo (checked on a real box), and so also when it has no setpriv, the
    first program of every box-user command. Other providers start commands with no such
    program, and the bridge turns their refusals into ``ServiceError``, so they
    have no image refusal to match.

    Args:
        error: The error that the command raised.

    Returns:
        ``True`` for Vercel's ``executable_not_found`` answer.
    """
    api = sandbox_api_error(error)
    return getattr(api, "status_code", None) == 400 and getattr(api, "code", None) == "executable_not_found"


def _unwritable_workdir(result: Any) -> str | None:
    """Name the working folder when :data:`_WORKDIR_PROBE` shows the box user cannot write it.

    Args:
        result: The result of :data:`_WORKDIR_PROBE`.

    Returns:
        The folder, or ``the working folder`` when the box did not report it; ``None`` when it is writable.
    """
    lines = result.stdout.splitlines()
    if result.exit_code == 0 and lines and lines[-1] == _WORKDIR_DONE:
        return None
    folder = lines[0] if lines else ""
    return folder if _PRINTABLE_PATH.fullmatch(folder) else "the working folder"


def _missing_tools(result: Any, role: str) -> list[str]:
    """Name the box tools that the box user's PATH does not find, and the upload programs it cannot run.

    Args:
        result: The result of the role's :data:`_BOX_TOOLS_PROBE`.
        role: ``run`` or ``metric``.

    Returns:
        The missing tools, in :data:`_BOX_TOOLS`, :data:`_ROOT_TOOLS` and then
        :data:`_UPLOAD_PROGRAMS` order. All of them when the probe did not run to its end.
    """
    tools = list(dict.fromkeys((*_BOX_TOOLS[role], *_ROOT_TOOLS, *_UPLOAD_PROGRAMS)))
    lines = result.stdout.splitlines()
    if result.exit_code != 0 or not lines or lines[-1] != _BOX_TOOLS_DONE:
        return tools
    return [tool for tool in tools if tool in lines[:-1]]


def _missing_upload_programs(result: Any) -> list[str]:
    """Name the upload programs that the box tools probe found missing, for a check that exited 127.

    The probe runs only shell builtins. So when it also exits 127 with no output,
    the box found no shell: setpriv exits 127 on Vercel when ``/bin/bash`` is missing.

    Args:
        result: The result of the role's :data:`_BOX_TOOLS_PROBE`.

    Returns:
        The missing upload programs in :data:`_UPLOAD_PROGRAMS` order, or an empty list when the probe names none.
    """
    lines = result.stdout.splitlines()
    if result.exit_code == 0 and lines and lines[-1] == _BOX_TOOLS_DONE:
        return [program for program in _UPLOAD_PROGRAMS if program in lines[:-1]]
    if result.exit_code == 127 and not result.stdout.strip():
        return ["/bin/bash"]
    return []


def _check_image_user(registry: ImageRegistry | None, user_image: UserImage, role: str, username: str) -> None:
    """Refuse an image whose own USER is not a numeric non-root uid, read outside the box (SKY-891).

    The registry reads the config of an image in the owner's repository, for
    each platform. Any other image must be in local Docker, which the check
    box's start pulled it into. A USER that cannot be read refuses the image.
    A USER name refuses the image too: the image's own ``/etc/passwd`` could
    map it to uid 0, and only the image's own programs can read that file.

    Args:
        registry: The user image registry, or ``None`` when local Docker keeps the images.
        user_image: The checked box image.
        role: ``run`` or ``metric``.
        username: The run's owner.

    Raises:
        BoxImageError: When the USER is root, a name, or cannot be read.
    """
    image = user_image.reference
    if registry is not None and user_image.repository is not None:
        try:
            users: tuple[str, ...] | None = registry.image_users(image_owner(username), _digest(image))
        except ImageRegistryError:
            users = None
    else:
        users = _local_image_user(image)
    if not users:
        raise BoxImageError(UNREADABLE_IMAGE_USER.format(role=role, image=image, app=APP_NAME))
    if any(_root_user(user) for user in users):
        raise BoxImageError(ROOT_IMAGE_USER.format(role=role, image=image, app=APP_NAME))
    if not all(_NUMERIC_USER.fullmatch(user) for user in users):
        raise BoxImageError(NAMED_IMAGE_USER.format(role=role, image=image, app=APP_NAME))


def _local_image_user(image: str) -> tuple[str, ...] | None:
    """Read an image's USER from local Docker.

    Args:
        image: The image id or reference.

    Returns:
        The USER, or ``None`` when Docker cannot read it.
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.User}}", image],
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
        user = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return (user,) if isinstance(user, str) else None


def _root_user(user: str) -> bool:
    """Tell whether an image USER runs as root: no user, ``root`` or uid 0, with any group.

    Args:
        user: The image config ``User``, as ``name``, ``uid``, ``name:group`` or ``uid:gid``.

    Returns:
        True for root. Any other user name gives False here, and
        :func:`_check_image_user` refuses it as a name.
    """
    name = user.split(":", 1)[0]
    return name in ("", "root") or (name.isascii() and name.isdigit() and int(name) == 0)


def _root_box_user(probe: Any) -> bool:
    """Tell whether the probe's box user is root, or the probe reports no box uid (SKY-891).

    Args:
        probe: The result of :data:`_TRUSTED_PYTHON_PROBE`.

    Returns:
        True when no ``account ok <uid>`` line is there or any of them has uid 0.
    """
    uids = [int(match.group(1)) for line in probe.stdout.split("\n") if (match := _BOX_UID.fullmatch(line))]
    return not uids or 0 in uids


def _trusted_python(probe: Any) -> bool:
    """Tell whether every path that the image's base interpreter uses is out of the box user's reach.

    The listing must follow the exact walks that the probe makes, so a
    missing tool, a short listing or any odd line refuses.

    Args:
        probe: The result of :data:`_TRUSTED_PYTHON_PROBE`.

    Returns:
        True when ``BASE_PYTHON`` resolves to a root-owned regular file that
        group and others cannot write and others can read and run, each of
        ``_ROOT_PROGRAMS``, ``/etc/passwd``, and ``/etc/nsswitch.conf`` and
        ``/usr/bin/find`` (when present) resolves to one that group and others cannot write and others can read,
        ``/tmp`` is a root-owned 1777 folder, each ``_ROOT_PATH`` folder is
        missing or one that group and others cannot write and others can
        search, the probe reports each folder and tool pair once, every entry of
        ``_ROOT_TOOLS`` in those folders resolves to such a file and each
        tool has one, :func:`_trusted_sudo` accepts the sudo part, both runs
        find a passwd line for the box user, and both runs list at least one
        ``path``, ``file`` and ``dir`` entry, each of which passes
        :func:`_walk` with the file type that its tag allows, every
        regular file among them is readable by others, and every ``absent``
        entry is missing.
    """
    if not probe.ok:
        return False
    # Only a newline ends a line; ``splitlines`` would also split at other control bytes.
    lines = probe.stdout.removesuffix("\n").split("\n")[::-1]
    python = _walk(lines, BASE_PYTHON)
    if python in (None, _MISSING) or python[1] != "regular file" or python[0] & 0o022 or python[0] & 0o005 != 0o005:
        return False
    for path in _ROOT_PATHS:
        if not lines or lines.pop() != f"file {path}":
            return False
        entry = _walk(lines, path)
        if entry is None or (entry == _MISSING and path not in _OPTIONAL_ROOT_FILES):
            return False
        if entry == _MISSING:
            continue
        if path == _ROOT_TMP:
            if entry != (0o1777, "directory"):
                return False
        elif not _readable_file(entry):
            return False
    for folder in _ROOT_PATH.split(":"):
        if not lines or lines.pop() != f"bin {folder}":
            return False
        entry = _walk(lines, folder)
        # The box user cannot see into a folder without o+x, but root still runs its tools.
        if entry is None or (
            entry != _MISSING and (entry[1] != "directory" or entry[0] & 0o022 or not entry[0] & 0o001)
        ):
            return False
    found = set()
    for folder in _ROOT_PATH.split(":"):
        for tool in _ROOT_TOOLS:
            line = lines.pop() if lines else ""
            if line == f"notool {folder}/{tool}":
                continue
            if line != f"tool {folder}/{tool}":
                return False
            entry = _walk(lines, f"{folder}/{tool}")
            if entry in (None, _MISSING) or not _readable_file(entry):
                return False
            found.add(tool)
    if found != set(_ROOT_TOOLS) or not _trusted_sudo(lines):
        return False
    for mode in _RUN_MODES:
        if not lines or lines.pop() != f"run {mode}":
            return False
        tags = set()
        while lines and not lines[-1].startswith("run "):
            tag, _, path = lines.pop().partition(" ")
            if tag in ("env", "locale") and _VARIABLE_NAME.fullmatch(path):
                continue
            if tag == "account" and _ACCOUNT_OK.fullmatch(path):
                tags.add(tag)
                continue
            if tag not in _ENTRY_KINDS or not _PRINTABLE_PATH.fullmatch(path):
                return False
            entry = _walk(lines, path)
            if entry is None or (entry == _MISSING and tag not in _MAY_BE_MISSING):
                return False
            if entry != _MISSING and (entry[1] not in _ENTRY_KINDS[tag] or entry[0] & 0o022):
                return False
            # A file that the box user cannot read could name paths, such as an ld.so.preload library, that the probe never sees.
            if entry != _MISSING and entry[1] == "regular file" and not _readable_file(entry):
                return False
            tags.add(tag)
        if not {"account", "path", "file", "dir"} <= tags:
            return False
    return not lines


def _trusted_sudo(lines: list[str]) -> bool:
    """Tell whether every sudo that Vercel may run, and the sudo and PAM files it reads, are out of the box user's reach.

    Args:
        lines: The probe's remaining lines, last line first; the check pops
            the sudo part of the listing.

    Returns:
        True when all of these hold. The probe reports the sudo of each
        ``_ROOT_PATH`` folder once, and each one that exists resolves to a
        file that :func:`_readable_file` accepts. Each image PATH folder up to
        the first one with a sudo is missing, or a folder that group and
        others cannot write and others can search, and that sudo is such a
        file that others can run. Each of ``_SUDO_FILES`` is missing or a regular
        file that group and others cannot write. Each of ``_SUDO_FOLDERS`` is
        missing or a folder that :func:`_trusted_folder` accepts. The listing
        ends with ``_SUDO_DONE``.
    """
    for folder in _ROOT_PATH.split(":"):
        line = lines.pop() if lines else ""
        if line == f"nosudo {folder}/sudo":
            continue
        if line != f"sudo {folder}/sudo":
            return False
        entry = _walk(lines, f"{folder}/sudo")
        if entry in (None, _MISSING) or not _readable_file(entry):
            return False
    while lines and lines[-1].startswith("pathdir "):
        folder = lines.pop().removeprefix("pathdir ")
        if not _PRINTABLE_PATH.fullmatch(folder):
            return False
        entry = _walk(lines, folder)
        if entry is None or (
            entry != _MISSING and (entry[1] != "directory" or entry[0] & 0o022 or not entry[0] & 0o001)
        ):
            return False
        if lines and lines[-1] == f"sudo {folder}/sudo":
            lines.pop()
            entry = _walk(lines, f"{folder}/sudo")
            # A non-root PATH lookup skips a file it cannot run and goes on to folders that the probe never
            # walks, so others must be able to run this sudo.
            if entry in (None, _MISSING) or not _readable_file(entry) or not entry[0] & 0o001:
                return False
            break
    for path in (*_SUDO_FILES, *_SUDO_FOLDERS):
        if not lines or lines.pop() != f"conf {path}":
            return False
        entry = _walk(lines, path)
        if entry is None:
            return False
        if entry == _MISSING:
            continue
        if path in _SUDO_FILES and (entry[1] != "regular file" or entry[0] & 0o022):
            return False
        if path in _SUDO_FOLDERS and not _trusted_folder(lines, path, entry, path == _DEEP_FOLDER):
            return False
    return bool(lines) and lines.pop() == _SUDO_DONE


def _trusted_folder(lines: list[str], folder: str, entry: tuple[int, str], deep: bool) -> bool:
    """Tell whether a sudo or PAM folder and each entry that the probe listed in it are out of the box user's reach.

    Args:
        lines: The probe's remaining lines, last line first; the check pops
            the folder's ``entry`` lines and their walks.
        folder: The folder's path.
        entry: The folder's walked mode and type.
        deep: Whether the folder may hold folders, whose entries the probe
            lists next.

    Returns:
        True when the folder is a folder that group and others cannot write
        and others can list. Each entry is directly in it, has a printable
        name, and is a regular file that group and others cannot write. When
        ``deep`` is set, an entry may also be such a folder, with such files.
    """
    # Sudo and PAM read every entry as root, so the box user must see them all.
    if entry[1] != "directory" or entry[0] & 0o022 or entry[0] & 0o005 != 0o005:
        return False
    while lines and lines[-1].startswith(f"entry {folder}/"):
        path = lines.pop().removeprefix("entry ")
        name = path.removeprefix(f"{folder}/")
        if name in ("", ".", "..") or "/" in name or not _PRINTABLE_PATH.fullmatch(path):
            return False
        entry = _walk(lines, path)
        if entry in (None, _MISSING):
            return False
        if deep and entry[1] == "directory":
            if not _trusted_folder(lines, path, entry, False):
                return False
        elif entry[1] != "regular file" or entry[0] & 0o022:
            return False
    return True


def _readable_file(entry: tuple[int, str]) -> bool:
    """Tell whether a walked entry is a regular file that group and others cannot write and others can read.

    Args:
        entry: The mode and file type that :func:`_walk` returned.

    Returns:
        True for such a file.
    """
    return entry[1] == "regular file" and not entry[0] & 0o022 and bool(entry[0] & 0o004)


def _missing_account(probe: Any) -> str | None:
    """Return the box uid when a probe run found no ``/etc/passwd`` line for it.

    Args:
        probe: The result of :data:`_TRUSTED_PYTHON_PROBE`.

    Returns:
        The uid, or ``None`` when no run reported a missing line.
    """
    for line in probe.stdout.split("\n"):
        missing = _ACCOUNT_NONE.fullmatch(line)
        if missing:
            return missing.group(1)
    return None


def _root_variables(probe: Any) -> list[str]:
    """List the variables in the probe's environments that change what root's shell, loader or libc runs.

    A failed probe still counts: its ``env`` lines come before its walks, and
    a refusal is the safe answer either way.

    Args:
        probe: The result of :data:`_TRUSTED_PYTHON_PROBE`.

    Returns:
        The sorted names, without duplicates.
    """
    lines = probe.stdout.split("\n")
    names = {line.removeprefix("env ") for line in lines if line.startswith("env ")}
    found = {name for name in names if name.startswith(_ROOT_PREFIXES) or name in _ROOT_VARIABLES}
    for line in lines:
        if line.startswith("locale "):
            name, _, value = line.removeprefix("locale ").partition("=")
            if not _locale_ok(name, value):
                found.add(name)
    return sorted(found)


def _locale_ok(name: str, value: str) -> bool:
    """Tell whether a locale variable names only C, POSIX or a UTF-8 locale.

    Args:
        name: ``LANG``, ``LANGUAGE`` or an ``LC_*`` name.
        value: Its value; ``LANGUAGE`` is a ``:`` list of locales.

    Returns:
        True when every locale in the value is empty, C, POSIX or a UTF-8 locale.
    """
    values = value.split(":") if name == "LANGUAGE" else [value]
    return all(item in ("", "C", "POSIX") or _UTF8_LOCALE.fullmatch(item) for item in values)


def _walk(lines: list[str], path: str) -> tuple[int, str] | str | None:
    """Follow the probe's walk of one path from ``/``, the way the kernel resolves it.

    Args:
        lines: The probe's remaining lines, last line first; the walk pops its own.
        path: The absolute path that the probe walked.

    Returns:
        The octal mode and the file type of the path's last part,
        :data:`_MISSING` when a part does not exist, or None when a line is
        not the one the walk expects, a folder on the way is not a root-owned
        folder that group and others cannot write, or the links take more
        than ``_MAX_LINK_HOPS`` hops.
    """
    root = _stat_entry(lines.pop() if lines else "", "/")
    if root is None or root[1] != "directory" or root[0] & 0o022:
        return None
    folder, rest, hops, entry = "", path.removeprefix("/"), 0, root
    while rest:
        part, _, rest = rest.partition("/")
        if part in ("", "."):
            continue
        if part == "..":
            folder = folder.rpartition("/")[0]
            continue
        here = f"{folder}/{part}"
        if lines and lines[-1] == f"none {here}":
            lines.pop()
            return _MISSING
        entry = _stat_entry(lines.pop() if lines else "", here)
        if entry is None:
            return None
        if entry[1] == "symbolic link":
            # A link's own mode is always 777 and grants nothing; its folder guards it.
            hops += 1
            if hops > _MAX_LINK_HOPS or not lines or not lines[-1].startswith("link "):
                return None
            target = lines.pop().removeprefix("link ")
            if not target or not _PRINTABLE_PATH.fullmatch("/" + target):
                return None
            if target.startswith("/"):
                root = _stat_entry(lines.pop() if lines else "", "/")
                if root is None or root[1] != "directory" or root[0] & 0o022:
                    return None
                folder, target = "", target.removeprefix("/")
            rest = f"{target}/{rest}" if rest else target
            continue
        if rest and (entry[1] != "directory" or entry[0] & 0o022):
            return None
        folder = here
    return entry


def _stat_entry(line: str, name: str) -> tuple[int, str] | None:
    """Read one ``stat -c '%u %a %F %n'`` line for the path the walk expects.

    Args:
        line: The probe's line.
        name: The path the line must name.

    Returns:
        The octal mode and the file type, or None when the line names another
        path, is malformed, or the owner is not root.
    """
    if not line.endswith(" " + name):
        return None
    parts = line[: -len(name) - 1].split(" ", 2)
    # GNU stat names a zero-byte file so, as Debian's own /etc/environment is (SKY-912).
    if len(parts) == 3 and parts[2] == "regular empty file":
        parts[2] = "regular file"
    # ``%a`` prints a mode-0000 file, such as a Red Hat /etc/shadow, as ``0`` (SKY-912).
    if len(parts) != 3 or parts[0] != "0" or not re.fullmatch(r"[0-7]{1,4}", parts[1]) or parts[2] not in _FILE_TYPES:
        return None
    return int(parts[1], 8), parts[2]


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
    payload: dict[str, Any],
    staged: StagedRepository,
    gateway: Any,
    *,
    owner_id: str,
    managed_key: str = "",
    skip_setup: tuple[str, ...] = (),
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
        skip_setup: Roles (``run``, ``metric``) whose setup box does not open. A setup
            check skips each role with a user image; the payload then gets no
            evaluator route (SKY-893).

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
            run_image=staged.box_user_images.get("run") or (staged.box_images.get("run") or {}).get("image"),
            metric_image=staged.box_user_images.get("metric") or (staged.box_images.get("metric") or {}).get("image"),
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
    scorer.prepare(skip=skip_setup)
    # A version needs both roles' setup, so a skipped role leaves the guest no scorer route.
    if not skip_setup:
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

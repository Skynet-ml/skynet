"""Read a repository run's files at its pinned commit. [INTERNAL]

Frontend-only (hidden from public docs):
- ``GET /optimizations/{id}/repository/tree`` — every file and folder at the commit.
- ``GET /optimizations/{id}/repository/file`` — one file's text at the commit.
- ``POST /optimizations/{id}/repository/archive`` — the whole repository with one version applied, as a zip.
- ``POST /optimizations/{id}/repository/apply`` — write a local run's best version into its folder (SKY-991).
- ``POST /optimizations/{id}/repository/undo`` — put the folder back as it was before Apply (SKY-991).

Every version of a repository run is a patch against one pinned commit; the
run page applies the patches itself and only needs the base tree and files.
The repository host is read with the run owner's connection, since the commit belongs to
the run rather than to whoever is viewing it, and anonymously when the owner
has none, so public repositories still open. A local folder run reads the
copy of the folder that staging kept on this computer (SKY-990), and only a
local run can Apply its best version to that folder and Undo it.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ....config import settings
from ....connectors import repo_hosts
from ....connectors.github_repo import RepoFetchError, github_token, patched_repo_zip
from ....models import (
    BlackboxRepoArchiveRequest,
    BlackboxRepoFileResponse,
    BlackboxRepoTreeEntry,
    BlackboxRepoTreeResponse,
    OptimizationStatus,
)
from ....models.blackbox import LOCAL_FOLDER_ONLY_HERE
from ....service_gateway.optimization.blackbox.repo_constraints import (
    CONSTRAINTS_FILE,
    ConstraintsError,
    parse_constraints,
    read_constraints,
)
from ....service_gateway.optimization.blackbox.repo_tree import patch_violations
from ....worker.local_folder import (
    APPLY_LOCK,
    LOCAL_PROVIDER,
    LocalFolderConflictError,
    LocalFolderError,
    apply_version,
    check_applied,
    forget_apply,
    forget_undone,
    is_applied,
    kept_snapshot,
    snapshot_file,
    snapshot_tree,
    snapshot_zip,
    undo_version,
)
from ....worker.repo_staging import hidden_repo_paths
from ...auth import AuthenticatedUser, get_authenticated_user
from ...converters import job_owner
from ...errors import DomainError
from ...sharing_access import ShareRole
from .._helpers import load_job_for_user, require_role_at_least

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

MAX_FILE_BYTES = 1024 * 1024
_TREE_CACHE_ENTRIES = 32
_FILE_CACHE_ENTRIES = 2048
_FILE_CACHE_BYTES = 64 * 1024 * 1024
# A running run still writes its result, so Apply and Undo wait until it stops.
_STOPPED = {
    OptimizationStatus.success.value,
    OptimizationStatus.failed.value,
    OptimizationStatus.cancelled.value,
    OptimizationStatus.stopped.value,
}

T = TypeVar("T")


class _BoundedCache:
    """A thread-safe least-recently-used cache capped by entry count and total weight."""

    def __init__(self, max_entries: int, max_weight: int | None = None) -> None:
        """Create an empty cache.

        Args:
            max_entries: Most entries kept at once.
            max_weight: Most total weight kept at once; ``None`` for no cap.
        """
        self._entries: OrderedDict[tuple[str, ...], tuple[Any, int]] = OrderedDict()
        self._max_entries = max_entries
        self._max_weight = max_weight
        self._weight = 0
        self._lock = threading.Lock()

    def get(self, key: tuple[str, ...]) -> Any | None:
        """Return a cached value and mark it recently used.

        Args:
            key: Cache key.

        Returns:
            The value, or ``None`` when absent.
        """
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            self._entries.move_to_end(key)
            return hit[0]

    def put(self, key: tuple[str, ...], value: Any, weight: int = 0) -> None:
        """Store a value, evicting the least recently used entries past the caps.

        Args:
            key: Cache key.
            value: Value to keep.
            weight: The value's share of ``max_weight``, such as its byte size.
        """
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._weight -= previous[1]
            self._entries[key] = (value, weight)
            self._weight += weight
            while len(self._entries) > self._max_entries or (
                self._max_weight is not None and self._weight > self._max_weight and len(self._entries) > 1
            ):
                _, (_, evicted) = self._entries.popitem(last=False)
                self._weight -= evicted

    def clear(self) -> None:
        """Drop every entry."""
        with self._lock:
            self._entries.clear()
            self._weight = 0


# Keyed by owner as well as (repository, commit): a run only proves its
# owner could read the repository, so another owner's run on the same commit
# must not be served from it without its own read.
_trees = _BoundedCache(_TREE_CACHE_ENTRIES)
_files = _BoundedCache(_FILE_CACHE_ENTRIES, _FILE_CACHE_BYTES)


def clean_repo_path(raw: str) -> str:
    """Normalize a repository-relative path, refusing ones that leave the repository.

    Args:
        raw: The path the caller asked for.

    Returns:
        The ``/``-joined path without leading or trailing slashes.

    Raises:
        DomainError: 400 when the path is empty, absolute, or walks out with ``..``.
    """
    path = raw.strip().replace("\\", "/")
    if not path or path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise DomainError("connectors.invalid_ref", status=400)
    path = path.rstrip("/")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise DomainError("connectors.invalid_ref", status=400)
    return path


def _repo_target(job_data: dict[str, Any], optimization_id: str) -> tuple[str, str]:
    """Read the repository and pinned commit of a repository run.

    Args:
        job_data: The stored job row.
        optimization_id: The run, for the error message.

    Returns:
        ``(repository, commit)``.

    Raises:
        DomainError: 404 when the run is not a repository run; 409 while its
            commit is not pinned yet, which happens before it first starts.
    """
    payload = job_data.get("payload")
    target = payload.get("target") if isinstance(payload, dict) else None
    repo = target.get("repo") if isinstance(target, dict) and target.get("kind") == "repo" else None
    if not isinstance(repo, dict) or not isinstance(repo.get("repository"), str):
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id)
    commit = repo.get("commit")
    if not isinstance(commit, str) or not commit:
        raise DomainError("optimization.no_result_pending", status=409)
    return repo["repository"], commit


def _repo_provider(job_data: dict[str, Any]) -> str:
    """Return the host of a repository run's repository.

    Args:
        job_data: The stored job row, already checked by :func:`_repo_target`.

    Returns:
        ``github``, ``gitlab``, ``bitbucket``, or ``local`` for a folder on this computer.
    """
    repo = job_data["payload"]["target"]["repo"]
    if repo.get("provider") == LOCAL_PROVIDER:
        return LOCAL_PROVIDER
    return repo_hosts.provider_of(repo)


def _read_local(read: Callable[[], T]) -> T:
    """Run a read of a local run's kept folder copy.

    Args:
        read: The read.

    Returns:
        What ``read`` returns.

    Raises:
        DomainError: 502 when the kept copy is missing or unreadable.
    """
    try:
        return read()
    except LocalFolderError as error:
        raise DomainError("optimization.repo_archive_failed", status=502, reason=str(error)) from None


def _owner_token(engine: Any, owner: str | None, provider: str) -> str | None:
    """Return the owner's token for a repository host, or ``None`` without a usable connection.

    Args:
        engine: SQLAlchemy engine holding the connector vault, or ``None``.
        owner: The run's owner.
        provider: The repository host.

    Returns:
        The token, or ``None``.
    """
    if engine is None or not owner:
        return None
    try:
        if provider == repo_hosts.DEFAULT_PROVIDER:
            return github_token(engine, owner)
        return repo_hosts.host_token(engine, owner, provider)
    except RepoFetchError:
        return None


def _read_as_owner(
    engine: Any, owner: str | None, read: Callable[[str | None], T], provider: str = repo_hosts.DEFAULT_PROVIDER
) -> T:
    """Run a repository host read with the owner's connection, falling back to an anonymous read.

    Args:
        engine: SQLAlchemy engine holding the connector vault, or ``None``.
        owner: The run's owner.
        read: The read, given a token or ``None``.
        provider: The repository host.

    Returns:
        What ``read`` returns.

    Raises:
        DomainError: The host's refusal of the anonymous read, or of the owner's
            read when it failed for another reason than a rejected token.
    """
    token = _owner_token(engine, owner, provider)
    if token is None:
        return read(None)
    try:
        return read(token)
    except DomainError as exc:
        # A revoked or narrowed token must not hide a repository anyone can read.
        if exc.code != "connectors.rejected":
            raise
        return read(None)


def _file_response(path: str, fetched: dict[str, Any]) -> BlackboxRepoFileResponse:
    """Turn a fetched file into the response, decoding text and flagging binaries.

    Args:
        path: The cleaned repository path.
        fetched: What :func:`core.connectors.github.file_at` returned.

    Returns:
        The response.
    """
    content: bytes | None = fetched["content"]
    text = None
    binary = False
    if content is not None:
        if b"\x00" in content:
            binary = True
        else:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                binary = True
    return BlackboxRepoFileResponse(
        path=path,
        content=text,
        binary=binary,
        too_large=fetched["too_large"],
        missing=fetched["missing"],
        size=fetched["size"],
    )


def _run_drafts(job_data: dict[str, Any]) -> dict[str, str]:
    """Read the setup draft's files that the run wrote over its pinned commit.

    Args:
        job_data: The stored job row of a repository run.

    Returns:
        Repository path to file text; empty for a run without draft files.
    """
    drafts = job_data["payload"]["target"]["repo"].get("draft_files")
    return {str(path): str(text) for path, text in drafts.items()} if isinstance(drafts, dict) else {}


def _run_deleted(job_data: dict[str, Any]) -> list[str]:
    """Read the folder files the run's setup deleted (SKY-997).

    Args:
        job_data: The stored job row of a repository run.

    Returns:
        The paths; empty for a run that deleted none.
    """
    deleted = job_data["payload"]["target"]["repo"].get("draft_deleted")
    return [str(path) for path in deleted] if isinstance(deleted, list) else []


def _run_readonly_paths(job_data: dict[str, Any]) -> list[str]:
    """Read the run's recorded submodule and Git LFS paths, which a draft cannot change.

    Args:
        job_data: The stored job row of a repository run.

    Returns:
        The recorded read-only paths; empty when the run recorded no environment.
    """
    environment = job_data["payload"]["target"]["repo"].get("environment")
    paths = environment.get("readonly_paths") if isinstance(environment, dict) else None
    return [str(path) for path in paths] if isinstance(paths, list) else []


def _hidden_paths(commit: str, drafts: dict[str, str]) -> list[str]:
    """List the scoring-side paths of a local run, which its best version may never change.

    The worker drops the best version's edits to these paths by exact name
    only, so Apply checks them again on the real files, in any case.

    Args:
        commit: The run's pinned source id.
        drafts: The run's draft files, whose contract file wins over the kept one.

    Returns:
        The hidden paths of the kept contract file and of a drafted one, and the contract file itself.

    Raises:
        DomainError: 409 when the kept copy or a contract file cannot be read.
    """
    try:
        found = [*hidden_repo_paths(read_constraints(kept_snapshot(commit), require_version=False, legacy_forms=True))]
        if CONSTRAINTS_FILE in drafts:
            drafted = parse_constraints(drafts[CONSTRAINTS_FILE].encode(), require_version=False, legacy_forms=True)
            found += hidden_repo_paths(drafted)
    except (ConstraintsError, LocalFolderError, OSError) as error:
        raise DomainError("optimization.local_folder_failed", status=409, reason=str(error)) from None
    return [*found, CONSTRAINTS_FILE]


def _local_run(job_data: dict[str, Any], optimization_id: str) -> tuple[dict[str, Any], str]:
    """Check that a run is a stopped local folder run on this computer.

    Args:
        job_data: The stored job row.
        optimization_id: The run, for the error message.

    Returns:
        ``(repo, commit)``: the run's repository target and its pinned commit.

    Raises:
        DomainError: 404 when the run is not a local folder run; 409 before
            its commit is pinned, away from the desktop app, or while it runs.
    """
    _repo, commit = _repo_target(job_data, optimization_id)
    if _repo_provider(job_data) != LOCAL_PROVIDER:
        raise DomainError("optimization.not_found", status=404, optimization_id=optimization_id)
    if not settings.local_mode:
        raise DomainError("optimization.local_folder_failed", status=409, reason=LOCAL_FOLDER_ONLY_HERE)
    if job_data.get("status") not in _STOPPED:
        raise DomainError("optimization.local_folder_failed", status=409, reason="The run is still running.")
    return job_data["payload"]["target"]["repo"], commit


def _change_folder(change: Callable[[], Any]) -> None:
    """Run an Apply or an Undo, and turn its refusal into an API error.

    Args:
        change: The Apply or the Undo.

    Raises:
        DomainError: 409 with the changed files when the folder changed, or
            with the reason when the folder cannot be changed.
    """
    try:
        change()
    except LocalFolderConflictError as error:
        raise DomainError("optimization.local_folder_conflict", status=409, files=", ".join(error.paths)) from None
    except LocalFolderError as error:
        raise DomainError("optimization.local_folder_failed", status=409, reason=str(error)) from None


def _record_apply(job_store, optimization_id: str, applied: bool, *, undo_needed: bool = False) -> dict[str, Any]:
    """Save whether the best version is in the folder, and when it was first applied.

    The Undo record decides ``applied``; the row only shows it. The caller
    holds ``APPLY_LOCK``, so the row is read again here and two requests
    cannot save over each other. ``applied_at`` is the feature's metric, so
    Undo keeps it, and only an Apply sets it.

    Args:
        job_store: Job-store the run is saved in.
        optimization_id: The run.
        applied: Whether the version is in the folder now.
        undo_needed: A failed Apply kept the Undo record, so the page must offer Undo.

    Returns:
        ``{"applied", "applied_at"}``, plus ``undo_needed`` when it is set, as the
        run's ``result.details.local_apply`` now holds it.
    """
    result = dict(job_store.get_job(optimization_id).get("result") or {})
    details = dict(result.get("details") or {})
    previous = details.get("local_apply") if isinstance(details.get("local_apply"), dict) else {}
    first = previous.get("applied_at") or (datetime.now(UTC).isoformat() if applied else None)
    state: dict[str, Any] = {"applied": applied, "applied_at": first}
    if undo_needed:
        state["undo_needed"] = True
    # An Undo on a run never applied changes nothing, so it saves and logs nothing either.
    if previous == state or (not applied and not undo_needed and not previous):
        return state
    details["local_apply"] = state
    result["details"] = details
    job_store.update_job(optimization_id, result=result)
    # The log keeps a trace of each change; the page reloads the row itself, as a stopped run gets no live update.
    job_store.append_log(
        optimization_id,
        level="INFO",
        logger_name="skynet.local_folder",
        message="Applied the best version to the folder."
        if applied
        else "Apply stopped part way, so Undo must put the folder back."
        if undo_needed
        else "Undid the best version in the folder.",
    )
    return state


def _is_applied(optimization_id: str) -> bool:
    """Check whether the run's Undo record exists, and turn a refusal into an API error.

    Args:
        optimization_id: The run.

    Returns:
        True while the Undo record of the run's Apply exists.

    Raises:
        DomainError: 409 when the run id is not valid or there is no local state folder.
    """
    try:
        return is_applied(optimization_id)
    except LocalFolderError as error:
        raise DomainError("optimization.local_folder_failed", status=409, reason=str(error)) from None


def _says_applied(job_store, optimization_id: str) -> bool:
    """Check whether the run's saved state says its best version is in the folder.

    The caller holds ``APPLY_LOCK``, so the row is read again here.

    Args:
        job_store: Job-store the run is saved in.
        optimization_id: The run.

    Returns:
        True when ``result.details.local_apply.applied`` is true.
    """
    result = job_store.get_job(optimization_id).get("result") or {}
    state = (result.get("details") or {}).get("local_apply")
    return isinstance(state, dict) and state.get("applied") is True


def _tree_with_drafts(
    tree: BlackboxRepoTreeResponse, drafts: dict[str, str], deleted: list[str]
) -> BlackboxRepoTreeResponse:
    """Show the run's draft files in the commit's tree, as the run started from both.

    The cache holds the commit's tree for every run on that commit, so the
    drafts of one run go on a copy, never into the cached tree. The deletes
    apply first, as when the run packs its tree, so a deleted file can come
    back as a folder of draft files.

    Args:
        tree: The commit's tree.
        drafts: Repository path to file text.
        deleted: The files the run's setup deleted (SKY-997).

    Returns:
        ``tree`` itself without drafts or deletes; otherwise a copy without
        the deleted files and the folders they emptied, that lists each draft
        file with its size, and each folder above a new one.
    """
    if not drafts and not deleted:
        return tree
    entries = {entry.path: entry for entry in tree.entries}
    for path in deleted:
        if path not in entries or entries[path].type != "file":
            continue
        del entries[path]
        parent = path.rpartition("/")[0]
        # Only a folder the deletes emptied goes; a folder that was empty at the commit stays.
        while parent and not any(other.startswith(f"{parent}/") for other in entries):
            entries.pop(parent, None)
            parent = parent.rpartition("/")[0]
    for path, text in drafts.items():
        parts = path.split("/")
        for depth in range(1, len(parts)):
            folder = "/".join(parts[:depth])
            entries.setdefault(folder, BlackboxRepoTreeEntry(path=folder, type="dir"))
        entries[path] = BlackboxRepoTreeEntry(path=path, type="file", size=len(text.encode("utf-8")))
    return tree.model_copy(update={"entries": sorted(entries.values(), key=lambda entry: entry.path)})


def recorded_patches(job_data: dict[str, Any]) -> set[str]:
    """Collect every version text a repository run recorded.

    Args:
        job_data: The stored job row.

    Returns:
        The patches of its starting point, versions, lineage and winner, plus
        the empty patch for the pinned commit itself.
    """
    result = job_data.get("result")
    found = {""}
    if not isinstance(result, dict):
        return found
    candidates = [result.get("seed_candidate"), result.get("best_candidate")]
    for key in ("versions", "candidate_tree", "lanes"):
        entries = result.get(key)
        if isinstance(entries, list):
            candidates.extend(entry.get("candidate") for entry in entries if isinstance(entry, dict))
    for candidate in candidates:
        if isinstance(candidate, str):
            found.add(candidate)
        elif isinstance(candidate, dict):
            found.update(value for value in candidate.values() if isinstance(value, str))
    return found


def register_repository_routes(router: APIRouter, *, job_store) -> None:
    """Register the repository-run file routes on ``router``.

    Args:
        router: The router to attach the routes to.
        job_store: Job-store the runs are read from; its ``engine`` holds the
            connector vault.
    """

    @router.get(
        "/optimizations/{optimization_id}/repository/tree",
        response_model=BlackboxRepoTreeResponse,
        summary="Every file and folder of a repository run at its pinned commit",
    )
    def get_repository_tree(optimization_id: str, current_user: AuthenticatedUserDep) -> BlackboxRepoTreeResponse:
        """List the repository a run optimizes, at the commit its versions patch.

        Args:
            optimization_id: The repository run.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            The repository, the commit, every path sorted with the run's draft
            files added, and whether the host truncated the tree.

        Raises:
            DomainError: 404 when the run is unknown, inaccessible or not a
                repository run; 409 before its commit is pinned; the host's
                refusal (404/409/502) when the tree cannot be read.
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)
        repository, commit = _repo_target(job_data, optimization_id)
        provider = _repo_provider(job_data)
        owner = job_owner(job_data) or ""
        key = (owner, provider, repository, commit)
        cached = _trees.get(key)
        if cached is None:
            if provider == LOCAL_PROVIDER:
                tree = _read_local(lambda: snapshot_tree(commit))
            else:
                tree = _read_as_owner(
                    getattr(job_store, "engine", None),
                    owner,
                    lambda token: repo_hosts.host(provider).tree_entries(token, repository, commit),
                    provider,
                )
            entries = sorted(
                (BlackboxRepoTreeEntry(path=e["path"], type=e["type"], size=e.get("size")) for e in tree["entries"]),
                key=lambda entry: entry.path,
            )
            cached = BlackboxRepoTreeResponse(
                repository=repository, commit=commit, entries=entries, truncated=tree["truncated"]
            )
            _trees.put(key, cached)
        return _tree_with_drafts(cached, _run_drafts(job_data), _run_deleted(job_data))

    @router.get(
        "/optimizations/{optimization_id}/repository/file",
        response_model=BlackboxRepoFileResponse,
        summary="One file of a repository run at its pinned commit",
    )
    def get_repository_file(
        optimization_id: str,
        current_user: AuthenticatedUserDep,
        path: str = Query(min_length=1, max_length=4096),
    ) -> BlackboxRepoFileResponse:
        """Read one file of the repository a run optimizes, at the commit its versions patch.

        Args:
            optimization_id: The repository run.
            current_user: Authenticated caller resolved from the bearer token.
            path: Repository-relative, ``/``-separated file path.

        Returns:
            The file's text (the run's draft text for a draft file), or flags
            for a missing (also a file the run's setup deleted), binary or
            over-1 MB file.

        Raises:
            DomainError: 400 for a path outside the repository; 404 when the
                run is unknown, inaccessible or not a repository run; 409
                before its commit is pinned; the host's refusal (409/502) when
                the file cannot be read.
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)
        clean = clean_repo_path(path)
        repository, commit = _repo_target(job_data, optimization_id)
        provider = _repo_provider(job_data)
        # The run started from its draft text, not the commit's, for these paths.
        if (draft := _run_drafts(job_data).get(clean)) is not None:
            return BlackboxRepoFileResponse(path=clean, content=draft, size=len(draft.encode("utf-8")))
        if clean in _run_deleted(job_data):
            return BlackboxRepoFileResponse(path=clean, missing=True)
        owner = job_owner(job_data) or ""
        key = (owner, provider, repository, commit, clean)
        cached = _files.get(key)
        if cached is None:
            if provider == LOCAL_PROVIDER:
                fetched = _read_local(lambda: snapshot_file(commit, clean, MAX_FILE_BYTES))
            else:
                fetched = _read_as_owner(
                    getattr(job_store, "engine", None),
                    owner,
                    lambda token: repo_hosts.host(provider).file_at(token, repository, commit, clean, MAX_FILE_BYTES),
                    provider,
                )
            cached = _file_response(clean, fetched)
            _files.put(key, cached, weight=len(cached.content or ""))
        return cached

    @router.post(
        "/optimizations/{optimization_id}/repository/archive",
        response_class=FileResponse,
        summary="The whole repository of a repository run with one version applied, as a zip",
    )
    def get_repository_archive(
        optimization_id: str,
        body: BlackboxRepoArchiveRequest,
        current_user: AuthenticatedUserDep,
    ) -> FileResponse:
        """Zip the repository a run optimizes at its pinned commit with one version's patch applied.

        Args:
            optimization_id: The repository run.
            body: The version, as the patch text the run recorded.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            The zip, removed from disk once it is sent.

        Raises:
            DomainError: 404 when the run is unknown, inaccessible or not a
                repository run, or the patch is not one of its versions; 409
                before its commit is pinned; 502 when the clone or the patch
                fails.
        """
        job_data = load_job_for_user(job_store, optimization_id, current_user)
        repository, commit = _repo_target(job_data, optimization_id)
        # Only a recorded version is zipped, so the route cannot be used to
        # stamp arbitrary content into a download that looks like the run's.
        if body.patch not in recorded_patches(job_data):
            raise DomainError("optimization.repo_version_unknown", status=404)
        provider = _repo_provider(job_data)
        workdir = Path(tempfile.mkdtemp(prefix="repo-archive-"))
        try:
            if provider == LOCAL_PROVIDER:
                archive = snapshot_zip(
                    commit,
                    body.patch,
                    workdir,
                    folder=repository.rsplit("/", 1)[-1],
                    drafts=_run_drafts(job_data),
                    readonly_paths=_run_readonly_paths(job_data),
                    deleted=_run_deleted(job_data),
                )
            else:
                token = _owner_token(getattr(job_store, "engine", None), job_owner(job_data) or "", provider)
                archive = patched_repo_zip(
                    repository,
                    commit,
                    body.patch,
                    token,
                    workdir,
                    **repo_hosts.git_kwargs(provider),
                    drafts=_run_drafts(job_data),
                    readonly_paths=_run_readonly_paths(job_data),
                )
        except RepoFetchError as error:
            shutil.rmtree(workdir, ignore_errors=True)
            raise DomainError("optimization.repo_archive_failed", status=502, reason=str(error)) from None
        except BaseException:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        return FileResponse(
            archive,
            media_type="application/zip",
            filename=archive.name,
            background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True),
        )

    @router.post(
        "/optimizations/{optimization_id}/repository/apply",
        summary="Write a local run's best version into its folder",
    )
    def apply_to_folder(optimization_id: str, current_user: AuthenticatedUserDep) -> dict[str, Any]:
        """Write the best version of a local folder run into the folder, and keep what Undo needs.

        Args:
            optimization_id: The local folder run.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            ``{"applied": True, "applied_at"}``, also when it was already applied.

        Raises:
            DomainError: 403 below the editor role; 404 when the run is not a
                local folder run; 409 away from the desktop app, while the run
                runs, without a best version or setup drafts, when it breaks the run's editing
                rules, when a file to change changed, or when an earlier Apply
                left only part of the version in the folder.
        """
        job_data, _role = require_role_at_least(job_store, optimization_id, current_user, ShareRole.editor)
        repo, commit = _local_run(job_data, optimization_id)
        result = job_data.get("result")
        patch = result.get("best_candidate") if isinstance(result, dict) else None
        drafts = _run_drafts(job_data)
        deleted = _run_deleted(job_data)
        # A best version equal to the start still differs from the folder by the setup drafts.
        if not isinstance(patch, str) or not (patch or drafts or deleted):
            raise DomainError("optimization.no_result_pending", status=409)
        environment = repo.get("environment") if isinstance(repo.get("environment"), dict) else {}
        editable = repo.get("editable_paths") or ()
        readonly = environment.get("readonly_paths") or ()
        protected = environment.get("protected_paths") or ()
        hidden = _hidden_paths(commit, drafts)
        # A local run skips publish, which checks the best version again, so the check runs here.
        problems = patch_violations(patch, editable, readonly, protected, exploit_paths=hidden)
        if problems:
            raise DomainError("optimization.local_folder_failed", status=409, reason=problems[0])
        with APPLY_LOCK:
            if _is_applied(optimization_id) and not _says_applied(job_store, optimization_id):
                # A record delete that failed after a saved Undo leaves the record with every file back.
                _change_folder(lambda: forget_undone(repo["path"], optimization_id))
            try:
                # A repeat Apply only saves the row a failed save missed, and only when
                # the files hold the version: a failed rollback keeps the record too.
                if _is_applied(optimization_id):
                    _change_folder(lambda: check_applied(repo["path"], optimization_id))
                else:
                    _change_folder(
                        lambda: apply_version(
                            repo["path"],
                            commit,
                            patch,
                            optimization_id,
                            drafts=drafts,
                            readonly_paths=_run_readonly_paths(job_data),
                            editable_paths=editable,
                            blocked_paths=[*readonly, *protected, *hidden],
                            deleted=deleted,
                        )
                    )
            except DomainError:
                # A kept record that the row does not show can mean part of the version is in
                # the folder, so the row asks for Undo; the page then offers it.
                if _is_applied(optimization_id) and not _says_applied(job_store, optimization_id):
                    _record_apply(job_store, optimization_id, applied=False, undo_needed=True)
                raise
            return _record_apply(job_store, optimization_id, applied=True)

    @router.post(
        "/optimizations/{optimization_id}/repository/undo",
        summary="Put a local run's folder back as it was before Apply",
    )
    def undo_in_folder(optimization_id: str, current_user: AuthenticatedUserDep) -> dict[str, Any]:
        """Restore every file Apply changed in a local folder run's folder.

        Args:
            optimization_id: The local folder run.
            current_user: Authenticated caller resolved from the bearer token.

        Returns:
            ``{"applied": False, "applied_at"}``, which keeps the first Apply's time,
            also when nothing was applied.

        Raises:
            DomainError: 403 below the editor role; 404 when the run is not a
                local folder run; 409 away from the desktop app, while the run
                runs, when a file changed after Apply, or when the run is
                applied but its Undo record is gone.
        """
        job_data, _role = require_role_at_least(job_store, optimization_id, current_user, ShareRole.editor)
        repo, _commit = _local_run(job_data, optimization_id)
        with APPLY_LOCK:
            if _is_applied(optimization_id):
                _change_folder(lambda: undo_version(repo["path"], optimization_id))
                # The record goes only after the row save, so a failed save leaves a repeat Undo that works.
                state = _record_apply(job_store, optimization_id, applied=False)
                _change_folder(lambda: forget_apply(optimization_id))
                return state
            if _says_applied(job_store, optimization_id):
                # Saving "not applied" here would hide that the folder still holds the version.
                raise DomainError("optimization.local_folder_undo_missing", status=409)
            return _record_apply(job_store, optimization_id, applied=False)

"""Read a repository run's files at its pinned commit. [INTERNAL]

Frontend-only (hidden from public docs):
- ``GET /optimizations/{id}/repository/tree`` — every file and folder at the commit.
- ``GET /optimizations/{id}/repository/file`` — one file's text at the commit.
- ``POST /optimizations/{id}/repository/archive`` — the whole repository with one version applied, as a zip.

Every version of a repository run is a patch against one pinned commit; the
run page applies the patches itself and only needs the base tree and files.
The repository host is read with the run owner's connection, since the commit belongs to
the run rather than to whoever is viewing it, and anonymously when the owner
has none, so public repositories still open.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ....connectors import repo_hosts
from ....connectors.github_repo import RepoFetchError, github_token, patched_repo_zip
from ....models import (
    BlackboxRepoArchiveRequest,
    BlackboxRepoFileResponse,
    BlackboxRepoTreeEntry,
    BlackboxRepoTreeResponse,
)
from ...auth import AuthenticatedUser, get_authenticated_user
from ...converters import job_owner
from ...errors import DomainError
from .._helpers import load_job_for_user

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

MAX_FILE_BYTES = 1024 * 1024
_TREE_CACHE_ENTRIES = 32
_FILE_CACHE_ENTRIES = 2048
_FILE_CACHE_BYTES = 64 * 1024 * 1024

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
        ``github``, ``gitlab`` or ``bitbucket``.
    """
    return repo_hosts.provider_of(job_data["payload"]["target"]["repo"])


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
            The repository, the commit, every path sorted, and whether the host
            truncated the tree.

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
        return cached

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
            The file's text, or flags for a missing, binary or over-1 MB file.

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
        owner = job_owner(job_data) or ""
        key = (owner, provider, repository, commit, clean)
        cached = _files.get(key)
        if cached is None:
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
        token = _owner_token(getattr(job_store, "engine", None), job_owner(job_data) or "", provider)
        workdir = Path(tempfile.mkdtemp(prefix="repo-archive-"))
        try:
            archive = patched_repo_zip(
                repository, commit, body.patch, token, workdir, **repo_hosts.git_kwargs(provider)
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

"""Convert a saved program into a private repository without running paid work."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ....billing.protected_credentials import scrub_execution_credentials
from ....connectors.github import API_URL, PROVIDER, _headers
from ....connectors.github_create_repo import (
    CreatedRepository,
    RepoCreateError,
    create_repository_with_files,
    existing_repository,
    push_initial_commit,
)
from ....connectors.github_repo import RepoFetchError, github_token
from ....connectors.transport import get_json
from ....models import BlackboxRunRequest, BlackboxRunResponse
from ....models.artifacts import ProgramArtifact
from ....storage.usage import json_byte_size
from ...auth import AuthenticatedUser, get_authenticated_user
from ...converters import job_owner, parse_overview
from ...observability import advisory_lock
from .._helpers import enforce_storage_quota, load_job_for_user
from ._program_repository import ProgramRepository, build_program_repository

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


class ProgramRepositoryResponse(BaseModel):
    optimization_id: str
    repository: str
    commit: str
    url: str


class ProgramRepositoryRequest(BaseModel):
    allow_placeholder_tools: bool = False


def _publish(token: str, name: str, files: dict[str, str]) -> CreatedRepository:
    """Create the private repository, or finish an identical interrupted first push.

    Args:
        token: The owner's GitHub connection, used only in the trusted parent.
        name: Deterministic name derived from the run id.
        files: Secret-free program files prepared before publication.

    Returns:
        The repository and its pinned commit.

    Raises:
        RepoCreateError: When a conflicting or public repository already exists.
    """
    existing = existing_repository(token, name)
    if existing is None:
        return create_repository_with_files(token, name, files, private=True)
    repository, branch = existing
    info = get_json(f"{API_URL}/repos/{repository}", provider=PROVIDER, headers=_headers(token))
    if info.get("private") is not True:
        raise RepoCreateError("The conversion repository must be private.")
    commit = push_initial_commit(repository, branch, files, "Initial commit", token)
    return CreatedRepository(repository, branch, commit)


def repository_run(
    job: dict[str, Any], package: ProgramRepository, repository: CreatedRepository
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build the replacement run while preserving its original records.

    Args:
        job: The owner's successful saved program run.
        package: Program files and the original evaluation cases.
        repository: Published repository whose first commit is the saved program.

    Returns:
        The repository request, result, and display overview.
    """
    original = scrub_execution_credentials(job["payload"])
    old_result = scrub_execution_credentials(job["result"])
    overview = {
        key: value
        for key, value in parse_overview(job).items()
        if key
        in {"name", "description", "username", "pinned", "model_name", "reflection_model", "token_source", "seed"}
    }
    task_model = original.get("task_model_config") or original.get("model_config")
    reflection_model = original.get("reflection_model_config") or task_model
    request = BlackboxRunRequest(
        name=overview.get("name") or original.get("name"),
        description=overview.get("description") or original.get("description"),
        username=job_owner(job),
        objective=original.get("description") or original.get("name") or "Improve the saved program's metric.",
        recipe="repo",
        target={
            "kind": "repo",
            "repo": {
                "repository": repository.full_name,
                "branch": repository.default_branch,
                "commit": repository.commit,
                "editable_paths": package.editable_paths,
                "created_by_skynet": True,
            },
        },
        scorer={"kind": "python", "metric_code": package.scorer_code, "model": task_model},
        reflection_model_config=reflection_model,
        cases=None,
        seed=original.get("seed"),
        seed_candidate="",
        strategy={"mode": "single", "engine": "gepa"},
        token_source=original.get("token_source") or "managed",
        is_private=True,
        max_cost_cents=original.get("max_cost_cents"),
    )
    score = old_result.get("optimized_test_metric")
    origin = {
        "optimization_id": job["optimization_id"],
        "baseline_test_metric": old_result.get("baseline_test_metric"),
        "optimized_test_metric": score,
        "split_counts": old_result.get("split_counts"),
        "metric_name": old_result.get("metric_name"),
        "dataset_rows_omitted": True,
        "retrieval_placeholder": bool(original.get("tool_source")),
    }
    result = BlackboxRunResponse(
        optimizer_name="gepa",
        strategy_mode="single",
        engine_used="gepa",
        baseline_score=score,
        best_score=score,
        metric_improvement=0.0 if score is not None else None,
        seed_candidate="",
        best_candidate="",
        runtime_seconds=old_result.get("runtime_seconds") or 0,
        num_lm_calls=old_result.get("num_lm_calls") or 0,
        details={
            "program_origin": origin,
            "score_provenance": "saved_program_test",
            "dataset_missing": True,
        },
    )
    overview.pop("payload_kind", None)
    overview.update(
        optimization_type="blackbox",
        module_name="blackbox",
        optimizer_name="gepa",
        composition="single",
        is_private=True,
        serving_enabled=False,
        program_repository={"repository": repository.full_name, "commit": repository.commit},
    )
    return request.model_dump(mode="json", by_alias=True), result.model_dump(mode="json"), overview


def convert_program_repository(
    job_store: Any, optimization_id: str, user: AuthenticatedUser, *, allow_placeholder_tools: bool = False
) -> ProgramRepositoryResponse:
    """Publish the owner's saved program and atomically switch its existing run to the repository.

    Args:
        job_store: Store of the run and the owner's GitHub connection.
        optimization_id: Successful program run to convert in place.
        user: Signed-in owner requesting the conversion.
        allow_placeholder_tools: Explicit consent to retain missing tools as non-executable placeholders.

    Returns:
        The repository location; repeated calls return the same conversion.

    Raises:
        HTTPException: When ownership, reconstruction, publication, or concurrency blocks conversion.
    """
    lock_key = int.from_bytes(
        hashlib.sha256(f"program-repository:{optimization_id}".encode()).digest()[:8], signed=True
    )
    with advisory_lock(job_store.engine, lock_key) as acquired:
        if not acquired:
            raise HTTPException(409, "This run is already being converted. Retry shortly.")
        job = load_job_for_user(job_store, optimization_id, user)
        if job_owner(job) != user.username:
            raise HTTPException(403, "Only the run's owner can convert it.")
        overview = parse_overview(job)
        converted = overview.get("program_repository")
        if converted:
            return ProgramRepositoryResponse(
                optimization_id=optimization_id, **converted, url=f"https://github.com/{converted['repository']}"
            )
        if job.get("status") != "success" or overview.get("optimization_type", "run") != "run":
            raise HTTPException(409, "Convert a successful saved program run.")
        try:
            artifact = ProgramArtifact.model_validate((job.get("result") or {}).get("program_artifact") or {})
            package = build_program_repository(
                optimization_id,
                artifact,
                overview,
                job["payload"],
                job["result"],
                allow_placeholder_tools=allow_placeholder_tools,
            )
            placeholder = CreatedRepository("owner/prepared", "main", "0" * 40)
            prepared = repository_run(job, package, placeholder)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        enforce_storage_quota(job_store, user.username, incoming_bytes=sum(json_byte_size(value) for value in prepared))
        try:
            token = github_token(job_store.engine, user.username)
            repository = _publish(token, f"skynet-run-{optimization_id}", package.files)
        except (RepoCreateError, RepoFetchError) as error:
            raise HTTPException(409, str(error)) from None
        payload, result, overview = repository_run(job, package, repository)
        job_store.update_job(
            optimization_id,
            optimization_type="blackbox",
            composition="single",
            payload=payload,
            result=result,
            payload_overview=overview,
            latest_metrics={
                "baseline_test_metric": result["baseline_score"],
                "optimized_test_metric": result["best_score"],
                "metric_improvement": result["metric_improvement"],
            },
        )
        return ProgramRepositoryResponse(
            optimization_id=optimization_id,
            repository=repository.full_name,
            commit=repository.commit,
            url=f"https://github.com/{repository.full_name}",
        )


def register_program_repository_routes(router: APIRouter, *, job_store: Any) -> None:
    """Register the explicit owner action that converts a saved program.

    Args:
        router: Optimizations router.
        job_store: Shared run store.
    """

    @router.post("/optimizations/{optimization_id}/convert-to-repository", response_model=ProgramRepositoryResponse)
    def convert(
        optimization_id: str, body: ProgramRepositoryRequest, user: AuthenticatedUserDep
    ) -> ProgramRepositoryResponse:
        """Convert the signed-in owner's run to a private repository.

        Args:
            optimization_id: Saved program run.
            body: Whether missing tools may become marked placeholders.
            user: Authenticated owner.

        Returns:
            The published repository location.
        """
        return convert_program_repository(
            job_store, optimization_id, user, allow_placeholder_tools=body.allow_placeholder_tools
        )

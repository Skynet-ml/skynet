"""Tests for pushing every version of a run on a repository Skynet created."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.connectors.github_publish import BEST_BRANCH, RepoPublishError
from core.connectors.github_repo import RepoSnapshot
from core.models.blackbox import BlackboxRepoSource
from core.worker import repo_staging

_COMMIT = "c" * 40
_EDIT = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
_OTHER = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 3\n"
_OUTSIDE = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"


def _payload(created: bool) -> dict[str, Any]:
    """Build a staged repository payload.

    Args:
        created: Whether Skynet created the repository.

    Returns:
        The payload.
    """
    repo = {
        "repository": "ada/starter",
        "branch": "main",
        "commit": _COMMIT,
        "editable_paths": ["src"],
        "created_by_skynet": created,
    }
    return {"target": {"kind": "repo", "repo": repo}}


def _staged(tmp_path: Path) -> repo_staging.StagedRepository:
    """Build a staged repository without fetching anything.

    Args:
        tmp_path: Pytest temp dir.

    Returns:
        The staged repository.
    """
    snapshot = RepoSnapshot(commit=_COMMIT, archive=tmp_path / "tree.tgz", readonly_paths=(), size_bytes=1)
    return repo_staging.StagedRepository(snapshot=snapshot, secrets={}, workdir=tmp_path)


def _result(best: str, best_score: float, versions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a finished repository result.

    Args:
        best: The best version's patch.
        best_score: Held-out score of the best version; the start scored 0.5.
        versions: The recorded versions.

    Returns:
        The result.
    """
    return {
        "seed_candidate": "",
        "best_candidate": best,
        "baseline_score": 0.5,
        "best_score": best_score,
        "versions": versions,
        "details": {},
    }


def _publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: dict[str, Any], *, created: bool = True
) -> tuple[dict[str, Any] | None, list[tuple[Any, ...]]]:
    """Publish a result with a fake GitHub push.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Pytest temp dir.
        result: Finished result.
        created: Whether Skynet created the repository.

    Returns:
        The recorded outcome and the arguments of each push.
    """
    calls: list[tuple[Any, ...]] = []

    def fake_push(repository: str, commit: str, versions: list[tuple[str, str]], best_id: str, token: str) -> Any:
        """Record the push and name a commit per branch.

        Args:
            repository: ``owner/name``.
            commit: Pinned commit.
            versions: ``(id, patch)`` pairs.
            best_id: Id of the best version.
            token: GitHub token.

        Returns:
            Branch to fake commit id.
        """
        calls.append((repository, commit, list(versions), best_id, token))
        pushed = {f"skynet/candidate-{version_id}": f"sha-{version_id}" for version_id, _ in versions}
        return {**pushed, BEST_BRANCH: f"sha-{best_id}"}

    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")
    monkeypatch.setattr(repo_staging, "push_candidates", fake_push)
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pytest.fail("opened a pull request branch"))
    outcome = repo_staging.publish_improvement(
        result,
        _payload(created),
        _staged(tmp_path),
        username="ada",
        engine=None,
        optimization_id="0123456789abcdef",
        app_url="https://skynetml.com",
    )
    return outcome, calls


def test_created_repository_gets_every_version_and_the_best_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each version goes to its own branch, and skynet/best points at the winner."""
    versions = [{"candidate": ""}, {"candidate": _OTHER}, {"candidate": _EDIT}]
    result = _result(_EDIT, 0.8, versions)

    outcome, calls = _publish(monkeypatch, tmp_path, result)

    assert calls == [
        (
            "ada/starter",
            _COMMIT,
            [("0123456789ab-1", ""), ("0123456789ab-2", _OTHER), ("0123456789ab-3", _EDIT)],
            "0123456789ab-3",
            "tok",
        )
    ]
    assert outcome == result["details"][repo_staging.CANDIDATE_BRANCHES_DETAIL]
    assert outcome is not None
    assert outcome["best_version"] == "0123456789ab-3"
    assert outcome["branches"][BEST_BRANCH] == "sha-0123456789ab-3"
    assert outcome["skipped"] == 0
    assert repo_staging.PULL_REQUEST_DETAIL not in result["details"]


def test_created_repository_points_best_at_the_start_without_an_improvement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run that beat nothing still pushes its versions; skynet/best stays on the start."""
    result = _result(_EDIT, 0.4, [{"candidate": _EDIT}])

    outcome, calls = _publish(monkeypatch, tmp_path, result)

    assert calls[0][2:4] == ([("0123456789ab-1", _EDIT), ("0123456789ab-start", "")], "0123456789ab-start")
    assert outcome is not None
    assert outcome["best_version"] == "0123456789ab-start"


def test_created_repository_never_pushes_a_version_that_breaks_the_path_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read-only edits stay out of the repository; the watch hook never removes a version (SKY-349)."""
    versions = [{"candidate": ""}, {"candidate": _OUTSIDE}]
    result = _result("", 0.9, versions)

    outcome, calls = _publish(monkeypatch, tmp_path, result)

    assert calls[0][2:4] == ([("0123456789ab-1", "")], "0123456789ab-1")
    assert outcome is not None
    assert outcome["skipped"] == 1


def test_second_run_on_a_created_repository_uses_new_branch_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A later run never reuses an earlier run's branch, so its push needs no rewrite."""
    result = _result(_EDIT, 0.8, [{"candidate": _EDIT}])
    _, first = _publish(monkeypatch, tmp_path, result)
    calls: list[Any] = []
    monkeypatch.setattr(repo_staging, "push_candidates", lambda *args: calls.append(args) or {BEST_BRANCH: "sha"})
    repo_staging.publish_candidates(
        _result(_OTHER, 0.8, [{"candidate": _OTHER}]),
        _payload(True),
        _staged(tmp_path),
        username="ada",
        engine=None,
        optimization_id="fedcba9876543210",
    )

    assert {version_id for version_id, _ in first[0][2]}.isdisjoint({version_id for version_id, _ in calls[0][2]})


def test_created_repository_push_failure_is_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The run keeps its result when GitHub refuses the push."""

    def refuse(*args: Any) -> Any:
        """Refuse every push.

        Args:
            *args: Ignored.

        Raises:
            RepoPublishError: Always.
        """
        raise RepoPublishError("Could not push the candidate branches: denied")

    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")
    monkeypatch.setattr(repo_staging, "push_candidates", refuse)
    result = _result(_EDIT, 0.8, [{"candidate": _EDIT}])

    outcome = repo_staging.publish_candidates(
        result, _payload(True), _staged(tmp_path), username="ada", engine=None, optimization_id="0123456789abcdef"
    )

    assert outcome == {"error": "Could not push the candidate branches: denied"}
    assert result["details"][repo_staging.CANDIDATE_BRANCHES_DETAIL] == outcome


def test_unexpected_push_failure_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unexpected error during the push still leaves the run's result intact."""

    def explode(engine: Any, username: str) -> str:
        """Fail to read the token.

        Args:
            engine: Ignored.
            username: Ignored.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("vault down")

    monkeypatch.setattr(repo_staging, "github_token", explode)
    result = _result(_EDIT, 0.8, [{"candidate": _EDIT}])

    outcome = repo_staging.publish_candidates(
        result, _payload(True), _staged(tmp_path), username="ada", engine=None, optimization_id="0123456789abcdef"
    )

    assert outcome == {"error": "The candidate branches could not be pushed."}


def test_existing_repository_keeps_the_pull_request_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A repository Skynet did not create never gets candidate branches."""
    monkeypatch.setattr(repo_staging, "push_candidates", lambda *args: pytest.fail("pushed candidate branches"))
    monkeypatch.setattr(repo_staging, "improved_patch", lambda result: None)

    outcome = repo_staging.publish_improvement(
        _result(_EDIT, 0.4, []),
        _payload(False),
        _staged(tmp_path),
        username="ada",
        engine=None,
        optimization_id="0123456789abcdef",
        app_url="https://skynetml.com",
    )

    assert outcome is None


def test_repo_source_marker_defaults_to_an_existing_repository() -> None:
    """Old payloads without the marker keep the pull request path."""
    source = BlackboxRepoSource(repository="ada/starter", editable_paths=["src"])

    assert source.created_by_skynet is False
    assert BlackboxRepoSource(
        repository="ada/starter", editable_paths=["src"], created_by_skynet=True
    ).created_by_skynet


def test_created_repository_keeps_only_the_top_scoring_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run with many versions pushes the best one plus the next top scorers, in run order."""
    patches = [_EDIT.replace("+x = 2", f"+x = {value}") for value in range(10, 25)]
    versions = [{"candidate": patch, "score": index / 100} for index, patch in enumerate(patches)]
    versions[0]["score"] = 0.0
    best = patches[3]
    result = _result(best, 0.9, versions)

    outcome, calls = _publish(monkeypatch, tmp_path, result)

    pushed_ids = [version_id for version_id, _ in calls[0][2]]
    assert len(pushed_ids) == repo_staging.MAX_CANDIDATE_BRANCHES
    assert pushed_ids == ["0123456789ab-4", *[f"0123456789ab-{index}" for index in range(7, 16)]]
    assert outcome is not None
    assert outcome["best_version"] == "0123456789ab-4"
    assert outcome["left_out"] == len(patches) - repo_staging.MAX_CANDIDATE_BRANCHES

"""Tests for ``POST /connectors/github/repos``, which creates a seeded repository.

The GitHub credential check runs against a mocked :mod:`httpx` boundary, and
the create library is replaced in the router module, so no test reaches the
network or runs git.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest

from ...connectors.github_create_repo import CreatedRepository, RepoCreateError
from ..routers import connectors as connectors_router
from .test_connectors_providers import generic_oauth_off  # noqa: F401 - fixture re-export
from .test_connectors_router import _make_client, _response, vault_key  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.usefixtures("vault_key", "generic_oauth_off")


def _github_user_api(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Answer the token check GitHub runs when the credential is saved.

    Args:
        method: HTTP method.
        url: Request URL.
        **kwargs: Request options.

    Returns:
        The canned response.
    """
    if urlparse(url).path == "/user":
        return _response(200, {"login": "octo"})
    raise AssertionError(f"unexpected call {method} {url}")


def _linked_client():
    """Build a client whose caller has a linked GitHub token.

    Returns:
        The test client.
    """
    client, _ = _make_client()
    with patch("core.connectors.transport.CLIENT.request", side_effect=_github_user_api):
        saved = client.put("/connectors/github/credentials", json={"fields": {"token": "ghp_secret"}})
    assert saved.status_code == 200, saved.text
    return client


def test_create_seeds_readme_with_objective() -> None:
    """The route passes the token, visibility and a README holding the objective."""
    client = _linked_client()
    created = CreatedRepository(full_name="octo/new-app", default_branch="main", commit="a" * 40)
    with patch.object(connectors_router, "create_repository_with_files", return_value=created) as create:
        response = client.post(
            "/connectors/github/repos",
            json={"name": "new-app", "private": False, "objective": "  Sort numbers fast.  "},
        )
    assert response.status_code == 201, response.text
    assert response.json() == {"full_name": "octo/new-app", "default_branch": "main", "commit": "a" * 40}
    args, kwargs = create.call_args
    assert args == ("ghp_secret", "new-app", {"README.md": "# new-app\n\nSort numbers fast.\n"})
    assert kwargs == {"private": False, "description": None}


_CASES = [{"input": "3,1,2", "expected": "1,2,3"}, {"input": "שלום", "expected": "ok"}]
_CASES_JSONL = '{"input": "3,1,2", "expected": "1,2,3"}\n{"input": "שלום", "expected": "ok"}\n'
_README = "# app\n\nSort.\n"


@pytest.mark.parametrize(
    ("extra", "files"),
    [
        ({}, {"README.md": _README}),
        (
            {"scorer_code": "def score(x):\n    return 1.0"},
            {"README.md": _README, "scorer.py": "def score(x):\n    return 1.0\n"},
        ),
        ({"cases": _CASES}, {"README.md": _README, "data/cases.jsonl": _CASES_JSONL}),
        (
            {"scorer_code": "def score(x):\n    return 1.0\n", "cases": _CASES},
            {"README.md": _README, "scorer.py": "def score(x):\n    return 1.0\n", "data/cases.jsonl": _CASES_JSONL},
        ),
        ({"scorer_code": "   ", "cases": []}, {"README.md": _README}),
    ],
    ids=["readme-only", "with-scorer", "with-cases", "with-scorer-and-cases", "blank-scorer-empty-cases"],
)
def test_create_commits_each_file_combination(extra: dict[str, Any], files: dict[str, str]) -> None:
    """The first commit holds the README, plus the scorer and the cases file only when given."""
    client = _linked_client()
    created = CreatedRepository(full_name="octo/app", default_branch="main", commit="c" * 40)
    with patch.object(connectors_router, "create_repository_with_files", return_value=created) as create:
        response = client.post("/connectors/github/repos", json={"name": "app", "objective": "Sort.", **extra})
    assert response.status_code == 201, response.text
    assert create.call_args.args[2] == files


def test_create_rejects_oversized_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cases over the byte cap get 413 and nothing is created."""
    monkeypatch.setattr(connectors_router, "GITHUB_CASES_MAX_BYTES", 100)
    client = _linked_client()
    with patch.object(connectors_router, "create_repository_with_files") as create:
        response = client.post(
            "/connectors/github/repos",
            json={"name": "app", "objective": "Sort.", "cases": [{"input": "x" * 200}]},
        )
    assert response.status_code == 413
    create.assert_not_called()


def test_create_defaults_to_private() -> None:
    """A body without ``private`` creates a private repository."""
    client = _linked_client()
    created = CreatedRepository(full_name="octo/x", default_branch="main", commit="b" * 40)
    with patch.object(connectors_router, "create_repository_with_files", return_value=created) as create:
        client.post("/connectors/github/repos", json={"name": "x", "objective": "Do it."})
    assert create.call_args.kwargs["private"] is True


@pytest.mark.parametrize(
    ("message", "status"),
    [
        ("Your GitHub account already has a repository named 'x'. Choose another name.", 409),
        ("GitHub does not accept 'x' as a repository name.", 422),
        ("GitHub refused to create the repository. Connect GitHub again and retry.", 502),
        ("Could not push the first commit: boom", 502),
    ],
)
def test_create_maps_library_errors(message: str, status: int) -> None:
    """Each create failure keeps its user-facing text and gets a fitting status."""
    client = _linked_client()
    with patch.object(connectors_router, "create_repository_with_files", side_effect=RepoCreateError(message)):
        response = client.post("/connectors/github/repos", json={"name": "x", "objective": "Do it."})
    assert response.status_code == status
    assert response.json()["detail"] == message


def test_create_without_github_link_is_409() -> None:
    """A caller with no GitHub link gets ``connectors.not_connected`` and nothing is created."""
    client, _ = _make_client()
    with patch.object(connectors_router, "create_repository_with_files") as create:
        response = client.post("/connectors/github/repos", json={"name": "x", "objective": "Do it."})
    assert response.status_code == 409
    assert response.json()["code"] == "connectors.not_connected"
    create.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"name": "has space", "objective": "Do it."},
        {"name": "a/b", "objective": "Do it."},
        {"name": "x" * 101, "objective": "Do it."},
        {"name": "ok", "objective": ""},
        {"name": "ok", "objective": "Do it.", "scorer_code": "x" * 200_001},
        {"name": "ok", "objective": "Do it.", "cases": [{}] * 10_001},
        {"name": "ok", "objective": "Do it.", "cases": ["not an object"]},
    ],
)
def test_create_rejects_bad_body(body: dict[str, Any]) -> None:
    """A bad name, an empty objective, or an oversized scorer or case list fails validation first."""
    client = _linked_client()
    with patch.object(connectors_router, "create_repository_with_files") as create:
        response = client.post("/connectors/github/repos", json=body)
    assert response.status_code == 422
    create.assert_not_called()

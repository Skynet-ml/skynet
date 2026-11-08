"""Bitbucket connector: list Bitbucket Cloud repositories, read their files and open pull requests.

Linking works through a Bitbucket OAuth consumer, or a pasted access token
(a repository, project or workspace access token) sent as a bearer token.
The functions mirror the GitHub module's names and return shapes, so the
repository picker, the agents and the run worker treat every host alike.
Bitbucket is a repository host only: it has no data-file browser.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..api.errors import DomainError
from ..config import settings
from .base import Credential, Entry, range_header
from .github import REPO_NAME, clean_file_path
from .github_publish import HardeningSummary, PublishedChange, RepoPublishError, hardening_line
from .github_repo import COMMIT_LIMIT, RepoFetchError, commit_summary
from .gitlab import display_file, matches_search, repo_path, search_words
from .oauth import OAuthApp
from .oauth import oauth_available as _oauth_available
from .transport import download, get_json, label, request
from .vault import ConnectorSecret

PROVIDER = "bitbucket"
WEB_URL = "https://bitbucket.org"
API_URL = "https://api.bitbucket.org/2.0"
PAGE_SIZE = 100
REPO_LIMIT = 100
BRANCH_LIMIT = 300
# Bitbucket lists a tree one page of 100 entries at a time; past this many pages it is reported as truncated.
TREE_PAGE_LIMIT = 50
TREE_DEPTH = 64
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def oauth_app() -> OAuthApp:
    """Describe the Bitbucket OAuth consumer from settings.

    Returns:
        The app; ``client_id`` is ``None`` when unconfigured. Scopes are set
        on the consumer itself, so the flow sends none.
    """
    secret = settings.bitbucket_oauth_client_secret
    return OAuthApp(
        provider=PROVIDER,
        authorize_url=f"{WEB_URL}/site/oauth2/authorize",
        token_url=f"{WEB_URL}/site/oauth2/access_token",
        scopes="",
        client_id=settings.bitbucket_oauth_client_id,
        client_secret=secret.get_secret_value() if secret is not None else None,
        extra_authorize_params={},
        basic_auth_form=True,
    )


def oauth_available() -> bool:
    """Report whether "Continue with Bitbucket" can be offered.

    Returns:
        ``True`` when the client id and the vault key are configured.
    """
    return _oauth_available(oauth_app())


def _headers(token: str | None) -> dict[str, str]:
    """Build bearer headers for the Bitbucket API.

    Args:
        token: OAuth or access token; empty calls anonymously.

    Returns:
        The headers.
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_account_label(token: str) -> str | None:
    """Look up the username behind a token.

    Args:
        token: A Bitbucket token.

    Returns:
        The username, or ``None`` when the lookup fails.
    """
    try:
        body = get_json(f"{API_URL}/user", provider=PROVIDER, headers=_headers(token))
    except DomainError:
        return None
    username = body.get("username") if isinstance(body, dict) else None
    return username if isinstance(username, str) else None


def verify_credentials(fields: dict[str, str]) -> Credential:
    """Validate a pasted access token against ``/user``.

    Args:
        fields: ``{"token": ...}``.

    Returns:
        The credential to store, labelled with the username.

    Raises:
        DomainError: 400 when Bitbucket rejects the token.
    """
    token = fields.get("token", "").strip()
    if not token:
        raise DomainError("connectors.invalid_credentials", status=400)
    try:
        body = get_json(f"{API_URL}/user", provider=PROVIDER, headers=_headers(token))
    except DomainError as exc:
        if exc.code == "connectors.rejected":
            raise DomainError("connectors.invalid_credentials", status=400) from exc
        raise
    username = body.get("username") if isinstance(body, dict) else None
    return Credential(secret=token, auth_method="token", account_label=username if isinstance(username, str) else None)


def browse(secret: ConnectorSecret, location: str, search: str) -> list[Entry]:
    """Refuse data-file browsing, which Bitbucket does not offer.

    Args:
        secret: The stored connector.
        location: Ignored.
        search: Ignored.

    Raises:
        DomainError: 404 always.
    """
    raise DomainError("connectors.unknown_provider", status=404, provider=PROVIDER)


def preview(secret: ConnectorSecret, ref: str) -> dict[str, Any]:
    """Refuse data-file previews, which Bitbucket does not offer.

    Args:
        secret: The stored connector.
        ref: Ignored.

    Raises:
        DomainError: 404 always.
    """
    raise DomainError("connectors.unknown_provider", status=404, provider=PROVIDER)


def import_ref(secret: ConnectorSecret, ref: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Refuse data-file imports, which Bitbucket does not offer.

    Args:
        secret: The stored connector.
        ref: Ignored.

    Raises:
        DomainError: 404 always.
    """
    raise DomainError("connectors.unknown_provider", status=404, provider=PROVIDER)


def _repo_url(full_name: str) -> str:
    """Build the API URL of one repository.

    Args:
        full_name: The repository as ``workspace/slug``.

    Returns:
        The URL.

    Raises:
        DomainError: 400 when the value is not ``workspace/slug`` or names another host.
    """
    value = repo_path(full_name, WEB_URL, PROVIDER, segments=2)
    if not REPO_NAME.match(value):
        raise DomainError("connectors.invalid_ref", status=400)
    return f"{API_URL}/repositories/{value}"


def _repo_summary(repo: dict[str, Any]) -> dict[str, Any]:
    """Map a Bitbucket repository to the fields the repository picker shows.

    Args:
        repo: A repository object from the Bitbucket API.

    Returns:
        ``full_name``, ``private``, ``description``, ``language``,
        ``default_branch`` and ``pushed_at``.
    """
    description = repo.get("description")
    language = repo.get("language")
    main = repo.get("mainbranch")
    default = main.get("name") if isinstance(main, dict) else None
    updated = repo.get("updated_on")
    return {
        "full_name": repo["full_name"],
        "private": bool(repo.get("is_private", True)),
        "description": description if isinstance(description, str) and description else None,
        "language": language if isinstance(language, str) and language else None,
        "default_branch": default if isinstance(default, str) else None,
        "pushed_at": updated if isinstance(updated, str) else None,
    }


def _pages(url: str, token: str | None, params: dict[str, Any], limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Collect ``values`` from a paginated Bitbucket list, following ``next``.

    Args:
        url: The first page.
        token: Bearer token.
        params: Query of the first page; ``next`` links carry their own.
        limit: Stop after this many pages.

    Returns:
        ``(values, more)``; ``more`` is set when pages were left unread.
    """
    values: list[dict[str, Any]] = []
    next_url: str | None = url
    query: dict[str, Any] | None = params
    for _ in range(limit):
        body = get_json(next_url, provider=PROVIDER, headers=_headers(token), params=query)
        if not isinstance(body, dict):
            raise DomainError("connectors.provider_error", status=502, provider=label(PROVIDER), status_code=200)
        values.extend(v for v in body.get("values") or [] if isinstance(v, dict))
        link = body.get("next")
        if not isinstance(link, str) or not link.startswith(f"{API_URL}/"):
            return values, False
        next_url, query = link, None
    return values, True


def list_repositories(secret: ConnectorSecret, search: str) -> list[dict[str, Any]]:
    """List the repositories the account is a member of, most recently updated first.

    A ``search`` shaped like ``workspace/slug`` that the list lacks is looked
    up directly, so a public repository outside the account can be picked.

    Args:
        secret: The stored connector.
        search: Case-insensitive words matched against the name and the description.

    Returns:
        Repository summaries, see :func:`_repo_summary`.
    """
    token = secret.access_token
    found, _ = _pages(
        f"{API_URL}/repositories", token, {"role": "member", "sort": "-updated_on", "pagelen": REPO_LIMIT}, 1
    )
    needle = repo_path(search, WEB_URL, PROVIDER, segments=2)
    words = search_words(needle)
    repos = [
        summary
        for summary in (_repo_summary(r) for r in found if isinstance(r.get("full_name"), str))
        if matches_search(summary, words)
    ]
    if REPO_NAME.match(needle) and not any(r["full_name"].lower() == needle.lower() for r in repos):
        try:
            direct = get_json(_repo_url(needle), provider=PROVIDER, headers=_headers(token))
        except DomainError as exc:
            if exc.code != "connectors.not_found":
                raise
            direct = None
        if isinstance(direct, dict) and isinstance(direct.get("full_name"), str):
            repos.insert(0, _repo_summary(direct))
    return repos


def _default_branch(token: str | None, full_name: str) -> str | None:
    """Look up a repository's main branch.

    Args:
        token: Bearer token.
        full_name: ``workspace/slug``.

    Returns:
        The branch name, or ``None`` for an empty repository.
    """
    meta = get_json(_repo_url(full_name), provider=PROVIDER, headers=_headers(token))
    main = meta.get("mainbranch") if isinstance(meta, dict) else None
    name = main.get("name") if isinstance(main, dict) else None
    return name if isinstance(name, str) else None


def list_branches(secret: ConnectorSecret, full_name: str) -> dict[str, Any]:
    """List a repository's branch names with its main branch.

    Args:
        secret: The stored connector.
        full_name: ``workspace/slug``.

    Returns:
        ``{"default_branch": str | None, "branches": [name, ...]}``: up to
        :data:`BRANCH_LIMIT` names, plus the main branch when the cap cut it.
    """
    token = secret.access_token
    default = _default_branch(token, full_name)
    found, _ = _pages(
        f"{_repo_url(full_name)}/refs/branches", token, {"pagelen": PAGE_SIZE}, -(-BRANCH_LIMIT // PAGE_SIZE)
    )
    branches = [b["name"] for b in found if isinstance(b.get("name"), str)][:BRANCH_LIMIT]
    if default and default not in branches:
        branches.insert(0, default)
    return {"default_branch": default, "branches": branches}


def list_commits(secret: ConnectorSecret, full_name: str, branch: str) -> list[dict[str, Any]]:
    """List the newest commits of a repository branch, newest first.

    ``include`` names the branch as a query value, so a branch with a slash
    works, unlike the ``/commits/{revision}`` path form.

    Args:
        secret: The stored connector.
        full_name: ``workspace/slug``.
        branch: Branch name; empty for the main branch.

    Returns:
        Up to :data:`COMMIT_LIMIT` summaries, see :func:`commit_summary`.
    """
    token = secret.access_token
    ref = branch.strip() or _default_branch(token, full_name) or ""
    if not ref:
        return []
    body = get_json(
        f"{_repo_url(full_name)}/commits",
        provider=PROVIDER,
        headers=_headers(token),
        params={"include": ref, "pagelen": COMMIT_LIMIT},
    )
    values = body.get("values") if isinstance(body, dict) else None
    found = (
        commit_summary(c.get("hash"), c.get("message"), c.get("date")) for c in values or [] if isinstance(c, dict)
    )
    return [c for c in found if c][:COMMIT_LIMIT]


def _commit(token: str | None, full_name: str, ref: str) -> str:
    """Resolve a branch, tag or commit to a commit hash.

    Bitbucket's ``/src`` paths cannot hold a branch name with a slash, so
    every read first pins the ref to a hash.

    Args:
        token: Bearer token.
        full_name: ``workspace/slug``.
        ref: Branch, tag or commit; empty for the main branch.

    Returns:
        The 40-character commit hash.
    """
    value = ref.strip()
    if COMMIT_SHA.match(value):
        return value
    value = value or _default_branch(token, full_name) or ""
    if not value:
        raise DomainError("connectors.not_found", status=404, provider=label(PROVIDER))
    body = get_json(
        f"{_repo_url(full_name)}/commit/{quote(value, safe='')}", provider=PROVIDER, headers=_headers(token)
    )
    return str(body["hash"])


def repository_tree(secret: ConnectorSecret, full_name: str, branch: str) -> dict[str, Any]:
    """List every file and folder of a repository at a branch.

    Args:
        secret: The stored connector.
        full_name: ``workspace/slug``.
        branch: Branch name; empty for the main branch.

    Returns:
        See :func:`tree_entries`.
    """
    return tree_entries(secret.access_token, full_name, branch)


def tree_entries(token: str | None, full_name: str, branch: str) -> dict[str, Any]:
    """List every file and folder of a repository at a ref with a bare token.

    Args:
        token: Bearer token with read access; ``None`` for a public repository.
        full_name: ``workspace/slug``.
        branch: Branch name or commit hash; empty for the main branch.

    Returns:
        ``{"entries": [{"path", "type": "file" | "dir", "size"?}], "truncated": bool}``.
    """
    sha = _commit(token, full_name, branch)
    kinds = {"commit_file": "file", "commit_directory": "dir"}
    found, more = _pages(
        f"{_repo_url(full_name)}/src/{sha}/", token, {"max_depth": TREE_DEPTH, "pagelen": PAGE_SIZE}, TREE_PAGE_LIMIT
    )
    entries: list[dict[str, Any]] = []
    for item in found:
        kind = kinds.get(item.get("type"))  # type: ignore[arg-type]
        path = item.get("path")
        if kind is None or not isinstance(path, str):
            continue
        entry: dict[str, Any] = {"path": path, "type": kind}
        if kind == "file" and isinstance(item.get("size"), int):
            entry["size"] = item["size"]
        entries.append(entry)
    return {"entries": entries, "truncated": more}


def _src_url(full_name: str, sha: str, path: str) -> str:
    """Build the ``/src`` URL of one file at a commit.

    Args:
        full_name: ``workspace/slug``.
        sha: Commit hash.
        path: File path inside the repository.

    Returns:
        The URL, with each path segment encoded.
    """
    return f"{_repo_url(full_name)}/src/{sha}/{quote(path.strip('/'), safe='/')}"


def read_text_file(token: str, full_name: str, branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
    """Read one file of a repository at a branch as text, up to a byte cap.

    Args:
        token: Bearer token with read access to the repository.
        full_name: ``workspace/slug``.
        branch: Branch name; empty for the main branch.
        path: File path inside the repository.
        max_bytes: Stop reading after this many bytes.

    Returns:
        ``(text, truncated)``; undecodable bytes are replaced.
    """
    content, truncated = download(
        _src_url(full_name, _commit(token, full_name, branch), path),
        provider=PROVIDER,
        headers={**_headers(token), **range_header(max_bytes)},
        max_bytes=max_bytes,
    )
    return content.decode("utf-8", errors="replace"), truncated


def file_at(token: str | None, full_name: str, ref: str, path: str, max_bytes: int) -> dict[str, Any]:
    """Read one file of a repository at a ref as raw bytes, unless it is too large.

    Args:
        token: Bearer token with read access; ``None`` for a public repository.
        full_name: ``workspace/slug``.
        ref: Commit hash, branch or tag; empty for the main branch.
        path: File path inside the repository.
        max_bytes: Files larger than this are reported but not read.

    Returns:
        ``{"content": bytes | None, "size": int | None, "missing": bool,
        "too_large": bool}``; ``missing`` covers an absent path or a folder.

    Raises:
        DomainError: When Bitbucket refuses the read for any reason other
            than the path being absent.
    """
    absent = {"content": None, "size": None, "missing": True, "too_large": False}
    try:
        url = _src_url(full_name, _commit(token, full_name, ref), path)
        meta = get_json(url, provider=PROVIDER, headers=_headers(token), params={"format": "meta"})
    except DomainError as exc:
        if exc.code != "connectors.not_found":
            raise
        return absent
    if not isinstance(meta, dict) or meta.get("type") != "commit_file":
        return absent
    size = meta.get("size") if isinstance(meta.get("size"), int) else None
    if size is not None and size > max_bytes:
        return {"content": None, "size": size, "missing": False, "too_large": True}
    content, truncated = download(url, provider=PROVIDER, headers=_headers(token), max_bytes=max_bytes)
    if truncated:
        return {"content": None, "size": size, "missing": False, "too_large": True}
    return {"content": content, "size": len(content), "missing": False, "too_large": False}


def read_repo_file(token: str | None, full_name: str, ref: str, path: str, max_bytes: int) -> dict[str, Any]:
    """Read one repository file for display, decoding text and flagging everything else.

    Args:
        token: Bearer token with read access; ``None`` for a public repository.
        full_name: ``workspace/slug``.
        ref: Commit hash or branch; empty for the main branch.
        path: File path inside the repository.
        max_bytes: Files larger than this are reported but not read.

    Returns:
        ``{"path", "text": str | None, "binary", "too_large", "missing"}``.
    """
    clean = clean_file_path(path)
    return display_file(clean, file_at(token, full_name, ref, clean, max_bytes))


def resolve_commit(token: str, repository: str, branch: str | None) -> str:
    """Pin a branch, or the main branch, to its current commit.

    Args:
        token: Bitbucket token with read access to the repository.
        repository: ``workspace/slug``.
        branch: Branch to pin; ``None`` picks the main branch.

    Returns:
        The 40-character commit hash.

    Raises:
        RepoFetchError: When Bitbucket refuses the lookup.
    """
    try:
        return _commit(token, repository, branch or "")
    except DomainError as error:
        raise RepoFetchError(f"Bitbucket could not find {repository} at '{branch or 'its main branch'}'.") from error


def open_change(
    token: str,
    repository: str,
    *,
    head: str,
    base_branch: str | None,
    title: str,
    body: str,
    hardening: HardeningSummary | None = None,
) -> PublishedChange:
    """Open a draft pull request from a pushed branch, or a regular one where drafts are refused.

    Args:
        token: Bitbucket token with pull request write access.
        repository: ``workspace/slug``.
        head: The pushed branch.
        base_branch: Branch to merge into; ``None`` picks the main branch.
        title: Pull request title.
        body: Pull request description.
        hardening: The run's finished hardening job; ``None`` keeps the body as given.

    Returns:
        The opened pull request.

    Raises:
        RepoPublishError: When Bitbucket refuses the request.
    """
    if hardening is not None:
        body = f"{body}\n\n{hardening_line(hardening)}" if body else hardening_line(hardening)
    url = f"{_repo_url(repository)}/pullrequests"
    try:
        target = base_branch or _default_branch(token, repository)
        if not target:
            raise RepoPublishError("The Bitbucket repository has no main branch to merge into.")
        fields = {
            "title": title,
            "description": body,
            "source": {"branch": {"name": head}},
            "destination": {"branch": {"name": target}},
        }
        try:
            response = request("POST", url, provider=PROVIDER, headers=_headers(token), json={**fields, "draft": True})
        except DomainError as error:
            # Workspaces without draft pull requests answer 400; open a regular one instead.
            if error.params.get("status_code") != 400:
                raise
            response = request("POST", url, provider=PROVIDER, headers=_headers(token), json=fields)
    except DomainError as error:
        raise RepoPublishError("Bitbucket refused to open the pull request.") from error
    opened = response.json()
    return PublishedChange(
        branch=head,
        url=str(opened["links"]["html"]["href"]),
        number=int(opened["id"]),
        draft=bool(opened.get("draft")),
    )

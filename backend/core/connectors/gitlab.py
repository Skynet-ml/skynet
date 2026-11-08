"""GitLab connector: list gitlab.com projects, read their files and open merge requests.

Linking works through a gitlab.com OAuth application (``api`` scope, so the
run can push a branch and open a merge request) or a pasted personal access
token. The functions mirror the GitHub module's names and return shapes, so
the repository picker, the agents and the run worker treat both hosts alike.
GitLab is a repository host only: it has no data-file browser.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ..api.errors import DomainError
from ..config import settings
from .base import Credential, Entry, range_header
from .github import clean_file_path
from .github_publish import HardeningSummary, PublishedChange, RepoPublishError, hardening_line
from .github_repo import COMMIT_LIMIT, RepoFetchError, commit_summary
from .oauth import OAuthApp
from .oauth import oauth_available as _oauth_available
from .transport import download, get_json, label, request
from .vault import ConnectorSecret

PROVIDER = "gitlab"
WEB_URL = "https://gitlab.com"
API_URL = f"{WEB_URL}/api/v4"
SCOPES = "api read_user"
PAGE_SIZE = 100
REPO_LIMIT = 100
BRANCH_LIMIT = 300
# 50 pages of 100 entries; a larger tree is reported as truncated.
TREE_PAGE_LIMIT = 50
# GitLab projects can sit in nested subgroups: ``group/sub/project``.
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}(/[A-Za-z0-9_.-]{1,255}){1,20}$")
DRAFT_PREFIX = "Draft: "
# ``git@host:path`` clone addresses, which ``urlsplit`` does not parse.
SCP_URL = re.compile(r"^[\w.-]+@([^:/\s]+):(.+)$")


def repo_path(value: str, web_url: str, provider: str, segments: int | None = None) -> str:
    """Turn a pasted web or clone URL into the host's repository path.

    The host name is compared without case and without its port. A trailing
    ``.git`` and outer slashes are dropped, and so is any page, query or
    fragment after the repository in a URL (``/-/tree/...`` on GitLab,
    ``/src/...`` on Bitbucket). Percent-encoded URL paths are decoded.

    Args:
        value: What the user or an agent gave: a path or a URL.
        web_url: The host's web address, for example ``https://gitlab.com``.
        provider: Connector name for the refusal.
        segments: Keep at most this many URL path segments; ``None`` keeps all.

    Returns:
        The repository path, for example ``group/sub/project``.

    Raises:
        DomainError: 400 ``other_repo_host`` when the URL names another host,
            such as a self-managed GitLab server; 400 ``invalid_ref`` when the
            URL cannot be parsed or has no host.
    """
    text = value.strip()
    scp = SCP_URL.match(text)
    if scp or "://" in text:
        if scp:
            host_name, path = scp.group(1), scp.group(2)
        else:
            try:
                parts = urlsplit(text)
                host_name, path = parts.hostname or "", unquote(parts.path)
            except ValueError as exc:
                raise DomainError("connectors.invalid_ref", status=400) from exc
        host_name = host_name.lower().rstrip(".")
        if not host_name:
            raise DomainError("connectors.invalid_ref", status=400)
        expected = urlsplit(web_url).hostname or ""
        if host_name not in (expected, f"www.{expected}"):
            raise DomainError(
                "connectors.other_repo_host", status=400, provider=label(provider), host=host_name, expected=expected
            )
        text = path.split("/-/")[0]
        if segments is not None:
            text = "/".join(text.strip("/").split("/")[:segments])
    return text.strip("/").removesuffix(".git")


def has_dot_segment(path: str) -> bool:
    """Tell whether a repository path has a ``.`` or ``..`` segment.

    Args:
        path: The repository path.

    Returns:
        ``True`` when a segment is ``.`` or ``..``.
    """
    return any(part in (".", "..") for part in path.split("/"))


def oauth_app() -> OAuthApp:
    """Describe the gitlab.com OAuth application from settings.

    Returns:
        The app; ``client_id`` is ``None`` when unconfigured.
    """
    secret = settings.gitlab_oauth_client_secret
    return OAuthApp(
        provider=PROVIDER,
        authorize_url=f"{WEB_URL}/oauth/authorize",
        token_url=f"{WEB_URL}/oauth/token",
        scopes=SCOPES,
        client_id=settings.gitlab_oauth_client_id,
        client_secret=secret.get_secret_value() if secret is not None else None,
        extra_authorize_params={},
    )


def oauth_available() -> bool:
    """Report whether "Continue with GitLab" can be offered.

    Returns:
        ``True`` when the client id and the vault key are configured.
    """
    return _oauth_available(oauth_app())


def _headers(token: str | None) -> dict[str, str]:
    """Build bearer headers for the GitLab API.

    Args:
        token: OAuth or personal access token; empty calls anonymously.

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
        token: A GitLab token.

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
    """Validate a pasted personal access token against ``/user``.

    Args:
        fields: ``{"token": ...}``.

    Returns:
        The credential to store, labelled with the username.

    Raises:
        DomainError: 400 when GitLab rejects the token.
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
    """Refuse data-file browsing, which GitLab does not offer.

    Args:
        secret: The stored connector.
        location: Ignored.
        search: Ignored.

    Raises:
        DomainError: 404 always.
    """
    raise DomainError("connectors.unknown_provider", status=404, provider=PROVIDER)


def preview(secret: ConnectorSecret, ref: str) -> dict[str, Any]:
    """Refuse data-file previews, which GitLab does not offer.

    Args:
        secret: The stored connector.
        ref: Ignored.

    Raises:
        DomainError: 404 always.
    """
    raise DomainError("connectors.unknown_provider", status=404, provider=PROVIDER)


def import_ref(secret: ConnectorSecret, ref: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Refuse data-file imports, which GitLab does not offer.

    Args:
        secret: The stored connector.
        ref: Ignored.

    Raises:
        DomainError: 404 always.
    """
    raise DomainError("connectors.unknown_provider", status=404, provider=PROVIDER)


def _project_url(full_name: str) -> str:
    """Build the API URL of one project from its path.

    Args:
        full_name: The project as ``group[/subgroup]/name``.

    Returns:
        The URL.

    Raises:
        DomainError: 400 when the value is not a project path or names another host.
    """
    value = repo_path(full_name, WEB_URL, PROVIDER)
    if not REPO_NAME.match(value) or has_dot_segment(value):
        raise DomainError("connectors.invalid_ref", status=400)
    return f"{API_URL}/projects/{quote(value, safe='')}"


def _repo_summary(project: dict[str, Any]) -> dict[str, Any]:
    """Map a GitLab project to the fields the repository picker shows.

    Args:
        project: A project object from the GitLab API.

    Returns:
        ``full_name``, ``private``, ``description``, ``language``,
        ``default_branch`` and ``pushed_at``; GitLab lists no main language.
    """
    description = project.get("description")
    default = project.get("default_branch")
    pushed = project.get("last_activity_at")
    return {
        "full_name": project["path_with_namespace"],
        "private": project.get("visibility") != "public",
        "description": description if isinstance(description, str) and description else None,
        "language": None,
        "default_branch": default if isinstance(default, str) else None,
        "pushed_at": pushed if isinstance(pushed, str) else None,
    }


def search_words(search: str) -> list[str]:
    """Split a picker search into lowercased words.

    Args:
        search: What the user typed.

    Returns:
        The words; a search of only punctuation stays one word, so it
        matches nothing instead of everything.
    """
    needle = search.strip().lower()
    words = [word for word in re.split(r"[\W_]+", needle) if word]
    return words or ([needle] if needle else [])


def matches_search(summary: dict[str, Any], words: list[str]) -> bool:
    """Say whether every search word is in the project's path or description.

    Args:
        summary: One summary from :func:`_repo_summary`.
        words: The lowercased search words; empty matches every project.

    Returns:
        True when each word is a substring of the path plus the description.
    """
    haystack = f"{summary['full_name']} {summary['description'] or ''}".lower()
    return all(word in haystack for word in words)


def list_repositories(secret: ConnectorSecret, search: str) -> list[dict[str, Any]]:
    """List the projects the account is a member of, most recently active first.

    A ``search`` shaped like a project path that the list lacks is looked up
    directly, so a public project outside the account can be picked.

    Args:
        secret: The stored connector.
        search: Case-insensitive words matched against the path and the description.

    Returns:
        Repository summaries, see :func:`_repo_summary`.
    """
    token = secret.access_token
    needle = repo_path(search, WEB_URL, PROVIDER)
    body = get_json(
        f"{API_URL}/projects",
        provider=PROVIDER,
        headers=_headers(token),
        params={"membership": "true", "order_by": "last_activity_at", "per_page": REPO_LIMIT},
    )
    words = search_words(needle)
    repos = [
        summary
        for summary in (
            _repo_summary(p)
            for p in body or []
            if isinstance(p, dict) and isinstance(p.get("path_with_namespace"), str)
        )
        if matches_search(summary, words)
    ]
    if (
        REPO_NAME.match(needle)
        and not has_dot_segment(needle)
        and not any(r["full_name"].lower() == needle.lower() for r in repos)
    ):
        try:
            found = get_json(_project_url(needle), provider=PROVIDER, headers=_headers(token))
        except DomainError as exc:
            if exc.code != "connectors.not_found":
                raise
            found = None
        if isinstance(found, dict) and isinstance(found.get("path_with_namespace"), str):
            repos.insert(0, _repo_summary(found))
    return repos


def _default_branch(token: str | None, full_name: str) -> str | None:
    """Look up a project's default branch.

    Args:
        token: Bearer token.
        full_name: The project path.

    Returns:
        The branch name, or ``None`` for an empty project.
    """
    meta = get_json(_project_url(full_name), provider=PROVIDER, headers=_headers(token))
    default = meta.get("default_branch") if isinstance(meta, dict) else None
    return default if isinstance(default, str) else None


def list_branches(secret: ConnectorSecret, full_name: str) -> dict[str, Any]:
    """List a project's branch names with its default branch.

    Args:
        secret: The stored connector.
        full_name: The project path.

    Returns:
        ``{"default_branch": str | None, "branches": [name, ...]}``: up to
        :data:`BRANCH_LIMIT` names, plus the default branch when the cap cut it.
    """
    token = secret.access_token
    default = _default_branch(token, full_name)
    branches: list[str] = []
    page = 1
    while len(branches) < BRANCH_LIMIT:
        body = get_json(
            f"{_project_url(full_name)}/repository/branches",
            provider=PROVIDER,
            headers=_headers(token),
            params={"per_page": PAGE_SIZE, "page": page},
        )
        batch = [b["name"] for b in body or [] if isinstance(b, dict) and isinstance(b.get("name"), str)]
        branches.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    branches = branches[:BRANCH_LIMIT]
    if default and default not in branches:
        branches.insert(0, default)
    return {"default_branch": default, "branches": branches}


def list_commits(secret: ConnectorSecret, full_name: str, branch: str) -> list[dict[str, Any]]:
    """List the newest commits of a project branch, newest first.

    Args:
        secret: The stored connector.
        full_name: The project path.
        branch: Branch name; empty for the default branch.

    Returns:
        Up to :data:`COMMIT_LIMIT` summaries, see :func:`commit_summary`.
    """
    params: dict[str, Any] = {"per_page": COMMIT_LIMIT}
    if branch.strip():
        params["ref_name"] = branch.strip()
    body = get_json(
        f"{_project_url(full_name)}/repository/commits",
        provider=PROVIDER,
        headers=_headers(secret.access_token),
        params=params,
    )
    items = body if isinstance(body, list) else []
    found = (
        commit_summary(c.get("id"), c.get("message") or c.get("title"), c.get("committed_date"))
        for c in items
        if isinstance(c, dict)
    )
    return [c for c in found if c][:COMMIT_LIMIT]


def repository_tree(secret: ConnectorSecret, full_name: str, branch: str) -> dict[str, Any]:
    """List every file and folder of a project at a branch.

    Args:
        secret: The stored connector.
        full_name: The project path.
        branch: Branch name; empty for the default branch.

    Returns:
        See :func:`tree_entries`.
    """
    return tree_entries(secret.access_token, full_name, branch)


def tree_entries(token: str | None, full_name: str, branch: str) -> dict[str, Any]:
    """List every file and folder of a project at a ref with a bare token.

    Args:
        token: Bearer token with read access; ``None`` for a public project.
        full_name: The project path.
        branch: Branch name or commit id; empty for the default branch.

    Returns:
        ``{"entries": [{"path", "type": "file" | "dir"}], "truncated": bool}``;
        GitLab's tree lists no file sizes, and ``truncated`` is set past
        :data:`TREE_PAGE_LIMIT` pages.
    """
    url = f"{_project_url(full_name)}/repository/tree"
    params: dict[str, Any] = {"recursive": "true", "per_page": PAGE_SIZE}
    if branch.strip():
        params["ref"] = branch.strip()
    kinds = {"blob": "file", "tree": "dir"}
    entries: list[dict[str, Any]] = []
    for page in range(1, TREE_PAGE_LIMIT + 1):
        body = get_json(url, provider=PROVIDER, headers=_headers(token), params={**params, "page": page})
        if not isinstance(body, list):
            raise DomainError("connectors.provider_error", status=502, provider=label(PROVIDER), status_code=200)
        entries.extend(
            {"path": item["path"], "type": kinds[item["type"]]}
            for item in body
            if isinstance(item, dict) and item.get("type") in kinds and isinstance(item.get("path"), str)
        )
        if len(body) < PAGE_SIZE:
            return {"entries": entries, "truncated": False}
    return {"entries": entries, "truncated": True}


def _file_url(full_name: str, path: str) -> str:
    """Build the repository-files API URL of one path.

    Args:
        full_name: The project path.
        path: File path inside the repository.

    Returns:
        The URL; the path is one encoded segment, as GitLab requires.
    """
    return f"{_project_url(full_name)}/repository/files/{quote(path.strip('/'), safe='')}"


def _ref(token: str | None, full_name: str, ref: str) -> str:
    """Return ``ref``, or the default branch when it is empty.

    Args:
        token: Bearer token.
        full_name: The project path.
        ref: Branch, tag or commit id; may be empty.

    Returns:
        A ref GitLab's file API accepts.
    """
    return ref.strip() or _default_branch(token, full_name) or "HEAD"


def read_text_file(token: str, full_name: str, branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
    """Read one file of a project at a branch as text, up to a byte cap.

    Args:
        token: Bearer token with read access to the project.
        full_name: The project path.
        branch: Branch name; empty for the default branch.
        path: File path inside the repository.
        max_bytes: Stop reading after this many bytes.

    Returns:
        ``(text, truncated)``; undecodable bytes are replaced.
    """
    content, truncated = download(
        f"{_file_url(full_name, path)}/raw",
        provider=PROVIDER,
        headers={**_headers(token), **range_header(max_bytes)},
        max_bytes=max_bytes,
        params={"ref": _ref(token, full_name, branch)},
    )
    return content.decode("utf-8", errors="replace"), truncated


def file_at(token: str | None, full_name: str, ref: str, path: str, max_bytes: int) -> dict[str, Any]:
    """Read one file of a project at a ref as raw bytes, unless it is too large.

    Args:
        token: Bearer token with read access; ``None`` for a public project.
        full_name: The project path.
        ref: Commit id, branch or tag; empty for the default branch.
        path: File path inside the repository.
        max_bytes: Files larger than this are reported but not read.

    Returns:
        ``{"content": bytes | None, "size": int | None, "missing": bool,
        "too_large": bool}``; ``missing`` covers an absent path or a folder.

    Raises:
        DomainError: When GitLab refuses the read for any reason other than
            the path being absent.
    """
    try:
        body = get_json(
            _file_url(full_name, path),
            provider=PROVIDER,
            headers=_headers(token),
            params={"ref": _ref(token, full_name, ref)},
        )
    except DomainError as exc:
        if exc.code != "connectors.not_found":
            raise
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("content"), str):
        return {"content": None, "size": None, "missing": True, "too_large": False}
    size = body.get("size") if isinstance(body.get("size"), int) else None
    if size is not None and size > max_bytes:
        return {"content": None, "size": size, "missing": False, "too_large": True}
    if body.get("encoding") != "base64":
        raise DomainError("connectors.provider_error", status=502, provider=label(PROVIDER), status_code=200)
    content = base64.b64decode(body["content"])
    if len(content) > max_bytes:
        return {"content": None, "size": len(content), "missing": False, "too_large": True}
    return {"content": content, "size": len(content), "missing": False, "too_large": False}


def read_repo_file(token: str | None, full_name: str, ref: str, path: str, max_bytes: int) -> dict[str, Any]:
    """Read one project file for display, decoding text and flagging everything else.

    Args:
        token: Bearer token with read access; ``None`` for a public project.
        full_name: The project path.
        ref: Commit id or branch; empty for the default branch.
        path: File path inside the repository.
        max_bytes: Files larger than this are reported but not read.

    Returns:
        ``{"path", "text": str | None, "binary", "too_large", "missing"}``,
        the same shape as :func:`core.connectors.github.read_repo_file`.
    """
    clean = clean_file_path(path)
    fetched = file_at(token, full_name, ref, clean, max_bytes)
    return display_file(clean, fetched)


def display_file(path: str, fetched: dict[str, Any]) -> dict[str, Any]:
    """Turn raw file bytes into what the file viewer shows.

    Args:
        path: The cleaned file path.
        fetched: What a host's ``file_at`` returned.

    Returns:
        ``{"path", "text", "binary", "too_large", "missing"}``; ``binary``
        marks content holding a NUL byte or invalid UTF-8.
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
    return {
        "path": path,
        "text": text,
        "binary": binary,
        "too_large": fetched["too_large"],
        "missing": fetched["missing"],
    }


def resolve_commit(token: str, repository: str, branch: str | None) -> str:
    """Pin a branch, or the default branch, to its current commit.

    Args:
        token: GitLab token with read access to the project.
        repository: The project path.
        branch: Branch to pin; ``None`` picks the default branch.

    Returns:
        The 40-character commit id.

    Raises:
        RepoFetchError: When GitLab refuses the lookup.
    """
    try:
        ref = branch or _default_branch(token, repository) or "HEAD"
        url = f"{_project_url(repository)}/repository/commits/{quote(ref, safe='')}"
        return str(get_json(url, provider=PROVIDER, headers=_headers(token))["id"])
    except DomainError as error:
        raise RepoFetchError(f"GitLab could not find {repository} at '{branch or 'its default branch'}'.") from error


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
    """Open a draft merge request from a pushed branch.

    Args:
        token: GitLab token with ``api`` scope.
        repository: The project path.
        head: The pushed branch.
        base_branch: Branch to merge into; ``None`` picks the default branch.
        title: Merge request title, without the draft prefix.
        body: Merge request description.
        hardening: The run's finished hardening job; ``None`` keeps the body as given.

    Returns:
        The opened merge request; ``number`` is its project-scoped ``iid``.

    Raises:
        RepoPublishError: When GitLab refuses the request.
    """
    if hardening is not None:
        body = f"{body}\n\n{hardening_line(hardening)}" if body else hardening_line(hardening)
    try:
        target = base_branch or _default_branch(token, repository)
        if not target:
            raise RepoPublishError("The GitLab project has no default branch to merge into.")
        # GitLab marks a merge request as a draft by the title prefix.
        fields = {
            "source_branch": head,
            "target_branch": target,
            "title": f"{DRAFT_PREFIX}{title}",
            "description": body,
        }
        response = request(
            "POST",
            f"{_project_url(repository)}/merge_requests",
            provider=PROVIDER,
            headers=_headers(token),
            json=fields,
        )
    except DomainError as error:
        raise RepoPublishError("GitLab refused to open the merge request.") from error
    opened = response.json()
    return PublishedChange(
        branch=head, url=str(opened["web_url"]), number=int(opened["iid"]), draft=bool(opened.get("draft", True))
    )

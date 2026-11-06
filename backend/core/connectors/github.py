"""GitHub connector: browse repositories and import a data file from one.

Linking works through a GitHub OAuth app (``repo`` scope so private
repositories resolve) or a pasted personal access token. Browsing lists the
account's repositories, then walks a repository's tree through the contents
API; CSV, TSV, JSON, JSONL and Parquet files are importable.
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from typing import Any
from urllib.parse import quote

from ..api.errors import DomainError
from ..config import settings
from .base import Credential, Entry, import_file, preview_file, range_header
from .oauth import OAuthApp
from .oauth import oauth_available as _oauth_available
from .tabular import check_size, is_supported
from .transport import download, get_json, label
from .vault import ConnectorSecret

PROVIDER = "github"
API_URL = "https://api.github.com"
SCOPES = "repo read:user"
REPO_LIMIT = 100
TREE_TTL_SECONDS = 300.0
BRANCH_PAGE_SIZE = 100
BRANCH_LIMIT = 300
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
FILE_PATH_MAX = 1024

_trees: dict[tuple[str, str, str], tuple[float, list[str] | None]] = {}
_tree_locks: dict[tuple[str, str, str], threading.Lock] = {}
_trees_lock = threading.Lock()


def oauth_app() -> OAuthApp:
    """Describe the GitHub OAuth app from settings.

    Returns:
        The app; ``client_id`` is ``None`` when unconfigured.
    """
    secret = settings.github_oauth_client_secret
    return OAuthApp(
        provider=PROVIDER,
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scopes=SCOPES,
        client_id=settings.github_oauth_client_id,
        client_secret=secret.get_secret_value() if secret is not None else None,
        extra_authorize_params={},
    )


def oauth_available() -> bool:
    """Report whether "Continue with GitHub" can be offered.

    Returns:
        ``True`` when the client id and the vault key are configured.
    """
    return _oauth_available(oauth_app())


def _headers(token: str | None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    """Bearer headers for the GitHub API.

    Args:
        token: The access token; empty or ``None`` calls anonymously, which
            reaches public repositories only.
        accept: Media type to request.

    Returns:
        The headers.
    """
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_account_label(token: str) -> str | None:
    """Look up the login behind a token.

    Args:
        token: A GitHub token.

    Returns:
        The login, or ``None`` when the lookup fails.
    """
    try:
        body = get_json(f"{API_URL}/user", provider=PROVIDER, headers=_headers(token))
    except DomainError:
        return None
    login = body.get("login") if isinstance(body, dict) else None
    return login if isinstance(login, str) else None


def verify_credentials(fields: dict[str, str]) -> Credential:
    """Validate a pasted personal access token against ``/user``.

    Args:
        fields: ``{"token": ...}``.

    Returns:
        The credential to store, labelled with the login.

    Raises:
        DomainError: 400 when GitHub rejects the token.
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
    login = body.get("login") if isinstance(body, dict) else None
    return Credential(secret=token, auth_method="token", account_label=login if isinstance(login, str) else None)


def _split_location(location: str) -> tuple[str, str, str]:
    """Split ``"owner/repo/dir/file"`` into ``(owner, repo, "dir/file")``.

    Args:
        location: A browse location or file ref.

    Returns:
        The parts; ``path`` is empty at the repository root.

    Raises:
        DomainError: 400 when the location has no ``owner/repo`` prefix.
    """
    parts = location.strip("/").split("/", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise DomainError("connectors.invalid_ref", status=400)
    return parts[0], parts[1], parts[2] if len(parts) == 3 else ""


def _list_repos(token: str, search: str) -> list[Entry]:
    """List repositories the account can see, most recently pushed first.

    A ``search`` shaped like ``owner/repo`` is also offered directly, so a
    public repository outside the account can be opened by name.

    Args:
        token: Bearer token.
        search: Substring filter on ``owner/repo``.

    Returns:
        Folder entries keyed ``owner/repo``.
    """
    body = get_json(
        f"{API_URL}/user/repos",
        provider=PROVIDER,
        headers=_headers(token),
        params={"per_page": REPO_LIMIT, "sort": "pushed", "affiliation": "owner,collaborator,organization_member"},
    )
    needle = search.strip().lower()
    entries = [
        Entry(ref=repo["full_name"], name=repo["full_name"], kind="folder", modified=repo.get("pushed_at"))
        for repo in body or []
        if isinstance(repo.get("full_name"), str) and needle in repo["full_name"].lower()
    ]
    if needle.count("/") == 1 and all(needle.split("/")) and not any(e.ref.lower() == needle for e in entries):
        entries.insert(0, Entry(ref=search.strip(), name=search.strip(), kind="folder"))
    return entries


def _repo_url(owner: str, repo: str) -> str:
    """Build the API URL of one repository.

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        The URL.
    """
    return f"{API_URL}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def _split_repo(full_name: str) -> tuple[str, str]:
    """Split ``"owner/name"`` into its parts.

    Args:
        full_name: The repository as ``owner/name``.

    Returns:
        ``(owner, name)``.

    Raises:
        DomainError: 400 when the value is not shaped like ``owner/name``.
    """
    value = full_name.strip()
    if not REPO_NAME.match(value):
        raise DomainError("connectors.invalid_ref", status=400)
    owner, name = value.split("/")
    return owner, name


def _repo_summary(repo: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields the repository picker shows.

    Args:
        repo: A repository object from the GitHub API.

    Returns:
        ``full_name``, ``private``, ``description``, ``language``,
        ``default_branch`` and ``pushed_at``.
    """
    return {
        "full_name": repo["full_name"],
        "private": bool(repo.get("private")),
        "description": repo.get("description") if isinstance(repo.get("description"), str) else None,
        "language": repo.get("language") if isinstance(repo.get("language"), str) else None,
        "default_branch": repo.get("default_branch") if isinstance(repo.get("default_branch"), str) else None,
        "pushed_at": repo.get("pushed_at") if isinstance(repo.get("pushed_at"), str) else None,
    }


def _matches(repo: dict[str, Any], words: list[str]) -> bool:
    """Say whether every search word is in the repository's name or description.

    Args:
        repo: One repository from GitHub's list.
        words: The lowercased search words; empty matches every repository.

    Returns:
        True when each word is a substring of ``owner/name`` plus the description.
    """
    description = repo.get("description") if isinstance(repo.get("description"), str) else ""
    haystack = f"{repo['full_name']} {description}".lower()
    return all(word in haystack for word in words)


def list_repositories(secret: ConnectorSecret, search: str) -> list[dict[str, Any]]:
    """List the account's repositories with the details the picker shows, most recently pushed first.

    A ``search`` shaped like ``owner/name`` that the account's list lacks is
    looked up directly, so a public repository outside the account can be
    picked; it is left out when GitHub does not know it.

    Args:
        secret: The stored connector.
        search: Case-insensitive words; a repository matches when each word
            is in its ``owner/name`` or its description. Spaces, hyphens
            and other separators only split words, so ``personal website``
            finds ``personal-website``.

    Returns:
        Repository summaries, see :func:`_repo_summary`.
    """
    token = secret.access_token
    body = get_json(
        f"{API_URL}/user/repos",
        provider=PROVIDER,
        headers=_headers(token),
        params={"per_page": REPO_LIMIT, "sort": "pushed", "affiliation": "owner,collaborator,organization_member"},
    )
    needle = search.strip().lower()
    words = [word for word in re.split(r"[^a-z0-9]+", needle) if word]
    repos = [
        _repo_summary(repo)
        for repo in body or []
        if isinstance(repo, dict) and isinstance(repo.get("full_name"), str) and _matches(repo, words)
    ]
    if REPO_NAME.match(needle) and not any(r["full_name"].lower() == needle for r in repos):
        owner, name = search.strip().split("/")
        try:
            found = get_json(_repo_url(owner, name), provider=PROVIDER, headers=_headers(token))
        except DomainError as exc:
            if exc.code != "connectors.not_found":
                raise
            found = None
        if isinstance(found, dict) and isinstance(found.get("full_name"), str):
            repos.insert(0, _repo_summary(found))
    return repos


def list_branches(secret: ConnectorSecret, full_name: str) -> dict[str, Any]:
    """List a repository's branch names with its default branch.

    Args:
        secret: The stored connector.
        full_name: The repository as ``owner/name``.

    Returns:
        ``{"default_branch": str | None, "branches": [name, ...]}``: up to
        :data:`BRANCH_LIMIT` names, plus the default branch when the cap cut it.
    """
    owner, name = _split_repo(full_name)
    headers = _headers(secret.access_token)
    meta = get_json(_repo_url(owner, name), provider=PROVIDER, headers=headers)
    default = meta.get("default_branch") if isinstance(meta, dict) else None
    default = default if isinstance(default, str) else None
    branches: list[str] = []
    page = 1
    while len(branches) < BRANCH_LIMIT:
        body = get_json(
            f"{_repo_url(owner, name)}/branches",
            provider=PROVIDER,
            headers=headers,
            params={"per_page": BRANCH_PAGE_SIZE, "page": page},
        )
        batch = [b["name"] for b in body or [] if isinstance(b, dict) and isinstance(b.get("name"), str)]
        branches.extend(batch)
        if len(batch) < BRANCH_PAGE_SIZE:
            break
        page += 1
    branches = branches[:BRANCH_LIMIT]
    if default and default not in branches:
        branches.insert(0, default)
    return {"default_branch": default, "branches": branches}


def repository_tree(secret: ConnectorSecret, full_name: str, branch: str) -> dict[str, Any]:
    """List every file and folder of a repository at a branch in one call.

    Args:
        secret: The stored connector.
        full_name: The repository as ``owner/name``.
        branch: Branch name; empty for the default branch.

    Returns:
        ``{"entries": [{"path", "type": "file" | "dir"}], "truncated": bool}``;
        ``truncated`` is GitHub's flag for a tree too large to list whole.
    """
    return tree_entries(secret.access_token, full_name, branch)


def tree_entries(token: str | None, full_name: str, branch: str) -> dict[str, Any]:
    """List every file and folder of a repository at a branch with a bare token.

    Args:
        token: Bearer token with read access to the repository; ``None`` for
            an anonymous read of a public repository.
        full_name: The repository as ``owner/name``.
        branch: Branch name or commit id; empty for the default branch.

    Returns:
        ``{"entries": [{"path", "type": "file" | "dir", "size"?}], "truncated": bool}``;
        ``size`` is a file's byte count, when GitHub reports it.
    """
    owner, name = _split_repo(full_name)
    # A branch name may hold slashes; encoded, it stays one path segment.
    ref = quote(branch.strip(), safe="") or "HEAD"
    body = get_json(
        f"{_repo_url(owner, name)}/git/trees/{ref}",
        provider=PROVIDER,
        headers=_headers(token),
        params={"recursive": "1"},
    )
    if not isinstance(body, dict):
        raise DomainError("connectors.provider_error", status=502, provider=label(PROVIDER), status_code=200)
    kinds = {"blob": "file", "tree": "dir"}
    entries = [
        {
            "path": item["path"],
            "type": kinds[item.get("type")],
            **({"size": item["size"]} if isinstance(item.get("size"), int) else {}),
        }
        for item in body.get("tree") or []
        if isinstance(item, dict) and item.get("type") in kinds and isinstance(item.get("path"), str)
    ]
    return {"entries": entries, "truncated": bool(body.get("truncated"))}


def read_text_file(token: str, full_name: str, branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
    """Read one file of a repository at a branch as text, up to a byte cap.

    Args:
        token: Bearer token with read access to the repository.
        full_name: The repository as ``owner/name``.
        branch: Branch name; empty for the default branch.
        path: File path inside the repository.
        max_bytes: Stop reading after this many bytes.

    Returns:
        ``(text, truncated)``; undecodable bytes are replaced.
    """
    owner, name = _split_repo(full_name)
    params = {"ref": branch.strip()} if branch.strip() else None
    headers = {**_headers(token, accept="application/vnd.github.raw+json"), **range_header(max_bytes)}
    content, truncated = download(
        _contents_url(owner, name, path.strip("/")),
        provider=PROVIDER,
        headers=headers,
        max_bytes=max_bytes,
        params=params,
    )
    return content.decode("utf-8", errors="replace"), truncated


def file_at(token: str | None, full_name: str, ref: str, path: str, max_bytes: int) -> dict[str, Any]:
    """Read one file of a repository at a ref as raw bytes, unless it is too large.

    Args:
        token: Bearer token with read access to the repository; ``None`` for
            an anonymous read of a public repository.
        full_name: The repository as ``owner/name``.
        ref: Commit id, branch or tag; empty for the default branch.
        path: File path inside the repository.
        max_bytes: Files larger than this are reported but not read.

    Returns:
        ``{"content": bytes | None, "size": int | None, "missing": bool,
        "too_large": bool}``; ``missing`` covers a path that is absent or is
        not a regular file (a folder, symlink or submodule) at ``ref``.

    Raises:
        DomainError: When GitHub refuses the read for any reason other than
            the path being absent.
    """
    owner, name = _split_repo(full_name)
    try:
        body = get_json(
            _contents_url(owner, name, path.strip("/")),
            provider=PROVIDER,
            headers=_headers(token),
            params={"ref": ref} if ref else None,
        )
    except DomainError as exc:
        if exc.code != "connectors.not_found":
            raise
        body = None
    if not isinstance(body, dict) or body.get("type") != "file":
        return {"content": None, "size": None, "missing": True, "too_large": False}
    size = body.get("size") if isinstance(body.get("size"), int) else None
    if size is not None and size > max_bytes:
        return {"content": None, "size": size, "missing": False, "too_large": True}
    encoded = body.get("content")
    if body.get("encoding") != "base64" or not isinstance(encoded, str):
        raise DomainError("connectors.provider_error", status=502, provider=label(PROVIDER), status_code=200)
    content = base64.b64decode(encoded)
    if len(content) > max_bytes:
        return {"content": None, "size": len(content), "missing": False, "too_large": True}
    return {"content": content, "size": len(content), "missing": False, "too_large": False}


def clean_file_path(raw: str) -> str:
    """Normalize a repository file path, refusing ones that leave the repository or enter ``.git``.

    Args:
        raw: The path the caller asked for.

    Returns:
        The ``/``-joined path without a trailing slash.

    Raises:
        DomainError: 400 ``connectors.invalid_ref`` when the path is empty,
            too long, absolute, walks out with ``..`` or points into ``.git``.
    """
    path = raw.strip().replace("\\", "/").rstrip("/")
    if not path or len(path) > FILE_PATH_MAX or path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise DomainError("connectors.invalid_ref", status=400)
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts) or parts[0].lower() == ".git":
        raise DomainError("connectors.invalid_ref", status=400)
    return path


def read_repo_file(token: str | None, full_name: str, ref: str, path: str, max_bytes: int) -> dict[str, Any]:
    """Read one repository file for display, decoding text and flagging everything else.

    Args:
        token: Bearer token with read access to the repository; ``None`` for
            an anonymous read of a public repository.
        full_name: The repository as ``owner/name``.
        ref: Commit id or branch; empty for the default branch.
        path: File path inside the repository.
        max_bytes: Files larger than this are reported but not read.

    Returns:
        ``{"path", "text": str | None, "binary", "too_large", "missing"}``;
        ``binary`` marks content holding a NUL byte or invalid UTF-8, and
        ``text`` is set only for a readable text file.

    Raises:
        DomainError: 400 for a bad path or repository name; GitHub's refusal
            when the file cannot be read.
    """
    clean = clean_file_path(path)
    fetched = file_at(token, full_name, ref.strip(), clean, max_bytes)
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
        "path": clean,
        "text": text,
        "binary": binary,
        "too_large": fetched["too_large"],
        "missing": fetched["missing"],
    }


def _contents_url(owner: str, repo: str, path: str) -> str:
    """Build the contents-API URL for a path.

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: Path inside the repository.

    Returns:
        The URL.
    """
    return f"{API_URL}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/{quote(path)}"


def _list_tree(token: str, owner: str, repo: str, path: str) -> list[Entry]:
    """List one directory of a repository, folders first.

    Args:
        token: Bearer token.
        owner: Repository owner.
        repo: Repository name.
        path: Directory path, empty for the root.

    Returns:
        Folder entries for sub-directories and file entries for importable files.
    """
    body = get_json(_contents_url(owner, repo, path), provider=PROVIDER, headers=_headers(token))
    if not isinstance(body, list):
        raise DomainError("connectors.invalid_ref", status=400)
    folders = [
        Entry(ref=f"{owner}/{repo}/{item['path']}", name=item["name"], kind="folder")
        for item in body
        if item.get("type") == "dir" and isinstance(item.get("path"), str)
    ]
    files = [
        Entry(ref=f"{owner}/{repo}/{item['path']}", name=item["name"], kind="file", size=item.get("size"))
        for item in body
        if item.get("type") == "file" and isinstance(item.get("path"), str) and is_supported(item["name"])
    ]
    return folders + files


def web_url(secret: ConnectorSecret, location: str) -> str | None:
    """Link to a browse location on GitHub.

    Args:
        secret: The stored connector.
        location: Empty for the repositories, else ``owner/repo[/path]``.

    Returns:
        The URL.
    """
    if not location:
        return "https://github.com"
    owner, repo, path = _split_location(location)
    base = f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}"
    return f"{base}/tree/HEAD/{quote(path)}" if path else base


def _repo_files(token: str, owner: str, repo: str) -> list[str] | None:
    """List every file path in a repository in one call, cached briefly.

    Probes of sibling folders run in parallel against the same repository, so
    each repository's tree is fetched once under its own lock.

    Args:
        token: Bearer token.
        owner: Repository owner.
        repo: Repository name.

    Returns:
        The file paths, or ``None`` when GitHub truncated the tree.
    """
    key = (hashlib.sha256(token.encode()).hexdigest(), owner, repo)
    with _trees_lock:
        lock = _tree_locks.setdefault(key, threading.Lock())
    with lock:
        cached = _trees.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        body = get_json(
            f"{API_URL}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/trees/HEAD",
            provider=PROVIDER,
            headers=_headers(token),
            params={"recursive": "1"},
        )
        files = (
            None
            if not isinstance(body, dict) or body.get("truncated")
            else [
                item["path"]
                for item in body.get("tree") or []
                if item.get("type") == "blob" and isinstance(item.get("path"), str)
            ]
        )
        _trees[key] = (time.monotonic() + TREE_TTL_SECONDS, files)
        return files


def has_importable(secret: ConnectorSecret, ref: str) -> bool | None:
    """Settle whether a repository or directory holds an importable file, from the repository tree.

    Args:
        secret: The stored connector.
        ref: ``owner/repo[/dir]``.

    Returns:
        ``True``, ``False``, or ``None`` when the tree is too big to tell.
    """
    owner, repo, path = _split_location(ref)
    files = _repo_files(secret.access_token, owner, repo)
    if files is None:
        return None
    prefix = f"{path}/" if path else ""
    return any(f.startswith(prefix) and is_supported(f) for f in files)


def browse(secret: ConnectorSecret, location: str, search: str) -> list[Entry]:
    """List repositories at the root, or one directory of a repository.

    Args:
        secret: The stored connector.
        location: Empty for the root, else ``owner/repo[/dir]``.
        search: Repository filter, honoured at the root.

    Returns:
        The entries.
    """
    if not location:
        return _list_repos(secret.access_token, search)
    owner, repo, path = _split_location(location)
    return _list_tree(secret.access_token, owner, repo, path)


def _fetcher(token: str, ref: str):
    """Build the download closure for one file ref.

    Args:
        token: Bearer token.
        ref: ``owner/repo/path/to/file``.

    Returns:
        A callable taking an optional byte cap and returning ``(bytes, truncated)``.
    """
    owner, repo, path = _split_location(ref)
    if not path:
        raise DomainError("connectors.invalid_ref", status=400)
    url = _contents_url(owner, repo, path)

    def fetch(max_bytes: int | None) -> tuple[bytes, bool]:
        """Download the raw file, honouring the cap.

        Args:
            max_bytes: Byte cap for previews, ``None`` for the whole file.

        Returns:
            ``(content, truncated)``.
        """
        headers = {**_headers(token, accept="application/vnd.github.raw+json"), **range_header(max_bytes)}
        return download(url, provider=PROVIDER, headers=headers, max_bytes=max_bytes)

    return fetch


def preview(secret: ConnectorSecret, ref: str) -> dict[str, Any]:
    """Decode the first rows of a repository file.

    Args:
        secret: The stored connector.
        ref: ``owner/repo/path/to/file``.

    Returns:
        ``{"columns": [...], "rows": [...], "num_rows_total": None}``.
    """
    return preview_file(_fetcher(secret.access_token, ref), ref)


def import_ref(secret: ConnectorSecret, ref: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Download a repository file and decode every row.

    Args:
        secret: The stored connector.
        ref: ``owner/repo/path/to/file``.

    Returns:
        ``(rows, column_schema, default_name)``.
    """
    owner, repo, path = _split_location(ref)
    meta = get_json(_contents_url(owner, repo, path), provider=PROVIDER, headers=_headers(secret.access_token))
    check_size(meta.get("size") if isinstance(meta, dict) else None)
    rows, schema = import_file(_fetcher(secret.access_token, ref), ref)
    return rows, schema, f"{repo}/{path.rsplit('/', 1)[-1]}"

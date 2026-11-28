"""Tests for the repository browser's opening look (size gate, README + manifest preload) and local folders."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ...api.errors import DomainError
from ...config import settings
from ...local.folder_tree import folder_entries, folder_source_id
from ...local.tests.test_folder_tree import SHARED_VECTOR_ID
from ..agents import repo_browser
from ..agents.repo_browser import (
    KEY_FILE_BYTES,
    LOCAL_PROVIDER,
    LocalFolderBrowser,
    LocalFolderError,
    RepoBrowser,
    read_folder_text,
)


def _browser(paths: list[str]) -> RepoBrowser:
    """Build a browser over a flat list of file paths."""
    return RepoBrowser(
        token="t",
        repository="acme/shop",
        branch="main",
        editable_paths=["."],
        entries=[{"path": p, "type": "file"} for p in paths],
        truncated=False,
    )


def test_preload_reads_only_root_readme_and_manifest(monkeypatch) -> None:
    """One README and one manifest at the root are read, capped, and land in the summary."""
    reads: list[tuple[str, int]] = []

    def fake_read(_token: str, _repo: str, _branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
        """Record the read and return short text; the manifest comes back cut."""
        reads.append((path, max_bytes))
        return f"contents of {path}", path == "package.json"

    monkeypatch.setattr(repo_browser.github, "read_text_file", fake_read)
    browser = _browser(["README.md", "package.json", "pyproject.toml", "docs/README.md", "src/app.ts"])
    browser.preload_key_files()

    assert reads == [("README.md", KEY_FILE_BYTES), ("package.json", KEY_FILE_BYTES)]
    summary = browser.summary()
    lines = summary.splitlines()
    assert "--- README.md (already read for you) ---" in lines
    assert lines[lines.index("--- README.md (already read for you) ---") + 1] == "contents of README.md"
    assert "contents of package.json\n... (file cut here)" in summary


def test_preload_skips_unreadable_and_missing_files(monkeypatch) -> None:
    """A failing read is skipped, and a repository without key files reads nothing."""

    def failing_read(*_args: Any) -> tuple[str, bool]:
        """Fail like GitHub would on a lost connection."""
        raise RuntimeError("boom")

    monkeypatch.setattr(repo_browser.github, "read_text_file", failing_read)
    browser = _browser(["readme.rst"])
    browser.preload_key_files()
    assert "already read for you" not in browser.summary()

    _browser(["src/main.go"]).preload_key_files()


def test_reads_flag_failures_and_the_empty_folder_by_key(monkeypatch) -> None:
    """A failed read returns ``{"error": ...}`` and an empty folder ``{"note": ...}``; the chat card reads the key."""

    def broken(*_args: Any) -> tuple[str, bool]:
        """Fail like a host read error."""
        raise RuntimeError("502 Bad Gateway")

    monkeypatch.setattr(repo_browser.github, "read_text_file", broken)
    browser = RepoBrowser(
        token="t",
        repository="acme/shop",
        branch="main",
        editable_paths=["."],
        entries=[{"path": "empty", "type": "dir"}, {"path": "src/app.ts", "type": "file"}],
        truncated=False,
    )

    assert browser.list_repo_folder("empty") == {"note": "The folder is empty."}
    assert browser.list_repo_folder("missing") == {"error": "No folder named 'missing' in the repository."}
    assert browser.read_repo_file("missing.py") == {
        "error": "No file named 'missing.py' in the repository. Check the tree for the exact path."
    }
    assert browser.read_repo_file("src/app.ts") == {"error": "Could not read it: 502 Bad Gateway"}
    assert isinstance(browser.list_repo_folder("src"), str)
    browser._reads = repo_browser.READS_PER_TURN
    assert browser.read_repo_file("src/app.ts") == {
        "error": "Read limit for this turn reached. Work with what you have read, or ask the user."
    }


def test_summary_tree_lines_are_pinned() -> None:
    """The chat card's ``repoTree`` finds the tree by these exact lines, so a change here must update it too."""
    browser = RepoBrowser(
        token="t",
        repository="acme/shop",
        branch="main",
        editable_paths=["."],
        entries=[{"path": "a.py", "type": "file"}],
        truncated=True,
    )
    lines = browser.summary().splitlines()

    assert "Tree ([editable] marks what the optimizer may change):" in lines
    assert "GitHub cut the tree short, so some files are missing above." in lines


def test_summary_without_editable_paths_points_to_the_repository_screen() -> None:
    """The setup agent gets no editable paths, so the summary must not claim the whole repository may change."""
    summary = _with_editable([]).summary()

    assert "The Repository screen sets the paths the optimizer may change." in summary.splitlines()
    assert "whole repository" not in summary
    assert "The optimizer may change: src." in _with_editable(["src"]).summary().splitlines()


def _with_editable(paths: list[str]) -> RepoBrowser:
    """Build a one-file browser with the given editable paths.

    Args:
        paths: The editable paths.

    Returns:
        The browser.
    """
    return RepoBrowser(
        token="t",
        repository="acme/shop",
        branch="main",
        editable_paths=paths,
        entries=[{"path": "a.py", "type": "file"}],
        truncated=False,
    )


def test_read_unmetered_skips_the_read_limit_and_the_read_list(monkeypatch) -> None:
    """An unmetered read works past the read limit, does not use a read, and returns ``None`` for a missing file."""
    reads: list[tuple[str, int]] = []

    def fake_read(_token: str, _repo: str, _branch: str, path: str, max_bytes: int) -> tuple[str, bool]:
        """Record the read and return short text."""
        reads.append((path, max_bytes))
        return f"contents of {path}", False

    monkeypatch.setattr(repo_browser.github, "read_text_file", fake_read)
    browser = _browser(["ascent.toml"])
    browser._reads = repo_browser.READS_PER_TURN

    assert browser.read_unmetered("ascent.toml", 100) == ("contents of ascent.toml", False)
    assert browser.read_unmetered("missing.toml", 100) is None
    assert reads == [("ascent.toml", 100)]
    assert browser._reads == repo_browser.READS_PER_TURN
    assert browser.files_read == []


def test_read_unmetered_asks_the_host_when_the_tree_is_cut(monkeypatch) -> None:
    """On a cut tree an unlisted file is read from the host: a 404 means no file, another failure raises."""
    answers: dict[str, Exception | tuple[str, bool]] = {
        "ascent.toml": ("contents", False),
        "missing.toml": DomainError("connectors.not_found", status=404, provider="GitHub"),
        "broken.toml": DomainError("connectors.unreachable", status=502, provider="GitHub"),
    }

    def fake_read(_token: str, _repo: str, _branch: str, path: str, _max_bytes: int) -> tuple[str, bool]:
        """Answer like the host would for each path."""
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(repo_browser.github, "read_text_file", fake_read)
    browser = RepoBrowser(
        token="t", repository="acme/shop", branch="main", editable_paths=["."], entries=[], truncated=True
    )

    assert browser.read_unmetered("ascent.toml", 100) == ("contents", False)
    assert browser.read_unmetered("missing.toml", 100) is None
    with pytest.raises(DomainError):
        browser.read_unmetered("broken.toml", 100)


@pytest.fixture
def local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the test as the desktop app does.

    Args:
        monkeypatch: The test's monkeypatch.
    """
    monkeypatch.setattr(settings, "local_mode", True)


def _folder(root: Path, files: dict[str, str]) -> Path:
    """Write files under a folder.

    Args:
        root: The folder.
        files: Path from the folder root to text.

    Returns:
        The folder.
    """
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _paths(entries: list[dict[str, Any]]) -> list[str]:
    """Return the entry paths, with a slash after a folder.

    Args:
        entries: Tree entries.

    Returns:
        The paths.
    """
    return [e["path"] + ("/" if e["type"] == "dir" else "") for e in entries]


@pytest.mark.usefixtures("local_mode")
def test_a_local_folder_gives_the_same_tree_and_file_output_as_a_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The local browser lists, reads and summarizes like the GitHub browser over the same files."""
    files = {"README.md": "# Shop\n", "pyproject.toml": "[project]\n", "src/app.py": "x = 1\ny = 2\n"}
    root = _folder(tmp_path, files)

    def fake_read(_token: str, _repo: str, _branch: str, path: str, _max_bytes: int) -> tuple[str, bool]:
        """Return the same text the folder holds."""
        return files[path], False

    monkeypatch.setattr(repo_browser.github, "read_text_file", fake_read)
    local = LocalFolderBrowser(root)
    hosted = RepoBrowser(
        token="t",
        repository="acme/shop",
        branch="main",
        editable_paths=[],
        entries=local._entries,
        truncated=False,
    )

    assert _paths(local._entries) == ["README.md", "pyproject.toml", "src/", "src/app.py"]
    assert local.provider == LOCAL_PROVIDER
    assert local._token is None
    assert local.read_repo_file("src/app.py") == hosted.read_repo_file("src/app.py") == "   1 x = 1\n   2 y = 2"
    assert local.list_repo_folder("src") == hosted.list_repo_folder("src")
    local.preload_key_files()
    hosted.preload_key_files()
    assert local.summary().splitlines()[1:] == hosted.summary().splitlines()[1:]
    assert local.summary().splitlines()[0] == (
        f"Local folder {root.resolve()}. It has no branch, no pull request and no host link."
    )


@pytest.mark.usefixtures("local_mode")
def test_the_local_tree_skips_git_env_files_and_what_gitignore_ignores(tmp_path: Path) -> None:
    """The tree follows .gitignore (nested files, negation, folder-only rules) and never shows .git or .env*."""
    root = _folder(
        tmp_path,
        {
            ".git/config": "[core]\n",
            ".env": "SECRET=1\n",
            ".env.local": "SECRET=2\n",
            "config/.envrc": "export SECRET=3\n",
            ".gitignore": "*.log\n!keep.log\nbuild/\n/top.txt\n",
            "app.log": "",
            "keep.log": "",
            "top.txt": "",
            "build/out.js": "",
            "src/build": "a file named build stays",
            "src/top.txt": "",
            "src/.gitignore": "secret_*.py\n",
            "src/secret_key.py": "",
            "src/main.py": "",
            "other/secret_key.py": "",
        },
    )

    entries, truncated = folder_entries(root)

    assert not truncated
    assert _paths(entries) == [
        ".gitignore",
        "config/",
        "keep.log",
        "other/",
        "other/secret_key.py",
        "src/",
        "src/.gitignore",
        "src/build",
        "src/main.py",
        "src/top.txt",
    ]


@pytest.mark.usefixtures("local_mode")
def test_the_local_tree_stops_at_the_entry_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A folder past the cap shows a cut tree and says so in the summary."""
    monkeypatch.setattr(repo_browser, "FOLDER_ENTRIES_MAX", 2)
    root = _folder(tmp_path, {"a.py": "", "b.py": "", "c.py": ""})

    entries, truncated = folder_entries(root, 2)

    assert (_paths(entries), truncated) == (["a.py", "b.py"], True)
    assert "The folder has more than 2 entries, so some files are missing above." in (
        LocalFolderBrowser(root).summary().splitlines()
    )


@pytest.mark.usefixtures("local_mode")
@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt", "/etc/passwd", "C:/x.txt", ""])
def test_a_local_read_refuses_a_path_that_leaves_the_folder(tmp_path: Path, path: str) -> None:
    """A ``..`` part, an absolute path or an empty path is refused before any disk read."""
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    root = _folder(tmp_path / "repo", {"src/app.py": "x = 1\n"})

    with pytest.raises(LocalFolderError):
        read_folder_text(root, path, 100)


@pytest.mark.usefixtures("local_mode")
def test_a_local_folder_never_follows_a_link(tmp_path: Path) -> None:
    """No link is in the tree, and a read through any link is refused, as the run never copies one."""
    outside = _folder(tmp_path / "outside", {"secret.txt": "outside", "inner/x.txt": "outside"})
    root = _folder(tmp_path / "repo", {"src/app.py": "x = 1\n", "src/inner.py": "y = 1\n"})
    (root / "leak.txt").symlink_to(outside / "secret.txt")
    (root / "leakdir").symlink_to(outside / "inner")
    (root / "srclink").symlink_to(root / "src")
    (root / "alias.py").symlink_to(root / "src" / "inner.py")

    assert _paths(folder_entries(root)[0]) == ["src/", "src/app.py", "src/inner.py"]
    for path in ("leak.txt", "leakdir/x.txt", "srclink/app.py", "alias.py"):
        with pytest.raises(LocalFolderError, match="goes through a link"):
            read_folder_text(root, path, 100)
    browser = LocalFolderBrowser(root)
    assert browser.read_repo_file("leak.txt") == {
        "error": "No file named 'leak.txt' in the folder. Check the tree for the exact path."
    }


@pytest.mark.usefixtures("local_mode")
@pytest.mark.parametrize("swap", ["link", "fifo"])
def test_a_file_swapped_after_the_check_is_refused_without_a_hang(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, swap: str
) -> None:
    """A link or pipe put in place of a checked file fails the open, and never leaks or blocks the read."""
    outside = _folder(tmp_path / "outside", {"secret.txt": "outside"})
    root = _folder(tmp_path / "repo", {"app.py": "x = 1\n"})
    checked = repo_browser._folder_file

    def check_then_swap(base: Path, path: str) -> str:
        """Pass the checks, then swap the file on disk before the open."""
        rel = checked(base, path)
        (base / rel).unlink()
        if swap == "link":
            (base / rel).symlink_to(outside / "secret.txt")
        else:
            os.mkfifo(base / rel)
        return rel

    monkeypatch.setattr(repo_browser, "_folder_file", check_then_swap)

    with pytest.raises(OSError):
        read_folder_text(root, "app.py", 100, {"app.py"})


@pytest.mark.usefixtures("local_mode")
def test_a_cut_listing_never_reads_a_file_the_filter_drops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Past the entry cap, a gitignored file stays unreadable, though the shown tree is cut."""
    monkeypatch.setattr(repo_browser, "FOLDER_ENTRIES_MAX", 2)
    root = _folder(
        tmp_path, {".gitignore": "ascent.toml\n", "ascent.toml": "[run]\n", "a.py": "", "b.py": "", "c.py": ""}
    )

    browser = LocalFolderBrowser(root)

    assert browser._truncated
    assert len(browser._entries) == 2
    assert browser.read_unmetered("ascent.toml", 100) is None
    assert browser.read_unmetered("c.py", 100) == ("", False)
    assert "error" in browser.read_repo_file("ascent.toml")
    with pytest.raises(DomainError):
        read_folder_text(root, "ascent.toml", 100)


@pytest.mark.usefixtures("local_mode")
def test_a_folder_over_the_cap_is_walked_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The cut tree is the full listing's prefix, so one walk gives the tree, the cut flag and the id."""
    root = _folder(tmp_path, {"a.py": "", "b.py": "", "c/d.py": "1\n"})
    monkeypatch.setattr(repo_browser, "FOLDER_ENTRIES_MAX", 2)
    cut, _ = folder_entries(root, 2)
    calls: list[int | None] = []

    def counted(base: Path, limit: int | None) -> tuple[list[dict[str, Any]], bool]:
        """Count each walk of the folder."""
        calls.append(limit)
        return folder_entries(base, limit)

    monkeypatch.setattr(repo_browser, "folder_entries", counted)

    browser = LocalFolderBrowser(root)

    assert len(calls) == 1
    assert (browser._entries, browser._truncated) == (cut, True)
    assert browser.branch == folder_source_id(root)
    assert browser._files == {"a.py", "b.py", "c/d.py"}


@pytest.mark.usefixtures("local_mode")
def test_a_local_folder_never_returns_env_file_content(tmp_path: Path) -> None:
    """A ``.env*`` file, a file in a ``.env*`` folder, and a link to one are refused."""
    root = _folder(
        tmp_path, {".env": "SECRET=1\n", ".Env.local": "SECRET=3\n", ".envs/prod.txt": "SECRET=2\n", "app.py": ""}
    )
    (root / "settings.txt").symlink_to(root / ".env")

    assert _paths(folder_entries(root)[0]) == ["app.py"]
    for path in (".env", "./.env", ".Env.local", ".envs/prod.txt"):
        with pytest.raises(LocalFolderError, match=r"\.env file"):
            read_folder_text(root, path, 100)
    with pytest.raises(LocalFolderError, match="goes through a link"):
        read_folder_text(root, "settings.txt", 100)
    browser = LocalFolderBrowser(root)
    browser.preload_key_files()
    assert "SECRET" not in browser.summary()
    assert "SECRET" not in str(browser.read_repo_file(".env"))


@pytest.mark.usefixtures("local_mode")
def test_a_local_read_of_a_missing_file_is_not_found_and_a_long_file_is_cut(tmp_path: Path) -> None:
    """A missing file raises the hosts' ``connectors.not_found``; a read stops at the limit and says so."""
    root = _folder(tmp_path, {"big.txt": "abcdef"})

    with pytest.raises(DomainError) as missing:
        read_folder_text(root, "none.txt", 100)
    assert (missing.value.code, missing.value.status_code) == ("connectors.not_found", 404)
    assert read_folder_text(root, "big.txt", 4) == ("abcd", True)
    assert read_folder_text(root, "big.txt", 6) == ("abcdef", False)


def test_a_local_folder_is_refused_outside_local_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The server never reads a disk folder; only the desktop app does, and only by an absolute path."""
    root = _folder(tmp_path, {"app.py": ""})
    monkeypatch.setattr(settings, "local_mode", False)

    for read in (
        lambda: LocalFolderBrowser(root),
        lambda: read_folder_text(root, "app.py", 9),
    ):
        with pytest.raises(LocalFolderError, match="only in the desktop app"):
            read()
    monkeypatch.setattr(settings, "local_mode", True)
    with pytest.raises(LocalFolderError, match="absolute"):
        LocalFolderBrowser("relative/folder")
    with pytest.raises(LocalFolderError, match="No folder"):
        LocalFolderBrowser(root / "app.py")


@pytest.mark.usefixtures("local_mode")
def test_the_browser_branch_is_the_run_source_id(tmp_path: Path) -> None:
    """The draft base equals the id the run pins as its commit, for the shared SKY-990 folder too."""
    root = _folder(
        tmp_path,
        {
            "src/main.py": "x = 1\n",
            "README.md": "hi\n",
            ".env": "KEY=1\n",
            ".git/HEAD": "ref\n",
            ".gitignore": "*.log\n",
            "a.log": "1\n",
            "a-b.txt": "1\n",
        },
    )

    assert LocalFolderBrowser(root).branch == folder_source_id(root) == SHARED_VECTOR_ID


@pytest.mark.usefixtures("local_mode")
def test_a_cut_listing_still_pins_the_full_source_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A folder past the entry cap hashes all of its files, so the base still equals the run's id."""
    root = _folder(tmp_path, {"a.py": "", "b.py": "", "c.py": ""})
    full = folder_source_id(root)
    monkeypatch.setattr(repo_browser, "FOLDER_ENTRIES_MAX", 2)

    browser = LocalFolderBrowser(root)

    assert browser._truncated
    assert browser.branch == full


@pytest.mark.usefixtures("local_mode")
def test_reading_a_local_folder_leaves_it_unchanged(tmp_path: Path) -> None:
    """Listing, reading and hashing the folder write nothing to it."""
    root = _folder(tmp_path, {"README.md": "# Shop\n", "src/app.py": "x = 1\n"})
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*") if p.is_file()}

    browser = LocalFolderBrowser(root)
    browser.preload_key_files()
    browser.read_repo_file("src/app.py")
    browser.list_repo_folder("src")
    browser.summary()

    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.usefixtures("local_mode")
def test_a_padded_path_reads_the_stripped_name(tmp_path: Path) -> None:
    """The read tools strip spaces and slashes, as the draft tools do, so a padded path reads the plain file."""
    root = _folder(tmp_path, {"notes": "plain\n", "dir/a.py": "x = 1\n"})
    browser = LocalFolderBrowser(root)

    assert browser.read_repo_file(" notes ") == "   1 plain"
    assert browser.read_repo_file(" /notes/ ") == "   1 plain"
    assert isinstance(browser.list_repo_folder(" dir "), str)
    assert isinstance(_browser(["src/app.py"]).list_repo_folder(" src "), str)


@pytest.mark.usefixtures("local_mode")
def test_a_linked_folder_root_is_refused_and_a_linked_parent_is_not(tmp_path: Path) -> None:
    """The run refuses a linked root, so the agent does too; a link above the folder still works."""
    root = _folder(tmp_path / "real" / "shop", {"app.py": "x = 1\n"})
    (tmp_path / "shop-link").symlink_to(root)
    (tmp_path / "parent-link").symlink_to(tmp_path / "real")

    with pytest.raises(LocalFolderError, match="The folder is a link"):
        LocalFolderBrowser(tmp_path / "shop-link")
    with pytest.raises(LocalFolderError, match="The folder is a link"):
        read_folder_text(tmp_path / "shop-link", "app.py", 100)
    assert LocalFolderBrowser(tmp_path / "parent-link" / "shop").branch == folder_source_id(root)


def test_a_git_source_names_itself_a_repository_and_a_local_one_a_folder() -> None:
    """The agent's error texts say "repository" for a host and "folder" for a local folder."""
    assert repo_browser._source_noun("github") == "repository"
    assert repo_browser._source_noun(None) == "repository"
    assert repo_browser._source_noun(LOCAL_PROVIDER) == "folder"
    assert _browser([]).read_repo_file("a.py") == {
        "error": "No file named 'a.py' in the repository. Check the tree for the exact path."
    }

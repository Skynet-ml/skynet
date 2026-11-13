"""Tests for the repository browser's opening look (size gate, README + manifest preload)."""

from __future__ import annotations

from typing import Any

from ..agents import repo_browser
from ..agents.repo_browser import KEY_FILE_BYTES, RepoBrowser


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

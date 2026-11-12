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
    assert "--- README.md (already read for you) ---\ncontents of README.md" in summary
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

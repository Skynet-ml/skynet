"""Tests for listing a local folder and hashing it into a source id (SKY-990)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from core.local import folder_tree
from core.local.folder_tree import FolderTreeError

# SKY-995 imports this id: its setup agent's draft base for this folder must be the same id.
SHARED_VECTOR_ID = "3f69ae325c7b8edd80580f2c9b10e6c286fff1fc"


def _write(root: Path, files: dict[str, str]) -> Path:
    """Write text files below a folder.

    Args:
        root: The folder.
        files: Root-relative path to text.

    Returns:
        The folder.
    """
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _names(root: Path) -> list[str]:
    """List the paths the walk keeps.

    Args:
        root: The folder.

    Returns:
        The kept paths, in walk order.
    """
    return [entry["path"] for entry in folder_tree.folder_entries(root)[0]]


def test_the_walk_skips_tool_state_installed_packages_and_secrets(tmp_path: Path) -> None:
    """``.git``, package folders, any Python venv and ``.env*`` names stay out, in any case."""
    root = _write(
        tmp_path / "app",
        {
            "main.py": "x = 1\n",
            ".git/HEAD": "ref\n",
            "node_modules/pkg/index.js": "1\n",
            ".venv/bin/python": "1\n",
            "venv/lib/a.py": "1\n",
            "env311/pyvenv.cfg": "home = /usr\n",
            "env311/lib/b.py": "1\n",
            "src/__pycache__/m.pyc": "1\n",
            "src/m.py": "1\n",
            ".env": "KEY=1\n",
            ".Env.local": "KEY=2\n",
            "sub/.ENV.production": "KEY=3\n",
            ".envs/prod/key.txt": "KEY=4\n",
        },
    )
    (root / "sub" / ".git").write_text("gitdir: elsewhere\n")

    assert _names(root) == ["main.py", "src", "src/m.py", "sub"]


def test_the_walk_skips_every_link(tmp_path: Path) -> None:
    """A link to a file, to a folder, or out of the folder is never listed or followed."""
    root = _write(tmp_path / "app", {"src/main.py": "x = 1\n"})
    (root / "inside.py").symlink_to("src/main.py")
    (root / "linked").symlink_to(root / "src")
    (root / "outside").symlink_to("/etc/hosts")
    (root / "broken").symlink_to(root / "gone")

    assert _names(root) == ["src", "src/main.py"]
    with pytest.raises(OSError):
        folder_tree.file_digest(root, "inside.py")


def test_a_linked_root_is_refused(tmp_path: Path) -> None:
    """The root itself is never followed through a link."""
    real = _write(tmp_path / "real", {"a.txt": "1\n"})
    (tmp_path / "alias").symlink_to(real)

    with pytest.raises(FolderTreeError, match="does not exist"):
        folder_tree.folder_entries(tmp_path / "alias")
    with pytest.raises(FolderTreeError, match="does not exist"):
        folder_tree.folder_entries(tmp_path / "gone")


def test_the_walk_follows_root_and_nested_gitignore_files(tmp_path: Path) -> None:
    """Git's rules apply: negation, dir-only lines, anchored lines, ``**`` and a deeper file that wins."""
    root = _write(
        tmp_path / "app",
        {
            ".gitignore": "*.log\n!keep.log\nbuild/\n/top.txt\ndocs/**/draft.md\ncache\n",
            "a.log": "1\n",
            "keep.log": "1\n",
            "build/out.js": "1\n",
            "top.txt": "1\n",
            "nested/top.txt": "1\n",
            "docs/a/b/draft.md": "1\n",
            "docs/a/final.md": "1\n",
            "cache/x": "1\n",
            "pkg/.gitignore": "!debug.log\n",
            "pkg/debug.log": "1\n",
            "pkg/other.log": "1\n",
        },
    )
    # A dir-only line leaves a file with the same name in.
    (root / "nested" / "build").write_text("1\n")

    assert _names(root) == [
        ".gitignore",
        "docs",
        "docs/a",
        "docs/a/b",
        "docs/a/final.md",
        "keep.log",
        "nested",
        "nested/build",
        "nested/top.txt",
        "pkg",
        "pkg/.gitignore",
        "pkg/debug.log",
    ]


def test_a_bad_gitignore_line_is_skipped_as_git_does(tmp_path: Path) -> None:
    """A line no regex can hold, such as ``[z-a]``, gives no rule and the other lines still apply."""
    root = _write(tmp_path / "app", {".gitignore": "[z-a]\n*.log\n", "a.log": "1\n", "z": "1\n"})

    assert _names(root) == [".gitignore", "z"]


def test_an_escaped_trailing_space_stays_in_the_pattern(tmp_path: Path) -> None:
    """``foo\\ `` matches ``foo `` only; a plain trailing space is dropped."""
    root = _write(tmp_path / "app", {".gitignore": "foo\\ \nbar   \n", "foo ": "1\n", "foo": "1\n", "bar": "1\n"})

    assert _names(root) == [".gitignore", "foo"]


def test_the_listing_stops_at_its_limit(tmp_path: Path) -> None:
    """A cut listing says so; ``None`` lists every entry."""
    root = _write(tmp_path / "app", {f"f{index}.txt": "1\n" for index in range(5)})

    entries, truncated = folder_tree.folder_entries(root, limit=3)
    full, full_truncated = folder_tree.folder_entries(root, limit=None)

    assert [entry["path"] for entry in entries] == ["f0.txt", "f1.txt", "f2.txt"]
    assert truncated is True
    assert len(full) == 5
    assert full_truncated is False
    assert full[0] == {"path": "f0.txt", "type": "file", "size": 2}


def test_the_digest_is_cached_until_the_file_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file with the same size, time and inode is hashed once; a change hashes it again."""
    path = _write(tmp_path, {"big.bin": "a" * 100}) / "big.bin"
    monkeypatch.setattr(folder_tree, "_digests", {})
    calls: list[str] = []
    real = hashlib.file_digest

    def counting(body: object, name: str) -> object:
        calls.append(name)
        return real(body, name)  # type: ignore[arg-type]

    monkeypatch.setattr(folder_tree.hashlib, "file_digest", counting)

    first = folder_tree.file_digest(tmp_path, "big.bin")
    second = folder_tree.file_digest(tmp_path, "big.bin")
    path.write_text("b" * 101)
    third = folder_tree.file_digest(tmp_path, "big.bin")

    assert first == second == hashlib.sha1(b"a" * 100, usedforsecurity=False).digest()
    assert third == hashlib.sha1(b"b" * 101, usedforsecurity=False).digest()
    assert len(calls) == 2


def test_the_source_id_matches_the_setup_agents_draft_base(tmp_path: Path) -> None:
    """The id formula and the skips give the same id the setup agent records as a draft's base."""
    root = _write(
        tmp_path / "app",
        {
            "src/main.py": "x = 1\n",
            "README.md": "hi\n",
            ".env": "KEY=1\n",
            ".git/HEAD": "ref\n",
            ".gitignore": "*.log\n",
            "a.log": "l\n",
            "a-b.txt": "1\n",
        },
    )

    assert folder_tree.folder_source_id(root) == SHARED_VECTOR_ID
    assert folder_tree.folder_source_id(root, folder_tree.folder_entries(root, limit=None)[0]) == SHARED_VECTOR_ID


def test_files_that_the_operating_system_writes_keep_the_id(tmp_path: Path) -> None:
    """Finder, Explorer and Spotlight files stay out, so opening the folder keeps the setup agent's id."""
    files = {
        "src/main.py": "x = 1\n",
        "README.md": "hi\n",
        ".env": "KEY=1\n",
        ".git/HEAD": "ref\n",
        ".gitignore": "*.log\n",
        "a.log": "l\n",
        "a-b.txt": "1\n",
    }
    root = _write(tmp_path / "app", files)
    before = folder_tree.folder_source_id(root)
    _write(
        root,
        {
            ".DS_Store": "finder\n",
            "src/.ds_store": "finder\n",
            "._README.md": "appledouble\n",
            "Thumbs.db": "explorer\n",
            "src/desktop.ini": "explorer\n",
            ".Spotlight-V100/store.db": "index\n",
            ".Trashes/501/x": "trash\n",
        },
    )

    assert before == SHARED_VECTOR_ID
    assert folder_tree.folder_source_id(root) == SHARED_VECTOR_ID
    assert [entry["path"] for entry in folder_tree.folder_entries(root)[0]] == [
        ".gitignore",
        "README.md",
        "a-b.txt",
        "src",
        "src/main.py",
    ]


def test_a_file_that_goes_away_still_moves_the_id(tmp_path: Path) -> None:
    """A listed file that cannot be read gives another id than the readable folder."""
    root = _write(tmp_path / "app", {"a.txt": "1\n", "b.txt": "2\n"})
    entries = folder_tree.folder_entries(root)[0]
    before = folder_tree.folder_source_id(root, entries)
    (root / "b.txt").unlink()

    assert folder_tree.folder_source_id(root, entries) != before


def test_the_execute_bit_moves_the_id_even_with_a_cached_digest(tmp_path: Path) -> None:
    """A chmod changes only the ctime, and the cache key holds it, so the id follows the execute bit."""
    root = _write(tmp_path / "app", {"run.sh": "echo hi\n"})
    script = root / "run.sh"
    script.chmod(0o644)
    plain = folder_tree.folder_source_id(root)
    stamp = script.stat()

    script.chmod(0o755)
    os.utime(script, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    assert folder_tree.file_digest(root, "run.sh").endswith(folder_tree.EXEC_MARKER)
    assert folder_tree.folder_source_id(root) != plain
    script.chmod(0o640)
    assert folder_tree.folder_source_id(root) == plain


def test_the_digest_never_leaves_the_folder_through_a_parent_swapped_for_a_link(tmp_path: Path) -> None:
    """A listed subfolder that becomes a link to an outside folder is refused, not followed."""
    root = _write(tmp_path / "app", {"sub/hosts": "inside\n"})
    outside = _write(tmp_path / "secret", {"hosts": "outside\n"})
    (root / "sub" / "hosts").unlink()
    (root / "sub").rmdir()
    (root / "sub").symlink_to(outside)

    with pytest.raises(OSError):
        folder_tree.file_digest(root, "sub/hosts")


def test_the_digest_refuses_a_pipe_without_blocking(tmp_path: Path) -> None:
    """A file swapped for a pipe with no writer is refused at once."""
    root = tmp_path / "app"
    root.mkdir()
    os.mkfifo(root / "a.txt")

    with pytest.raises(OSError, match="Not a regular file"):
        folder_tree.file_digest(root, "a.txt")

"""Tests for the setup agent's ``apply_patch`` tool on a folder copy (SKY-997)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_gateway.agents.apply_patch import PatchError, apply_patch, parse_patch


def _snapshot(root: Path) -> dict[str, bytes]:
    """Read every file below a folder.

    Args:
        root: The folder.

    Returns:
        Path to bytes.
    """
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _patch(*lines: str) -> str:
    """Build a patch text from its body lines.

    Args:
        lines: The lines between the begin and end lines.

    Returns:
        The patch.
    """
    return "\n".join(["*** Begin Patch", *lines, "*** End Patch"])


def test_add_update_delete_and_move_change_the_copy(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n")
    (tmp_path / "old.txt").write_text("bye\n")
    (tmp_path / "run.sh").write_text("echo a\n")
    (tmp_path / "run.sh").chmod(0o755)
    summary = apply_patch(
        tmp_path,
        _patch(
            "*** Add File: docs/new.md",
            "+# New",
            "+text",
            "*** Update File: src/app.py",
            "@@ def main():",
            "-    return 1",
            "+    return 2",
            "*** Delete File: old.txt",
            "*** Update File: run.sh",
            "*** Move to: bin/run.sh",
            "@@",
            "-echo a",
            "+echo b",
        ),
    )
    assert summary == ["A docs/new.md", "M src/app.py", "D old.txt", "R run.sh -> bin/run.sh"]
    assert (tmp_path / "docs" / "new.md").read_text() == "# New\ntext\n"
    assert (tmp_path / "src" / "app.py").read_text() == "def main():\n    return 2\n"
    assert not (tmp_path / "old.txt").exists()
    assert not (tmp_path / "run.sh").exists()
    assert (tmp_path / "bin" / "run.sh").read_text() == "echo b\n"
    # A move keeps the execute bit.
    assert (tmp_path / "bin" / "run.sh").stat().st_mode & 0o111


def test_the_first_chunk_may_skip_its_header_line(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    apply_patch(tmp_path, _patch("*** Update File: a.txt", " one", "-two", "+TWO"))
    assert (tmp_path / "a.txt").read_text() == "one\nTWO\nthree\n"


def test_a_hunk_matches_lines_with_other_trailing_spaces(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x = 1   \ny = 2\n")
    apply_patch(tmp_path, _patch("*** Update File: a.txt", "@@", "-x = 1", "+x = 3"))
    assert (tmp_path / "a.txt").read_text() == "x = 3\ny = 2\n"


def test_a_bad_hunk_changes_nothing_and_says_why(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\n")
    (tmp_path / "b.txt").write_text("two\n")
    before = _snapshot(tmp_path)
    with pytest.raises(PatchError, match=r"b\.txt"):
        apply_patch(
            tmp_path,
            _patch(
                "*** Update File: a.txt",
                "@@",
                "-one",
                "+ONE",
                "*** Add File: c.txt",
                "+new",
                "*** Update File: b.txt",
                "@@",
                "-not in the file",
                "+x",
            ),
        )
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("path", ["/etc/passwd", "~/x", "../outside.txt", "a/../../outside.txt"])
def test_a_path_outside_the_copy_is_refused(tmp_path: Path, path: str) -> None:
    root = tmp_path / "copy"
    root.mkdir()
    with pytest.raises(PatchError):
        apply_patch(root, _patch(f"*** Add File: {path}", "+x"))
    assert not (tmp_path / "outside.txt").exists()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("path", [".env", "config/.env.local", ".git/config"])
def test_env_and_git_paths_are_refused(tmp_path: Path, path: str) -> None:
    with pytest.raises(PatchError):
        apply_patch(tmp_path, _patch(f"*** Add File: {path}", "+SECRET=1"))
    assert not (tmp_path / path).exists()


def test_a_link_in_the_path_is_refused_and_its_target_stays(tmp_path: Path) -> None:
    root = tmp_path / "copy"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "a.txt").write_text("outside\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "file_link.txt").symlink_to(outside / "a.txt")
    for patch in (
        _patch("*** Add File: linked/b.txt", "+x"),
        _patch("*** Update File: linked/a.txt", "@@", "-outside", "+inside"),
        _patch("*** Update File: file_link.txt", "@@", "-outside", "+inside"),
        _patch("*** Delete File: file_link.txt"),
        _patch("*** Add File: file_link.txt", "+x"),
    ):
        with pytest.raises(PatchError, match="link"):
            apply_patch(root, patch)
    assert (outside / "a.txt").read_text() == "outside\n"
    assert sorted(path.name for path in outside.iterdir()) == ["a.txt"]
    assert (root / "file_link.txt").is_symlink()


def test_an_update_of_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="missing"):
        apply_patch(tmp_path, _patch("*** Update File: gone.txt", "@@", "-a", "+b"))


@pytest.mark.parametrize(
    "text",
    [
        "*** Add File: a.txt\n+x\n*** End Patch",
        "*** Begin Patch\n*** Add File: a.txt\n+x",
        "*** Begin Patch\n*** End Patch",
        "*** Begin Patch\n*** Update File: a.txt\n*** End Patch",
        "*** Begin Patch\n*** Update File: a.txt\n@@\n?bad\n*** End Patch",
        "*** Begin Patch\nhello\n*** End Patch",
    ],
)
def test_a_patch_that_breaks_the_format_is_refused(text: str) -> None:
    with pytest.raises(PatchError):
        parse_patch(text)


def test_one_patch_can_replace_a_file_with_a_folder(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("old\n")
    summary = apply_patch(tmp_path, _patch("*** Delete File: a", "*** Add File: a/b", "+new"))
    assert summary == ["D a", "A a/b"]
    assert _snapshot(tmp_path) == {"a/b": b"new\n"}


def test_a_file_and_a_file_below_it_in_one_patch_are_refused_and_change_nothing(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n")
    for lines in (
        ("*** Add File: a", "+x", "*** Add File: a/b", "+y"),
        ("*** Add File: a/b", "+y", "*** Add File: a", "+x"),
    ):
        with pytest.raises(PatchError, match="a"):
            apply_patch(tmp_path, _patch(*lines))
        assert _snapshot(tmp_path) == {"keep.txt": b"keep\n"}


def test_a_deleted_link_does_not_free_the_path_below_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").symlink_to(outside)
    with pytest.raises(PatchError):
        apply_patch(root, _patch("*** Delete File: a", "*** Add File: a/b", "+new"))
    assert list(outside.iterdir()) == []

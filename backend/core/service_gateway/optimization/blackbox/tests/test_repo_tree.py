"""Tests for shipping a repository tree and checking the versions written against it."""

from __future__ import annotations

import base64
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from ..repo_tree import (
    apply_patch,
    archive_chunks,
    drop_paths,
    patch_paths,
    patch_violations,
    reset_tree,
    strip_archive,
    unpack_tree,
    version_patch,
)

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _tree(root: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``root`` and pack them the way the parent ships a snapshot.

    Args:
        root: Scratch folder.
        files: Relative path to text.

    Returns:
        The packed archive.
    """
    source = root / "source"
    for name, text in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(text)
    archive = root / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    return archive


def _unpacked(tmp_path: Path, files: dict[str, str]) -> Path:
    """Ship ``files`` through base64 chunks and rebuild them as a checkout.

    Args:
        tmp_path: Scratch folder.
        files: Relative path to text.

    Returns:
        The checkout.
    """
    chunk_paths = []
    for index, chunk in enumerate(archive_chunks(_tree(tmp_path, files))):
        chunk_paths.append(tmp_path / f"chunk.{index}")
        chunk_paths[-1].write_text(chunk)
    return unpack_tree(chunk_paths, tmp_path / "checkout")


def test_a_version_round_trips_through_a_patch(tmp_path: Path) -> None:
    """Capture edits, additions and deletions as one patch that rebuilds the same tree."""
    checkout = _unpacked(tmp_path, {"src/app.py": "x = 1\n", "src/old.py": "gone\n", "README.md": "hi\n"})
    (checkout / "src/app.py").write_text("x = 2\n")
    (checkout / "src/old.py").unlink()
    (checkout / "src/new file.py").write_text("y = 3\n")
    (checkout / "src/blob.bin").write_bytes(bytes(range(256)))

    patch = version_patch(checkout)
    reset_tree(checkout)
    assert (checkout / "src/app.py").read_text() == "x = 1\n"
    assert not (checkout / "src/new file.py").exists()
    apply_patch(checkout, patch)

    assert (checkout / "src/app.py").read_text() == "x = 2\n"
    assert not (checkout / "src/old.py").exists()
    assert (checkout / "src/new file.py").read_text() == "y = 3\n"
    assert (checkout / "src/blob.bin").read_bytes() == bytes(range(256))
    assert set(patch_paths(patch)) == {"src/app.py", "src/old.py", "src/new file.py", "src/blob.bin"}
    assert patch_violations(patch, ["src"]) == []


def test_commits_in_the_checkout_stay_in_the_version(tmp_path: Path) -> None:
    """Diff against the shipped commit, so committing an edit cannot hide it."""
    checkout = _unpacked(tmp_path, {"src/a.py": "a\n"})
    (checkout / "src/a.py").write_text("b\n")
    subprocess.run([*_GIT, "commit", "--quiet", "-am", "agent"], cwd=checkout, check=True)

    assert patch_paths(version_patch(checkout)) == ["src/a.py"]
    reset_tree(checkout)
    assert (checkout / "src/a.py").read_text() == "a\n"


def test_ignored_files_are_left_out_of_a_version(tmp_path: Path) -> None:
    """Keep caches and build output the repository ignores out of the patch."""
    checkout = _unpacked(tmp_path, {".gitignore": "*.log\n", "src/a.py": "a\n"})
    (checkout / "src/run.log").write_text("noise\n")
    (checkout / "src/a.py").write_text("b\n")

    assert patch_paths(version_patch(checkout)) == ["src/a.py"]


def test_edits_outside_the_editable_paths_are_reported(tmp_path: Path) -> None:
    """Name every file a version may not touch, including the targets of a rename."""
    checkout = _unpacked(tmp_path, {"src/a.py": "a\n", "tests/test_a.py": "t\n"})
    (checkout / "tests/test_a.py").write_text("rigged\n")
    (checkout / "lib").mkdir()
    subprocess.run([*_GIT, "mv", "src/a.py", "lib/a.py"], cwd=checkout, check=True)

    problems = patch_violations(version_patch(checkout), ["src"])

    assert any("tests/test_a.py" in problem for problem in problems)
    assert any("lib/a.py" in problem for problem in problems)


def test_editable_root_allows_everything_but_read_only_paths(tmp_path: Path) -> None:
    """Treat ``.`` as the whole tree while submodules and LFS files stay read-only."""
    checkout = _unpacked(tmp_path, {"src/a.py": "a\n", "assets/model.bin": "lfs pointer\n"})
    (checkout / "src/a.py").write_text("b\n")
    (checkout / "assets/model.bin").write_text("swapped\n")

    problems = patch_violations(version_patch(checkout), ["."], ["assets/model.bin"])

    assert problems == ["'assets/model.bin' is a submodule or Git LFS file, which are read-only."]


@pytest.mark.parametrize(
    "header",
    [
        "diff --git a/../etc/passwd b/../etc/passwd",
        "diff --git a/.git/config b/.git/config",
        'diff --git "a/src/\\303\\251.py" "b/src/../../x.py"',
    ],
)
def test_paths_that_escape_the_repository_are_refused(header: str) -> None:
    """Reject traversal and git metadata no matter how the header spells them."""
    assert any("not a path inside" in problem for problem in patch_violations(header + "\n", ["."]))


def test_quoted_unicode_paths_are_decoded() -> None:
    """Read git's octal-escaped names as the real file names."""
    assert patch_paths('diff --git "a/src/\\303\\251.py" "b/src/\\303\\251.py"\n') == ["src/é.py"]


def test_submodule_changes_are_refused() -> None:
    """Refuse any patch that adds or moves a gitlink."""
    patch = "diff --git a/vendor/lib b/vendor/lib\nnew file mode 160000\nindex 0000000..abc1234\n"

    assert any("Submodules are read-only" in problem for problem in patch_violations(patch, ["."]))


def test_oversized_patches_are_refused() -> None:
    """Stop a version larger than one sandbox upload before it is parsed."""
    assert "larger than" in patch_violations("+" * (9 * 1024 * 1024), ["."])[0]


def test_archive_chunks_rebuild_the_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Split an archive into base64 pieces that decode back to the same bytes."""
    monkeypatch.setattr("core.service_gateway.optimization.blackbox.repo_tree.ARCHIVE_CHUNK_BYTES", 7)
    archive = tmp_path / "blob"
    archive.write_bytes(bytes(range(50)))

    pieces = list(archive_chunks(archive))

    assert len(pieces) == 8
    assert b"".join(base64.b64decode(piece) for piece in pieces) == bytes(range(50))


def test_protected_paths_are_refused_even_under_the_whole_tree(tmp_path: Path) -> None:
    """A protected file is named with its rule while an editable sibling passes."""
    checkout = _unpacked(tmp_path, {"src/a.py": "a\n", "eval/labels.json": "[]\n"})
    (checkout / "src/a.py").write_text("b\n")
    (checkout / "eval/labels.json").write_text("[1]\n")

    problems = patch_violations(version_patch(checkout), ["."], [], ["eval"])

    assert problems == ["'eval/labels.json' is protected by the repository's optimize.toml ([paths] protected)."]


def test_strip_archive_leaves_out_every_hidden_path(tmp_path: Path) -> None:
    """The agent's copy has no metric file and no exploit folder; every other file stays (SKY-353)."""
    archive = _tree(
        tmp_path,
        {
            "src/app.py": "x = 1\n",
            "eval/scorer.py": "def score(): ...\n",
            "eval/data.csv": "1\n",
            "evaluation/keep.py": "kept\n",
            ".optimize/exploits/cheat.py": "cheat\n",
        },
    )

    stripped = strip_archive(archive, ["eval/scorer.py", ".optimize/exploits"], tmp_path / "agent.tgz")

    with tarfile.open(stripped) as packed:
        names = {name.removeprefix("./") for name in packed.getnames()}
    assert {"src/app.py", "eval/data.csv", "evaluation/keep.py"} <= names
    assert not {name for name in names if name.startswith(("eval/scorer.py", ".optimize/exploits"))}
    with tarfile.open(archive) as full:
        assert "eval/scorer.py" in {name.removeprefix("./") for name in full.getnames()}


def test_strip_archive_drops_a_link_to_a_hidden_file(tmp_path: Path) -> None:
    """A hard link to a metric file would carry its bytes, so it goes too."""
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        data = tarfile.TarInfo("eval/scorer.py")
        data.size = 1
        packed.addfile(data, io.BytesIO(b"x"))
        link = tarfile.TarInfo("src/copy.py")
        link.type, link.linkname = tarfile.LNKTYPE, "eval/scorer.py"
        packed.addfile(link)

    with tarfile.open(strip_archive(archive, ["eval"], tmp_path / "agent.tgz")) as packed:
        assert packed.getnames() == []


def test_drop_paths_removes_the_sections_that_touch_a_hidden_path() -> None:
    """A version's edit to a metric file is ignored; its other edits stay."""
    patch = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        "diff --git a/eval/scorer.py b/eval/scorer.py\n--- a/eval/scorer.py\n+++ b/eval/scorer.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )

    kept = drop_paths(patch, ["eval/scorer.py"])

    assert patch_paths(kept) == ["src/app.py"]
    assert drop_paths(patch, []) == patch
    assert drop_paths("", ["eval"]) == ""

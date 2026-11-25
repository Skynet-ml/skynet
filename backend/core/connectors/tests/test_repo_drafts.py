"""Tests for the run's draft files: the request's path rules and their writes over the pinned tree."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from ...models.blackbox import DRAFT_FILE_BYTES, BlackboxRepoSource
from ..repo_drafts import DraftFileError, apply_to_archive, clean_drafts, write_to_checkout


def _source(drafts: dict[str, str]) -> BlackboxRepoSource:
    """Build a repository source that carries draft files.

    Args:
        drafts: Repository path to file text.

    Returns:
        The validated source.
    """
    return BlackboxRepoSource(repository="acme/app", editable_paths=["src"], draft_files=drafts)


def _archive(root: Path, files: dict[str, str], links: dict[str, str] | None = None) -> Path:
    """Pack a tree the way the fetch packs it.

    Args:
        root: Folder the archive goes in.
        files: Root-relative path to text; each file gets mode 0o755.
        links: Root-relative link path to its target.

    Returns:
        The archive.
    """
    archive = root / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            packed.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            packed.addfile(info)
    return archive


def _read(archive: Path) -> dict[str, tuple[str, int]]:
    """Read every regular file of an archive.

    Args:
        archive: Packed tree.

    Returns:
        Path to its text and mode.
    """
    with tarfile.open(archive) as packed:
        return {
            member.name: (packed.extractfile(member).read().decode(), member.mode)  # type: ignore[union-attr]
            for member in packed.getmembers()
            if member.isreg()
        }


def test_the_request_keeps_clean_draft_paths() -> None:
    """A draft path loses its outer spaces and trailing slash; its text stays as it is."""
    assert _source({" src/app.py ": "x = 2\n"}).draft_files == {"src/app.py": "x = 2\n"}


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../x", "src/../../x", "a\\b", ".git/config", "src/.GIT/hooks/post", "a//b", "./a", "a\x00b", ""],
)
def test_the_request_refuses_a_path_outside_the_repository_or_in_git(path: str) -> None:
    """An absolute, escaping, odd or .git path never reaches the run."""
    with pytest.raises(ValidationError, match=r"path|\.git folder"):
        _source({path: "x"})


def test_the_request_refuses_two_names_for_one_file_and_too_large_files() -> None:
    """Two paths that clean to one file, one file over the cap and text that is not UTF-8 are refused."""
    with pytest.raises(ValidationError, match="twice"):
        _source({"a.py": "1", "a.py/": "2"})
    with pytest.raises(ValidationError):
        _source({"a.py": "x" * (DRAFT_FILE_BYTES + 1)})
    with pytest.raises(ValidationError):
        _source({"a.py": "\ud800"})


def test_a_draft_under_another_draft_is_refused() -> None:
    """One draft cannot be a file and a folder of another draft at once."""
    with pytest.raises(DraftFileError, match="under another draft"):
        clean_drafts({"src": "x", "src/app.py": "y"})


def test_drafts_replace_and_add_files_in_the_packed_tree(tmp_path: Path) -> None:
    """A draft replaces an existing file and keeps its mode, and adds a new file next to the others."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "README.md": "hi\n"})

    apply_to_archive(archive, {"src/app.py": "x = 2\n", "src/new/mod.py": "y = 3\n"})

    assert _read(archive) == {
        "src/app.py": ("x = 2\n", 0o755),
        "README.md": ("hi\n", 0o755),
        "src/new/mod.py": ("y = 3\n", 0o644),
    }


def test_no_drafts_leave_the_packed_tree_as_it_is(tmp_path: Path) -> None:
    """A run without drafts does not repack its tree."""
    archive = _archive(tmp_path, {"a.py": "1"})
    before = archive.read_bytes()

    apply_to_archive(archive, None)
    apply_to_archive(archive, {})

    assert archive.read_bytes() == before


@pytest.mark.parametrize(
    ("drafts", "readonly", "refusal"),
    [
        ({"linked/x.py": "1"}, (), "under a link or a file"),
        ({"a.py/x.py": "1"}, (), "under a link or a file"),
        ({"linked": "1"}, (), "a link or a folder"),
        ({"src": "1"}, (), "a link or a folder"),
        ({"deep": "1"}, (), "a link or a folder"),
        ({"vendor/lib/x.py": "1"}, ("vendor/lib",), "read-only"),
    ],
)
def test_a_draft_never_follows_a_link_or_changes_a_read_only_path(
    tmp_path: Path, drafts: dict[str, str], readonly: tuple[str, ...], refusal: str
) -> None:
    """A draft through a link, over a link or folder, under a file, or in a submodule is refused; the tree stays."""
    archive = _archive(tmp_path, {"a.py": "1", "src/app.py": "x", "deep/er/x.py": "y"}, links={"linked": "/etc"})
    before = archive.read_bytes()

    with pytest.raises(DraftFileError, match=refusal):
        apply_to_archive(archive, drafts, readonly)

    assert archive.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tree.tgz"]


def test_drafts_write_into_a_checkout(tmp_path: Path) -> None:
    """The checkout gets each draft file, in new folders where needed."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("x = 1\n")

    written = write_to_checkout(tmp_path, {"src/app.py": "x = 2\n", "docs/new.md": "new\n"})

    assert written == ["src/app.py", "docs/new.md"]
    assert (tmp_path / "src/app.py").read_text() == "x = 2\n"
    assert (tmp_path / "docs/new.md").read_text() == "new\n"


def test_a_checkout_draft_never_follows_a_link(tmp_path: Path) -> None:
    """A draft through a linked folder or over a linked file is refused and writes nothing."""
    outside = tmp_path / "outside"
    outside.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "linked").symlink_to(outside)
    (checkout / "file").symlink_to(outside / "target.txt")

    with pytest.raises(DraftFileError):
        write_to_checkout(checkout, {"linked/x.py": "1"})
    with pytest.raises(DraftFileError):
        write_to_checkout(checkout, {"file": "1"})

    assert list(outside.iterdir()) == []


def test_a_checkout_draft_never_changes_a_read_only_path(tmp_path: Path) -> None:
    """A draft in a submodule or over a Git LFS file is refused and writes nothing."""
    (tmp_path / "vendor/lib").mkdir(parents=True)

    with pytest.raises(DraftFileError, match="read-only"):
        write_to_checkout(tmp_path, {"src/ok.py": "1", "vendor/lib/x.py": "1"}, ("vendor/lib",))

    assert not (tmp_path / "src").exists()
    assert list((tmp_path / "vendor/lib").iterdir()) == []


def test_drafts_that_push_the_tree_over_the_size_cap_are_refused(tmp_path: Path) -> None:
    """The fetch checks the size before the drafts, so the repack checks it again; the tree stays."""
    archive = _archive(tmp_path, {"a.py": "1"})
    before = archive.read_bytes()
    # Random text does not compress, so the repacked tree grows past the cap.
    noise = "".join(f"{index:08x}" for index in range(4096))

    with pytest.raises(DraftFileError, match="larger than"):
        apply_to_archive(archive, {"big.txt": noise}, max_bytes=len(before) + 1024)

    assert archive.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tree.tgz"]

"""Tests for the provider-neutral tree-id sandbox helpers (SKY-718)."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.sandbox import trees as tree_helpers
from core.sandbox.trees import (
    BadArchiveError,
    BoxFailureError,
    FenceLostError,
    GrantDeniedError,
    TreeSandboxError,
    tar_to_tree,
    tree_to_tar,
)
from core.storage.models import Base, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore, object_id_of

RUN = "job-ada"


@pytest.fixture
def trees(tmp_path: Path) -> Iterator[TreeStore]:
    """Open a tree store over a SQLite step log with one job.

    Args:
        tmp_path: Scratch folder for the database and the objects.

    Yields:
        The tree store.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(JobModel(optimization_id=RUN, username="ada", execution_generation=1))
        session.commit()
    log = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w1", flush_window_seconds=60.0)
    yield TreeStore(log)
    log.close()


def _members(data: bytes) -> dict[str, tuple[str, bytes | str]]:
    """Read an archive into a map of path to kind and content.

    Args:
        data: A gzip tar archive.

    Returns:
        Maps each member path to ``("dir", "")``, ``("link", target)`` or
        ``(mode, bytes)`` where mode is ``file`` or ``exec``.
    """
    seen: dict[str, tuple[str, bytes | str]] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            if member.isdir():
                seen[member.name] = ("dir", "")
            elif member.issym():
                seen[member.name] = ("link", member.linkname)
            else:
                handle = archive.extractfile(member)
                assert handle is not None
                seen[member.name] = ("exec" if member.mode & 0o111 else "file", handle.read())
    return seen


def test_a_tree_turns_into_an_archive_and_back_to_the_same_id(trees: TreeStore) -> None:
    """Files, an executable, a symlink, a subtree and an empty folder survive the trip."""
    inner = trees.put_tree(RUN, [TreeEntry("deep.txt", "file", trees.put_blob(RUN, b"deep\n"))])
    empty = trees.put_tree(RUN, [])
    root = trees.put_tree(
        RUN,
        [
            TreeEntry("a.txt", "file", trees.put_blob(RUN, b"a\n")),
            TreeEntry("run.sh", "exec", trees.put_blob(RUN, b"#!/bin/sh\n")),
            TreeEntry("link", "symlink", trees.put_blob(RUN, b"a.txt")),
            TreeEntry("sub", "tree", inner),
            TreeEntry("empty", "tree", empty),
        ],
    )

    data = tree_to_tar(trees, RUN, root)

    assert _members(data) == {
        "a.txt": ("file", b"a\n"),
        "run.sh": ("exec", b"#!/bin/sh\n"),
        "link": ("link", "a.txt"),
        "sub": ("dir", ""),
        "sub/deep.txt": ("file", b"deep\n"),
        "empty": ("dir", ""),
    }
    assert tar_to_tree(trees, RUN, data) == root


def test_an_archive_from_tar_with_a_dot_root_gives_the_same_tree(trees: TreeStore, tmp_path: Path) -> None:
    """A ``tar -C dir .`` archive names its members ``./name``; the tree has no ``.`` entry."""
    folder = tmp_path / "box"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "app.py").write_text("x = 1\n")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(folder, arcname=".")

    tree_id = tar_to_tree(trees, RUN, buffer.getvalue())

    entries = trees.get_tree(RUN, tree_id)
    assert [entry.name for entry in entries or []] == ["src"]
    assert _members(tree_to_tar(trees, RUN, tree_id))["src/app.py"] == ("file", b"x = 1\n")


def test_an_archive_member_that_leaves_the_tree_is_refused(trees: TreeStore) -> None:
    """A ``..`` path cannot write outside the tree."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("../escape.txt")
        archive.addfile(info, io.BytesIO(b""))

    with pytest.raises(ValueError, match="leaves the tree"):
        tar_to_tree(trees, RUN, buffer.getvalue())


def test_a_tree_the_run_cannot_see_is_a_lookup_error(trees: TreeStore) -> None:
    """An unknown tree id gives no archive."""
    with pytest.raises(LookupError):
        tree_to_tar(trees, RUN, "0" * 64)


@pytest.mark.parametrize("error_type", [FenceLostError, GrantDeniedError, BoxFailureError])
def test_each_typed_error_names_its_step(error_type: type[TreeSandboxError]) -> None:
    """Fence loss, grant denial and box failure all keep the step id."""
    error = error_type("job-ada:agent-1:3", "it failed")

    assert isinstance(error, TreeSandboxError)
    assert error.step_id == "job-ada:agent-1:3"
    assert "job-ada:agent-1:3" in str(error)


def _archive(*members: tuple[tarfile.TarInfo, bytes], format: int = tarfile.PAX_FORMAT) -> bytes:
    """Build a gzip archive from raw members.

    Args:
        *members: Each member header and its file content.
        format: The tar header format.

    Returns:
        The archive bytes.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=format) as archive:
        for info, content in members:
            if info.isreg():
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            else:
                archive.addfile(info)
    return buffer.getvalue()


def _info(name: str, kind: bytes = tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
    """Return one member header.

    Args:
        name: The member path.
        kind: The tar member type.
        linkname: The link target, for links.

    Returns:
        The header.
    """
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info


def _cut_last_member(*members: tuple[tarfile.TarInfo, bytes]) -> bytes:
    """Build a plain archive whose last member's content ends early.

    Args:
        *members: Each regular-file header and its content; the last content is cut.

    Returns:
        The archive bytes, cut 10 bytes into the last member's content.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for info, content in members:
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    with tarfile.open(fileobj=io.BytesIO(buffer.getvalue()), mode="r:") as archive:
        last = archive.getmembers()[-1]
    return buffer.getvalue()[: last.offset_data + 10]


def _pax_named(name: str) -> tarfile.TarInfo:
    """Return a regular-file header whose path comes only from a pax header.

    Args:
        name: The path, which may hold bytes a USTAR header cannot.

    Returns:
        The header.
    """
    info = _info("placeholder")
    info.pax_headers = {"path": name}
    return info


@pytest.mark.parametrize(
    "data",
    [
        _archive((_info("a"), b"a\n"), (_info("../x"), b"x\n")),
        _archive((_info("a"), b"a\n"), (_info("b", tarfile.LNKTYPE, "missing"), b"")),
        _cut_last_member((_info("a"), b"a\n"), (_info("b"), b"b" * 600)),
        _archive((_info("a"), b"a\n"), (_pax_named("b\0c"), b"x\n")),
        _archive((_info("a"), b"a\n"), (_info(".git/config"), b"x\n")),
        _archive((_info("a"), b"a\n"), (_info("sub/.GIT/x"), b"x\n")),
        _archive((_info("a"), b"a\n"), (_info(".Git"), b"x\n")),
        _archive((_info("a"), b"a\n"), (_info("sub/.gIt", tarfile.DIRTYPE), b"")),
    ],
    ids=[
        "path-leaves-the-tree",
        "hard-link-to-nothing",
        "content-cut-short",
        "pax-nul-name",
        "git-folder",
        "upper-case-git-folder",
        "git-file",
        "git-dir-member",
    ],
)
def test_a_refused_archive_stores_no_blob(trees: TreeStore, monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    """A bad last member stops the archive before the good first member's blob is stored.

    Args:
        trees: The tree store.
        monkeypatch: Pytest monkeypatch.
        data: An archive whose last member is bad.
    """
    stored: list[bytes] = []
    put_blob = trees.put_blob

    def _recording_put_blob(run_id: str, content: bytes) -> str:
        """Record the content, then store it.

        Args:
            run_id: The run.
            content: The blob bytes.

        Returns:
            The blob id.
        """
        stored.append(bytes(content))
        return put_blob(run_id, content)

    monkeypatch.setattr(trees, "put_blob", _recording_put_blob)

    with pytest.raises(BadArchiveError):
        tar_to_tree(trees, RUN, data)

    assert stored == []


@pytest.mark.parametrize("name", [".git/config", "sub/.GIT/x", ".Git"])
def test_a_git_path_part_in_any_letter_case_is_a_bad_archive(trees: TreeStore, name: str) -> None:
    """A member with a ``.git`` part in any letter case is refused with a clear reason (SKY-810).

    Args:
        trees: The tree store.
        name: The member path.
    """
    with pytest.raises(BadArchiveError, match=r"holds a \.git"):
        tar_to_tree(trees, RUN, _archive((_info(name), b"x\n")))


def test_names_near_git_stay_in_the_tree(trees: TreeStore) -> None:
    """``.gitignore``, ``.gitattributes``, ``x.git`` and ``a.git/b`` are plain names."""
    data = _archive(
        (_info(".gitignore"), b"1"), (_info(".gitattributes"), b"2"), (_info("x.git"), b"3"), (_info("a.git/b"), b"4")
    )

    tree_id = tar_to_tree(trees, RUN, data)

    assert sorted(entry.name for entry in trees.get_tree(RUN, tree_id) or []) == [
        ".gitattributes",
        ".gitignore",
        "a.git",
        "x.git",
    ]


def _put_raw_tree(trees: TreeStore, rows: list[list[str]]) -> str:
    """Store a tree object without :func:`encode_tree`, as a tree stored before SKY-810 can be.

    Args:
        trees: The tree store.
        rows: The ``[name, mode, object_id]`` entries, unchecked.

    Returns:
        The tree's object id.
    """
    stored = b"tree\0" + json.dumps(rows, separators=(",", ":")).encode("utf-8")
    object_id = object_id_of(stored)
    trees._step_log.put_object(RUN, object_id, stored)
    return object_id


def test_an_old_tree_with_git_parts_is_written_out_without_them(trees: TreeStore) -> None:
    """``tree_to_tar`` leaves out ``.git`` entries in any letter case at any depth (SKY-810)."""
    blob = trees.put_blob(RUN, b"x\n")
    git = _put_raw_tree(trees, [["config", "file", blob]])
    sub = _put_raw_tree(trees, [[".GIT", "tree", git], ["keep.txt", "file", blob]])
    root = _put_raw_tree(
        trees, [[".Git", "file", blob], [".git", "tree", git], ["a.py", "file", blob], ["sub", "tree", sub]]
    )

    members = _members(tree_to_tar(trees, RUN, root))

    assert sorted(members) == ["a.py", "sub", "sub/keep.txt"]


def test_an_absolute_member_name_lands_inside_the_tree(trees: TreeStore) -> None:
    """A leading ``/`` is dropped, so the member is a normal tree path."""
    tree_id = tar_to_tree(trees, RUN, _archive((_info("/etc/passwd"), b"x\n")))

    assert _members(tree_to_tar(trees, RUN, tree_id)) == {"etc": ("dir", ""), "etc/passwd": ("file", b"x\n")}


def test_a_member_under_a_file_is_a_bad_archive(trees: TreeStore) -> None:
    """A path cannot pass through a name that is already a file."""
    data = _archive((_info("a"), b"file\n"), (_info("a/b"), b"inner\n"))

    with pytest.raises(BadArchiveError, match="under a file"):
        tar_to_tree(trees, RUN, data)


def test_a_hard_link_stores_the_target_file_again(trees: TreeStore) -> None:
    """A hard link becomes a second entry with the target's mode and blob."""
    data = _archive((_info("a.txt"), b"same\n"), (_info("b.txt", tarfile.LNKTYPE, "a.txt"), b""))

    tree_id = tar_to_tree(trees, RUN, data)

    assert _members(tree_to_tar(trees, RUN, tree_id)) == {
        "a.txt": ("file", b"same\n"),
        "b.txt": ("file", b"same\n"),
    }


@pytest.mark.parametrize("target", ["missing.txt", "folder", "link"])
def test_a_hard_link_to_no_earlier_file_is_a_bad_archive(trees: TreeStore, target: str) -> None:
    """A hard link to a missing path, a folder or a symlink is refused, not a crash.

    Args:
        trees: The tree store.
        target: The hard-link target.
    """
    data = _archive(
        (_info("folder", tarfile.DIRTYPE), b""),
        (_info("link", tarfile.SYMTYPE, "folder"), b""),
        (_info("b.txt", tarfile.LNKTYPE, target), b""),
    )

    with pytest.raises(BadArchiveError, match="names no earlier file"):
        tar_to_tree(trees, RUN, data)


def test_an_archive_over_the_size_limit_is_refused_before_it_opens(
    trees: TreeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compressed size is checked first."""
    monkeypatch.setattr(tree_helpers, "MAX_ARCHIVE_BYTES", 10)

    with pytest.raises(BadArchiveError, match="larger than"):
        tar_to_tree(trees, RUN, _archive((_info("a.txt"), b"a\n")))


def test_an_archive_with_too_many_members_is_refused(trees: TreeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """The member count is capped."""
    monkeypatch.setattr(tree_helpers, "MAX_TREE_MEMBERS", 2)
    data = _archive(*[(_info(f"{index}.txt"), b"") for index in range(3)])

    with pytest.raises(BadArchiveError, match="more than 2 members"):
        tar_to_tree(trees, RUN, data)


def test_an_archive_that_unpacks_too_large_is_refused(trees: TreeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A small gzip that unpacks to many bytes stops at the header sizes."""
    monkeypatch.setattr(tree_helpers, "MAX_TREE_BYTES", 1000)
    data = _archive((_info("a.bin"), b"\0" * 600), (_info("b.bin"), b"\0" * 600))

    with pytest.raises(BadArchiveError, match="unpacks to more than"):
        tar_to_tree(trees, RUN, data)


def test_data_that_is_not_an_archive_is_a_bad_archive(trees: TreeStore) -> None:
    """A read error is a bad archive, not a raw tar error."""
    with pytest.raises(BadArchiveError, match="does not read"):
        tar_to_tree(trees, RUN, b"not a tar archive")


@pytest.mark.parametrize("format", [tarfile.GNU_FORMAT, tarfile.PAX_FORMAT])
def test_a_huge_long_name_header_is_refused_before_it_is_read(trees: TreeStore, format: int) -> None:
    """A small gzip with a 5 MB long-name header stops at the read limit.

    Args:
        trees: The tree store.
        format: The tar format that holds the long name.
    """
    data = _archive((_info("a" * 5 * 1024 * 1024), b""), format=format)

    with pytest.raises(BadArchiveError, match="header larger than the read limit"):
        tar_to_tree(trees, RUN, data)


@pytest.mark.parametrize("field", ["name", "linkname"])
def test_a_member_name_or_link_target_over_the_name_limit_is_refused(trees: TreeStore, field: str) -> None:
    """A name or a symlink target longer than the name limit is a bad archive.

    Args:
        trees: The tree store.
        field: The header field that is too long.
    """
    long = "a" * (tree_helpers.MAX_NAME_BYTES + 1)
    info = _info(long) if field == "name" else _info("link", tarfile.SYMTYPE, long)

    with pytest.raises(BadArchiveError, match="longer than"):
        tar_to_tree(trees, RUN, _archive((info, b"")))


def test_a_member_path_over_the_depth_limit_is_a_bad_archive(trees: TreeStore) -> None:
    """A path of 1500 short folders is under the name limit but would overflow the stack as it is stored."""
    deep = "a/" * 1500 + "f"
    assert len(deep.encode()) <= tree_helpers.MAX_NAME_BYTES

    with pytest.raises(BadArchiveError, match="deeper than"):
        tar_to_tree(trees, RUN, _archive((_info(deep), b"")))


def test_a_member_path_at_the_depth_limit_is_stored(trees: TreeStore) -> None:
    """A path of exactly the depth limit stores as a tree, and the file reads back at the bottom."""
    path = "a/" * (tree_helpers.MAX_TREE_DEPTH - 1) + "f"

    tree_id = tar_to_tree(trees, RUN, _archive((_info(path), b"x")))

    for _ in range(tree_helpers.MAX_TREE_DEPTH - 1):
        [entry] = trees.get_tree(RUN, tree_id) or []
        assert (entry.name, entry.mode) == ("a", "tree")
        tree_id = entry.object_id
    [leaf] = trees.get_tree(RUN, tree_id) or []
    assert leaf.name == "f"
    assert trees.get_blob(RUN, leaf.object_id) == b"x"


def test_a_member_path_one_level_over_the_depth_limit_is_a_bad_archive(trees: TreeStore) -> None:
    """A path of the depth limit plus one level is refused."""
    path = "a/" * tree_helpers.MAX_TREE_DEPTH + "f"

    with pytest.raises(BadArchiveError, match="deeper than"):
        tar_to_tree(trees, RUN, _archive((_info(path), b"x")))


@pytest.mark.parametrize(
    ("pax_headers", "content"),
    [
        ({"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.realsize": "0"}, b"1\n0\n0\n"),
        ({"GNU.sparse.map": "0,0"}, b""),
        ({"GNU.sparse.name": "f"}, b""),
    ],
)
def test_a_pax_sparse_member_is_a_bad_archive(trees: TreeStore, pax_headers: dict[str, str], content: bytes) -> None:
    """A GNU sparse 1.0 or 0.1 member, or any other GNU sparse pax key, is refused before tarfile reads a map.

    Args:
        trees: The tree store.
        pax_headers: The member's pax keys.
        content: The member's data; for sparse 1.0 it starts with the map.
    """
    info = _info("f")
    info.pax_headers = pax_headers

    with pytest.raises(BadArchiveError, match="sparse"):
        tar_to_tree(trees, RUN, _archive((info, content)))


def test_an_old_gnu_sparse_member_is_a_bad_archive(trees: TreeStore) -> None:
    """An old GNU sparse header is refused before tarfile reads its extended headers."""
    with pytest.raises(BadArchiveError, match="sparse"):
        tar_to_tree(trees, RUN, _archive((_info("f", tarfile.GNUTYPE_SPARSE), b""), format=tarfile.GNU_FORMAT))


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("_proc_sparse", (None,)),
        ("_proc_gnusparse_00", (None, {}, b"")),  # Python 3.11: (next, pax_headers, buf)
        ("_proc_gnusparse_00", (None, {})),  # Python 3.12+: (next, raw_headers)
        ("_proc_gnusparse_01", (None, {})),
        ("_proc_gnusparse_10", (None, {}, None)),
    ],
)
def test_the_sparse_overrides_refuse_on_every_python_signature(method: str, args: tuple[object, ...]) -> None:
    """Each sparse override raises BadArchiveError for the 3.11 and the 3.12+ argument counts.

    Args:
        method: The overridden tarfile method.
        args: The positional arguments that tarfile passes on one Python version.
    """
    with pytest.raises(BadArchiveError, match="sparse"):
        getattr(tree_helpers._NoSparseTarInfo("f"), method)(*args)


def test_a_long_chain_of_pax_headers_is_a_bad_archive(trees: TreeStore) -> None:
    """tarfile recurses once per pax header, so a long chain is refused as a bad archive, not a RecursionError."""
    one = tarfile.TarInfo("x")
    one.pax_headers = {"comment": "c"}
    block = one.tobuf(tarfile.PAX_FORMAT)
    # The pax header and its data, without the real member header after them.
    pax_only = block[: len(block) - tarfile.BLOCKSIZE]
    data = gzip.compress(pax_only * 2000 + tarfile.TarInfo("f").tobuf(tarfile.PAX_FORMAT) + b"\0" * 1024)

    with pytest.raises(BadArchiveError, match="RecursionError"):
        tar_to_tree(trees, RUN, data)


def test_a_file_larger_than_one_read_chunk_comes_back_whole(trees: TreeStore) -> None:
    """A file of several read chunks and a partial last chunk keeps every byte."""
    content = bytes(range(256)) * (tree_helpers._CHUNK_BYTES * 5 // 512) + b"tail"
    tree_id = tar_to_tree(trees, RUN, _archive((_info("big.bin"), content)))

    [entry] = trees.get_tree(RUN, tree_id) or []
    assert trees.get_blob(RUN, entry.object_id) == content


def test_a_name_limit_counts_bytes_not_characters(trees: TreeStore) -> None:
    """A name of 4096 two-byte characters is over the 4096-byte limit."""
    with pytest.raises(BadArchiveError, match="longer than"):
        tar_to_tree(trees, RUN, _archive((_info("é" * tree_helpers.MAX_NAME_BYTES), b"")))


def test_a_huge_member_of_an_unknown_type_is_refused_before_the_seek(
    trees: TreeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped member that claims 50 GiB does not make the reader unpack past the limit.

    Args:
        trees: The tree store.
        monkeypatch: Sets small size limits and counts the unpacked bytes.
    """
    monkeypatch.setattr(tree_helpers, "MAX_TREE_BYTES", 0)
    monkeypatch.setattr(tree_helpers, "MAX_TREE_MEMBERS", 1)
    info = tarfile.TarInfo("huge")
    info.type, info.size = b"Z", 50 * 1024**3
    # Zeros after the header give the seek real data to unpack, as a gzip bomb would.
    data = gzip.compress(info.tobuf(tarfile.GNU_FORMAT) + bytes(1024 * 1024))
    peaks: list[int] = []

    class _Counting(gzip.GzipFile):
        """Record the highest unpacked position that the reader reaches."""

        def read(self, size: int = -1) -> bytes:
            """Read and record the position.

            Args:
                size: The number of bytes.

            Returns:
                The bytes read.
            """
            chunk = super().read(size)
            peaks.append(self.tell())
            return chunk

        def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
            """Seek and record the position.

            Args:
                offset: The new position, relative to ``whence``.
                whence: The ``io`` seek origin.

            Returns:
                The new position.
            """
            position = super().seek(offset, whence)
            peaks.append(position)
            return position

    monkeypatch.setattr(tree_helpers.gzip, "GzipFile", _Counting)

    with pytest.raises(BadArchiveError, match="unpacks to more than"):
        tar_to_tree(trees, RUN, data)
    assert max(peaks) <= 1024


def test_header_bytes_count_toward_the_unpacked_size(trees: TreeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-name header with no file data still counts toward the size limit."""
    monkeypatch.setattr(tree_helpers, "MAX_TREE_BYTES", 0)
    monkeypatch.setattr(tree_helpers, "MAX_TREE_MEMBERS", 1)
    data = _archive((_info("a" * 2000), b""), format=tarfile.GNU_FORMAT)

    with pytest.raises(BadArchiveError, match="unpacks to more than"):
        tar_to_tree(trees, RUN, data)


@pytest.mark.parametrize("kind", ["name", "symlink"])
def test_a_name_or_link_target_that_is_not_utf8_is_a_bad_archive(trees: TreeStore, kind: str) -> None:
    """A non-UTF-8 byte in a name or a symlink target is refused, not a box failure.

    Args:
        trees: The tree store.
        kind: Where the non-UTF-8 byte is.
    """
    info = _info("a\udcff.txt") if kind == "name" else _info("link", tarfile.SYMTYPE, "a\udcff")

    with pytest.raises(BadArchiveError, match="does not store"):
        tar_to_tree(trees, RUN, _archive((info, b"x"), format=tarfile.GNU_FORMAT))


@pytest.mark.parametrize("kind", [tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_a_file_after_a_folder_of_the_same_name_is_a_bad_archive(trees: TreeStore, kind: bytes) -> None:
    """A later file, symlink or hard link does not replace a folder and its files.

    Args:
        trees: The tree store.
        kind: The tar type of the later member.
    """
    data = _archive(
        (_info("b.txt"), b"b\n"),
        (_info("a", tarfile.DIRTYPE), b""),
        (_info("a/x"), b"x\n"),
        (_info("a", kind, "b.txt"), b""),
    )

    with pytest.raises(BadArchiveError, match="is a folder and a file"):
        tar_to_tree(trees, RUN, data)

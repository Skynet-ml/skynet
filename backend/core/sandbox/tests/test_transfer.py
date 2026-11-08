"""Tests for the tar file transfer and its path safety checks."""

from __future__ import annotations

import errno
import gzip
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from core.sandbox.transfer import UnsafeArchiveError, pack, unpack


def _archive(*members: tarfile.TarInfo) -> bytes:
    """Build a tar archive from bare members, with empty file bodies.

    Args:
        members: The members to add.

    Returns:
        The tar archive bytes.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for member in members:
            tar.addfile(member, io.BytesIO(b"") if member.isreg() else None)
    return buffer.getvalue()


def _member(name: str, kind: bytes = tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
    """Make one tar member of the given type.

    Args:
        name: The member path inside the archive.
        kind: The tar member type.
        linkname: The link target, for links.

    Returns:
        The member.
    """
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a small repo tree with an executable, a symlink and excluded paths."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "main.py").write_text("print('hi')\n")
    (root / "pkg" / "main.py").chmod(0o644)
    (root / "run.sh").write_text("#!/bin/sh\necho ok\n")
    (root / "run.sh").chmod(0o755)
    (root / "link.py").symlink_to("pkg/main.py")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\0")
    (root / "secrets.env").write_text("NAME=value\n")
    return root


def test_round_trip_keeps_contents_modes_and_inner_symlinks(repo: Path, tmp_path: Path) -> None:
    """Unpacking a packed tree gives back the same bytes, modes and in-tree links."""
    dest = tmp_path / "out"
    unpack(pack([repo]), dest)

    out = dest / "repo"
    assert (out / "pkg" / "main.py").read_text() == "print('hi')\n"
    assert (out / "run.sh").read_text() == "#!/bin/sh\necho ok\n"
    assert stat.S_IMODE((out / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((out / "pkg" / "main.py").stat().st_mode) == 0o644
    assert (out / "link.py").is_symlink()
    assert (out / "link.py").readlink() == Path("pkg/main.py")


def test_pack_leaves_out_default_and_caller_exclusions(repo: Path) -> None:
    """Default exclusions and caller patterns are not in the archive."""
    with tarfile.open(fileobj=io.BytesIO(pack([repo], exclude=["*.env"]))) as tar:
        names = set(tar.getnames())

    assert "repo/pkg/main.py" in names
    assert not any(".git" in name or "__pycache__" in name for name in names)
    assert "repo/secrets.env" not in names


def test_pack_keeps_a_given_path_whose_own_name_matches_an_exclusion(tmp_path: Path) -> None:
    """A path the caller names is packed; exclusions apply only below it."""
    venv = tmp_path / "venv"
    (venv / "__pycache__").mkdir(parents=True)
    (venv / "app.py").write_text("x = 1\n")
    (venv / "__pycache__" / "app.pyc").write_bytes(b"\0")

    with tarfile.open(fileobj=io.BytesIO(pack([venv]))) as tar:
        names = set(tar.getnames())

    assert names == {"venv", "venv/app.py"}


@pytest.mark.parametrize("relative", [".", "pkg/.."], ids=["dot", "trailing-dotdot"])
def test_pack_names_dot_paths_after_the_resolved_directory(
    relative: str, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.`` and ``sub/..`` pack under the resolved directory name, and the archive unpacks."""
    monkeypatch.chdir(repo)
    data = pack([relative])

    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = tar.getnames()
    unpack(data, tmp_path / "out")

    assert "repo/pkg/main.py" in names
    assert all(name == "repo" or name.startswith("repo/") for name in names)
    assert (tmp_path / "out" / "repo" / "pkg" / "main.py").is_file()


def test_pack_refuses_path_without_base_name() -> None:
    """The filesystem root has no name to put in the archive."""
    with pytest.raises(ValueError, match="no base name"):
        pack(["/"])


def test_unpack_refuses_compressed_archive(tmp_path: Path) -> None:
    """Only uncompressed tar is read, so a small archive cannot expand without limit."""
    with pytest.raises(tarfile.ReadError):
        unpack(gzip.compress(_archive(_member("ok.txt"))), tmp_path / "out")


def test_pack_takes_single_files(repo: Path, tmp_path: Path) -> None:
    """A file path goes into the archive under its base name."""
    dest = tmp_path / "out"
    unpack(pack([repo / "run.sh"]), dest)

    assert (dest / "run.sh").read_text() == "#!/bin/sh\necho ok\n"


@pytest.fixture(params=["data-filter", "manual-fallback"])
def extract_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test once with the ``data`` filter and once on the pre-3.11.4 manual fallback."""
    if request.param == "manual-fallback":
        monkeypatch.delattr(tarfile, "data_filter")
    return request.param


def _outside(tmp_path: Path, dest: Path) -> list[Path]:
    """List every path under ``tmp_path`` that is not ``dest`` or inside it, without following links.

    Args:
        tmp_path: The test's temporary root, which holds ``dest``.
        dest: The extraction destination.

    Returns:
        The paths outside ``dest``, other than its parents.
    """
    found = []
    for folder, dirs, files in os.walk(tmp_path):
        for name in dirs + files:
            path = Path(folder) / name
            if path != dest and dest not in path.parents and path not in dest.parents:
                found.append(path)
    return found


# Python 3.12 and 3.13 warn when ``extract`` runs without a filter; that is the fallback under test.
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(
    "member",
    [
        _member("../x"),
        _member("a/../../x"),
        _member("/etc/x"),
        _member("out", tarfile.SYMTYPE, "../../etc"),
        _member("abs", tarfile.SYMTYPE, "/etc/passwd"),
        _member("hard", tarfile.LNKTYPE, "../x"),
        _member("hard", tarfile.LNKTYPE, "missing"),
        _member("hard", tarfile.LNKTYPE, "hard"),
        _member("tty", tarfile.CHRTYPE),
        _member("disk", tarfile.BLKTYPE),
        _member("pipe", tarfile.FIFOTYPE),
    ],
    ids=[
        "dotdot",
        "nested-dotdot",
        "absolute",
        "symlink-out",
        "symlink-absolute",
        "hardlink-out",
        "hardlink-missing",
        "hardlink-self",
        "chr",
        "blk",
        "fifo",
    ],
)
def test_unpack_refuses_unsafe_member_before_writing(
    member: tarfile.TarInfo, tmp_path: Path, extract_path: str
) -> None:
    """An unsafe member fails the whole unpack, and no member is written anywhere."""
    dest = tmp_path / "box" / "out"

    with pytest.raises(UnsafeArchiveError):
        unpack(_archive(_member("ok.txt"), member), dest)

    assert list(dest.iterdir()) == []
    assert _outside(tmp_path, dest) == []


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(
    "escape",
    [
        _member("up", tarfile.SYMTYPE, "here/.."),
        _member("here/../x"),
        _member("here/../hard", tarfile.LNKTYPE, "ok.txt"),
        _member("hard", tarfile.LNKTYPE, "here/../x"),
    ],
    ids=["symlink-through-link", "file-through-link", "hardlink-placed-through-link", "hardlink-to-through-link"],
)
def test_unpack_refuses_escape_through_extracted_link(
    escape: tarfile.TarInfo, tmp_path: Path, extract_path: str
) -> None:
    """A member that escapes only through an earlier extracted symlink fails, and writes nothing outside."""
    dest = tmp_path / "box" / "out"
    archive = _archive(_member("ok.txt"), _member("here", tarfile.SYMTYPE, "."), escape)

    with pytest.raises(UnsafeArchiveError):
        unpack(archive, dest)

    assert _outside(tmp_path, dest) == []
    assert not (dest / escape.name).is_symlink()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_manual_fallback_round_trip(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the data filter, a safe archive still extracts in full."""
    monkeypatch.delattr(tarfile, "data_filter")
    dest = tmp_path / "out"

    unpack(pack([repo]), dest)

    assert (dest / "repo" / "pkg" / "main.py").read_text() == "print('hi')\n"
    assert stat.S_IMODE((dest / "repo" / "run.sh").stat().st_mode) == 0o755


def _path_max_chain() -> bytes:
    """Build a symlink chain whose resolved path passes ``PATH_MAX``, then write through it.

    Returns:
        The tar archive bytes.
    """
    comp = "d" * 247
    steps = "abcdefghijklmnop"
    path = ""
    members = []
    for step in steps:
        members.append(_member(str(Path(path, comp)), tarfile.DIRTYPE))
        members.append(_member(str(Path(path, step)), tarfile.SYMTYPE, comp))
        path = str(Path(path, comp))
    linkpath = str(Path(*steps, "l" * 254))
    members.append(_member(linkpath, tarfile.SYMTYPE, "../" * len(steps)))
    members.append(_member("escape", tarfile.SYMTYPE, linkpath + "/.."))
    members.append(_member("escape/PWNED.txt"))
    return _archive(*members)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_unpack_refuses_path_max_symlink_chain(tmp_path: Path, extract_path: str) -> None:
    """A symlink chain longer than ``PATH_MAX`` fails before any member is written."""
    dest = tmp_path / "box" / "out"

    with pytest.raises(UnsafeArchiveError):
        unpack(_path_max_chain(), dest)

    assert list(dest.iterdir()) == []
    assert _outside(tmp_path, dest) == []


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_unpack_writes_into_read_only_directory_member(tmp_path: Path, extract_path: str) -> None:
    """A directory member without write or search bits does not block the files inside it."""
    folder = _member("d", tarfile.DIRTYPE)
    folder.mode = 0o555
    dest = tmp_path / "out"

    unpack(_archive(folder, _member("d/f.txt")), dest)

    assert (dest / "d" / "f.txt").is_file()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(("mode", "expected"), [(0o6777, 0o755), (0o1666, 0o644), (0o000, 0o600)])
def test_unpack_masks_file_modes(mode: int, expected: int, tmp_path: Path, extract_path: str) -> None:
    """File modes lose setuid, setgid, sticky and group/other write bits, and stay owner-writable."""
    script = _member("s.sh")
    script.mode = mode
    dest = tmp_path / "out"

    unpack(_archive(script), dest)

    assert stat.S_IMODE((dest / "s.sh").stat().st_mode) == expected


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(
    "members",
    [
        [_member("d", tarfile.DIRTYPE), _member("hard", tarfile.LNKTYPE, "d")],
        [_member("hard", tarfile.LNKTYPE, "later.txt"), _member("later.txt")],
        [_member("sym", tarfile.SYMTYPE, "ok.txt"), _member("hard", tarfile.LNKTYPE, "sym")],
    ],
    ids=["to-directory", "to-later-file", "to-symlink"],
)
def test_unpack_refuses_hardlink_to_non_earlier_file(
    members: list[tarfile.TarInfo], tmp_path: Path, extract_path: str
) -> None:
    """A hardlink to anything but an earlier regular file fails before any member is written."""
    dest = tmp_path / "out"

    with pytest.raises(UnsafeArchiveError, match="hardlink target"):
        unpack(_archive(_member("ok.txt"), *members), dest)

    assert list(dest.iterdir()) == []


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_unpack_extracts_hardlink_to_earlier_file(tmp_path: Path, extract_path: str) -> None:
    """A hardlink to an earlier regular file, written with a ``./`` prefix, extracts."""
    body = b"shared\n"
    first = _member("ok.txt")
    first.size = len(body)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.addfile(first, io.BytesIO(body))
        tar.addfile(_member("hard", tarfile.LNKTYPE, "./ok.txt"))
    dest = tmp_path / "out"

    unpack(buffer.getvalue(), dest)

    assert (dest / "hard").read_bytes() == body


def _failing_open(code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every file that ``tarfile`` opens for writing fail with ``code``.

    Args:
        code: The ``errno`` value for the raised ``OSError``.
        monkeypatch: The fixture that restores ``tarfile`` afterwards.
    """
    real_open = tarfile.bltn_open

    def fail_on_write(path: str, mode: str = "r", *args: object, **kwargs: object) -> object:
        """Raise ``OSError(code)`` for write modes and open other files normally.

        Args:
            path: The file to open.
            mode: The open mode.
            args: Other positional arguments for ``open``.
            kwargs: Keyword arguments for ``open``.

        Returns:
            The open file, for read modes.

        Raises:
            OSError: ``mode`` writes.
        """
        if "w" in mode:
            raise OSError(code, os.strerror(code))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(tarfile, "bltn_open", fail_on_write)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_unpack_passes_through_disk_errors(tmp_path: Path, extract_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A full disk is reported as the ``OSError`` it is, not as an unsafe archive."""
    _failing_open(errno.ENOSPC, monkeypatch)

    with pytest.raises(OSError) as raised:
        unpack(_archive(_member("ok.txt")), tmp_path / "out")

    assert raised.type is OSError
    assert raised.value.errno == errno.ENOSPC


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize("code", [errno.ENAMETOOLONG, errno.ELOOP], ids=["ENAMETOOLONG", "ELOOP"])
def test_unpack_maps_link_chain_errors_to_unsafe(
    code: int, tmp_path: Path, extract_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The errors that a too-long link chain causes become ``UnsafeArchiveError`` on both paths."""
    _failing_open(code, monkeypatch)

    with pytest.raises(UnsafeArchiveError):
        unpack(_archive(_member("ok.txt")), tmp_path / "out")

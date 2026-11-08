"""Tests for the tar file transfer and its path safety checks."""

from __future__ import annotations

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

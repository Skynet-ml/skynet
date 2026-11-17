"""Tests for the provider-neutral tree-id sandbox helpers (SKY-718)."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.sandbox.grants import Grant
from core.sandbox.trees import (
    BoxFailureError,
    BoxSource,
    FenceLostError,
    GrantDeniedError,
    TreeSandboxError,
    tar_to_tree,
    tree_to_tar,
)
from core.storage.models import Base, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore

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


class _ListSource:
    """Give boxes from a list and record each release."""

    def __init__(self) -> None:
        """Start with no releases."""
        self.released: list[tuple[str, bool]] = []

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> str:
        """Return a box name for the agent.

        Args:
            agent_id: The agent that runs the step.
            grants: The step's grants.

        Returns:
            The box name.
        """
        return f"{agent_id}:{len(grants)}"

    def release(self, box: str, healthy: bool) -> None:
        """Record the release.

        Args:
            box: The box name.
            healthy: Whether the box is still good.
        """
        self.released.append((box, healthy))


def test_a_small_class_with_acquire_and_release_is_a_box_source() -> None:
    """The protocol needs only ``acquire(agent_id, grants)`` and ``release(box, healthy)``."""
    source: BoxSource[str] = _ListSource()

    box = source.acquire("agent-1", [Grant("api.example.com")])
    source.release(box, healthy=False)

    assert source.released == [("agent-1:1", False)]  # type: ignore[attr-defined]

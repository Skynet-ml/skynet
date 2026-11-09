"""Tests for the after-setup snapshots of the scorer box and of the stage boxes (SKY-531)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.billing.runtime import UsagePendingError
from core.exceptions import ServiceError

from ..repo_scorer import _STAGE_LAYOUT_SECONDS
from ..repo_workspace import PREPARED_DIR, WORK_DIR, RepoSetupError, RepoWorkspace
from ..sandbox import LocalSubprocessRuntime, SandboxSpec
from ..sandbox_contract import SandboxSession
from .test_repo_scorer import STAGES, _stage_scorer
from .test_repo_workspace import _ONLINE, _archive, _Box, _patch

_SETUP = 'echo "key=${API_KEY:-unset}"; cat src/app.py > built.txt'
_TWO_STAGES = (
    "from skynet import call\n"
    "def score(repo_path, case=None):\n"
    "    doubled = call('double', {'v': 3})\n"
    "    return {'score': doubled['v'] / 10, 'feedback': call('shout', 'hi')}\n"
)


class _SnapshotRuntime:
    """Open local boxes and save a box's folder as a snapshot, the way a provider would."""

    injects_headers = False

    def __init__(
        self,
        root: Path,
        *,
        snapshot_error: BaseException | None = None,
        open_error: BaseException | None = None,
        saves: bool = True,
        tamper: Callable[[Path], None] | None = None,
    ) -> None:
        """Choose how the snapshot and the restored boxes behave.

        Args:
            root: Where the saved snapshots go.
            snapshot_error: What ``snapshot`` raises, if anything.
            open_error: What an open from a snapshot raises, if anything.
            saves: ``False`` makes ``snapshot`` return ``None``, as with no cache.
            tamper: Changes a restored box's folder before the workspace sees it.
        """
        self._local = LocalSubprocessRuntime()
        self._root = root
        self._snapshot_error = snapshot_error
        self._open_error = open_error
        self._saves = saves
        self._tamper = tamper
        self.specs: list[SandboxSpec] = []
        self.keys: list[str] = []
        self.saved: dict[str, Path] = {}
        self.boxes: list[_Box] = []

    def open(self, spec: SandboxSpec) -> _Box:
        """Open a box, filled from the named snapshot when the spec has one.

        Args:
            spec: The requested box.

        Returns:
            The box.

        Raises:
            BaseException: The configured open error, for a box from a snapshot.
        """
        self.specs.append(spec)
        if spec.snapshot_id is not None and self._open_error is not None:
            raise self._open_error
        inner = self._local.open(spec)
        if spec.snapshot_id is not None:
            shutil.copytree(self.saved[spec.snapshot_id], inner.path, dirs_exist_ok=True)
            if self._tamper is not None:
                self._tamper(inner.path)
        box = _Box(inner, online=not spec.network_disabled, cutoff="ok")
        self.boxes.append(box)
        return box

    def snapshot(self, session: _Box, cache_key: str) -> str | None:
        """Copy the box's folder as a snapshot.

        Args:
            session: A box from ``open``.
            cache_key: Names the box's setup.

        Returns:
            The snapshot id, or ``None`` when the runtime does not save.

        Raises:
            BaseException: The configured snapshot error.
        """
        self.keys.append(cache_key)
        if self._snapshot_error is not None:
            raise self._snapshot_error
        if not self._saves:
            return None
        snapshot_id = f"snap-{len(self.saved)}"
        shutil.copytree(session._inner.path, self._root / snapshot_id, symlinks=True)
        self.saved[snapshot_id] = self._root / snapshot_id
        return snapshot_id


class _StageSnapshotRuntime:
    """Open local stage boxes, record what each box gets, and save a box as a snapshot."""

    injects_headers = False

    def __init__(self, root: Path, *, snapshot_error: BaseException | None = None) -> None:
        """Start with no boxes.

        Args:
            root: Where the saved snapshots go.
            snapshot_error: What ``snapshot`` raises, if anything.
        """
        self._local = LocalSubprocessRuntime()
        self._root = root
        self._snapshot_error = snapshot_error
        self.specs: list[SandboxSpec] = []
        self.written: list[list[str]] = []
        self.keys: list[str] = []
        self.saved: dict[str, Path] = {}

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open a local box and record the paths written into it.

        Args:
            spec: The requested stage box.

        Returns:
            The box, filled from the named snapshot when the spec has one.
        """
        self.specs.append(spec)
        session = self._local.open(spec)
        if spec.snapshot_id is not None:
            shutil.copytree(self.saved[spec.snapshot_id], session.path, dirs_exist_ok=True)
        written: list[str] = []
        self.written.append(written)
        write = session.write_files

        def recording_write(files: dict[str, str]) -> None:
            """Record the paths, then write them.

            Args:
                files: Relative path to text.
            """
            written.extend(files)
            write(files)

        session.write_files = recording_write  # type: ignore[method-assign]
        return session

    def snapshot(self, session: Any, cache_key: str) -> str:
        """Copy the box's folder as a snapshot.

        Args:
            session: A box from ``open``.
            cache_key: Names the box's setup.

        Returns:
            The snapshot id.

        Raises:
            BaseException: The configured snapshot error.
        """
        self.keys.append(cache_key)
        if self._snapshot_error is not None:
            raise self._snapshot_error
        snapshot_id = f"stage-{len(self.saved)}"
        shutil.copytree(session.path, self._root / snapshot_id, symlinks=True)
        self.saved[snapshot_id] = self._root / snapshot_id
        return snapshot_id


def _space(archive: Path, runtime: Any) -> RepoWorkspace:
    """Build an online workspace with one secret and a setup command.

    Args:
        archive: Packed tree.
        runtime: Where its boxes open.

    Returns:
        The workspace.
    """
    return RepoWorkspace(
        runtime=runtime,
        spec=_ONLINE,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=_SETUP,
        secrets={"API_KEY": "very-secret-value"},
    )


def _setups(box: _Box) -> int:
    """Count the setup commands that ran in a box.

    Args:
        box: A recording box.

    Returns:
        How many commands ran the setup.
    """
    return sum("built.txt" in command for command, _, _ in box.commands)


def _texts(folder: Path) -> str:
    """Join the text of every file under a folder.

    Args:
        folder: The folder.

    Returns:
        All the file text, joined.
    """
    return "\n".join(path.read_text(errors="replace") for path in folder.rglob("*") if path.is_file())


def test_prepared_box_is_saved_and_the_kept_box_opens_from_it_offline(tmp_path: Path) -> None:
    """Save the prepared box once, then open the kept box deny-all from that snapshot with no second setup.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps")
    space = _space(archive, runtime)
    try:
        space.session()
        space.checkout(_patch(tmp_path / "v1", archive, {"src/app.py": "x = 2\n"}))
        assert space.session().read_file(f"{WORK_DIR}/src/app.py") == "x = 2\n"
    finally:
        space.close()

    assert [spec.snapshot_id for spec in runtime.specs] == [None, "snap-0"]
    restored = runtime.specs[1]
    assert restored.network_disabled is True
    assert restored.allowed_hosts == ()
    assert runtime.boxes[0].closed
    assert _setups(runtime.boxes[0]) == 1
    assert _setups(runtime.boxes[1]) == 0
    assert "very-secret-value" not in _texts(runtime.saved["snap-0"])


def test_snapshot_without_the_prepared_tree_fails_setup(tmp_path: Path) -> None:
    """Refuse a box from a snapshot whose prepared tree changed, and keep refusing later calls.

    Args:
        tmp_path: Scratch folder.
    """

    def tamper(folder: Path) -> None:
        """Change the prepared tree in the restored box.

        Args:
            folder: The restored box's folder.
        """
        (folder / PREPARED_DIR / "src/app.py").write_text("x = 99\n")

    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps", tamper=tamper)
    space = _space(archive, runtime)
    try:
        with pytest.raises(RepoSetupError, match="setup snapshot does not hold"):
            space.session()
        with pytest.raises(RepoSetupError):
            space.session()
    finally:
        space.close()
    assert all(box.closed for box in runtime.boxes)
    assert len(runtime.specs) == 2


@pytest.mark.parametrize("fault", ["snapshot", "open"])
def test_snapshot_fault_falls_back_to_a_normal_box(tmp_path: Path, fault: str) -> None:
    """Set up a normal box when the snapshot cannot be saved or a box cannot open from it.

    Args:
        tmp_path: Scratch folder.
        fault: Which step the provider refuses.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    error = ServiceError("provider refused")
    runtime = _SnapshotRuntime(
        tmp_path / "snaps",
        snapshot_error=error if fault == "snapshot" else None,
        open_error=error if fault == "open" else None,
    )
    space = _space(archive, runtime)
    try:
        space.checkout("")
        assert space.session().read_file(f"{WORK_DIR}/built.txt") == "x = 1\n"
    finally:
        space.close()
    expected = [None, None] if fault == "snapshot" else [None, "snap-0", None]
    assert [spec.snapshot_id for spec in runtime.specs] == expected
    assert _setups(runtime.boxes[-1]) == 1
    assert not runtime.boxes[-1].online
    assert runtime.boxes[0].closed


def test_budget_refusal_during_the_snapshot_stops_setup(tmp_path: Path) -> None:
    """Raise a budget refusal from the snapshot instead of falling back to a normal box.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps", snapshot_error=UsagePendingError("usage pending"))
    space = _space(archive, runtime)
    try:
        with pytest.raises(UsagePendingError):
            space.session()
    finally:
        space.close()
    assert len(runtime.specs) == 1
    assert runtime.boxes[0].closed


def test_no_snapshot_keeps_the_prepared_box(tmp_path: Path) -> None:
    """Keep the prepared box and cut its network when the run has no snapshot cache.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps", saves=False)
    space = _space(archive, runtime)
    try:
        space.session()
    finally:
        space.close()
    assert [spec.snapshot_id for spec in runtime.specs] == [None]
    assert not runtime.boxes[0].online


def test_each_workspace_gets_its_own_snapshot_key(tmp_path: Path) -> None:
    """Give two workspaces of one tree different keys, so no box reuses another's snapshot.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps")
    for _ in range(2):
        space = _space(archive, runtime)
        try:
            space.session()
        finally:
            space.close()
    assert len(set(runtime.keys)) == 2


def test_stage_boxes_start_from_their_own_snapshot_without_scorer_files(tmp_path: Path) -> None:
    """Build the stage snapshot from the stage tree alone, and start every stage box from it.

    The scorer's tree holds ``eval/metric.py`` and the scorer-only
    ``judge/labels.csv``; neither may reach the snapshot that candidate code
    starts from.

    Args:
        tmp_path: Scratch folder.
    """
    runtime = _StageSnapshotRuntime(tmp_path / "stage-snaps")
    scorer_runtime = _SnapshotRuntime(tmp_path / "scorer-snaps")
    stages = {name: STAGES[name] for name in ("double", "shout")}
    scorer, archive = _stage_scorer(tmp_path, _TWO_STAGES, runtime, stages, scorer_runtime=scorer_runtime)
    try:
        assert scorer.score("")["score"] == pytest.approx(0.6)
        version = _patch(tmp_path / "v1", archive, {"src/stages.py": "def double(x):\n    return {'v': x['v'] * 3}\n"})
        assert scorer.score(version)["score"] == pytest.approx(0.9)
    finally:
        scorer.close()

    builder, *boxes = runtime.specs
    assert builder.snapshot_id is None
    assert builder.lifetime_seconds == _STAGE_LAYOUT_SECONDS
    assert len(boxes) == 4
    assert {spec.snapshot_id for spec in boxes} == {"stage-0"}
    assert any(name.endswith(".b64") for name in runtime.written[0])
    assert all(names[0] == ".app-stage/version.patch" and len(names) == 2 for names in runtime.written[1:])
    assert all(names[1].endswith("/input.json") for names in runtime.written[1:])

    saved = runtime.saved["stage-0"]
    names = {path.name for path in saved.rglob("*")}
    assert "stages.py" in names
    assert "metric.py" not in names
    assert "labels.csv" not in names
    assert "hidden\n" not in _texts(saved)
    assert runtime.keys[0] not in scorer_runtime.keys
    assert all(spec.snapshot_id != "stage-0" for spec in scorer_runtime.specs)


def test_stage_snapshot_fault_falls_back_to_unpacked_boxes(tmp_path: Path) -> None:
    """Unpack the tree in each stage box when the stage snapshot fails, and try it only once.

    Args:
        tmp_path: Scratch folder.
    """
    runtime = _StageSnapshotRuntime(tmp_path / "stage-snaps", snapshot_error=ServiceError("refused"))
    scorer, _ = _stage_scorer(tmp_path, _TWO_STAGES, runtime, {name: STAGES[name] for name in ("double", "shout")})
    try:
        assert scorer.score("")["score"] == pytest.approx(0.6)
        assert scorer.score("")["score"] == pytest.approx(0.6)
    finally:
        scorer.close()
    assert len(runtime.keys) == 1
    assert all(spec.snapshot_id is None for spec in runtime.specs)
    assert all(any(name.endswith(".b64") for name in names) for names in runtime.written[1:])


def test_stage_runtime_without_snapshots_opens_no_builder(tmp_path: Path) -> None:
    """Open only the stage boxes, each unpacked, when the stage runtime cannot save boxes.

    Args:
        tmp_path: Scratch folder.
    """
    runtime = _StageSnapshotRuntime(tmp_path / "stage-snaps")
    runtime.snapshot = None  # type: ignore[assignment,method-assign]
    scorer, _ = _stage_scorer(tmp_path, _TWO_STAGES, runtime, {name: STAGES[name] for name in ("double", "shout")})
    try:
        assert scorer.score("")["score"] == pytest.approx(0.6)
    finally:
        scorer.close()
    assert len(runtime.specs) == 2
    assert all(spec.snapshot_id is None for spec in runtime.specs)

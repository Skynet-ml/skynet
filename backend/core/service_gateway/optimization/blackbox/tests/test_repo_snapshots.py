"""Tests for the saved setup states of the repository boxes and for the stage snapshots (SKY-531, SKY-398)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.operation_pricing import json_fingerprint
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.billing.vercel_usage import SANDBOX_NETWORK_BYTES_CAP, VercelUsageReservation
from core.exceptions import ServiceError
from core.sandbox.providers.vercel import box_name
from core.sandbox.services import ComposeService
from core.storage.models import Base, BillingCustomerModel

from ..repo_scorer import _STAGE_LAYOUT_SECONDS, RepoScoreError
from ..repo_workspace import PREPARED_DIR, SERVICES_DIR, WORK_DIR, CandidateBox, RepoWorkspace
from ..sandbox import LocalSubprocessRuntime, SandboxSpec
from ..sandbox_broker import SandboxBroker
from ..sandbox_contract import SandboxSession
from .test_repo_scorer import STAGES, _stage_scorer
from .test_repo_workspace import _ONLINE, _archive, _Box, _patch
from .test_sandbox_broker import IMAGE, FakeRuntime

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
        self.released: list[str] = []
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
            shutil.copytree(self.saved[spec.snapshot_id], inner.path, dirs_exist_ok=True, symlinks=True)
            if self._tamper is not None:
                self._tamper(inner.path)
        box = _Box(inner, spec)
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
        shutil.copytree(session.path, self._root / snapshot_id, symlinks=True)
        self.saved[snapshot_id] = self._root / snapshot_id
        return snapshot_id

    def release_snapshot(self, cache_key: str) -> None:
        """Record that the caller is done with the snapshot for ``cache_key``.

        Args:
            cache_key: Names the box's setup.
        """
        self.released.append(cache_key)


class _StageSnapshotRuntime:
    """Open local stage boxes, record what each box gets, and save a box as a snapshot."""

    injects_headers = False
    supports_snapshots = True

    def __init__(
        self,
        root: Path,
        *,
        snapshot_error: BaseException | None = None,
        open_error: BaseException | None = None,
        empty_restore: bool = False,
        restore_edit: Callable[[Path], None] | None = None,
    ) -> None:
        """Start with no boxes.

        Args:
            root: Where the saved snapshots go.
            snapshot_error: What ``snapshot`` raises, if anything.
            open_error: What an open from a snapshot raises, if anything.
            empty_restore: Open a box from a snapshot without the saved tree.
            restore_edit: Changes a restored box's folder after the copy.
        """
        self._local = LocalSubprocessRuntime()
        self._root = root
        self._snapshot_error = snapshot_error
        self._open_error = open_error
        self._empty_restore = empty_restore
        self._restore_edit = restore_edit
        self.specs: list[SandboxSpec] = []
        self.written: list[list[str]] = []
        self.keys: list[str] = []
        self.saved: dict[str, Path] = {}
        self.released: list[str] = []

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open a local box and record the paths written into it.

        Args:
            spec: The requested stage box.

        Returns:
            The box, filled from the named snapshot when the spec has one.

        Raises:
            BaseException: The configured open error, for a box from a snapshot.
        """
        self.specs.append(spec)
        if spec.snapshot_id is not None and self._open_error is not None:
            raise self._open_error
        session = self._local.open(spec)
        if spec.snapshot_id is not None and not self._empty_restore:
            shutil.copytree(self.saved[spec.snapshot_id], session.path, dirs_exist_ok=True)
            if self._restore_edit is not None:
                self._restore_edit(session.path)
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

    def release_snapshot(self, cache_key: str) -> None:
        """Record that the caller is done with the snapshot for ``cache_key``.

        Args:
            cache_key: Names the box's setup.
        """
        self.released.append(cache_key)


class _BilledSnapshotRuntime(_SnapshotRuntime):
    """Reserve each box in a real budget ledger before it opens, the way the Vercel provider does."""

    def __init__(self, root: Path, budget: BudgetRuntime, **options: Any) -> None:
        """Bill every box against one budget.

        Args:
            root: Where the saved snapshots go.
            budget: The budget runtime the boxes reserve from.
            **options: The ``_SnapshotRuntime`` options.
        """
        super().__init__(root, **options)
        self._budget = budget
        self.names: list[str] = []

    def open(self, spec: SandboxSpec) -> _Box:
        """Reserve the box under its operation key, then open it.

        Args:
            spec: The requested box.

        Returns:
            The box.
        """
        assert spec.operation_key is not None
        name = box_name(spec.operation_key, self._budget)
        hosts = sorted(set(spec.allowed_hosts))
        request = {
            "name": name,
            "image": IMAGE,
            "lifetime_ms": int(spec.lifetime_seconds * 1000),
            "vcpus": 2,
            "network_disabled": spec.network_disabled,
            **({"allowed_hosts": hosts, "network_bytes_cap": SANDBOX_NETWORK_BYTES_CAP} if hosts else {}),
            "ports": [],
            "persistent": False,
            "environment_fingerprint": json_fingerprint(dict(spec.env)),
            **({"snapshot_id": spec.snapshot_id} if spec.snapshot_id else {}),
        }
        VercelUsageReservation(self._budget, request, operation_key=spec.operation_key)
        self.names.append(name)
        return super().open(spec)


class _BilledPlainRuntime(_BilledSnapshotRuntime):
    """Reserve each box in a real budget ledger, on a runtime that cannot save boxes."""

    snapshot = None
    release_snapshot = None


def _budget(tmp_path: Path) -> BudgetRuntime:
    """Build a real budget runtime over a SQLite ledger.

    Args:
        tmp_path: Where the ledger file goes.

    Returns:
        A setup-phase runtime with a funded budget.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="cus", balance_cents=1000, grant_remaining=0)
        )
        session.commit()
    service = BudgetService(engine=engine)
    budget = service.create("alice", 500, idempotency_key="run")
    return BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)


def _space(archive: Path, runtime: Any, **options: Any) -> RepoWorkspace:
    """Build an online workspace with one secret and a setup command.

    Args:
        archive: Packed tree.
        runtime: Where its boxes open.
        **options: More :class:`RepoWorkspace` arguments.

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
        **options,
    )


def _candidate(space: RepoWorkspace, patch: str = "") -> CandidateBox:
    """Open a candidate box that must lay the version out.

    Args:
        space: The workspace.
        patch: The version.

    Returns:
        The open box.
    """
    opened = space.open_candidate(patch)
    assert isinstance(opened, CandidateBox), opened
    return opened


def _setups(box: _Box) -> int:
    """Count the setup commands that ran in a box.

    Args:
        box: A recording box.

    Returns:
        How many commands ran the setup.
    """
    return sum("built.txt" in command for command, _ in box.commands)


def _texts(folder: Path) -> str:
    """Join the text of every file under a folder.

    Args:
        folder: The folder.

    Returns:
        All the file text, joined.
    """
    return "\n".join(path.read_text(errors="replace") for path in folder.rglob("*") if path.is_file())


def _tamper(folder: Path) -> None:
    """Change the prepared tree in a box restored from the repository snapshot.

    Args:
        folder: The restored box's folder.
    """
    prepared = folder / PREPARED_DIR / "src/app.py"
    if prepared.is_file():
        prepared.write_text("x = 99\n")


def test_the_setup_state_is_saved_once_and_each_candidate_box_opens_from_it_offline(tmp_path: Path) -> None:
    """Save the setup box once, then open every candidate box deny-all from that snapshot with no second setup.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps")
    space = _space(archive, runtime)
    try:
        first = _candidate(space, _patch(tmp_path / "v1", archive, {"src/app.py": "x = 2\n"}))
        assert first.session.read_file(f"{WORK_DIR}/src/app.py") == "x = 2\n"
        first.close()
        second = _candidate(space)
        assert second.session.read_file(f"{WORK_DIR}/src/app.py") == "x = 1\n"
        second.close()
    finally:
        space.close()

    assert [spec.snapshot_id for spec in runtime.specs] == [None, "snap-0", "snap-0"]
    assert all(spec.network_disabled and spec.allowed_hosts == () for spec in runtime.specs[1:])
    assert all(box.closed for box in runtime.boxes)
    assert [_setups(box) for box in runtime.boxes] == [1, 0, 0]
    assert "very-secret-value" not in _texts(runtime.saved["snap-0"])


def test_a_candidate_box_without_the_prepared_tree_sets_up_again_with_tar_states(tmp_path: Path) -> None:
    """Close a box from a snapshot whose prepared tree changed, and set up again with a tar state in the same call.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps", tamper=_tamper)
    space = _space(archive, runtime)
    try:
        box = _candidate(space)
        assert box.session.read_file(f"{WORK_DIR}/src/app.py") == "x = 1\n"
        box.close()
        _candidate(space).close()
    finally:
        space.close()
    assert [spec.snapshot_id for spec in runtime.specs] == [None, "snap-0", None, None, None]
    assert [bool(spec.snapshot_archive) for spec in runtime.specs] == [False, False, False, True, True]
    assert all(box.closed for box in runtime.boxes)
    assert [_setups(box) for box in runtime.boxes] == [1, 0, 1, 0, 0]
    assert len(runtime.saved) == 1
    assert space._snapshots_off


@pytest.mark.parametrize("fault", ["snapshot", "open"])
def test_a_snapshot_fault_keeps_the_state_as_a_tar(tmp_path: Path, fault: str) -> None:
    """Keep a tar state when the snapshot cannot be saved or a box cannot open from it.

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
        box = _candidate(space)
        assert box.session.read_file(f"{WORK_DIR}/built.txt") == "x = 1\n"
        box.close()
    finally:
        space.close()
    expected = [None, None] if fault == "snapshot" else [None, "snap-0", None, None]
    assert [spec.snapshot_id for spec in runtime.specs] == expected
    assert runtime.specs[-1].snapshot_archive
    assert not runtime.boxes[-1].online
    assert all(box.closed for box in runtime.boxes)


@pytest.mark.parametrize("path", ["restored", "bad-tree", "snapshot-fault", "open-fault", "no-snapshots"])
def test_each_box_reserves_under_its_own_operation_key(tmp_path: Path, path: str) -> None:
    """Reserve the setup, metric setup, candidate and metric boxes in the real ledger, each on a new key (E31).

    Args:
        tmp_path: Scratch folder.
        path: Which boxes open: boxes from the snapshots, a new setup after a
            restored box with a bad tree or after a box that did not open from
            the snapshot, or tar states after a failed snapshot or on a runtime
            with no snapshots.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", ".optimize/metric-requirements.txt": "numpy\n"})
    runtime_class = _BilledPlainRuntime if path == "no-snapshots" else _BilledSnapshotRuntime
    runtime = runtime_class(
        tmp_path / "snaps",
        _budget(tmp_path),
        tamper=_tamper if path == "bad-tree" else None,
        snapshot_error=ServiceError("refused") if path == "snapshot-fault" else None,
        open_error=ServiceError("refused") if path == "open-fault" else None,
    )
    space = RepoWorkspace(
        runtime=runtime,
        spec=replace(_ONLINE, operation_key="repo-scorer:job"),
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=_SETUP,
        secrets={"API_KEY": "very-secret-value"},
    )
    try:
        box = _candidate(space)
        assert box.session.read_file(f"{WORK_DIR}/built.txt") == "x = 1\n"
        box.close()
        space.open_metric().close()
    finally:
        space.close()
    first = ["setup-0", "metric-setup-1", "candidate-2"]
    again = ["setup-3", "metric-setup-4", "candidate-5", "metric-6"]
    expected = {
        "restored": [*first, "metric-3"],
        "bad-tree": [*first, *again],
        "snapshot-fault": [*first, "metric-3"],
        "open-fault": [*first, *again],
        "no-snapshots": [*first, "metric-3"],
    }[path]
    assert [spec.operation_key for spec in runtime.specs] == [f"repo-scorer:job:{suffix}" for suffix in expected]
    assert len(set(runtime.names)) == len(expected)
    if path == "restored":
        assert [spec.snapshot_id for spec in runtime.specs] == [None, None, "snap-0", "snap-1"]


def test_a_metric_box_without_the_metric_packages_sets_up_again_with_tar_states(tmp_path: Path) -> None:
    """Close a metric box from a snapshot without the installed packages, and set up again in the same call.

    Args:
        tmp_path: Scratch folder.
    """

    def drop_packages(folder: Path) -> None:
        """Remove the mark the metric setup step leaves.

        Args:
            folder: The restored box's folder.
        """
        (folder / ".app-metric/ready").unlink(missing_ok=True)

    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", ".optimize/metric-requirements.txt": "numpy\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps", tamper=drop_packages)
    space = _space(archive, runtime)
    try:
        space.open_metric().close()
    finally:
        space.close()
    assert [spec.operation_key for spec in runtime.specs] == [
        "op:setup-0",
        "op:metric-setup-1",
        "op:metric-2",
        "op:setup-3",
        "op:metric-setup-4",
        "op:metric-5",
    ]
    assert runtime.specs[2].snapshot_id == "snap-1"
    assert runtime.specs[-1].snapshot_archive
    assert all(box.closed for box in runtime.boxes)


def test_budget_refusal_during_the_snapshot_stops_setup(tmp_path: Path) -> None:
    """Raise a budget refusal from the snapshot instead of falling back to a tar state.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps", snapshot_error=UsagePendingError("usage pending"))
    space = _space(archive, runtime)
    try:
        with pytest.raises(UsagePendingError):
            space.prepare()
    finally:
        space.close()
    assert len(runtime.specs) == 1
    assert runtime.boxes[0].closed


def test_budget_refusal_from_a_box_from_the_snapshot_is_raised(tmp_path: Path) -> None:
    """Raise a budget refusal from a candidate box that opens from the snapshot instead of setting up again.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps", open_error=UsagePendingError("usage pending"))
    space = _space(archive, runtime)
    try:
        with pytest.raises(UsagePendingError):
            space.open_candidate("")
    finally:
        space.close()
    assert [spec.snapshot_id for spec in runtime.specs] == [None, "snap-0"]
    assert not space._snapshots_off


def test_a_runtime_without_a_cache_keeps_tar_states(tmp_path: Path) -> None:
    """Keep a tar state when the runtime saves no snapshot.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps", saves=False)
    space = _space(archive, runtime)
    try:
        _candidate(space).close()
    finally:
        space.close()
    assert [spec.snapshot_id for spec in runtime.specs] == [None, None]
    assert runtime.specs[-1].snapshot_archive


def test_each_workspace_and_each_state_gets_its_own_snapshot_key(tmp_path: Path) -> None:
    """Give two workspaces of one tree, and the repository and metric states, different keys.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", ".optimize/metric-requirements.txt": "numpy\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps")
    for _ in range(2):
        space = _space(archive, runtime)
        try:
            space.prepare()
        finally:
            space.close()
    assert len(runtime.keys) == 4
    assert len(set(runtime.keys)) == 4


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


@pytest.mark.parametrize("fault", ["open", "tree"])
def test_stage_boxes_from_a_bad_snapshot_fall_back_to_unpacked_boxes(tmp_path: Path, fault: str) -> None:
    """Drop the stage snapshot and score with unpacked boxes when boxes from it fail, for this and later versions.

    Args:
        tmp_path: Scratch folder.
        fault: ``open`` when a box cannot open from the snapshot, ``tree`` when
            the restored box has no tree.
    """
    runtime = _StageSnapshotRuntime(
        tmp_path / "stage-snaps",
        open_error=ServiceError("refused") if fault == "open" else None,
        empty_restore=fault == "tree",
    )
    scorer, _ = _stage_scorer(tmp_path, _TWO_STAGES, runtime, {name: STAGES[name] for name in ("double", "shout")})
    try:
        assert scorer.score("")["score"] == pytest.approx(0.6)
        assert scorer.score("")["score"] == pytest.approx(0.6)
    finally:
        scorer.close()
    assert len(runtime.keys) == 1
    snapshot_ids = [spec.snapshot_id for spec in runtime.specs[1:]]
    tried = 1 if fault == "open" else 2
    assert snapshot_ids[:tried] == ["stage-0"] * tried
    assert snapshot_ids[tried:] == [None] * 4


def test_a_version_that_does_not_apply_in_restored_stage_boxes_keeps_the_stage_snapshot(tmp_path: Path) -> None:
    """Fail only that version when its patch does not apply in stage boxes from the snapshot, and keep the snapshot.

    Args:
        tmp_path: Scratch folder.
    """

    def edit(folder: Path) -> None:
        """Change the file the version patches, so the patch does not apply.

        Args:
            folder: The restored stage box's folder.
        """
        (folder / ".app-stage/tree/src/app.py").write_text("x = 5\n")

    runtime = _StageSnapshotRuntime(tmp_path / "stage-snaps", restore_edit=edit)
    stages = {name: STAGES[name] for name in ("double", "shout")}
    scorer, archive = _stage_scorer(tmp_path, _TWO_STAGES, runtime, stages)
    try:
        with pytest.raises(RepoScoreError, match="could not lay out"):
            scorer.score(_patch(tmp_path / "v1", archive, {"src/app.py": "x = 2\n"}))
        assert scorer._stage_snapshot_id == "stage-0"
        assert scorer.score("")["score"] == pytest.approx(0.6)
    finally:
        scorer.close()
    assert [spec.snapshot_id for spec in runtime.specs[1:]] == ["stage-0"] * 4


def test_budget_refusal_from_a_snapshot_stage_box_is_raised(tmp_path: Path) -> None:
    """Raise a budget refusal from a stage box opened from the snapshot instead of retrying unpacked.

    Args:
        tmp_path: Scratch folder.
    """
    runtime = _StageSnapshotRuntime(tmp_path / "stage-snaps", open_error=UsagePendingError("usage pending"))
    scorer, _ = _stage_scorer(tmp_path, _TWO_STAGES, runtime, {name: STAGES[name] for name in ("double", "shout")})
    try:
        with pytest.raises(UsagePendingError):
            scorer.score("")
    finally:
        scorer.close()
    assert [spec.snapshot_id for spec in runtime.specs] == [None, "stage-0"]


def test_a_broker_parent_runtime_without_a_cache_opens_no_stage_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip the stage builder box when the real broker's parent runtime has no snapshot cache.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Records each box the scorer asks the parent runtime for.
    """
    runtime: Any = FakeRuntime()
    parent = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120).parent_runtime(())
    assert not parent.supports_snapshots
    asked: list[SandboxSpec] = []
    real_open = parent.open
    monkeypatch.setattr(parent, "open", lambda spec: asked.append(spec) or real_open(spec))
    scorer, _ = _stage_scorer(tmp_path, _TWO_STAGES, parent, {name: STAGES[name] for name in ("double", "shout")})
    try:
        assert scorer._stage_snapshot() is None
    finally:
        scorer.close()
    assert asked == []
    assert runtime.specs == []


def test_workspace_close_keeps_the_snapshots_and_release_drops_them(tmp_path: Path) -> None:
    """Keep the snapshots when the workspace closes, and release only the workspace's own keys.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", ".optimize/metric-requirements.txt": "numpy\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps")
    space = _space(archive, runtime)
    space.prepare()
    space.close()
    assert runtime.released == []
    space.release_snapshot()
    assert runtime.released == runtime.keys


def test_scorer_close_releases_the_workspace_and_stage_snapshots(tmp_path: Path) -> None:
    """Release the scorer box's key on its runtime and the stage key on the stage runtime when the scorer closes.

    Args:
        tmp_path: Scratch folder.
    """
    runtime = _StageSnapshotRuntime(tmp_path / "stage-snaps")
    scorer_runtime = _SnapshotRuntime(tmp_path / "scorer-snaps")
    scorer, _ = _stage_scorer(
        tmp_path,
        _TWO_STAGES,
        runtime,
        {name: STAGES[name] for name in ("double", "shout")},
        scorer_runtime=scorer_runtime,
    )
    try:
        assert scorer.score("")["score"] == pytest.approx(0.6)
        assert runtime.released == []
        assert scorer_runtime.released == []
    finally:
        scorer.close()
    assert runtime.released == [runtime.keys[0]]
    assert scorer_runtime.released[0] == scorer_runtime.keys[0]
    assert len(scorer_runtime.released) == 2


def test_a_bad_stage_snapshot_is_released_when_it_is_dropped(tmp_path: Path) -> None:
    """Release the stage key as soon as a bad stage snapshot is dropped, before the scorer closes.

    Args:
        tmp_path: Scratch folder.
    """
    runtime = _StageSnapshotRuntime(tmp_path / "stage-snaps", open_error=ServiceError("refused"))
    scorer, _ = _stage_scorer(tmp_path, _TWO_STAGES, runtime, {name: STAGES[name] for name in ("double", "shout")})
    try:
        assert scorer.score("")["score"] == pytest.approx(0.6)
        assert runtime.released == [runtime.keys[0]]
        assert scorer.score("")["score"] == pytest.approx(0.6)
        assert runtime.released == [runtime.keys[0]]
    finally:
        scorer.close()
    assert runtime.released == [runtime.keys[0]] * 2


def test_a_failed_workspace_release_is_left_for_the_run_end(tmp_path: Path) -> None:
    """Log and keep going when a release fails for a non-budget reason, and raise a budget refusal.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _SnapshotRuntime(tmp_path / "snaps")
    space = _space(archive, runtime)
    space.prepare()
    space.close()
    raised: list[Exception] = [ServiceError("down"), ServiceError("down"), UsagePendingError("pending")]

    def refuse(cache_key: str) -> None:
        """Fail the release with the next error.

        Args:
            cache_key: Names the box's setup.

        Raises:
            Exception: The next configured error.
        """
        raise raised.pop(0)

    runtime.release_snapshot = refuse  # type: ignore[method-assign]
    space.release_snapshot()
    with pytest.raises(UsagePendingError):
        space.release_snapshot()


def test_broker_release_without_a_cache_is_a_no_op() -> None:
    """Do nothing when the broker's parent runtime has no snapshot cache."""
    parent = SandboxBroker(FakeRuntime(), image=IMAGE, max_lifetime_seconds=120).parent_runtime(())
    parent.release_snapshot("any-key")


_API = ComposeService(name="api", command="python -m api")


class _ServiceBox(_Box):
    """Record when the workspace starts the compose services, and what the box holds then."""

    def __init__(self, inner: Any, spec: SandboxSpec) -> None:
        """Wrap a local session with no service started.

        Args:
            inner: The real local session.
            spec: The spec the box opened with.
        """
        super().__init__(inner, spec)
        self.starts: list[dict[str, Any]] = []

    def start_services(self, base_dir: str) -> None:
        """Record the start, and write into the base folder as a service with a data file does.

        Args:
            base_dir: The absolute folder the services run in.
        """
        base = Path(base_dir)
        self.starts.append(
            {
                "base": base,
                "online": self.online,
                "commands": len(self.commands),
                "built": (base / "built.txt").is_file(),
            }
        )
        (base / "service.db").write_text("rows\n")


class _ServiceRuntime(_SnapshotRuntime):
    """Open the snapshot runtime's boxes as boxes that can start compose services.

    With ``fixed`` set, every box works in that one folder, as each Vercel box
    has the same working folder; the workspace opens one box at a time there.
    """

    def __init__(self, root: Path, *, fixed: Path | None = None, **options: Any) -> None:
        """Choose where the boxes work.

        Args:
            root: Where the saved snapshots go.
            fixed: The one folder every box works in, if any.
            **options: The ``_SnapshotRuntime`` options.
        """
        super().__init__(root, **options)
        self._fixed = fixed

    def open(self, spec: SandboxSpec) -> _ServiceBox:
        """Open a box that records its service start.

        Args:
            spec: The requested box.

        Returns:
            The box.
        """
        box = super().open(spec)
        inner = box._inner
        if self._fixed is not None:
            if self._fixed.exists():
                # Setup leaves read-only folders, which a plain delete cannot remove.
                subprocess.run(["chmod", "-R", "u+w", str(self._fixed)], check=True)
                shutil.rmtree(self._fixed)
            inner._dir.rename(self._fixed)
            inner._dir = self._fixed
        self.boxes[-1] = _ServiceBox(inner, spec)
        return self.boxes[-1]


def _service_space(archive: Path, runtime: Any, setup: str = _SETUP) -> RepoWorkspace:
    """Build an online workspace with one compose service and a setup command.

    Args:
        archive: Packed tree.
        runtime: Where its boxes open.
        setup: The setup command.

    Returns:
        The workspace.
    """
    return RepoWorkspace(
        runtime=runtime,
        spec=replace(_ONLINE, services=(_API,)),
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=setup,
        secrets={},
    )


def _check_start(box: _ServiceBox) -> None:
    """Check that a box started its services once, offline, after the digest check, in a copy of the prepared tree.

    Args:
        box: The candidate box.
    """
    (start,) = box.starts
    assert start["base"] == (box.path / SERVICES_DIR).resolve()
    assert start["built"]
    assert not start["online"]
    assert "cp -a" in box.commands[start["commands"] - 1][0]
    before = [command for command, _ in box.commands[: start["commands"]]]
    assert any("hexdigest" in command for command in before)
    assert not any("version.patch" in command for command in before)


@pytest.mark.parametrize("saves", [True, False])
def test_services_start_in_the_candidate_box_after_the_digest_check(tmp_path: Path, saves: bool) -> None:
    """Start no service in the setup box or its saved state, and start them in each candidate box after its check.

    Args:
        tmp_path: Scratch folder.
        saves: Whether the state is a snapshot or a tar.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _ServiceRuntime(tmp_path / "snaps", saves=saves)
    space = _service_space(archive, runtime)
    try:
        _candidate(space, _patch(tmp_path / "v1", archive, {"src/app.py": "x = 2\n"})).close()
        _candidate(space).close()
    finally:
        space.close()
    setup, *candidates = runtime.boxes
    assert setup.starts == []
    assert all(not (folder / SERVICES_DIR).exists() for folder in runtime.saved.values())
    assert len(candidates) == 2
    for box in candidates:
        _check_start(box)


def test_a_candidate_box_without_the_prepared_tree_starts_no_service(tmp_path: Path) -> None:
    """Start no service in a box from a snapshot that fails its check, and start them in the box after the new setup.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _ServiceRuntime(tmp_path / "snaps", tamper=_tamper)
    space = _service_space(archive, runtime)
    try:
        _candidate(space).close()
    finally:
        space.close()
    assert [spec.snapshot_id for spec in runtime.specs] == [None, "snap-0", None, None]
    assert [box.starts for box in runtime.boxes[:3]] == [[], [], []]
    _check_start(runtime.boxes[3])


def test_services_use_their_copy_for_the_paths_that_setup_recorded(tmp_path: Path) -> None:
    """Point a read-only venv script in a read-only folder at the services copy, and keep it runnable.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _ServiceRuntime(tmp_path / "snaps", saves=False, fixed=tmp_path / "box")
    space = _service_space(
        archive,
        runtime,
        setup=(
            "mkdir -p .venv/bin; printf '#!%s/.venv/bin/python\\n' \"$(pwd -P)\" > .venv/bin/tool;"
            " chmod 555 .venv/bin/tool .venv/bin; cat src/app.py > built.txt"
        ),
    )
    try:
        box = _candidate(space)
        (opened,) = runtime.boxes[1:]
        base = opened.starts[0]["base"]
        tool = base / ".venv" / "bin" / "tool"
        assert tool.read_text() == f"#!{base}/.venv/bin/python\n"
        assert tool.stat().st_mode & 0o111
        prepared = (opened.path / PREPARED_DIR / ".venv" / "bin" / "tool").read_text()
        assert prepared == f"#!{(opened.path / WORK_DIR).resolve()}/.venv/bin/python\n"
    finally:
        for folder in (tmp_path / "box").glob("**/.venv/bin"):
            folder.chmod(0o755)
        box.close()
        space.close()

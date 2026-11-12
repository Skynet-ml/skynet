"""Tests for the worker's GEPA checkpoint persist/restore helpers.

Exercises ``_checkpoints_enabled``, ``_prepare_gepa_dir`` (restore-on-resume) and
``_persist_gepa_checkpoint`` (mtime-gated save) directly — the durable half of
resume — without spinning up the optimization subprocess.
"""

from __future__ import annotations

import pickle
import queue
import shutil
from types import SimpleNamespace
from typing import cast

import pytest
from gepa.core.state import GEPAState, ValsetEvaluation

from core.constants import OPTIMIZATION_TYPE_BLACKBOX, OPTIMIZATION_TYPE_TAGGING, PROGRESS_CANDIDATE
from core.service_gateway.optimization.trajectory import GEPA_STATE_FILENAME
from core.storage import JobStore
from core.worker import checkpoint_compat
from core.worker.checkpoint_compat import (
    CheckpointCompatibilityError,
    checkpoint_incumbent,
    checkpoint_manifest,
    validate_checkpoint,
)
from core.worker.constants import EVENT_PROGRESS
from core.worker.engine import BackgroundWorker

_PAYLOAD = {"strategy": {"mode": "single", "engine": "gepa"}, "cases": [{"input": "a"}]}


def _state(iteration: int) -> bytes:
    """Serialize a real GEPA state with the requested completed iteration.

    Args:
        iteration: Upstream loop counter to retain across restoration.

    Returns:
        Exact pickle format used by upstream's state serializer.
    """
    state = GEPAState({"predict": "seed"}, ValsetEvaluation({0: "answer"}, {0: 0.5}))
    state.i = iteration
    state.total_num_evals = 10
    state.num_full_ds_evals = 1
    return pickle.dumps(state.__dict__)


class _CheckpointStore:
    """In-memory stand-in exposing the checkpoint surface the helpers use."""

    def __init__(self) -> None:
        """Start with no saved checkpoints."""
        self.checkpoints: dict[int, tuple[bytes, int]] = {}
        self.manifests: dict[int, dict | None] = {}

    def get_job(self, optimization_id: str) -> dict:
        """Return the immutable execution payload used to validate recovery."""
        return {"payload": _PAYLOAD, "code_version": None}

    def save_gepa_checkpoint(
        self,
        optimization_id: str,
        data: bytes,
        iteration: int,
        pair_index: int = -1,
        *,
        manifest=None,
        expected_generation=None,
    ) -> None:
        """Record one run/pair's checkpoint bytes and iteration."""
        self.checkpoints[pair_index] = (data, iteration)
        self.manifests[pair_index] = manifest

    def get_gepa_checkpoint(self, optimization_id: str, pair_index: int = -1):
        """Return one run/pair's checkpoint record, or ``None``."""
        row = self.checkpoints.get(pair_index)
        if row is None:
            return None
        return SimpleNamespace(
            pair_index=pair_index,
            data=row[0],
            iteration=row[1],
            stored_bytes=len(row[0]),
            manifest=self.manifests.get(pair_index) or checkpoint_manifest(row[0], _PAYLOAD, None),
        )

    def list_gepa_checkpoints(self, optimization_id: str):
        """Return every stored checkpoint record."""
        return [self.get_gepa_checkpoint(optimization_id, idx) for idx in self.checkpoints]

    def delete_gepa_checkpoint(self, optimization_id: str, pair_index: int = -1) -> None:
        """Drop one run/pair's checkpoint."""
        self.checkpoints.pop(pair_index, None)


def _worker(store: object) -> BackgroundWorker:
    """Build a worker bound to ``store`` (never started)."""
    return BackgroundWorker(job_store=cast(JobStore, store))


def test_checkpoints_enabled_for_runs_on_capable_store() -> None:
    """Enabled for black-box runs on a capable store; never for other job kinds or stores."""
    worker = _worker(_CheckpointStore())
    assert worker._checkpoints_enabled(OPTIMIZATION_TYPE_BLACKBOX) is True
    assert worker._checkpoints_enabled(OPTIMIZATION_TYPE_TAGGING) is False
    assert worker._checkpoints_enabled("run") is False
    assert _worker(object())._checkpoints_enabled(OPTIMIZATION_TYPE_BLACKBOX) is False


def test_single_run_persist_is_mtime_gated_and_prepare_restores() -> None:
    """Single run: persist saves on change only; a later prepare seeds state back."""
    store = _CheckpointStore()
    worker = _worker(store)

    base = worker._prepare_gepa_dir("w1")
    try:
        assert base.exists()
        assert not (base / "gepa" / GEPA_STATE_FILENAME).exists()

        (base / "gepa").mkdir()
        (base / "gepa" / GEPA_STATE_FILENAME).write_bytes(_state(1))
        tracker: dict = {"payload": _PAYLOAD}
        worker._persist_gepa_checkpoint("w1", base, tracker)
        assert store.checkpoints[-1] == (_state(1), 1)

        before = store.checkpoints[-1]
        worker._persist_gepa_checkpoint("w1", base, tracker)
        assert store.checkpoints[-1] is before  # unchanged mtime → no re-save
    finally:
        shutil.rmtree(base, ignore_errors=True)

    base2 = worker._prepare_gepa_dir("w1")
    try:
        assert (base2 / "gepa" / GEPA_STATE_FILENAME).read_bytes() == _state(1)
    finally:
        shutil.rmtree(base2, ignore_errors=True)


def test_checkpoint_persists_best_json_safe_evaluated_incumbent() -> None:
    """Bind the best fully scored candidate event to the generation-fenced state manifest."""
    store = _CheckpointStore()
    worker = _worker(store)
    events: queue.Queue = queue.Queue()
    tracker: dict = {"payload": _PAYLOAD}
    for candidate_id, score, prompt in [
        ("0", 0.5, "seed"),
        ("1", 0.75, "better"),
        ("2", 0.6, "worse"),
    ]:
        events.put(
            {
                "type": EVENT_PROGRESS,
                "event": PROGRESS_CANDIDATE,
                "metrics": {
                    "candidate_id": candidate_id,
                    "score": score,
                    "prompt": {"predict": prompt},
                    "per_example": [{"id": "0", "score": score}],
                    "discovered_at_evals": int(candidate_id) + 1,
                    "iteration": int(candidate_id),
                },
            }
        )
    worker._drain_subprocess_events("incumbent", events, checkpoint_tracker=tracker)

    base = worker._prepare_gepa_dir("incumbent")
    try:
        (base / "gepa").mkdir()
        (base / "gepa" / GEPA_STATE_FILENAME).write_bytes(_state(2))
        worker._persist_gepa_checkpoint("incumbent", base, tracker)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    incumbent = checkpoint_incumbent(store.manifests[-1])
    assert incumbent is not None
    assert incumbent["candidate"] == {"predict": "better"}
    assert incumbent["selection_score"] == 0.75
    assert incumbent["candidate_origin"] == "optimized"


def test_image_checkpoint_resumes_under_any_worker_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept state written inside a pinned image whatever the worker now runs.

    Args:
        monkeypatch: Swaps the worker's own Python and dependency versions.
    """
    image = "registry.example/sandbox@sha256:" + "a" * 64
    data = _state(2)
    manifest = checkpoint_manifest(data, _PAYLOAD, "old-revision", sandbox_image=image)
    monkeypatch.setattr(checkpoint_compat.platform, "python_version", lambda: "9.9.9")
    validate_checkpoint(data, manifest, _PAYLOAD, "new-revision", sandbox_image=image)


def test_image_checkpoint_refuses_another_image() -> None:
    """Refuse to resume state in an image other than the one that wrote it."""
    data = _state(2)
    manifest = checkpoint_manifest(data, _PAYLOAD, None, sandbox_image="registry.example/a@sha256:" + "a" * 64)
    with pytest.raises(CheckpointCompatibilityError, match="sandbox_image"):
        validate_checkpoint(data, manifest, _PAYLOAD, None, sandbox_image="registry.example/b@sha256:" + "b" * 64)
    with pytest.raises(CheckpointCompatibilityError, match="sandbox_image"):
        validate_checkpoint(data, manifest, _PAYLOAD, None)
